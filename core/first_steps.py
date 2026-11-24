"""core/first_steps.py — the First steps checklist.

A few things to try, grouped by what the assistant is for: controlling the
computer, running the day, seeing and understanding. An item ticks itself off
when the matching tool really succeeded once (actions/action_history.py keeps
a per-tool success counter for exactly this), never because it was clicked.

Shown once on the first run, and any time from the SETUP drawer. Pure logic:
no Qt; the window draws what status() returns.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

AREAS = (
    ("control", "Control the computer"),
    ("day", "Run your day"),
    ("see", "See and understand"),
)

# (area, id, title, example to say, tools whose success ticks it)
STEPS = (
    ("control", "open_app", "Open an app", "Open the calculator", ("open_app", "app_control")),
    ("control", "setting", "Change a setting", "Turn the volume down a bit",
     ("computer_settings", "window_control")),
    ("day", "reminder", "Set a reminder", "Remind me in ten minutes to stretch",
     ("reminder", "routines")),
    ("day", "search", "Ask about today", "What's in the news today?", ("web_search", "world_time")),
    ("see", "screen", "Let it look at your screen", "What's on my screen?", ("screen_process",)),
    ("see", "file", "Hand it a file", "Summarise this file (drop one on the window first)",
     ("file_processor", "document_review")),
)

_lock = threading.Lock()


def _path() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parent.parent
    d = base / "memory" / "plugin_data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "first_steps.json"


def _state() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        print(f"[first steps] not saved: {e}")


def status(used: dict | None = None) -> list[dict]:
    """Every step with done=True when one of its tools has succeeded."""
    if used is None:
        try:
            from actions.action_history import tools_used
            used = tools_used()
        except Exception:
            used = {}
    out = []
    for area, sid, title, say, tools in STEPS:
        done = any(int((used.get(t) or {}).get("ok", 0)) > 0 for t in tools)
        out.append({"area": area, "id": sid, "title": title, "say": say, "done": done})
    return out


def progress(used: dict | None = None) -> tuple[int, int]:
    items = status(used)
    return sum(1 for i in items if i["done"]), len(items)


def should_offer() -> bool:
    """True once: on the first run, unless the user already closed it or
    everything is done."""
    with _lock:
        st = _state()
        if st.get("offered") or st.get("closed"):
            return False
        done, total = progress()
        st["offered"] = True
        _save(st)
        return done < total


def close() -> None:
    with _lock:
        st = _state()
        st["closed"] = True
        _save(st)


def as_text(used: dict | None = None) -> str:
    """The checklist as plain text (for the assistant or a panel)."""
    items = status(used)
    lines = []
    for key, label in AREAS:
        lines.append(label.upper())
        for i in items:
            if i["area"] == key:
                lines.append(f"  {'✓' if i['done'] else '○'} {i['title']} — say: “{i['say']}”")
    done, total = sum(1 for i in items if i["done"]), len(items)
    lines.append(f"{done} of {total} done.")
    return "\n".join(lines)
