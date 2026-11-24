"""core/briefing.py — what the opening/morning briefing says, shaped by voice.

The briefing is the built-in 'briefing' routine (core/routines). Its options
say which PARTS it is made of, in order, and how:

    parts        e.g. ["weather", "calendar", "news"] — see PARTS
    language     the language to speak it in ('' = the remembered one)
    news_topic   what the news part is about ('' = top world news)
    news_region  a country/region for the news ('' = none)
    city         the place for the weather ('' = the city in memory)
    extra        a free instruction the user added ("and the EUR/USD rate")

gather() fetches every part on worker threads, each with a deadline, and
returns only the parts whose data really arrived; the rest are listed as
skipped with the reason, never invented. main.py turns the result into the
spoken briefing (phase 2 of _send_startup_briefing).

Nothing here touches Qt or the Live session; everything is safe on a worker.
"""
from __future__ import annotations

import concurrent.futures as _cf
import importlib
import time
from datetime import date, datetime, timedelta

PARTS = ("weather", "calendar", "reminders", "email", "news", "focus")
LABELS = {
    "weather":   "Weather",
    "calendar":  "Calendar",
    "reminders": "Reminders today",
    "email":     "Unread email",
    "news":      "News",
    "focus":     "Focus time",
}
# Words people say for a part, so "add my agenda" finds 'calendar'.
_ALIASES = {
    "agenda": "calendar", "events": "calendar", "meetings": "calendar", "schedule": "calendar",
    "mail": "email", "emails": "email", "gmail": "email", "inbox": "email",
    "headlines": "news", "reminder": "reminders", "todo": "reminders",
    "pomodoro": "focus", "focus_stats": "focus", "stats": "focus", "forecast": "weather",
}
# What the briefing always said before it had parts: the news.
LEGACY_PARTS = ["news"]
DEFAULT_TOPIC = "top world news today"
FETCH_TIMEOUT = 12.0


# ── options ──────────────────────────────────────────────────────────────────
def part_name(word) -> str:
    """'agenda' -> 'calendar'; '' when it is not a part."""
    w = str(word or "").strip().lower().replace(" ", "_").replace("-", "_")
    w = _ALIASES.get(w, w)
    return w if w in PARTS else ""


def clean_parts(parts) -> list[str]:
    out: list[str] = []
    if isinstance(parts, str):
        parts = parts.replace(";", ",").split(",")
    for p in parts or []:
        name = part_name(p)
        if name and name not in out:
            out.append(name)
    return out


def normalize(options: dict | None) -> dict:
    """Every option present and valid. An older briefing with no `parts`
    keeps saying what it always said: the news, unless news was off."""
    o = dict(options or {})
    if "parts" in o and isinstance(o.get("parts"), (list, str)):
        parts = clean_parts(o["parts"])
    else:
        parts = [] if o.get("news") is False else list(LEGACY_PARTS)
    return {
        "parts": parts,
        "language": str(o.get("language") or "").strip()[:40],
        "news_topic": str(o.get("news_topic") or "").strip()[:120],
        "news_region": str(o.get("news_region") or "").strip()[:60],
        "city": str(o.get("city") or "").strip()[:80],
        "extra": str(o.get("extra") or "").strip()[:400],
    }


def edit_parts(current, set_parts=None, add=None, remove=None) -> list[str]:
    """New parts list: `set_parts` replaces, then `add` appends (keeping
    order), then `remove` drops."""
    parts = clean_parts(set_parts) if set_parts is not None else clean_parts(current)
    for p in clean_parts(add):
        if p not in parts:
            parts.append(p)
    drop = set(clean_parts(remove))
    return [p for p in parts if p not in drop]


def describe(options: dict | None) -> str:
    o = normalize(options)
    bits = [", ".join(o["parts"]) if o["parts"] else "a greeting only"]
    if "news" in o["parts"]:
        bits.append(f"news about {o['news_topic'] or DEFAULT_TOPIC}"
                    + (f" in {o['news_region']}" if o["news_region"] else ""))
    if o["language"]:
        bits.append(f"in {o['language']}")
    if o["city"] and "weather" in o["parts"]:
        bits.append(f"weather for {o['city']}")
    if o["extra"]:
        bits.append(f"plus: {o['extra']}")
    return "; ".join(bits)


