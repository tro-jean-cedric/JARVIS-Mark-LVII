"""core/recap.py — the weekly recap: what the assistant did for the user this
week, plus one thing they have not tried yet.

Built from actions/action_history.py (a week of tool calls: which tool, what
for, how it ended — never content) and its all-time per-tool counter. The
numbers are counted here, not by a model; main.py hands the summary to the
assistant to say in the user's language (the 'weekly_recap' built-in routine).
"""
from __future__ import annotations

import zlib
from collections import Counter
from datetime import datetime

WEEK = 7 * 86400

# Tool name → what it means to the user, for the summary.
_LABELS = {
    "open_app": "opened apps", "app_control": "controlled apps", "computer_settings": "changed settings",
    "computer_control": "worked the mouse and keyboard", "window_control": "arranged windows",
    "browser_control": "worked in the browser", "web_search": "searched the web",
    "reminder": "set reminders", "routines": "handled routines", "weather_report": "checked the weather",
    "send_message": "sent messages", "file_controller": "handled files", "file_processor": "read files",
    "screen_process": "looked at the screen", "youtube_video": "played videos",
    "video_player": "played videos", "world_time": "told the time abroad", "dev_agent": "wrote code",
    "code_helper": "helped with code", "background_tasks": "ran background tasks",
    "calendar_control": "managed the calendar", "gmail_control": "handled email",
    "pomodoro": "ran focus sessions", "shopping_list": "kept the shopping list",
    "quiz": "ran quizzes", "spaced_repetition": "ran flashcards", "document_review": "reviewed documents",
    "remote_mode": "kept the computer reachable", "system_status": "checked the computer",
}

# Features worth suggesting first, in order, with a phrase the user can say.
_TRY = (
    ("routines", "a morning briefing with weather and calendar", "Give me a morning briefing every day at 8"),
    ("screen_process", "asking about what is on the screen", "What's on my screen?"),
    ("pomodoro", "focus sessions", "Start a 25-minute focus session"),
    ("background_tasks", "background research", "Research the best laptops under 1000 euros in the background"),
    ("calendar_control", "the calendar", "What's on my calendar tomorrow?"),
    ("gmail_control", "email", "Do I have new email?"),
    ("quiz", "quizzes", "Quiz me on Spanish verbs"),
    ("file_processor", "reading files", "Summarise this file (drop one on the window)"),
    ("remote_mode", "Remote Mode", "Keep my computer awake while I'm away"),
    ("world_time", "world time", "What time is it in Tokyo?"),
)


def week_summary(entries: list[dict], now: float | None = None) -> dict:
    """Counts for the last seven days of `entries` (action_history records)."""
    import time as _t
    now = now or _t.time()
    week = [e for e in entries if isinstance(e, dict) and e.get("t", 0) >= now - WEEK]
    by_tool = Counter(e.get("tool", "?") for e in week)
    ok = Counter(e.get("tool", "?") for e in week if e.get("outcome") == "done")
    days = {datetime.fromtimestamp(float(e["t"])).strftime("%A") for e in week if e.get("t")}
    top = [{"tool": t, "what": _LABELS.get(t, t.replace("_", " ")), "times": n, "ok": ok.get(t, 0)}
           for t, n in by_tool.most_common(6)]
    return {"total": len(week), "succeeded": sum(ok.values()), "tools": len(by_tool),
            "active_days": len(days), "top": top}


def untried(available: list[dict] | set[str] | None, used: dict, seed: int = 0) -> dict | None:
    """One feature the user has never used: {tool, what, say}. Curated ones
    first; otherwise any available tool with no recorded use."""
    names = None
    descs: dict[str, str] = {}
    if available is not None:
        names = set()
        for t in available:
            if isinstance(t, dict):
                names.add(t.get("name", ""))
                descs[t.get("name", "")] = str(t.get("brief") or t.get("description") or "")
            else:
                names.add(str(t))

    def never(tool: str) -> bool:
        return int((used.get(tool) or {}).get("n", 0)) == 0

    for tool, what, say in _TRY:
        if (names is None or tool in names) and never(tool):
            return {"tool": tool, "what": what, "say": say}
    if names:
        rest = sorted(n for n in names if n and never(n))
        if rest:
            pick = rest[(seed or zlib.crc32(",".join(rest).encode())) % len(rest)]
            return {"tool": pick, "what": descs.get(pick, "")[:160] or pick.replace("_", " "),
                    "say": ""}
    return None


def instruction(summary: dict, tip: dict | None, lang_clause: str = "") -> str:
    """The [WEEKLY RECAP] turn for the assistant: these facts and nothing else."""
    if summary["total"] == 0:
        head = ("[WEEKLY RECAP] The user did not ask you to do anything with your tools this "
                "week. Say so warmly in one sentence.")
    else:
        lines = [f"- {t['what']}: {t['times']} time(s)" for t in summary["top"]]
        head = ("[WEEKLY RECAP] This is the weekly recap the user set up. Tell them, in two or "
                "three short sentences, what you did for them this week, using ONLY these "
                f"numbers from your own record: {summary['total']} actions on "
                f"{summary['active_days']} day(s), {summary['succeeded']} finished fine.\n"
                + "\n".join(lines))
    tail = ""
    if tip:
        tail = (f"\nThen suggest ONE thing they have not tried yet: {tip['what']}"
                + (f" — for example they can say “{tip['say']}”" if tip.get("say") else "")
                + ". One sentence.")
    return head + tail + " Do not call any tools." + lang_clause


def build(available=None) -> tuple[dict, dict | None]:
    """(summary, tip) from the live record. Never raises."""
    try:
        from actions.action_history import entries_since, tools_used
        entries, used = entries_since(WEEK), tools_used()
    except Exception:
        entries, used = [], {}
    summary = week_summary(entries)
    return summary, untried(available, used, seed=datetime.now().isocalendar()[1])
