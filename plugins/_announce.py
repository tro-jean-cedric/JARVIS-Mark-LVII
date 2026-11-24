"""
plugins/_announce.py — let a plugin speak later, from a background thread.

Helper module (leading underscore: the plugin loader skips it).

A plugin's run() returns at once, but timers and watchers (pomodoro,
water_reminder, posture_guard...) need to say something minutes later. main.py
gives every tool `player.request_say(instruction)`: a thread-safe channel that
injects an instruction into the live voice session, and the model phrases it in
the user's language. This wraps it so every plugin:

  * remembers the last `player` it was handed (timers outlive run());
  * falls back to the activity log when no voice session is connected;
  * never raises.

Usage:
    from plugins import _announce
    _announce.remember(player)                       # in run()
    _announce.say("The 25-minute focus block is over; suggest a 5-minute break.")
    _announce.log("Pomodoro: break started")
    _announce.data_path("pomodoro")                  # where to keep its JSON
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_player = None
_lock = threading.Lock()


def remember(player) -> None:
    global _player
    if player is not None:
        _player = player


def player():
    return _player


def log(text: str) -> None:
    p = _player
    if p is None:
        print(f"[plugin] {text}")
        return
    try:
        p.write_log(text)
    except Exception:
        print(f"[plugin] {text}")


def say(instruction: str, prefix: str = "[PLUGIN NOTICE]") -> bool:
    """Ask the assistant to say something now. Returns False if it could not
    reach a voice session (the text then goes to the activity log)."""
    p = _player
    fn = getattr(p, "request_say", None) if p is not None else None
    text = f"{prefix}\n{instruction}\nSay this to the user now, briefly, in the language " \
           f"they speak with you. Do not call any tools." if prefix else instruction
    if callable(fn):
        try:
            fn(text)
            return True
        except Exception as e:
            print(f"[plugin] request_say failed: {e}")
    log(instruction)
    return False


# ── Small persistent JSON store shared by plugins ───────────────────────────

def data_dir() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parent.parent
    d = base / "memory" / "plugin_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_path(name: str) -> Path:
    return data_dir() / f"{name}.json"


def load(name: str, default: Any = None) -> Any:
    path = data_path(name)
    with _lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except Exception as e:
            print(f"[plugin] {path.name} unreadable ({e}); starting fresh")
            return default


def save(name: str, value: Any) -> None:
    path = data_path(name)
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