# ── fetching each part ───────────────────────────────────────────────────────
class Skip(Exception):
    """A part with nothing true to say — the message says why (for the log)."""


def _plugin_ready(name: str, google_service: str):
    """The plugin module, when it is installed, switched on and signed in."""
    try:
        from memory.config_manager import get_plugin_enabled
        if not get_plugin_enabled(name):
            raise Skip(f"the {name} plugin is switched off")
    except Skip:
        raise
    except Exception:
        pass
    try:
        mod = importlib.import_module(f"plugins.{name}")
        from plugins import _google_core as g
    except Exception as e:
        raise Skip(f"the {name} plugin is not available ({e})")
    if not g.libs_available() or not g.is_connected(google_service):
        raise Skip(f"Google {google_service} is not connected")
    svc, err = g.service(google_service, None)
    if svc is None:
        raise Skip(err or f"Google {google_service} is not reachable")
    return mod, svc


def _memory_city() -> str:
    try:
        from memory.memory_manager import load_memory
        city = (load_memory().get("identity") or {}).get("city") or {}
        return str(city.get("value") if isinstance(city, dict) else city or "").strip()
    except Exception:
        return ""


def fetch_weather(o: dict) -> str:
    city = o["city"] or _memory_city()
    if not city:
        raise Skip("no city is set for the weather (say which city, or tell me where you live)")
    from core.cloud import weather_today
    w = weather_today(city)
    if w.get("now_c") is None and w.get("max_c") is None:
        raise Skip("the weather service returned nothing")
    parts = [f"{w.get('place') or city}:"]
    if w.get("now_c") is not None:
        parts.append(f"now {w['now_c']}°C{(' ' + w['now']) if w.get('now') else ''}")
    if w.get("min_c") is not None and w.get("max_c") is not None:
        parts.append(f"today {w['min_c']}–{w['max_c']}°C{(', ' + w['today']) if w.get('today') else ''}")
    if w.get("rain_chance_pct") is not None:
        parts.append(f"rain chance {w['rain_chance_pct']}%")
    return " ".join(parts[:1]) + " " + ", ".join(parts[1:])


def fetch_news(o: dict) -> str:
    from actions.web_search import _news
    topic = o["news_topic"] or DEFAULT_TOPIC
    if o["news_region"]:
        topic = (f"top news in {o['news_region']} today" if topic == DEFAULT_TOPIC
                 else f"{topic} {o['news_region']}")
    text = (_news(topic) or "").strip()
    if not text or text.startswith(("No news found", "Search failed", "Please provide")):
        raise Skip("no headlines could be fetched")
    return text


def fetch_calendar(o: dict) -> str:
    mod, svc = _plugin_ready("calendar_control", "calendar")
    text = mod._agenda(svc, {"date": ""}, None)
    if not text or "couldn't" in text.lower():
        raise Skip("the calendar did not answer")
    return text


def fetch_email(o: dict) -> str:
    mod, svc = _plugin_ready("gmail_control", "gmail")
    msgs, total = mod._list(svc, "is:unread in:inbox", 5)
    n = max(int(total or 0), len(msgs))
    if n == 0:
        return "No unread email in the inbox."
    return (f"{'About ' if n > len(msgs) else ''}{n} unread in the inbox. Latest: "
            f"{mod._spoken_list(msgs, 3)}.")


def fetch_reminders(o: dict) -> str:
    from core.routines import desktop
    eng = desktop.engine()
    if eng is None:
        raise Skip("routines are not running")
    now = datetime.now()
    end = now.replace(hour=23, minute=59, second=59)
    items = []
    for r in eng.list():
        if not r.get("enabled") or r.get("kind") not in ("say", "prompt", "task"):
            continue
        if (r.get("trigger") or {}).get("type") != "time":
            continue
        ts = float(r.get("next_ts") or 0)
        if not ts:
            continue
        at = datetime.fromtimestamp(ts)
        if now - timedelta(minutes=5) <= at <= end:
            items.append((at, (r.get("text") or r.get("name") or "")[:100]))
    if not items:
        return "Nothing scheduled for the rest of today."
    items.sort()
    return "; ".join(f"{at:%H:%M} {text}" for at, text in items[:8])


