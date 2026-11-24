"""
plugins/_planet_live.py — the data behind planet_watch.

Helper module (leading underscore: the loader skips it). Public, free, keyless
feeds only:

  earthquakes  USGS FDSN event service + EMSC (seismicportal.eu), merged
  disasters    GDACS event list (official alert levels) + NASA EONET open events
  aurora       NOAA SWPC planetary Kp (observed + forecast) and the OVATION
               aurora probability grid
  aircraft     OpenSky Network state vectors (optionally with the user's own
               OpenSky API client), with adsb.lol as a second source
  places       OpenStreetMap Nominatim, cached, one request at a time

Every fetch has a short deadline and returns ([], reason) instead of raising.
Everything here is a REPORT of something that already happened or is going on;
nothing in this file predicts anything.
"""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from plugins import _announce

USER_AGENT = "JARVIS-planet_watch/1.0 (open-source desktop voice assistant plugin)"
NS = "planet_watch"

USGS = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EMSC = "https://www.seismicportal.eu/fdsnws/event/1/query"
GDACS = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"
EONET = "https://eonet.gsfc.nasa.gov/api/v3/events"
KP_NOW = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
KP_FORECAST = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
OVATION = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
OPENSKY = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN = ("https://auth.opensky-network.org/auth/realms/opensky-network/"
                 "protocol/openid-connect/token")
ADSB_LOL = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{nm}"
NOMINATIM = "https://nominatim.openstreetmap.org/search"


def _get(url: str, params: Optional[dict] = None, timeout: float = 8.0, headers=None):
    import requests
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json()


def setting(key: str, default=None):
    try:
        from memory.config_manager import get_plugin_setting
        v = get_plugin_setting(NS, key, default)
        return default if v in (None, "") else v
    except Exception:
        return default


