"""
plugins/calendar_control.py — Google Calendar by voice: a day's agenda, a date
range, the next events, search, and adding an event.

Sign-in is shared with the other Google plugins through plugins/_google_core.py
(⚙ → PLUGIN SETTINGS → Google). Every time is read and written in this
computer's local time zone. Adding an event is reversible ("undo" deletes it),
so it happens at once rather than behind a confirmation.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta

from plugins import _google_core as g

PLUGIN = {
    "name": "calendar_control",
    "description": (
        "Reads the user's GOOGLE CALENDAR and adds events. Use for: 'what's on my "
        "calendar today / tomorrow / this week', 'what's my next meeting', 'am I free "
        "on Friday', 'find the dentist in my calendar', 'add a meeting on Friday at 3'. "
        "Prefer passing 'start' as ISO-8601 local time ('2026-08-09T15:00', or just "
        "'2026-08-09' with all_day true); natural words like 'friday 3pm' also work. "
        "For a day or range use 'date' (and 'end_date')."
    ),
    "brief": "Google Calendar: agenda, next events, search, add.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "agenda (a day or range, default), next (upcoming events), "
                                      "add (create an event), search."},
            "date": {"type": "STRING",
                     "description": "For agenda: first day, ISO date or words like 'tomorrow' "
                                    "(default today)."},
            "end_date": {"type": "STRING",
                         "description": "For agenda: optional last day of a range, inclusive."},
            "title": {"type": "STRING", "description": "For add: the event title."},
            "start": {"type": "STRING",
                      "description": "For add: start, ISO-8601 local ('2026-08-09T15:00') "
                                     "or the user's words."},
            "duration": {"type": "INTEGER",
                         "description": "For add: length in minutes (default 60)."},
            "all_day": {"type": "BOOLEAN", "description": "For add: true for a whole-day event."},
            "location": {"type": "STRING", "description": "For add: optional place."},
            "query": {"type": "STRING", "description": "For search: words to look for."},
            "count": {"type": "INTEGER",
                      "description": "For next/search: how many events (default 5)."},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = g.PLUGIN_SETTINGS_SHARED

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _tz():
    return datetime.now().astimezone().tzinfo


def _now() -> datetime:
    return datetime.now().astimezone()


# ── Parsing dates and times ──────────────────────────────────────────────────

def parse_iso(text: str):
    """(datetime | date, has_time) from an ISO string, or (None, False)."""
    s = (text or "").strip().replace("Z", "+00:00")
    if not s:
        return None, False
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return date.fromisoformat(m.group(1)), False
        except ValueError:
            return None, False
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt, True


def _parse_clock(text: str):
    """A time of day from free text, or None."""
    t = text.lower()
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return dtime(12, 0)
    if re.search(r"\bmidnight\b", t):
        return dtime(0, 0)
    m = re.search(r"\b(\d{1,2})(?:[:.h](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        pm = m.group(3).startswith("p")
        if h == 12:
            h = 0
        h += 12 if pm else 0
        if 0 <= h < 24 and 0 <= mi < 60:
            return dtime(h, mi)
    m = re.search(r"\b([01]?\d|2[0-3])[:h.]([0-5]\d)\b", t)
    if m:
        return dtime(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\bat (\d{1,2})\b(?!\s*(?:st|nd|rd|th|[/-]))", t)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:          # "at 3" for a meeting almost always means 15:00
            h += 12
        if 0 <= h < 24:
            return dtime(h, 0)
    for word, hh in (("morning", 9), ("afternoon", 14), ("evening", 18), ("tonight", 20)):
        if re.search(rf"\b{word}\b", t):
            return dtime(hh, 0)
    return None


def _parse_day(text: str, today: date):
    """A calendar day from free text, or None."""
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.search(r"\bday after tomorrow\b", t):
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b|\bthis (morning|afternoon|evening)\b", t):
        return today
    m = re.search(r"\bin (\d+) (day|week)s?\b", t)
    if m:
        n = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
        return today + timedelta(days=n)
    m = re.search(r"\bnext week\b", t)
    if m and not any(w in t for w in _WEEKDAYS):
        return today + timedelta(days=7 - today.weekday())
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(rf"\b{wd}\b", t):
            # "friday" = the coming one (today if it is Friday);
            # "next friday" = never today.
            ahead = (i - today.weekday()) % 7
            if ahead == 0 and re.search(rf"\bnext {wd}\b", t):
                ahead = 7
            return today + timedelta(days=ahead)
    mon = "|".join(_MONTHS) + "|" + "|".join(m[:3] for m in _MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({mon})\b", t) or \
        re.search(rf"\b({mon})\.? (\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.group(1), m.group(2)
        day_s, mon_s = (a, b) if a.isdigit() else (b, a)
        month = next(i + 1 for i, name in enumerate(_MONTHS) if name.startswith(mon_s[:3]))
        try:
            d = date(today.year, month, int(day_s))
        except ValueError:
            return None
        if d < today - timedelta(days=1):
            d = d.replace(year=today.year + 1)
        return d
    return None


def fallback_parse(text: str, now: datetime | None = None):
    """Rule-based (datetime | date, has_time) for when the model is unavailable."""
    now = now or _now()
    text = (text or "").strip()
    if not text:
        return None, False
    iso, has_time = parse_iso(text)
    if iso is not None:
        return iso, has_time
    t = text.lower()
    m = re.search(r"\bin (\d+) (minute|min|hour)s?\b", t)
    if m:
        n = int(m.group(1))
        delta = timedelta(hours=n) if m.group(2) == "hour" else timedelta(minutes=n)
        return (now + delta).replace(second=0, microsecond=0), True
    day = _parse_day(t, now.date())
    clock = _parse_clock(t)
    if day is None and clock is None:
        return None, False
    if day is None:
        day = now.date()
        if datetime.combine(day, clock, _tz()) < now:
            day += timedelta(days=1)
    if clock is None:
        return day, False
    return datetime.combine(day, clock, tzinfo=_tz()), True


def parse_when(text: str):
    """ISO first; then the model; then the rule-based parser."""
    iso, has_time = parse_iso(text)
    if iso is not None:
        return iso, has_time
    now = _now()
    try:
        from core import gemini
        prompt = (
            "Convert a spoken date/time into ISO-8601 local time.\n"
            f"Now: {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')}).\n"
            f"Text: {text!r}\n"
            'Reply with JSON only: {"start": "YYYY-MM-DDTHH:MM"} when a time of day is '
            'given, {"start": "YYYY-MM-DD"} for a day without a time, or {"start": null} '
            "when it is not a date. Prefer the next future occurrence."
        )
        data = gemini.as_json(prompt, tier=gemini.FAST, timeout_ms=12_000)
        if isinstance(data, dict) and data.get("start"):
            got, has_time = parse_iso(str(data["start"]))
            if got is not None:
                return got, has_time
    except Exception as e:
        print(f"[calendar] model date parse failed: {e}")
    return fallback_parse(text, now)


# ── Formatting ───────────────────────────────────────────────────────────────

def _event_start(ev: dict):
    s = ev.get("start") or {}
    if s.get("dateTime"):
        dt, _ = parse_iso(s["dateTime"])
        return dt.astimezone() if isinstance(dt, datetime) else dt
    if s.get("date"):
        return date.fromisoformat(s["date"])
    return None


def _clock(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _day_label(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (d - today).days < 7:
        return d.strftime("%A")
    return d.strftime("%a %d %b")


def describe(ev: dict, today: date, with_day: bool) -> str:
    st = _event_start(ev)
    title = ev.get("summary") or "(untitled)"
    if isinstance(st, datetime):
        when = _clock(st)
        if with_day:
            when = f"{_day_label(st.date(), today)} {when}"
    elif isinstance(st, date):
        when = f"{_day_label(st, today)}, all day" if with_day else "all day"
    else:
        when = ""
    loc = ev.get("location")
    return f"{when} {title}" + (f" ({loc})" if loc else "")


def _show(player, title: str, lines: list[str]) -> None:
    if player is None:
        return
    try:
        player.show_content(title, "\n".join(lines))
    except Exception:
        pass


def _count(params) -> int:
    try:
        return max(1, min(int(params.get("count") or 5), 25))
    except (TypeError, ValueError):
        return 5


def _list(svc, **kw) -> list[dict]:
    resp = svc.events().list(calendarId="primary", singleEvents=True,
                             orderBy="startTime", **kw).execute()
    return resp.get("items", []) or []


def _rfc(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ── Actions ──────────────────────────────────────────────────────────────────

def _day_from(text: str):
    if not (text or "").strip():
        return _now().date()
    got, _ = parse_iso(text)
    if got is None:
        got, _ = fallback_parse(text)
    if got is None:
        got, _ = parse_when(text)
    if isinstance(got, datetime):
        return got.date()
    return got


def _agenda(svc, params, player) -> str:
    first = _day_from(params.get("date", ""))
    if first is None:
        return "I couldn't understand that date."
    last = _day_from(params.get("end_date", "")) if params.get("end_date") else first
    if last is None or last < first:
        last = first
    last = min(last, first + timedelta(days=31))
    tz = _tz()
    t0 = datetime.combine(first, dtime(0, 0), tz)
    t1 = datetime.combine(last + timedelta(days=1), dtime(0, 0), tz)
    events = _list(svc, timeMin=_rfc(t0), timeMax=_rfc(t1), maxResults=100)
    today = _now().date()
    span = _day_label(first, today) if first == last else \
        f"{_day_label(first, today)} to {_day_label(last, today)}"
    if not events:
        return f"Your calendar is clear {('for ' + span) if first != today else 'today'}."
    multi = first != last
    lines = [describe(e, today, multi) for e in events]
    _show(player, f"Calendar — {span}", lines)
    spoken = "; ".join(lines[:8]) + ("; and more." if len(lines) > 8 else ".")
    return f"{len(events)} event{'s' if len(events) != 1 else ''} {span}: {spoken}"


def _next(svc, params, player) -> str:
    n = _count(params)
    events = _list(svc, timeMin=_rfc(_now()), maxResults=n)
    if not events:
        return "You have no upcoming events."
    today = _now().date()
    lines = [describe(e, today, True) for e in events]
    _show(player, "Upcoming events", lines)
    if n == 1 or len(events) == 1:
        return f"Your next event: {lines[0]}."
    return "Coming up: " + "; ".join(lines) + "."


def _search(svc, params, player) -> str:
    q = (params.get("query") or params.get("title") or "").strip()
    if not q:
        return "What should I search your calendar for?"
    events = _list(svc, q=q, timeMin=_rfc(_now() - timedelta(days=1)), maxResults=_count(params))
    if not events:
        return f"I couldn't find any upcoming events matching '{q}'."
    today = _now().date()
    lines = [describe(e, today, True) for e in events]
    _show(player, f"Calendar: {q}"[:48], lines)
    return "Found: " + "; ".join(lines) + "."


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "y")


def _add(svc, params, player) -> str:
    title = (params.get("title") or params.get("summary") or "").strip()
    if not title:
        return "What should I call the event?"
    when = (params.get("start") or params.get("date") or "").strip()
    if not when:
        return "When should it be? I need a date and time."
    all_day = _truthy(params.get("all_day"))
    start, has_time = parse_when(when)
    if start is None:
        return f"I couldn't understand '{when}' as a date and time. Say the day and time again."
    if not has_time and not all_day:
        d = start if isinstance(start, date) else start.date()
        return f"What time on {d.strftime('%A %d %B')}? Or should it be an all-day event?"
    body: dict = {"summary": title}
    if params.get("location"):
        body["location"] = str(params["location"])
    today = _now().date()
    if all_day:
        d = start.date() if isinstance(start, datetime) else start
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
        said = f"{_day_label(d, today)}, all day"
    else:
        try:
            minutes = int(params.get("duration") or 60)
        except (TypeError, ValueError):
            minutes = 60
        minutes = max(5, min(minutes, 24 * 60))
        end = start + timedelta(minutes=minutes)
        body["start"] = {"dateTime": _rfc(start)}
        body["end"] = {"dateTime": _rfc(end)}
        tzname = _iana_zone()
        if tzname:
            body["start"]["timeZone"] = body["end"]["timeZone"] = tzname
        said = f"{_day_label(start.date(), today)} at {_clock(start)}"
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    if not ev.get("id"):
        return "Google didn't confirm the new event, so I'm not sure it was added."
    _push_undo(ev["id"], title)
    _show(player, "Event added", [f"{title}", said, ev.get("htmlLink", "")])
    return f"Added '{title}' {said}."


def _iana_zone() -> str:
    """The local IANA zone name when it can be found; '' otherwise (the
    offset in the dateTime is then used, which Google accepts)."""
    import os
    tz = os.environ.get("TZ", "")
    if "/" in tz:
        return tz.lstrip(":")
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    return ""


def _push_undo(event_id: str, title: str) -> None:
    try:
        from core.undo import push_undo
    except Exception:
        return

    def undo():
        svc, err = g.service("calendar")
        if svc is None:
            return err
        try:
            svc.events().delete(calendarId="primary", eventId=event_id).execute()
            return f"Removed '{title}' from the calendar."
        except Exception as e:
            return g.explain_error(e, "calendar")

    push_undo(f"added '{title}' to the calendar", undo)


_ACTIONS = {
    "agenda": _agenda, "today": _agenda, "list": _agenda, "day": _agenda, "range": _agenda,
    "next": _next, "upcoming": _next,
    "add": _add, "create": _add, "new": _add,
    "search": _search, "find": _search,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    try:
        from plugins import _announce
        _announce.remember(player)
    except Exception:
        pass
    action = str(params.get("action") or "agenda").strip().lower()
    if action in ("help", "status", "setup"):
        return ("I can show a day's agenda, your next events, add an event, or search. "
                f"Connection: {g.status_text()}")
    fn = _ACTIONS.get(action)
    if fn is None:
        return "I can show a day's agenda, your next events, add an event, or search."
    svc, err = g.service("calendar", player)
    if svc is None:
        return err
    try:
        return fn(svc, params, player)
    except Exception as e:
        return g.explain_error(e, "calendar", player)
