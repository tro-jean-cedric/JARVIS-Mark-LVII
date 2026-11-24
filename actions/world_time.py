"""
JARVIS action — the exact time anywhere, daylight saving included.

The model is bad at this on its own: it knows that Tokyo is "UTC+9" but not
whether New York has already moved its clocks this week, and it has no idea
what time it is right now. So the answer is computed here, from the IANA
time-zone database, and the model only reads it out.

HOW A PLACE BECOMES A TIME ZONE
    1. An IANA name given directly ("Europe/Paris") or a common abbreviation
       ("UTC", "CET", "PST").
    2. A built-in table of a few hundred major cities and countries. Instant,
       works offline, and covers almost every question people actually ask.
    3. Open-Meteo's free geocoder, which answers any town in any language with
       its time zone. No key, no account.

    Windows ships without a time-zone database, and Python's zoneinfo needs the
    optional `tzdata` package there. When it is missing, the current UTC offset
    is asked from Open-Meteo instead (it reports the offset in force today, so
    daylight saving is still right), and a time conversion uses that offset.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from core import outcome

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:                       # pragma: no cover - Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Abbreviations people actually say. Deliberately mapped to a REGION rather
# than a fixed offset where the region observes daylight saving: "Eastern time"
# in July means EDT, and someone asking for "EST" almost always means "the time
# in New York", not the winter offset.
_ABBREV = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC", "zulu": "UTC",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "eastern": "America/New_York", "eastern time": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "central time": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mountain time": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
    "cet": "Europe/Paris", "cest": "Europe/Paris", "wet": "Europe/Lisbon",
    "eet": "Europe/Athens", "bst": "Europe/London", "ist": "Asia/Kolkata",
    "jst": "Asia/Tokyo", "kst": "Asia/Seoul", "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney", "nzst": "Pacific/Auckland", "msk": "Europe/Moscow",
    "hkt": "Asia/Hong_Kong", "sgt": "Asia/Singapore", "trt": "Europe/Istanbul",
}

# City / country -> IANA zone. Lower-case keys, ASCII and common native
# spellings. A country with several zones maps to its capital's (the geocoder
# handles anything more specific the user names).
_PLACES = {
    # Europe
    "london": "Europe/London", "uk": "Europe/London", "united kingdom": "Europe/London",
    "england": "Europe/London", "britain": "Europe/London", "scotland": "Europe/London",
    "edinburgh": "Europe/London", "manchester": "Europe/London", "wales": "Europe/London",
    "dublin": "Europe/Dublin", "ireland": "Europe/Dublin",
    "lisbon": "Europe/Lisbon", "lisboa": "Europe/Lisbon", "portugal": "Europe/Lisbon",
    "porto": "Europe/Lisbon",
    "madrid": "Europe/Madrid", "spain": "Europe/Madrid", "barcelona": "Europe/Madrid",
    "espana": "Europe/Madrid", "valencia": "Europe/Madrid", "seville": "Europe/Madrid",
    "paris": "Europe/Paris", "france": "Europe/Paris", "lyon": "Europe/Paris",
    "marseille": "Europe/Paris", "nice": "Europe/Paris", "toulouse": "Europe/Paris",
    "brussels": "Europe/Brussels", "bruxelles": "Europe/Brussels", "belgium": "Europe/Brussels",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam", "rotterdam": "Europe/Amsterdam",
    "luxembourg": "Europe/Luxembourg",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin", "deutschland": "Europe/Berlin",
    "munich": "Europe/Berlin", "munchen": "Europe/Berlin", "hamburg": "Europe/Berlin",
    "frankfurt": "Europe/Berlin", "cologne": "Europe/Berlin", "koln": "Europe/Berlin",
    "zurich": "Europe/Zurich", "geneva": "Europe/Zurich", "switzerland": "Europe/Zurich",
    "bern": "Europe/Zurich",
    "vienna": "Europe/Vienna", "wien": "Europe/Vienna", "austria": "Europe/Vienna",
    "rome": "Europe/Rome", "roma": "Europe/Rome", "italy": "Europe/Rome", "milan": "Europe/Rome",
    "milano": "Europe/Rome", "naples": "Europe/Rome", "venice": "Europe/Rome",
    "florence": "Europe/Rome",
    "copenhagen": "Europe/Copenhagen", "denmark": "Europe/Copenhagen",
    "oslo": "Europe/Oslo", "norway": "Europe/Oslo",
    "stockholm": "Europe/Stockholm", "sweden": "Europe/Stockholm",
    "helsinki": "Europe/Helsinki", "finland": "Europe/Helsinki",
    "reykjavik": "Atlantic/Reykjavik", "iceland": "Atlantic/Reykjavik",
    "warsaw": "Europe/Warsaw", "poland": "Europe/Warsaw", "krakow": "Europe/Warsaw",
    "prague": "Europe/Prague", "czechia": "Europe/Prague", "czech republic": "Europe/Prague",
    "budapest": "Europe/Budapest", "hungary": "Europe/Budapest",
    "bratislava": "Europe/Bratislava", "slovakia": "Europe/Bratislava",
    "ljubljana": "Europe/Ljubljana", "slovenia": "Europe/Ljubljana",
    "zagreb": "Europe/Zagreb", "croatia": "Europe/Zagreb",
    "belgrade": "Europe/Belgrade", "serbia": "Europe/Belgrade",
    "sarajevo": "Europe/Sarajevo", "bosnia": "Europe/Sarajevo",
    "sofia": "Europe/Sofia", "bulgaria": "Europe/Sofia",
    "bucharest": "Europe/Bucharest", "romania": "Europe/Bucharest",
    "athens": "Europe/Athens", "greece": "Europe/Athens",
    "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv", "ukraine": "Europe/Kyiv",
    "minsk": "Europe/Minsk", "belarus": "Europe/Minsk",
    "vilnius": "Europe/Vilnius", "lithuania": "Europe/Vilnius",
    "riga": "Europe/Riga", "latvia": "Europe/Riga",
    "tallinn": "Europe/Tallinn", "estonia": "Europe/Tallinn",
    "chisinau": "Europe/Chisinau", "moldova": "Europe/Chisinau",
    "moscow": "Europe/Moscow", "russia": "Europe/Moscow", "saint petersburg": "Europe/Moscow",
    "st petersburg": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "turkey": "Europe/Istanbul", "turkiye": "Europe/Istanbul",
    "ankara": "Europe/Istanbul", "izmir": "Europe/Istanbul", "antalya": "Europe/Istanbul",
    "nicosia": "Asia/Nicosia", "cyprus": "Asia/Nicosia",
    "malta": "Europe/Malta", "valletta": "Europe/Malta",
    # Middle East / Africa
    "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem", "israel": "Asia/Jerusalem",
    "beirut": "Asia/Beirut", "lebanon": "Asia/Beirut",
    "amman": "Asia/Amman", "jordan": "Asia/Amman",
    "damascus": "Asia/Damascus", "syria": "Asia/Damascus",
    "baghdad": "Asia/Baghdad", "iraq": "Asia/Baghdad",
    "tehran": "Asia/Tehran", "iran": "Asia/Tehran",
    "riyadh": "Asia/Riyadh", "saudi arabia": "Asia/Riyadh", "jeddah": "Asia/Riyadh",
    "mecca": "Asia/Riyadh", "medina": "Asia/Riyadh",
    "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "uae": "Asia/Dubai",
    "united arab emirates": "Asia/Dubai",
    "doha": "Asia/Qatar", "qatar": "Asia/Qatar",
    "kuwait": "Asia/Kuwait", "bahrain": "Asia/Bahrain", "manama": "Asia/Bahrain",
    "muscat": "Asia/Muscat", "oman": "Asia/Muscat",
    "baku": "Asia/Baku", "azerbaijan": "Asia/Baku",
    "tbilisi": "Asia/Tbilisi", "georgia country": "Asia/Tbilisi",
    "yerevan": "Asia/Yerevan", "armenia": "Asia/Yerevan",
    "cairo": "Africa/Cairo", "egypt": "Africa/Cairo",
    "casablanca": "Africa/Casablanca", "morocco": "Africa/Casablanca", "rabat": "Africa/Casablanca",
    "algiers": "Africa/Algiers", "algeria": "Africa/Algiers",
    "tunis": "Africa/Tunis", "tunisia": "Africa/Tunis",
    "tripoli": "Africa/Tripoli", "libya": "Africa/Tripoli",
    "lagos": "Africa/Lagos", "nigeria": "Africa/Lagos", "abuja": "Africa/Lagos",
    "accra": "Africa/Accra", "ghana": "Africa/Accra",
    "dakar": "Africa/Dakar", "senegal": "Africa/Dakar",
    "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi",
    "addis ababa": "Africa/Addis_Ababa", "ethiopia": "Africa/Addis_Ababa",
    "kampala": "Africa/Kampala", "uganda": "Africa/Kampala",
    "dar es salaam": "Africa/Dar_es_Salaam", "tanzania": "Africa/Dar_es_Salaam",
    "kinshasa": "Africa/Kinshasa", "luanda": "Africa/Luanda", "angola": "Africa/Luanda",
    "johannesburg": "Africa/Johannesburg", "south africa": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg", "pretoria": "Africa/Johannesburg",
    "durban": "Africa/Johannesburg",
    "harare": "Africa/Harare", "zimbabwe": "Africa/Harare",
    "khartoum": "Africa/Khartoum", "sudan": "Africa/Khartoum",
    # Asia
    "karachi": "Asia/Karachi", "pakistan": "Asia/Karachi", "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi",
    "kabul": "Asia/Kabul", "afghanistan": "Asia/Kabul",
    "tashkent": "Asia/Tashkent", "uzbekistan": "Asia/Tashkent",
    "almaty": "Asia/Almaty", "kazakhstan": "Asia/Almaty", "astana": "Asia/Almaty",
    "bishkek": "Asia/Bishkek", "kyrgyzstan": "Asia/Bishkek",
    "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "india": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "bombay": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "chennai": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata", "pune": "Asia/Kolkata",
    "kathmandu": "Asia/Kathmandu", "nepal": "Asia/Kathmandu",
    "colombo": "Asia/Colombo", "sri lanka": "Asia/Colombo",
    "dhaka": "Asia/Dhaka", "bangladesh": "Asia/Dhaka",
    "yangon": "Asia/Yangon", "myanmar": "Asia/Yangon",
    "bangkok": "Asia/Bangkok", "thailand": "Asia/Bangkok", "phuket": "Asia/Bangkok",
    "hanoi": "Asia/Ho_Chi_Minh", "ho chi minh city": "Asia/Ho_Chi_Minh",
    "saigon": "Asia/Ho_Chi_Minh", "vietnam": "Asia/Ho_Chi_Minh",
    "phnom penh": "Asia/Phnom_Penh", "cambodia": "Asia/Phnom_Penh",
    "kuala lumpur": "Asia/Kuala_Lumpur", "malaysia": "Asia/Kuala_Lumpur",
    "singapore": "Asia/Singapore",
    "jakarta": "Asia/Jakarta", "indonesia": "Asia/Jakarta", "bali": "Asia/Makassar",
    "manila": "Asia/Manila", "philippines": "Asia/Manila",
    "hong kong": "Asia/Hong_Kong", "macau": "Asia/Macau",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "china": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai", "guangzhou": "Asia/Shanghai", "chengdu": "Asia/Shanghai",
    "taipei": "Asia/Taipei", "taiwan": "Asia/Taipei",
    "seoul": "Asia/Seoul", "south korea": "Asia/Seoul", "korea": "Asia/Seoul",
    "busan": "Asia/Seoul", "pyongyang": "Asia/Pyongyang", "north korea": "Asia/Pyongyang",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "osaka": "Asia/Tokyo", "kyoto": "Asia/Tokyo",
    "ulaanbaatar": "Asia/Ulaanbaatar", "mongolia": "Asia/Ulaanbaatar",
    "vladivostok": "Asia/Vladivostok", "novosibirsk": "Asia/Novosibirsk",
    "yekaterinburg": "Asia/Yekaterinburg",
    # Oceania
    "sydney": "Australia/Sydney", "australia": "Australia/Sydney",
    "canberra": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane", "perth": "Australia/Perth",
    "adelaide": "Australia/Adelaide", "darwin": "Australia/Darwin",
    "hobart": "Australia/Hobart",
    "auckland": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
    "fiji": "Pacific/Fiji", "honolulu": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    # Americas
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "washington dc": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York",
    "philadelphia": "America/New_York", "detroit": "America/Detroit",
    "toronto": "America/Toronto", "montreal": "America/Toronto", "ottawa": "America/Toronto",
    "chicago": "America/Chicago", "houston": "America/Chicago", "dallas": "America/Chicago",
    "austin": "America/Chicago", "new orleans": "America/Chicago",
    "minneapolis": "America/Chicago", "winnipeg": "America/Winnipeg",
    "denver": "America/Denver", "salt lake city": "America/Denver",
    "calgary": "America/Edmonton", "edmonton": "America/Edmonton",
    "phoenix": "America/Phoenix", "arizona": "America/Phoenix",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "portland": "America/Los_Angeles", "silicon valley": "America/Los_Angeles",
    "vancouver": "America/Vancouver", "anchorage": "America/Anchorage",
    "alaska": "America/Anchorage",
    "usa": "America/New_York", "united states": "America/New_York",
    "america": "America/New_York", "us": "America/New_York",
    "canada": "America/Toronto", "halifax": "America/Halifax",
    "st johns": "America/St_Johns", "newfoundland": "America/St_Johns",
    "mexico city": "America/Mexico_City", "mexico": "America/Mexico_City",
    "guadalajara": "America/Mexico_City", "monterrey": "America/Monterrey",
    "cancun": "America/Cancun", "tijuana": "America/Tijuana",
    "guatemala": "America/Guatemala", "san jose costa rica": "America/Costa_Rica",
    "costa rica": "America/Costa_Rica", "panama": "America/Panama",
    "havana": "America/Havana", "cuba": "America/Havana",
    "kingston": "America/Jamaica", "jamaica": "America/Jamaica",
    "santo domingo": "America/Santo_Domingo", "dominican republic": "America/Santo_Domingo",
    "puerto rico": "America/Puerto_Rico", "san juan": "America/Puerto_Rico",
    "bogota": "America/Bogota", "colombia": "America/Bogota", "medellin": "America/Bogota",
    "caracas": "America/Caracas", "venezuela": "America/Caracas",
    "quito": "America/Guayaquil", "ecuador": "America/Guayaquil",
    "lima": "America/Lima", "peru": "America/Lima",
    "la paz": "America/La_Paz", "bolivia": "America/La_Paz",
    "santiago": "America/Santiago", "chile": "America/Santiago",
    "buenos aires": "America/Argentina/Buenos_Aires", "argentina": "America/Argentina/Buenos_Aires",
    "montevideo": "America/Montevideo", "uruguay": "America/Montevideo",
    "asuncion": "America/Asuncion", "paraguay": "America/Asuncion",
    "sao paulo": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo", "brasilia": "America/Sao_Paulo", "brazil": "America/Sao_Paulo",
    "brasil": "America/Sao_Paulo", "manaus": "America/Manaus",
}


def _fold(text: str) -> str:
    """Lower-case, accents removed, punctuation collapsed — 'São Paulo' -> 'sao paulo'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s/+-]", " ", t)
    return " ".join(t.split())


