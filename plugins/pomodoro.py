"""
plugins/pomodoro.py — a focus / break timer that speaks at each change and
keeps a daily record of focused minutes (per task).

The timer runs on its own daemon thread; run() only starts, stops or reads it.
Transitions are announced through plugins/_announce.say, so the model phrases
them in the user's language. History lives in memory/plugin_data/pomodoro.json.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from plugins import _announce

PLUGIN = {
    "name": "pomodoro",
    "description": (
        "Pomodoro focus timer that runs in the background: a focus block, then a "
        "break, repeating, with a spoken cue at every switch, and a record of how "
        "long the user focused today (optionally per task). Use for 'start a "
        "pomodoro', 'focus for 50 minutes on my thesis', 'stop the timer', 'how "
        "long is left', 'how much did I focus today'. For a single one-off "
        "reminder use the 'reminder' tool instead."
    ),
    "brief": "Pomodoro focus timer: start, stop, time left, today's focus total.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'start' (default), 'stop', 'status' (time left in "
                                      "the current phase) or 'stats' (today's focus)."},
            "focus_minutes": {"type": "INTEGER", "description": "Focus block length, default 25."},
            "break_minutes": {"type": "INTEGER", "description": "Short break length, default 5."},
            "long_break_minutes": {"type": "INTEGER",
                                   "description": "Break after every 4th focus block, default 15."},
            "task": {"type": "STRING",
                     "description": "Optional: what the user is working on (logged per task)."},
        },
        "required": [],
    },
}

_DATA = "pomodoro"
_lock = threading.RLock()
_state: dict | None = None   # the running timer, or None


# ── persistence ──────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _record(minutes: float, task: str, completed: bool) -> None:
    if minutes < 0.5 and not completed:
        return
    data = _announce.load(_DATA, {}) or {}
    days = data.setdefault("days", {})
    day = days.setdefault(_today(), {"minutes": 0.0, "sessions": 0, "tasks": {}})
    day["minutes"] = round(day.get("minutes", 0.0) + minutes, 1)
    if completed:
        day["sessions"] = day.get("sessions", 0) + 1
    if task:
        tasks = day.setdefault("tasks", {})
        tasks[task] = round(tasks.get(task, 0.0) + minutes, 1)
    # keep a year of history at most
    for old in sorted(days)[:-366]:
        days.pop(old, None)
    try:
        _announce.save(_DATA, data)
    except Exception as e:
        _announce.log(f"Pomodoro: could not save history ({e})")


def _today_stats() -> dict:
    data = _announce.load(_DATA, {}) or {}
    return (data.get("days") or {}).get(_today()) or {"minutes": 0.0, "sessions": 0, "tasks": {}}


# ── formatting ───────────────────────────────────────────────────────────────

def _mins(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, r = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {r} minute{'s' if r != 1 else ''}" if r else "")


def _clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


# ── the timer thread ─────────────────────────────────────────────────────────

def _loop(st: dict) -> None:
    stop: threading.Event = st["stop"]
    while not stop.is_set():
        left = st["ends"] - time.time()
        if left > 0:
            stop.wait(min(left, 1.0))
            continue
        with _lock:
            if stop.is_set():
                return
            if st["phase"] == "focus":
                st["blocks"] += 1
                _record(st["focus"], st["task"], completed=True)
                long_one = st["blocks"] % 4 == 0
                brk = st["long_break"] if long_one else st["break"]
                st["phase"], st["phase_len"] = "break", brk
                st["ends"] = time.time() + brk * 60
                st["phase_started"] = time.time()
                _announce.log(f"Pomodoro: focus block {st['blocks']} done, "
                              f"{brk}-minute {'long ' if long_one else ''}break")
                _announce.say(
                    f"A {st['focus']}-minute focus block just ended "
                    f"(number {st['blocks']} this session). Congratulate the user in one "
                    f"short sentence and tell them to take a {brk}-minute "
                    f"{'long ' if long_one else ''}break.")
            else:
                st["phase"], st["phase_len"] = "focus", st["focus"]
                st["ends"] = time.time() + st["focus"] * 60
                st["phase_started"] = time.time()
                _announce.log("Pomodoro: break over, focus started")
                _announce.say(
                    f"The break is over. In one short, encouraging sentence tell the user "
                    f"the next {st['focus']}-minute focus block starts now"
                    + (f" (task: {st['task']})." if st["task"] else "."))


# ── actions ──────────────────────────────────────────────────────────────────

def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _start(p: dict) -> str:
    global _state
    with _lock:
        if _state is not None:
            left = _state["ends"] - time.time()
            return (f"A pomodoro is already running — {_state['phase']}, "
                    f"{_clock(left)} left. Say 'stop the timer' to end it first.")
        focus = _int(p.get("focus_minutes"), 25, 1, 240)
        brk = _int(p.get("break_minutes"), 5, 1, 120)
        long_brk = _int(p.get("long_break_minutes"), 15, 1, 180)
        task = str(p.get("task") or "").strip()[:80]
        now = time.time()
        st = {"phase": "focus", "focus": focus, "break": brk, "long_break": long_brk,
              "phase_len": focus, "task": task, "ends": now + focus * 60,
              "phase_started": now, "blocks": 0, "stop": threading.Event()}
        st["thread"] = threading.Thread(target=_loop, args=(st,), daemon=True,
                                        name="pomodoro")
        _state = st
        st["thread"].start()
    _announce.log(f"Pomodoro: {focus} min focus / {brk} min break"
                  + (f" — {task}" if task else ""))
    return (f"Pomodoro started: {_mins(focus)} of focus"
            + (f" on {task}" if task else "")
            + f", then a {brk}-minute break. I'll tell you when it's time.")


def _stop() -> str:
    global _state
    with _lock:
        st, _state = _state, None
        if st is None:
            return "No pomodoro is running right now."
        st["stop"].set()
        if st["phase"] == "focus":
            done_min = (time.time() - st["phase_started"]) / 60
            _record(done_min, st["task"], completed=False)
    total = _today_stats().get("minutes", 0)
    _announce.log("Pomodoro: stopped")
    return (f"Pomodoro stopped after {st['blocks']} completed focus block"
            f"{'s' if st['blocks'] != 1 else ''}. Today's focus so far: {_mins(total)}.")


def _status() -> str:
    with _lock:
        st = _state
        if st is None:
            return "No pomodoro is running. Say 'start a pomodoro' to begin."
        left = st["ends"] - time.time()
        phase = "focus" if st["phase"] == "focus" else "break"
        return (f"{phase.capitalize()} phase, {_clock(left)} left"
                + (f", working on {st['task']}" if st["task"] and phase == "focus" else "")
                + f". {st['blocks']} block{'s' if st['blocks'] != 1 else ''} done this session.")


def _stats() -> str:
    day = _today_stats()
    minutes = day.get("minutes", 0) or 0
    # include the focus block in progress
    with _lock:
        st = _state
        if st is not None and st["phase"] == "focus":
            minutes += (time.time() - st["phase_started"]) / 60
    if minutes < 0.5 and not day.get("sessions"):
        return "You haven't logged any focus time today yet."
    text = (f"Today you've focused for {_mins(minutes)} across "
            f"{day.get('sessions', 0)} completed pomodoro"
            f"{'s' if day.get('sessions', 0) != 1 else ''}.")
    tasks = day.get("tasks") or {}
    if tasks:
        top = sorted(tasks.items(), key=lambda kv: kv[1], reverse=True)[:4]
        text += " By task: " + ", ".join(f"{k} {_mins(v)}" for k, v in top) + "."
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        action = str(p.get("action") or "start").strip().lower()
        if action in ("start", "begin", "resume"):
            return _start(p)
        if action in ("stop", "end", "cancel", "pause"):
            return _stop()
        if action in ("status", "left", "remaining", "time"):
            return _status()
        if action in ("stats", "statistics", "today", "total", "history"):
            return _stats()
        return "Pomodoro can start, stop, give the status or today's stats."
    except Exception as e:
        return f"The pomodoro timer hit an error: {e}"
