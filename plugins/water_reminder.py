"""
plugins/water_reminder.py — spoken reminders to drink water at an interval,
plus a daily intake log against a goal.

Reminders run on a daemon thread and are spoken through plugins/_announce.say.
Intake and settings persist in memory/plugin_data/water_reminder.json.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime

from plugins import _announce

GLASS_ML = 250

PLUGIN = {
    "name": "water_reminder",
    "description": (
        "Hydration helper: speaks a reminder to drink water every N minutes and "
        "tracks how much water the user drank today against a daily goal. Use for "
        "'remind me to drink water', 'remind me every 45 minutes to drink', 'I "
        "drank a glass of water', 'log 500 ml', 'how much water have I had', 'set "
        "my water goal to 3 litres', 'stop the water reminders' — in any language. "
        "For a single one-off reminder use the 'reminder' tool instead."
    ),
    "brief": "Water: repeating drink reminders, intake log, daily goal.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'start' (default) reminders, 'stop' them, 'drink' log "
                                      "water, 'status' today's intake, 'goal' set daily goal."},
            "amount": {"type": "STRING",
                       "description": "For 'drink': how much, e.g. '250 ml', '2 glasses', "
                                      "'0.5 l'. Default one glass (250 ml). For 'goal': the "
                                      "target, e.g. '3 litres' or '2500'."},
            "interval_minutes": {"type": "INTEGER",
                                 "description": "For 'start': minutes between reminders (default 60)."},
        },
        "required": [],
    },
}

_DATA = "water_reminder"
_lock = threading.RLock()
_timer: dict | None = None


# ── data ─────────────────────────────────────────────────────────────────────

def _load() -> dict:
    data = _announce.load(_DATA, {}) or {}
    data.setdefault("goal_ml", 2000)
    data.setdefault("days", {})
    return data


def _save(data: dict) -> None:
    days = data.get("days", {})
    for old in sorted(days)[:-120]:
        days.pop(old, None)
    _announce.save(_DATA, data)


def _today_total(data: dict) -> int:
    return int(sum(e.get("ml", 0) for e in data["days"].get(date.today().isoformat(), [])))


_UNIT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)?\s*(?P<unit>ml|millilit(?:er|re)s?|cl|dl|l\b|lit(?:er|re)s?|"
    r"glass(?:es)?|cups?|bottles?|oz|ounces?)?", re.I)

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "half": 0.5, "half a": 0.5}


def parse_ml(text, default: int | None = GLASS_ML, small_bare: str = "l") -> int | None:
    """'250 ml' → 250, '2 glasses' → 500, '1.5 l' → 1500, 'a bottle' → 500,
    '3 litres' → 3000, 500 → 500. A bare number under 10 is read in
    `small_bare` units ('l' for a goal, 'glass' for a drink). None when nothing
    usable is found."""
    if text is None or str(text).strip() == "":
        return default
    s = str(text).strip().lower()
    for w, n in sorted(_WORDS.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"^{w}\b", str(n), s)
    m = next((x for x in _UNIT_RE.finditer(s) if x.group("num") or x.group("unit")), None)
    if m is None:
        return None
    num = float((m.group("num") or "1").replace(",", "."))
    unit = (m.group("unit") or ("ml" if num >= 10 else small_bare)).lower()
    if unit.startswith(("ml", "milli")):
        ml = num
    elif unit == "cl":
        ml = num * 10
    elif unit == "dl":
        ml = num * 100
    elif unit.startswith("l"):
        ml = num * 1000
    elif unit.startswith(("glass", "cup")):
        ml = num * GLASS_ML
    elif unit.startswith("bottle"):
        ml = num * 500
    elif unit.startswith(("oz", "ounce")):
        ml = num * 29.57
    else:
        ml = num
    ml = int(round(ml))
    return ml if 0 < ml <= 20_000 else None


# ── reminder thread ──────────────────────────────────────────────────────────

def _loop(t: dict) -> None:
    stop: threading.Event = t["stop"]
    while not stop.wait(t["interval"] * 60):
        try:
            data = _load()
            total, goal = _today_total(data), data["goal_ml"]
            if total >= goal:
                _announce.say(
                    f"The user has drunk {total} ml today and reached their {goal} ml water "
                    f"goal. Congratulate them in one short sentence and say the water "
                    f"reminders are off for today.")
                with _lock:
                    global _timer
                    if _timer is t:
                        _timer = None
                return
            hour = datetime.now().hour
            if hour < 7 or hour >= 23:
                continue  # nobody wants a hydration prompt at 2 a.m.
            _announce.say(
                f"Remind the user to drink a glass of water now. So far today: {total} ml "
                f"of their {goal} ml goal, {goal - total} ml to go. One short, friendly "
                f"sentence.")
        except Exception as e:
            _announce.log(f"Water reminder error: {e}")


def _start(p: dict) -> str:
    global _timer
    try:
        interval = int(float(p.get("interval_minutes") or 60))
    except (TypeError, ValueError):
        interval = 60
    interval = max(5, min(interval, 600))
    with _lock:
        if _timer is not None:
            if _timer["interval"] == interval:
                return f"Water reminders are already running, every {interval} minutes."
            _timer["stop"].set()
        t = {"interval": interval, "stop": threading.Event()}
        threading.Thread(target=_loop, args=(t,), daemon=True, name="water-reminder").start()
        _timer = t
    data = _load()
    data["interval_min"] = interval
    _save(data)
    total, goal = _today_total(data), data["goal_ml"]
    _announce.log(f"Water reminders every {interval} min")
    return (f"I'll remind you to drink water every {interval} minutes. "
            f"Today so far: {total} of {goal} ml.")


def _stop() -> str:
    global _timer
    with _lock:
        t, _timer = _timer, None
    if t is None:
        return "Water reminders aren't running."
    t["stop"].set()
    return "Water reminders stopped."


def _drink(p: dict) -> str:
    ml = parse_ml(p.get("amount"), GLASS_ML, small_bare="glass")
    if ml is None:
        return "I couldn't tell how much you drank — say it in ml, litres or glasses."
    data = _load()
    before = _today_total(data)
    data["days"].setdefault(date.today().isoformat(), []).append(
        {"t": datetime.now().strftime("%H:%M"), "ml": ml})
    _save(data)
    total, goal = before + ml, data["goal_ml"]
    if before < goal <= total:
        return f"Logged {ml} ml. That's {total} ml today — you've hit your {goal} ml goal. Well done!"
    if total >= goal:
        return f"Logged {ml} ml. {total} ml today, already past your {goal} ml goal."
    return f"Logged {ml} ml. {total} of {goal} ml today, {goal - total} ml to go."


def _status() -> str:
    data = _load()
    total, goal = _today_total(data), data["goal_ml"]
    pct = int(100 * total / goal) if goal else 0
    with _lock:
        running = _timer
    tail = (f" Reminders are on, every {running['interval']} minutes."
            if running else " Reminders are off.")
    if total == 0:
        return f"No water logged today yet; your goal is {goal} ml.{tail}"
    return f"You've had {total} ml of your {goal} ml goal today ({pct}%).{tail}"


def _goal(p: dict) -> str:
    ml = parse_ml(p.get("amount"), default=None)
    if ml is None or ml < 250:
        return "What should the daily goal be? For example '2 litres' or '2500 ml'."
    data = _load()
    data["goal_ml"] = ml
    _save(data)
    return f"Daily water goal set to {ml} ml. You're at {_today_total(data)} ml today."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        action = str(p.get("action") or "start").strip().lower()
        if action in ("start", "on", "remind", "begin"):
            return _start(p)
        if action in ("stop", "off", "end", "cancel"):
            return _stop()
        if action in ("drink", "log", "drank", "add"):
            return _drink(p)
        if action in ("status", "today", "total", "stats"):
            return _status()
        if action in ("goal", "set_goal", "target"):
            return _goal(p)
        return "I can start or stop water reminders, log a drink, set the goal or give today's total."
    except Exception as e:
        return f"The water reminder hit an error: {e}"