# ── Geography ─────────────────────────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def bearing_word(lat1, lon1, lat2, lon2) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["north", "north-east", "east", "south-east", "south", "south-west", "west",
            "north-west"][int((deg + 22.5) // 45) % 8]


_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_coordinates(text: str) -> Optional[tuple[float, float]]:
    m = _COORD_RE.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


_geo_lock = threading.Lock()
_geo_last = 0.0


def geocode(place: str) -> Optional[dict]:
    """{'lat', 'lon', 'name'} for a place name, cached. Nominatim allows one
    request per second; this keeps to it and remembers every answer."""
    global _geo_last
    place = (place or "").strip()
    if not place:
        return None
    coords = parse_coordinates(place)
    if coords:
        return {"lat": coords[0], "lon": coords[1], "name": f"{coords[0]:.2f}, {coords[1]:.2f}"}
    key = place.lower()
    cache = _announce.load("planet_watch_places", {}) or {}
    if key in cache:
        return cache[key]
    with _geo_lock:
        wait = 1.1 - (time.time() - _geo_last)
        if wait > 0:
            time.sleep(wait)
        _geo_last = time.time()
        try:
            data = _get(NOMINATIM, {"q": place, "format": "json", "limit": 1}, timeout=8)
        except Exception as e:
            print(f"[PlanetWatch] geocoding failed: {e}")
            return None
    if not data:
        return None
    hit = data[0]
    out = {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
           "name": str(hit.get("display_name") or place).split(",")[0]}
    cache[key] = out
    try:
        _announce.save("planet_watch_places", cache)
    except Exception:
        pass
    return out


def home_location() -> Optional[dict]:
    loc = str(setting("location", "") or "").strip()
    return geocode(loc) if loc else None


def resolve(place: str = "") -> tuple[Optional[dict], str]:
    """The place named, or the user's saved location. (loc, problem)."""
    if place:
        loc = geocode(place)
        return (loc, "") if loc else (None, f"I couldn't find a place called '{place}'.")
    loc = home_location()
    if loc:
        return loc, ""
    return None, ("the user's location is not set: ask them to enter it in Settings > "
                  "PLANET WATCH (coordinates or a place name), or to name a place now")


# ── Earthquakes ───────────────────────────────────────────────────────────────
def _usgs(start: datetime, min_mag: float, loc: Optional[dict], radius_km: float, limit: int):
    params = {"format": "geojson", "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
              "minmagnitude": min_mag, "orderby": "time", "limit": limit}
    if loc:
        params.update(latitude=loc["lat"], longitude=loc["lon"],
                      maxradiuskm=min(20001.6, radius_km))
    out = []
    for f in (_get(USGS, params, timeout=10).get("features") or []):
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if len(g) < 2 or p.get("mag") is None:
            continue
        out.append({"id": f.get("id"), "source": "USGS", "mag": float(p["mag"]),
                    "place": p.get("place") or "", "time": (p.get("time") or 0) / 1000.0,
                    "lon": float(g[0]), "lat": float(g[1]),
                    "depth": float(g[2]) if len(g) > 2 and g[2] is not None else None,
                    "tsunami": bool(p.get("tsunami")), "url": p.get("url") or "",
                    "alert": p.get("alert")})
    return out


def _emsc(start: datetime, min_mag: float, loc: Optional[dict], radius_km: float, limit: int):
    params = {"format": "json", "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
              "minmag": min_mag, "limit": limit, "orderby": "time"}
    if loc:
        params.update(lat=loc["lat"], lon=loc["lon"], maxradius=min(180.0, radius_km / 111.2))
    out = []
    for f in (_get(EMSC, params, timeout=10).get("features") or []):
        p = f.get("properties") or {}
        try:
            t = datetime.fromisoformat(str(p.get("time")).replace("Z", "+00:00")).timestamp()
            out.append({"id": p.get("unid") or f.get("id"), "source": "EMSC",
                        "mag": float(p["mag"]), "place": str(p.get("flynn_region") or "").title(),
                        "time": t, "lat": float(p["lat"]), "lon": float(p["lon"]),
                        "depth": float(p["depth"]) if p.get("depth") is not None else None,
                        "tsunami": False,
                        "url": f"https://www.seismicportal.eu/eventdetails.html?unid={p.get('unid')}",
                        "alert": None})
        except Exception:
            continue
    return out


def merge_quakes(a: list, b: list) -> list:
    """One entry per real event: two reports within 60 s and 100 km are the same
    quake (the USGS one is kept; it carries tsunami flags)."""
    merged = list(a)
    for q in b:
        dup = any(abs(q["time"] - m["time"]) < 60 and
                  haversine_km(q["lat"], q["lon"], m["lat"], m["lon"]) < 100 for m in merged)
        if not dup:
            merged.append(q)
    merged.sort(key=lambda q: q["time"], reverse=True)
    return merged


def earthquakes(loc: Optional[dict] = None, radius_km: float = 500, min_mag: float = 2.5,
                days: float = 7, limit: int = 200) -> tuple[list, str]:
    start = datetime.now(timezone.utc) - timedelta(days=max(0.01, min(30.0, days)))
    results, errors = {}, []
    with ThreadPoolExecutor(2) as ex:
        futs = {"USGS": ex.submit(_usgs, start, min_mag, loc, radius_km, limit),
                "EMSC": ex.submit(_emsc, start, min_mag, loc, radius_km, limit)}
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=14)
            except Exception as e:
                errors.append(f"{name}: {str(e)[:80]}")
                results[name] = []
    if not results["USGS"] and not results["EMSC"] and errors:
        return [], "no earthquake service answered (" + "; ".join(errors) + ")"
    quakes = merge_quakes(results["USGS"], results["EMSC"])
    if loc:
        for q in quakes:
            q["distance_km"] = round(haversine_km(loc["lat"], loc["lon"], q["lat"], q["lon"]))
            q["direction"] = bearing_word(loc["lat"], loc["lon"], q["lat"], q["lon"])
        quakes = [q for q in quakes if q["distance_km"] <= radius_km]
    return quakes[:limit], ("; ".join(errors) if errors else "")


# ── Disasters ─────────────────────────────────────────────────────────────────
GDACS_KIND = {"EQ": "earthquake", "TC": "cyclone", "FL": "flood", "VO": "volcano",
              "DR": "drought", "WF": "wildfire", "TS": "tsunami"}
EONET_KIND = {"wildfires": "wildfire", "severeStorms": "storm", "volcanoes": "volcano",
              "floods": "flood", "drought": "drought", "seaLakeIce": "ice",
              "landslides": "landslide", "dustHaze": "dust", "snow": "snow",
              "tempExtremes": "temperature", "earthquakes": "earthquake", "manmade": "manmade",
              "waterColor": "water colour"}
_ALERT_RANK = {"red": 3, "orange": 2, "green": 1}


def _gdacs() -> list:
    out = []
    for f in (_get(GDACS, timeout=12).get("features") or []):
        p = f.get("properties") or {}
        g = (f.get("geometry") or {}).get("coordinates") or []
        if str(p.get("iscurrent", "true")).lower() != "true" or len(g) < 2:
            continue
        try:
            lon, lat = float(g[0]), float(g[1])
        except Exception:
            continue
        out.append({"source": "GDACS", "kind": GDACS_KIND.get(p.get("eventtype"), p.get("eventtype")),
                    "name": p.get("name") or p.get("description") or "",
                    "country": p.get("country") or "", "alert": str(p.get("alertlevel") or "").lower(),
                    "severity": ((p.get("severitydata") or {}).get("severitytext") or "").strip(),
                    "from": p.get("fromdate") or "", "to": p.get("todate") or "",
                    "lat": lat, "lon": lon,
                    "url": ((p.get("url") or {}).get("report") or "")})
    return out


def _eonet(days: int = 30) -> list:
    out = []
    data = _get(EONET, {"status": "open", "days": days, "limit": 400}, timeout=12)
    for e in data.get("events") or []:
        cats = e.get("categories") or []
        kind = EONET_KIND.get(cats[0].get("id"), cats[0].get("title")) if cats else "event"
        geoms = e.get("geometry") or []
        if not geoms:
            continue
        g = geoms[-1]          # the latest position
        c = g.get("coordinates")
        if g.get("type") == "Polygon":
            try:
                ring = c[0]
                c = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
            except Exception:
                continue
        try:
            lon, lat = float(c[0]), float(c[1])
        except Exception:
            continue
        mag = g.get("magnitudeValue")
        out.append({"source": "EONET", "kind": kind, "name": e.get("title") or "", "country": "",
                    "alert": "", "severity": (f"{mag:g} {g.get('magnitudeUnit') or ''}".strip()
                                              if isinstance(mag, (int, float)) else ""),
                    "from": g.get("date") or "", "to": "", "lat": lat, "lon": lon,
                    "url": e.get("link") or ""})
    return out


def disasters(loc: Optional[dict] = None, radius_km: float = 1500,
              kinds: Optional[list] = None) -> tuple[list, str]:
    results, errors = {}, []
    with ThreadPoolExecutor(2) as ex:
        futs = {"GDACS": ex.submit(_gdacs), "EONET": ex.submit(_eonet)}
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=15)
            except Exception as e:
                errors.append(f"{name}: {str(e)[:80]}")
                results[name] = []
    events = results["GDACS"] + results["EONET"]
    if not events and errors:
        return [], "the disaster feeds (GDACS, NASA EONET) are not answering (" + "; ".join(errors) + ")"
    if kinds:
        want = {k.strip().lower().rstrip("s") for k in kinds if k.strip()}
        alias = {"fire": "wildfire", "hurricane": "cyclone", "typhoon": "cyclone",
                 "volcanoe": "volcano", "quake": "earthquake"}
        want = {alias.get(k, k) for k in want}
        events = [e for e in events if str(e["kind"]).lower() in want]
    if loc:
        for e in events:
            e["distance_km"] = round(haversine_km(loc["lat"], loc["lon"], e["lat"], e["lon"]))
        events = [e for e in events if e["distance_km"] <= radius_km]
    events.sort(key=lambda e: (-_ALERT_RANK.get(e["alert"], 0), e.get("distance_km", 0)))
    return events, ("; ".join(errors) if errors else "")


