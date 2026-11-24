"""
plugins/planet_watch.py — what is happening on the planet, as facts and a map.

Earthquakes (USGS + EMSC), current disasters (GDACS + NASA EONET), the aurora
(NOAA SWPC), live aircraft near a place (OpenSky, adsb.lol) and a background
earthquake watch. Each answer is a short factual line for the assistant to
speak, a list in the content panel, and a still map on the HUD drawn by
plugins/_planet_view.py.

Not here: the interactive 3-D globe (that needs a new GUI surface).

Everything reported is something that already happened or is going on. The
words given to the assistant say so every time: no warnings, no predictions.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from plugins import _announce
from plugins import _planet_live as live
from plugins import _planet_view as view

PLUGIN = {
    "name": "planet_watch",
    "description": (
        "Live facts about the planet, with a map on the display: recent "
        "EARTHQUAKES near the user, near a place or worldwide (USGS + EMSC); "
        "current DISASTERS - cyclones, floods, volcanoes, wildfires, droughts "
        "(GDACS alert levels + NASA EONET); the AURORA / northern or southern "
        "lights chance (NOAA); live AIRCRAFT above a place; a map of a place; and "
        "a background earthquake watch that speaks when a new quake is reported. "
        "Actions: 'earthquakes' (scope near|world, place, radius_km, "
        "min_magnitude, days), 'disasters' (scope world|near, place, kinds), "
        "'aurora' (place), 'aircraft' (place, radius_km), 'map' (place, "
        "radius_km), 'quake_watch' (radius_km, min_magnitude, "
        "global_min_magnitude, duration_minutes), 'stop_quake_watch', 'status'. "
        "Any language. These are reports published AFTER events: never present "
        "them as warnings and never predict earthquakes or disasters."
    ),
    "brief": "Earthquakes, disasters, aurora chance, aircraft overhead, place maps, quake watch - with a map on screen.",
    "tier": "extra",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "earthquakes, disasters, aurora, aircraft, map, quake_watch, "
                                      "stop_quake_watch, status or close."},
            "place": {"type": "STRING",
                      "description": "A place the user named, in their words, or 'lat, lon'. Omit "
                                     "for the user's saved location."},
            "scope": {"type": "STRING", "description": "earthquakes/disasters: 'near' or 'world'."},
            "radius_km": {"type": "NUMBER", "description": "Area radius in km."},
            "min_magnitude": {"type": "NUMBER",
                              "description": "earthquakes / quake_watch: smallest magnitude."},
            "global_min_magnitude": {"type": "NUMBER",
                                     "description": "quake_watch: also any quake worldwide from this magnitude."},
            "days": {"type": "NUMBER", "description": "earthquakes: how many days back (max 30)."},
            "kinds": {"type": "STRING",
                      "description": "disasters: comma list of cyclone, flood, volcano, wildfire, "
                                     "drought, storm, earthquake."},
            "duration_minutes": {"type": "NUMBER", "description": "quake_watch: stop after this long."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": live.NS,
    "title": "PLANET WATCH",
    "fields": [
        {"key": "location", "label": "Your location (coordinates or a place name)", "type": "text",
         "placeholder": "e.g. 52.52, 13.40 or Berlin"},
        {"key": "quake_radius_km", "label": "Earthquakes 'near me' and the watch: radius (km)",
         "type": "text", "default": "500"},
        {"key": "watch_global_min", "label": "Watch: also any quake worldwide from magnitude",
         "type": "text", "default": "7"},
        {"key": "watch_interval_s", "label": "Watch: check every (seconds, 60-900)",
         "type": "text", "default": "120"},
        {"key": "watch_show_map", "label": "Watch: show the map when it reports", "type": "toggle",
         "default": True},
        {"key": "use_tiles", "label": "Use OpenStreetMap tiles under the maps", "type": "toggle",
         "default": True},
        {"key": "opensky_client_id", "label": "OpenSky API client id (optional)", "type": "text"},
        {"key": "opensky_client_secret", "label": "OpenSky API client secret (optional)",
         "type": "password"},
    ],
}

AFTER_THE_FACT = ("These are reports published after the events, not warnings: never "
                  "predict earthquakes or further events.")


def _num(v, default=None):
    try:
        return default if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return default


def _show_map_async(player, **kw) -> None:
    """Draw the map off the calling thread (tiles can take a few seconds)."""
    if player is None or not hasattr(player, "show_image"):
        return
    kw.setdefault("use_tiles", bool(live.setting("use_tiles", True)))

    def work():
        try:
            path = view.render(**kw)
            player.show_image(path, kw.get("title", "Planet watch"))
        except Exception as e:
            print(f"[PlanetWatch] map not drawn: {e}")
            try:
                player.write_log(f"Planet watch: the map could not be drawn ({e}).")
            except Exception:
                pass
    threading.Thread(target=work, name="planet-map", daemon=True).start()


def _content(player, title: str, text: str) -> None:
    if player is not None:
        try:
            player.show_content(title, text)
        except Exception:
            pass


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M UTC")


# ── Earthquakes ───────────────────────────────────────────────────────────────
def _earthquakes(p: dict, player) -> str:
    scope = str(p.get("scope") or "").lower()
    place = str(p.get("place") or "").strip()
    world = scope == "world" or (scope != "near" and not place and p.get("radius_km") is None
                                 and live.home_location() is None)
    loc = None
    if not world:
        loc, problem = live.resolve(place)
        if loc is None:
            return problem
    radius = _num(p.get("radius_km"), _num(live.setting("quake_radius_km", 500), 500))
    min_mag = _num(p.get("min_magnitude"), 4.5 if world else 2.5)
    days = _num(p.get("days"), 7 if world else 30)
    quakes, problem = live.earthquakes(loc, radius_km=radius, min_mag=min_mag, days=days)
    if not quakes and problem:
        return f"I couldn't get earthquake data: {problem}."
    where = "worldwide" if world else f"within {radius:.0f} km of {loc['name']}"
    if not quakes:
        _content(player, "Earthquakes", f"No earthquakes of magnitude {min_mag:g}+ {where} "
                                         f"in the last {days:g} days.")
        return (f"No earthquakes of magnitude {min_mag:g} or more were reported {where} in the "
                f"last {days:g} days. Say so plainly. {AFTER_THE_FACT}")

    lines = []
    for i, q in enumerate(quakes[:40], 1):
        dist = f" — {q['distance_km']} km {q['direction']}" if "distance_km" in q else ""
        flag = "  TSUNAMI FLAG" if q.get("tsunami") else ""
        depth = f", {q['depth']:.0f} km deep" if q.get("depth") is not None else ""
        lines.append(f"{i:>2}. M{q['mag']:.1f}  {q['place'] or '?'}{dist}  "
                     f"({live.ago(q['time'])}, {_utc(q['time'])}{depth}) [{q['source']}]{flag}")
    strongest = max(quakes, key=lambda q: q["mag"])
    _content(player, f"Earthquakes {where}"[:48],
             f"{len(quakes)} reported, M{min_mag:g}+, last {days:g} days (USGS + EMSC). "
             "Reports arrive minutes after a quake; this is not an early-warning system.\n\n"
             + "\n".join(lines))

    zoom = 1 if world else view.zoom_for_radius(loc["lat"], radius)
    now = time.time()
    pts = [{"lat": q["lat"], "lon": q["lon"], "r": view.quake_radius(q["mag"], zoom),
            "color": view.quake_color(now - q["time"]),
            "label": f"M{q['mag']:.1f}" if q["mag"] >= (min_mag + 1.5 if world else min_mag + 0.8) else ""}
           for q in reversed(quakes[:300])]
    _show_map_async(player, center=(loc["lat"], loc["lon"]) if loc else (20.0, 10.0), zoom=zoom,
                    title=f"Earthquakes M{min_mag:g}+ {where}, last {days:g} d", points=pts,
                    home=(loc["lat"], loc["lon"]) if loc else None,
                    legend=[("< 3 h", (255, 40, 40)), ("< 24 h", (255, 140, 0)), ("older", (255, 220, 60))],
                    out_name="planet_watch_quakes.png")

    latest = quakes[0]
    def brief(q):
        d = f", {q['distance_km']} km {q['direction']} of {loc['name']}" if loc else ""
        t = ", tsunami flag set" if q.get("tsunami") else ""
        return f"M{q['mag']:.1f} {q['place']}{d}, {live.ago(q['time'])}{t}"
    return (f"[planet_watch] {len(quakes)} earthquakes M{min_mag:g}+ {where} in the last {days:g} "
            f"days. Latest: {brief(latest)}. Strongest: {brief(strongest)}. The list and a map are "
            f"on screen. Summarise in one or two short sentences in the user's language. "
            f"{AFTER_THE_FACT}")


# ── Disasters ─────────────────────────────────────────────────────────────────
def _disasters(p: dict, player) -> str:
    scope = str(p.get("scope") or "world").lower()
    place = str(p.get("place") or "").strip()
    loc = None
    if scope == "near" or place:
        loc, problem = live.resolve(place)
        if loc is None:
            return problem
    radius = _num(p.get("radius_km"), 1500)
    kinds = [k for k in str(p.get("kinds") or "").replace(";", ",").split(",") if k.strip()]
    events, problem = live.disasters(loc, radius_km=radius, kinds=kinds or None)
    if not events and problem:
        return f"I couldn't get the disaster feeds: {problem}."
    where = f"within {radius:.0f} km of {loc['name']}" if loc else "worldwide"
    if not events:
        _content(player, "Disasters", f"No current events {where} in GDACS or NASA EONET.")
        return f"No current disaster events are listed {where}. Say so plainly."
    lines = []
    for i, e in enumerate(events[:45], 1):
        alert = f"[{e['alert'].upper()}] " if e["alert"] else ""
        dist = f" — {e['distance_km']} km away" if "distance_km" in e else ""
        sev = f" ({e['severity']})" if e["severity"] else ""
        country = f", {e['country']}" if e["country"] and e["country"] not in e["name"] else ""
        lines.append(f"{i:>2}. {alert}{e['kind']}: {e['name']}{country}{sev}{dist} [{e['source']}]")
    _content(player, f"Current events {where}"[:48],
             "GDACS (official alert levels) + NASA EONET. Ongoing events, not forecasts.\n\n"
             + "\n".join(lines))
    zoom = 1 if not loc else view.zoom_for_radius(loc["lat"], radius)
    pts = [{"lat": e["lat"], "lon": e["lon"], "shape": "diamond", "r": 7 if e["alert"] in ("red", "orange") else 5,
            "color": view.ALERT_COLOR.get(e["alert"], view.ALERT_COLOR[""]),
            "label": (e["kind"] if e["alert"] in ("red", "orange") else "")}
           for e in reversed(events[:300])]
    _show_map_async(player, center=(loc["lat"], loc["lon"]) if loc else (20.0, 10.0), zoom=zoom,
                    title=f"Current disasters {where}", points=pts,
                    home=(loc["lat"], loc["lon"]) if loc else None,
                    legend=[("GDACS red", view.ALERT_COLOR["red"]),
                            ("GDACS orange", view.ALERT_COLOR["orange"]),
                            ("GDACS green", view.ALERT_COLOR["green"]),
                            ("NASA EONET", view.ALERT_COLOR[""])],
                    out_name="planet_watch_events.png")
    counts: dict[str, int] = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    top = [e for e in events if e["alert"] in ("red", "orange")][:4]
    tops = "; ".join(f"{e['alert']} {e['kind']} {e['name']}" for e in top) or "none with an orange or red alert"
    return (f"[planet_watch] {len(events)} current events {where}: "
            + ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
            + f". Most serious: {tops}. The list and a map are on screen. Summarise in two short "
              "sentences in the user's language, the most serious first. These are reports of "
              "ongoing events, not forecasts.")


# ── Aurora ────────────────────────────────────────────────────────────────────
def _aurora(p: dict, player) -> str:
    loc, _problem = live.resolve(str(p.get("place") or "").strip())
    data, problem = live.aurora(loc)
    if problem and not data.get("grid") and "kp_now" not in data:
        return f"I couldn't get the aurora forecast: {problem}."
    facts = []
    if "kp_now" in data:
        facts.append(f"planetary Kp index now {data['kp_now']:.1f}")
    if "kp_forecast_max_24h" in data:
        facts.append(f"forecast peak Kp {data['kp_forecast_max_24h']:.1f} around "
                     f"{data['kp_forecast_peak_time']} UTC")
    if loc and "chance_overhead" in data:
        facts.append(f"OVATION chance overhead at {loc['name']}: {data['chance_overhead']:.0f}%, "
                     f"low on the poleward horizon: {data['chance_on_horizon']:.0f}%")
        facts.append("the sky there is dark now" if data.get("dark") else
                     "it is still daylight or twilight there")
    elif not loc:
        facts.append("the user's location is not set, so only the global picture is given")
    _content(player, "Aurora", "NOAA SWPC (Kp + OVATION model, ~30-90 min ahead).\n\n"
             + "\n".join(f"• {f}" for f in facts))
    grid = data.get("grid") or []
    if grid:
        north = loc is None or loc["lat"] >= 0
        lat0 = 62.0 if north else -62.0
        if loc:
            center = (max(-60.0, min(60.0, loc["lat"] + (8 if north else -8))), loc["lon"])
        else:
            center = (lat0 - (14 if north else -14), 0.0)
        _show_map_async(player, center=center, zoom=2,
                        title="Aurora probability (NOAA OVATION)",
                        heat=[(g[1], g[0], g[2]) for g in grid],
                        home=(loc["lat"], loc["lon"]) if loc else None,
                        legend=[("aurora probability", (40, 255, 120))],
                        out_name="planet_watch_aurora.png")
    return ("[planet_watch] Aurora: " + "; ".join(facts) + ". Answer in one or two short sentences "
            "in the user's language: the chance of seeing it from where they are and whether the "
            "sky is dark. This is a short-range model estimate, not a promise.")


# ── Aircraft ──────────────────────────────────────────────────────────────────
def _aircraft(p: dict, player) -> str:
    loc, problem = live.resolve(str(p.get("place") or "").strip())
    if loc is None:
        return problem
    radius = _num(p.get("radius_km"), 40)
    planes, source, problem = live.aircraft(loc, radius)
    if problem:
        return f"I couldn't get live aircraft: {problem}."
    flying = [a for a in planes if not a.get("on_ground")]
    if not planes:
        _content(player, "Aircraft", f"No aircraft reported within {radius:.0f} km of {loc['name']}.")
        return f"No aircraft are being reported within {radius:.0f} km of {loc['name']} right now."
    lines = []
    for i, a in enumerate(planes[:40], 1):
        alt = f"{a['alt_m']:.0f} m" if a.get("alt_m") is not None else "?"
        spd = f"{a['speed_kmh']} km/h" if a.get("speed_kmh") is not None else ""
        kind = f" {a['type']}" if a.get("type") else ""
        lines.append(f"{i:>2}. {a['callsign'] or a['id']}{kind}  {a['distance_km']} km away, "
                     f"{'on the ground' if a.get('on_ground') else alt} {spd}".rstrip())
    _content(player, f"Aircraft near {loc['name']}"[:48],
             f"{len(planes)} within {radius:.0f} km ({source}).\n\n" + "\n".join(lines))
    zoom = view.zoom_for_radius(loc["lat"], radius)
    pts = [{"lat": a["lat"], "lon": a["lon"], "shape": "plane", "r": 7, "track": a.get("track") or 0,
            "color": (0, 212, 255) if not a.get("on_ground") else (170, 170, 170),
            "label": a["callsign"] if i < 25 and not a.get("on_ground") else ""}
           for i, a in enumerate(planes[:150])]
    _show_map_async(player, center=(loc["lat"], loc["lon"]), zoom=zoom,
                    title=f"Aircraft within {radius:.0f} km of {loc['name']} ({source})",
                    points=pts, home=(loc["lat"], loc["lon"]),
                    legend=[("flying", (0, 212, 255)), ("on the ground", (170, 170, 170))],
                    out_name="planet_watch_aircraft.png")
    near = flying[0] if flying else planes[0]
    return (f"[planet_watch] {len(flying)} aircraft flying and {len(planes) - len(flying)} on the "
            f"ground within {radius:.0f} km of {loc['name']} (source {source}). Closest: "
            f"{near['callsign'] or near['id']} {near.get('type') or ''} at {near['distance_km']} km"
            + (f", {near['alt_m']:.0f} m up" if near.get("alt_m") is not None and not near.get("on_ground") else "")
            + ". A map and the list are on screen. Answer in one or two short sentences.")


def _map(p: dict, player) -> str:
    loc, problem = live.resolve(str(p.get("place") or "").strip())
    if loc is None:
        return problem
    radius = _num(p.get("radius_km"), 20)
    zoom = view.zoom_for_radius(loc["lat"], radius)
    if player is None:
        return "Maps can only be shown on the desktop display."
    _show_map_async(player, center=(loc["lat"], loc["lon"]), zoom=zoom,
                    title=f"{loc['name']} ({loc['lat']:.3f}, {loc['lon']:.3f})",
                    home=(loc["lat"], loc["lon"]), out_name="planet_watch_place.png")
    return f"A map of {loc['name']} is going up on the display."


# ── Earthquake watch ─────────────────────────────────────────────────────────
class _Watch:
    def __init__(self):
        self.thread = None
        self.stop = threading.Event()
        self.desc = ""

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


_watch = _Watch()


def _watch_loop(loc, radius, min_mag, global_min, interval, until, player):
    seen: set = set()
    first = True
    failing = False
    while not _watch.stop.is_set():
        new = []
        try:
            local, prob1 = live.earthquakes(loc, radius_km=radius, min_mag=min_mag, days=1)
            world, prob2 = live.earthquakes(None, radius_km=20000, min_mag=global_min, days=1)
            if not local and not world and prob1 and prob2:
                raise RuntimeError(prob1)
            if failing:
                failing = False
                live_log = "Earthquake watch: the services are answering again."
                _announce.log(live_log)
            for q in local + world:
                key = (round(q["time"] / 60), round(q["lat"]), round(q["lon"]))
                if key in seen:
                    continue
                seen.add(key)
                if not first:
                    new.append(q)
        except Exception as e:
            if not failing:
                failing = True
                _announce.say("Tell the user in one short sentence that the earthquake watch "
                              f"cannot reach the earthquake services right now ({str(e)[:80]}) "
                              "and keeps trying.")
        first = False
        for q in sorted(new, key=lambda q: q["time"])[:3]:
            dist = live.haversine_km(loc["lat"], loc["lon"], q["lat"], q["lon"])
            way = live.bearing_word(loc["lat"], loc["lon"], q["lat"], q["lon"])
            tsu = " A tsunami flag is set on it." if q.get("tsunami") else ""
            _announce.say(
                f"[Earthquake watch: the user asked to be told about new earthquakes of magnitude "
                f"{min_mag:g}+ within {radius:.0f} km, or {global_min:g}+ anywhere.] A magnitude "
                f"{q['mag']:.1f} earthquake was reported: {q['place']}, {dist:.0f} km {way} of "
                f"{loc['name']}, {live.ago(q['time'])}.{tsu} Tell the user in one or two short, "
                "calm sentences. This is a report published after the event, not a warning: do "
                "not predict aftershocks or further quakes.")
            if player is not None and live.setting("watch_show_map", True):
                try:
                    if not (hasattr(player, "video_is_playing") and player.video_is_playing()):
                        _show_map_async(player, center=(q["lat"], q["lon"]), zoom=5,
                                        title=f"M{q['mag']:.1f} {q['place']}",
                                        points=[{"lat": q["lat"], "lon": q["lon"], "r": 10,
                                                 "color": (255, 40, 40), "label": f"M{q['mag']:.1f}"}],
                                        home=(loc["lat"], loc["lon"]), out_name="planet_watch_alert.png")
                except Exception:
                    pass
        if until and time.time() >= until:
            _announce.say("The earthquake watch the user asked for has just ended. Mention it in "
                          "one short sentence.")
            break
        _watch.stop.wait(interval)


def _quake_watch(p: dict, player) -> str:
    if _watch.running():
        return f"The earthquake watch is already running ({_watch.desc})."
    loc, problem = live.resolve(str(p.get("place") or "").strip())
    if loc is None:
        return problem
    radius = _num(p.get("radius_km"), _num(live.setting("quake_radius_km", 500), 500))
    min_mag = _num(p.get("min_magnitude"), 3.5)
    global_min = _num(p.get("global_min_magnitude"), _num(live.setting("watch_global_min", 7), 7))
    interval = max(60.0, min(900.0, _num(live.setting("watch_interval_s", 120), 120)))
    dur = _num(p.get("duration_minutes"))
    until = time.time() + dur * 60 if dur else 0
    _watch.stop.clear()
    _watch.desc = (f"M{min_mag:g}+ within {radius:.0f} km of {loc['name']}, M{global_min:g}+ worldwide"
                   + (f", for {dur:g} minutes" if dur else ", while the assistant is running"))
    _watch.thread = threading.Thread(target=_watch_loop, name="quake-watch", daemon=True,
                                     args=(loc, radius, min_mag, global_min, interval, until, player))
    _watch.thread.start()
    return (f"[planet_watch] Earthquake watch on: {_watch.desc}; checked every {interval:.0f} s. "
            "Confirm in one short sentence. It reports quakes after they happen; it is not an "
            "early-warning system, so do not call it one.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    _announce.remember(player)
    p = parameters or {}
    action = str(p.get("action") or "earthquakes").strip().lower().replace(" ", "_")
    try:
        if action in ("earthquakes", "earthquake", "quakes"):
            return _earthquakes(p, player)
        if action in ("disasters", "events", "disaster"):
            return _disasters(p, player)
        if action in ("aurora", "northern_lights", "southern_lights"):
            return _aurora(p, player)
        if action in ("aircraft", "planes", "flights"):
            return _aircraft(p, player)
        if action in ("map", "globe", "place"):
            return _map(p, player)
        if action == "quake_watch":
            return _quake_watch(p, player)
        if action == "stop_quake_watch":
            if not _watch.running():
                return "The earthquake watch isn't running."
            _watch.stop.set()
            return "Earthquake watch stopped."
        if action == "close":
            if player is not None:
                try:
                    player.stop_video()
                except Exception:
                    pass
            return "Closed the map."
        if action == "status":
            loc = live.home_location()
            return ("Planet watch: location " + (loc["name"] if loc else "not set")
                    + "; earthquake watch " + (f"on ({_watch.desc})" if _watch.running() else "off")
                    + ". The interactive 3-D globe is not available in this version; maps are still pictures.")
        return (f"I don't know the planet_watch action '{action}'. I can do earthquakes, disasters, "
                "aurora, aircraft, map, quake_watch, stop_quake_watch and status.")
    except Exception as e:
        return f"Planet watch failed: {e}"
