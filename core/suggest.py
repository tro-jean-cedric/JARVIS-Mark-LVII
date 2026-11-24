"""core/suggest.py — "Things to say": a few suggestion chips under the head.

Built locally, with no model call, from what is true right now:
  * the time of day,
  * which plugins are installed and switched on,
  * the user's routines (is there a briefing, what does it already say),
  * what the user has not tried yet (actions/action_history.tools_used),
  * the First steps checklist while it is unfinished.

Each candidate is (phrase, needs, weight): `needs` names a tool that must be
available. The window sends a clicked chip as a typed command.
"""
from __future__ import annotations

import zlib
from datetime import datetime

# (phrase, tool it needs or "", the tool it exercises (for "not tried yet"), when)
# when: "morning" | "day" | "evening" | "any"
_POOL = (
    ("What's on my calendar today?", "calendar_control", "calendar_control", "morning"),
    ("Give me my morning briefing", "routines", "routines", "morning"),
    ("Do I have any new email?", "gmail_control", "gmail_control", "morning"),
    ("What's in the news today?", "web_search", "web_search", "morning"),
    ("Start a 25-minute focus session", "pomodoro", "pomodoro", "day"),
    ("Remind me in an hour to take a break", "reminder", "reminder", "day"),
    ("What's on my screen?", "screen_process", "screen_process", "day"),
    ("How is my computer doing?", "system_status", "system_status", "any"),
    ("What's on my shopping list?", "shopping_list", "shopping_list", "any"),
    ("Quiz me on something I'm learning", "quiz", "quiz", "evening"),
    ("Remind me to drink water every hour", "water_reminder", "water_reminder", "day"),
    ("What did you do for me today?", "action_history", "action_history", "evening"),
    ("What time is it in Tokyo?", "world_time", "world_time", "any"),
    ("Summarise a YouTube video for me", "video_summarizer", "video_summarizer", "any"),
    ("Keep my computer awake while I'm away", "remote_mode", "remote_mode", "evening"),
    ("Show me what you can do", "assistant_help", "assistant_help", "any"),
)


def part_of_day(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "day"
    return "evening"


def suggestions(now: datetime | None = None, available: set[str] | None = None,
                routines: list[dict] | None = None, used: dict | None = None,
                first_steps: list[dict] | None = None, limit: int = 4) -> list[str]:
    """Up to `limit` short phrases. `available` = tool names that can run now
    (None = do not filter). Deterministic for the same inputs and hour."""
    now = now or datetime.now()
    used = used or {}
    pod = part_of_day(now)
    scored: list[tuple[float, str]] = []

    def ok(tool: str) -> bool:
        return not tool or available is None or tool in available

    for phrase, needs, tool, when in _POOL:
        if not ok(needs):
            continue
        score = 1.0
        if when == pod:
            score += 2.0
        elif when != "any":
            score -= 0.5
        if int((used.get(tool) or {}).get("n", 0)) == 0:
            score += 1.5                       # not tried yet
        # a little rotation by the hour, so the row is not frozen all day
        score += ((zlib.crc32(phrase.encode()) ^ now.hour) % 7) / 20.0
        scored.append((score, phrase))

    # The user's routines: shape the briefing they already have.
    for r in routines or []:
        if r.get("builtin") == "briefing" and ok("routines"):
            try:
                from core import briefing
                parts = briefing.normalize(r.get("options"))["parts"]
            except Exception:
                parts = []
            if "weather" not in parts:
                scored.append((4.2 if pod == "morning" else 1.2, "Add the weather to my briefing"))
            elif "calendar" not in parts and (available is None or "calendar_control" in available):
                scored.append((4.1 if pod == "morning" else 1.1, "Add my calendar to my briefing"))
            if not r.get("enabled"):
                scored.append((1.0, "Turn my morning briefing back on"))
        if r.get("builtin") == "weekly_recap" and pod == "evening" and now.weekday() >= 4:
            scored.append((2.0, "What did you do for me this week?"))

    # First steps not done yet go first while the checklist is unfinished.
    for step in first_steps or []:
        if not step.get("done"):
            scored.append((4.4, step["say"]))
            break

    out: list[str] = []
    for _score, phrase in sorted(scored, key=lambda x: -x[0]):
        if phrase not in out:
            out.append(phrase)
        if len(out) >= limit:
            break
    return out


def current(available: set[str] | None = None, limit: int = 4) -> list[str]:
    """Suggestions from the live state of this app. Never raises."""
    try:
        from actions.action_history import tools_used
        used = tools_used()
    except Exception:
        used = {}
    try:
        from core.routines import desktop
        eng = desktop.engine()
        routines = eng.list() if eng else []
    except Exception:
        routines = []
    try:
        from core import first_steps
        steps = first_steps.status(used)
    except Exception:
        steps = []
    return suggestions(datetime.now(), available, routines, used, steps, limit)