# ── Aurora ────────────────────────────────────────────────────────────────────
def _kp_rows(data) -> list[tuple[str, float, str]]:
    """(time_tag, kp, observed|estimated|predicted). The feed was a list of lists
    with a header row and is now a list of objects; both are read."""
    rows = []
    if not isinstance(data, list) or not data:
        return rows
    if isinstance(data[0], list):
        head = [str(h).lower() for h in data[0]]
        for r in data[1:]:
            d = dict(zip(head, r))
            try:
                rows.append((str(d.get("time_tag")), float(d.get("kp")), str(d.get("observed") or "observed")))
            except Exception:
                pass
    else:
        for d in data:
            if not isinstance(d, dict):
                continue
            kp = d.get("kp", d.get("Kp"))
            try:
                rows.append((str(d.get("time_tag")), float(kp), str(d.get("observed") or "observed")))
            except Exception:
                pass
    return rows


def aurora(loc: Optional[dict]) -> tuple[dict, str]:
    out: dict = {}
    errors = []
    try:
        rows = _kp_rows(_get(KP_NOW, timeout=8))
        if rows:
            out["kp_now"], out["kp_time"] = rows[-1][1], rows[-1][0]
    except Exception as e:
        errors.append(f"Kp: {e}")
    try:
        rows = _kp_rows(_get(KP_FORECAST, timeout=8))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        future = [r for r in rows if r[0] >= now and r[2] != "observed"][:8]
        if future:
            peak = max(future, key=lambda r: r[1])
            out["kp_forecast_max_24h"], out["kp_forecast_peak_time"] = peak[1], peak[0]
    except Exception as e:
        errors.append(f"Kp forecast: {e}")
    grid = []
    try:
        data = _get(OVATION, timeout=15)
        out["ovation_time"] = data.get("Forecast Time")
        grid = [(float(c[0]), float(c[1]), float(c[2])) for c in data.get("coordinates") or []
                if len(c) >= 3 and c[2] and c[2] >= 3]
    except Exception as e:
        errors.append(f"OVATION: {e}")
    out["grid"] = grid
    if loc and grid:
        lon360 = loc["lon"] % 360
        # The chance overhead and the best chance within ~800 km poleward, which
        # is roughly what can be seen low on the horizon.
        near = [g for g in grid if abs(g[1] - loc["lat"]) <= 1.5 and
                min(abs(g[0] - lon360), 360 - abs(g[0] - lon360)) <= 2]
        out["chance_overhead"] = max((g[2] for g in near), default=0)
        sign = 1 if loc["lat"] >= 0 else -1
        horizon = [g for g in grid if 0 <= (g[1] - loc["lat"]) * sign <= 8 and
                   min(abs(g[0] - lon360), 360 - abs(g[0] - lon360)) <= 6]
        out["chance_on_horizon"] = max((g[2] for g in horizon), default=0)
        out["dark"] = _is_dark(loc["lat"], loc["lon"])
    if not grid and "kp_now" not in out:
        return out, "NOAA's aurora forecast is not answering (" + "; ".join(errors) + ")"
    return out, "; ".join(errors)