# ── Resolving a place ────────────────────────────────────────────────────────

def _geocode(place: str) -> dict | None:
    """{'name', 'country', 'timezone', 'latitude', 'longitude'} or None."""
    try:
        import requests
        r = requests.get(_GEOCODE_URL, params={"name": place, "count": 1, "format": "json"},
                         timeout=6)
        r.raise_for_status()
        hits = (r.json() or {}).get("results") or []
        return hits[0] if hits else None
    except Exception as e:
        print(f"[WorldTime] geocoding failed for {place!r}: {e}")
        return None


def _offset_from_web(lat: float, lon: float) -> tuple[int, str] | None:
    """(UTC offset in seconds, abbreviation) as it is TODAY at a coordinate."""
    try:
        import requests
        r = requests.get(_FORECAST_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m", "forecast_days": 1}, timeout=6)
        r.raise_for_status()
        j = r.json() or {}
        return int(j.get("utc_offset_seconds", 0)), str(j.get("timezone_abbreviation") or "")
    except Exception as e:
        print(f"[WorldTime] offset lookup failed: {e}")
        return None


def _zone(name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None


_UTC_OFFSET_RE = re.compile(r"^(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$")


def resolve(place: str) -> dict | None:
    """Turn a spoken place into {'label', 'zone' (name or ''), 'tz' (tzinfo)}.

    Never raises. None when nothing could be found.
    """
    raw = str(place or "").strip()
    if not raw:
        return None
    key = _fold(raw)

    m = _UTC_OFFSET_RE.match(key.replace(" ", ""))
    if m:
        sign = -1 if m.group(1) == "-" else 1
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return {"label": raw.upper(), "zone": "", "tz": timezone(sign * delta)}

    # An IANA name as given ("Europe/Paris", "america/new_york").
    if "/" in raw:
        for candidate in (raw.strip(), raw.strip().title().replace(" ", "_")):
            tz = _zone(candidate)
            if tz is not None:
                return {"label": candidate.split("/")[-1].replace("_", " "),
                        "zone": candidate, "tz": tz}

    zone_name = _ABBREV.get(key) or _PLACES.get(key)
    if zone_name is None and key.startswith("the "):
        zone_name = _PLACES.get(key[4:])
    if zone_name:
        tz = _zone(zone_name)
        if tz is not None:
            return {"label": raw, "zone": zone_name, "tz": tz}

    # The geocoder knows every town on Earth, in any language.
    hit = _geocode(raw)
    if hit and hit.get("timezone"):
        zone_name = str(hit["timezone"])
        label = ", ".join(x for x in (hit.get("name"), hit.get("country")) if x) or raw
        tz = _zone(zone_name)
        if tz is not None:
            return {"label": label, "zone": zone_name, "tz": tz}
        # No local tz database (Windows without `tzdata`): today's offset.
        off = _offset_from_web(hit.get("latitude", 0), hit.get("longitude", 0))
        if off is not None:
            return {"label": label, "zone": zone_name,
                    "tz": timezone(timedelta(seconds=off[0]), off[1] or None),
                    "fixed": True}
    elif zone_name:
        # A table hit, but the database is missing and the web did not answer.
        return None
    return None


# ── Formatting ───────────────────────────────────────────────────────────────

def _fmt_offset(td: timedelta | None) -> str:
    secs = int((td or timedelta()).total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    return f"UTC{sign}{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def _fmt_gap(seconds: int) -> str:
    if seconds == 0:
        return "the same time as here"
    ahead = seconds > 0
    seconds = abs(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    span = f"{h} hour{'s' if h != 1 else ''}" if h else ""
    if m:
        span = (span + " " if span else "") + f"{m} minutes"
    return f"{span} {'ahead of' if ahead else 'behind'} here"


def _describe(dt: datetime, label: str, zone: str, fixed: bool = False) -> str:
    local_off = datetime.now().astimezone().utcoffset() or timedelta()
    gap = int(((dt.utcoffset() or timedelta()) - local_off).total_seconds())
    dst = dt.dst()
    dst_txt = ""
    if not fixed and dst is not None:
        dst_txt = "daylight saving time in effect" if dst else "standard time (no daylight saving now)"
    bits = [
        f"{label}: {dt.strftime('%H:%M')} ({dt.strftime('%I:%M %p').lstrip('0')}) "
        f"on {dt.strftime('%A, %d %B %Y')}",
        f"{_fmt_offset(dt.utcoffset())}" + (f" [{zone}]" if zone else ""),
    ]
    if dst_txt:
        bits.append(dst_txt)
    bits.append(_fmt_gap(gap))
    return "; ".join(bits)


_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::|\.|h)?(\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.I)


def _parse_clock(text: str) -> tuple[int, int] | None:
    m = _CLOCK_RE.match(str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = (m.group(3) or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


# ── The tool ─────────────────────────────────────────────────────────────────

def world_time(parameters: dict, player=None) -> str:
    params = parameters or {}
    places = params.get("places") or params.get("place") or ""
    if isinstance(places, str):
        places = [p for p in re.split(r"\s*;\s*", places) if p.strip()]
    places = [str(p).strip() for p in (places or []) if str(p).strip()][:6]
    at = str(params.get("time") or "").strip()
    from_place = str(params.get("from_place") or "").strip()

    if not places:
        return outcome.ask("world_time", "which city or country the user means")

    # Converting a given clock time ("3 pm in London -> Tokyo").
    if at:
        clock = _parse_clock(at)
        if clock is None:
            return outcome.failed("world_time", f"'{at}' is not a time of day I can read; "
                                                "give it as HH:MM")
        if from_place:
            src = resolve(from_place)
            if src is None:
                return outcome.failed("world_time", f"no time zone found for '{from_place}'")
            src_tz, src_label = src["tz"], src["label"]
        else:
            src_tz, src_label = datetime.now().astimezone().tzinfo, "here"
        today = datetime.now(src_tz)
        start = today.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        lines = [f"{clock[0]:02d}:{clock[1]:02d} {src_label} "
                 f"({start.strftime('%A %d %B')}, {_fmt_offset(start.utcoffset())}) is:"]
        for p in places:
            dst = resolve(p)
            if dst is None:
                lines.append(f"- {p}: no time zone found")
                continue
            there = start.astimezone(dst["tz"])
            day_note = ""
            if there.date() != start.date():
                day_note = " (the next day)" if there.date() > start.date() else " (the day before)"
            lines.append(f"- {dst['label']}: {there.strftime('%H:%M')} "
                         f"{there.strftime('%A')}{day_note}, {_fmt_offset(there.utcoffset())}")
        return "\n".join(lines)

    # The time right now.
    out, missing = [], []
    for p in places:
        r = resolve(p)
        if r is None:
            missing.append(p)
            continue
        now = datetime.now(timezone.utc).astimezone(r["tz"])
        out.append(_describe(now, r["label"], r["zone"], r.get("fixed", False)))
    if not out:
        return outcome.failed("world_time", "no time zone was found for " + ", ".join(missing)
                              + " (and the online lookup did not answer)")
    if missing:
        out.append("Not found: " + ", ".join(missing))
    text = "\n".join(out)
    if player is not None:
        try:
            player.write_log(f"JARVIS: {out[0][:90]}")
        except Exception:
            pass
    return text


TOOL = {
    "name": "world_time",
    "description": (
        "The exact current time — or a time conversion — for any city, country or time "
        "zone, with daylight saving taken into account. Use it for 'what time is it in "
        "Tokyo?', 'is it night in New York?', 'how far ahead is Sydney?', or 'when it is "
        "3 pm in London, what time is it in Los Angeles?'. Never work out a foreign time "
        "yourself. For the local time you already have [CURRENT DATE & TIME]."
    ),
    "brief": "Current time or a time conversion for any city/country, DST-aware.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "places": {
                "type": "ARRAY", "items": {"type": "STRING"},
                "description": "One or more cities, countries or time zones, as the user said them",
            },
            "time": {
                "type": "STRING",
                "description": "Only to convert a clock time: the time as HH:MM (or '3 pm')",
            },
            "from_place": {
                "type": "STRING",
                "description": "Where 'time' is meant; leave empty for the user's own local time",
            },
        },
        "required": ["places"],
    },
    "handler": world_time,
}