def fetch_focus(o: dict) -> str:
    try:
        from plugins import _announce
        data = _announce.load("pomodoro", {}) or {}
    except Exception as e:
        raise Skip(f"focus history unreadable ({e})")
    days = data.get("days") or {}
    out = []
    for label, d in (("yesterday", date.today() - timedelta(days=1)), ("today", date.today())):
        day = days.get(d.isoformat()) or {}
        minutes = float(day.get("minutes") or 0)
        if minutes >= 1:
            out.append(f"{label}: {int(round(minutes))} focused minutes, "
                       f"{int(day.get('sessions') or 0)} pomodoros")
    week = sum(float((days.get((date.today() - timedelta(days=i)).isoformat()) or {})
                     .get("minutes") or 0) for i in range(7))
    if week >= 1:
        out.append(f"last 7 days: {int(round(week))} minutes")
    if not out:
        raise Skip("no focus time has been logged")
    return "; ".join(out)


_FETCHERS = {
    "weather": fetch_weather, "news": fetch_news, "calendar": fetch_calendar,
    "email": fetch_email, "reminders": fetch_reminders, "focus": fetch_focus,
}


def gather(options: dict | None, timeout: float = FETCH_TIMEOUT, fetchers: dict | None = None) -> dict:
    """{'sections': [{part, title, text}], 'skipped': [{part, why}], 'options'}.
    Parts run in parallel; one that fails or overruns is skipped, never guessed."""
    o = normalize(options)
    fetchers = fetchers or _FETCHERS
    sections, skipped = [], []
    if not o["parts"]:
        return {"sections": sections, "skipped": skipped, "options": o}
    pool = _cf.ThreadPoolExecutor(max_workers=len(o["parts"]), thread_name_prefix="briefing")
    try:
        futs = {p: pool.submit(fetchers[p], o) for p in o["parts"] if p in fetchers}
        deadline = time.monotonic() + timeout
        for part in o["parts"]:
            fut = futs.get(part)
            if fut is None:
                skipped.append({"part": part, "why": "unknown part"})
                continue
            try:
                text = fut.result(timeout=max(0.1, deadline - time.monotonic()))
                text = str(text or "").strip()
                if not text:
                    raise Skip("nothing came back")
                sections.append({"part": part, "title": LABELS[part], "text": text[:2500]})
            except Skip as e:
                skipped.append({"part": part, "why": str(e)})
            except _cf.TimeoutError:
                skipped.append({"part": part, "why": "took too long"})
            except Exception as e:
                skipped.append({"part": part, "why": f"{type(e).__name__}: {e}"[:200]})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {"sections": sections, "skipped": skipped, "options": o}


def panel_text(result: dict) -> str:
    """The briefing as it goes on screen under the head."""
    return "\n\n".join(f"{s['title'].upper()}\n{s['text']}" for s in result.get("sections") or [])


def spoken_instruction(result: dict, lang_clause: str = "") -> str:
    """Phase-2 instruction for the assistant: say these facts and nothing else."""
    o = result.get("options") or normalize({})
    secs = result.get("sections") or []
    extra = o.get("extra") or ""
    if not secs:
        if extra:
            return (f"[BRIEFING] The user asked for this to be part of their briefing: {extra} "
                    f"— do it now, using tools if it needs them.{lang_clause}")
        if result.get("skipped"):
            return ("[BRIEFING] None of the briefing's parts could be fetched just now. Say so "
                    f"in one short sentence. Do not call any tools.{lang_clause}")
        return ""
    data = "\n".join(f"- {s['title']}: {s['text']}" for s in secs)
    rules = ("Say it as one short spoken briefing, in this order, one or two sentences per part. "
             "Use ONLY the facts below — never add or guess anything they do not say. "
             "For the news, pick one or two headlines in your own words and say the full list "
             "is on screen. Do not mention parts that are not listed.")
    tools = (f"\n\nThen also do this, which the user asked to be part of their briefing: "
             f"{extra} — you may use tools for it." if extra else " Do not call any tools.")
    return f"[BRIEFING] {rules}\n{data}{tools}{lang_clause}"