def _is_dark(lat: float, lon: float, when: Optional[datetime] = None) -> bool:
    """Is the Sun more than 6° below the horizon here (civil twilight over)?"""
    when = when or datetime.now(timezone.utc)
    doy = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60.0
    decl = math.radians(-23.44 * math.cos(math.radians(360 / 365 * (doy + 10))))
    solar_time = hours + lon / 15.0
    ha = math.radians(15 * (solar_time - 12))
    la = math.radians(lat)
    alt = math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha))
    return math.degrees(alt) < -6


# ── Aircraft ──────────────────────────────────────────────────────────────────
_token = {"value": "", "until": 0.0}


def _opensky_headers() -> dict:
    cid = str(setting("opensky_client_id", "") or "").strip()
    secret = str(setting("opensky_client_secret", "") or "").strip()
    if not (cid and secret):
        return {}
    if _token["value"] and time.time() < _token["until"]:
        return {"Authorization": f"Bearer {_token['value']}"}
    try:
        import requests
        r = requests.post(OPENSKY_TOKEN, data={"grant_type": "client_credentials",
                                               "client_id": cid, "client_secret": secret},
                          timeout=8, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        d = r.json()
        _token["value"] = d.get("access_token", "")
        _token["until"] = time.time() + int(d.get("expires_in", 1800)) - 60
        return {"Authorization": f"Bearer {_token['value']}"} if _token["value"] else {}
    except Exception as e:
        print(f"[PlanetWatch] OpenSky sign-in failed: {e}")
        return {}


def _opensky(loc: dict, radius_km: float) -> list:
    dlat = radius_km / 111.2
    dlon = radius_km / max(1.0, 111.2 * math.cos(math.radians(loc["lat"])))
    params = {"lamin": loc["lat"] - dlat, "lamax": loc["lat"] + dlat,
              "lomin": loc["lon"] - dlon, "lomax": loc["lon"] + dlon}
    data = _get(OPENSKY, params, timeout=9, headers=_opensky_headers())
    out = []
    for s in data.get("states") or []:
        try:
            if s[5] is None or s[6] is None:
                continue
            alt = s[13] if s[13] is not None else s[7]
            out.append({"id": s[0], "callsign": (s[1] or "").strip(), "country": s[2] or "",
                        "lat": float(s[6]), "lon": float(s[5]),
                        "alt_m": float(alt) if alt is not None else None,
                        "on_ground": bool(s[8]),
                        "speed_kmh": round(s[9] * 3.6) if s[9] is not None else None,
                        "track": float(s[10]) if s[10] is not None else None, "type": ""})
        except Exception:
            continue
    return out


def _adsb_lol(loc: dict, radius_km: float) -> list:
    nm = max(1, min(250, int(radius_km / 1.852)))
    data = _get(ADSB_LOL.format(lat=loc["lat"], lon=loc["lon"], nm=nm), timeout=9)
    out = []
    for a in data.get("ac") or []:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_geom", a.get("alt_baro"))
        out.append({"id": a.get("hex"), "callsign": str(a.get("flight") or "").strip(),
                    "country": "", "lat": float(a["lat"]), "lon": float(a["lon"]),
                    "alt_m": round(alt * 0.3048) if isinstance(alt, (int, float)) else None,
                    "on_ground": alt == "ground",
                    "speed_kmh": round(a["gs"] * 1.852) if isinstance(a.get("gs"), (int, float)) else None,
                    "track": a.get("track"), "type": a.get("t") or "", "reg": a.get("r") or ""})
    return out


def aircraft(loc: dict, radius_km: float = 50) -> tuple[list, str, str]:
    """(planes, source, problem). Both sources are asked at once; OpenSky wins
    when it answers, adsb.lol fills in when it does not."""
    radius_km = max(5.0, min(400.0, radius_km))
    errors = []
    with ThreadPoolExecutor(2) as ex:
        fo = ex.submit(_opensky, loc, radius_km)
        fa = ex.submit(_adsb_lol, loc, radius_km)
        planes, source = [], ""
        try:
            planes, source = fo.result(timeout=12), "OpenSky Network"
        except Exception as e:
            errors.append(f"OpenSky: {str(e)[:80]}")
        if not planes:
            try:
                planes, source = fa.result(timeout=12), "adsb.lol"
            except Exception as e:
                errors.append(f"adsb.lol: {str(e)[:80]}")
    if not source:
        return [], "", "the aircraft services are not answering (" + "; ".join(errors) + ")"
    for p in planes:
        p["distance_km"] = round(haversine_km(loc["lat"], loc["lon"], p["lat"], p["lon"]), 1)
    planes = [p for p in planes if p["distance_km"] <= radius_km]
    planes.sort(key=lambda p: p["distance_km"])
    return planes, source, ""


def ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} days ago"
