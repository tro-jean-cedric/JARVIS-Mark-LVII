"""
holo_desk — the Holo Desk: a full-screen webcam view with floating cards that
the user's hands pick up and move.

This file is the voice side only. The desk is drawn by its own full-screen
window (gui/holo_desk.py), opened through the thread-safe
`player.holo_desk(cmd, **kw)`; plugins may not build widgets (AGENTS.md). The
panels live in plugins/_holo_desk_core.shared(), which this tool and the
window both use; what they show comes from plugins/_holo_desk_content.py.

Hands are tracked with OpenCV alone (plugins/_hands_onnx.py — MediaPipe's palm
and hand-landmark models from the OpenCV model zoo, ~8 MB, downloaded the first
time). The gestures are listed in plugins/_holo_desk_core.py and on the desk's
guide panel ('help').
"""
from __future__ import annotations

import time

from core import outcome
from plugins import _announce
from plugins import _holo_desk_core as core

PANEL_KINDS = ("clock", "system", "weather", "news", "notes", "routines", "model", "image",
               "video", "text")

PLUGIN = {
    "name": "holo_desk",
    "description": (
        "Holo Desk: a full-screen view of the webcam with floating info cards the user grabs "
        "and moves with their hands. Actions: open / close. show = add a card, opening the desk "
        "first if it is closed; the card type goes in `panel`: clock; system (CPU, memory, "
        "battery); weather (`arg` = place); news (`arg` = subject); notes (the user's Obsidian "
        "notes); routines (upcoming reminders and routines); model (`arg` = an .obj file, empty "
        "= the assistant's head); image (`arg` = file); video (`arg` = file or web link); text "
        "(`title` + `text`, for anything the user wants pinned up - a recipe, a list, an "
        "answer you gave). this = act on the card the user points at, holds or touched last - "
        "use it for 'this' / 'that' / 'it' while the desk is open; `op` is read, close, "
        "bigger, smaller or center, and read returns the card's content so you can say, "
        "explain or summarize it. save / load (`name`) store and reopen a named layout; desks "
        "lists the stored ones. clear hides every card, restore shows them again. mode: "
        "'overlay' lays the cards over the user's normal screen, 'camera' brings the webcam "
        "back behind them. help pins the gesture guide on the desk; status lists the cards. "
        "Whenever the desk is open, questions about what is on screen belong to this tool, "
        "not to the screen tools."
    ),
    "brief": "Full-screen camera desk with hand-moved panels: open / close / show / this / clear.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "open, close, show, this, clear, restore, save, load, "
                                      "desks, mode, help or status"},
            "panel": {"type": "STRING", "description": "for show: " + ", ".join(PANEL_KINDS)},
            "arg": {"type": "STRING",
                    "description": "for show: the city, news topic, or file path / link"},
            "title": {"type": "STRING", "description": "for show text: the panel's title"},
            "text": {"type": "STRING", "description": "for show text: what it shows"},
            "op": {"type": "STRING",
                   "description": "with action=this: read, close, bigger, smaller or center"},
            "name": {"type": "STRING", "description": "for save / load: the desk's name"},
            "mode": {"type": "STRING", "description": "for open / mode: camera or overlay"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "holo_desk",
    "title": "Holo Desk",
    "fields": [
        {"key": "pinch_sensitivity", "label": "Pinch sensitivity", "type": "choice",
         "options": ["gentle", "normal", "firm"], "default": "normal"},
        {"key": "start_mode", "label": "Open in", "type": "choice",
         "options": ["camera", "overlay"], "default": "camera"},
        {"key": "remember_layout", "label": "Bring back the last layout when it opens",
         "type": "toggle", "default": True},
        {"key": "show_hands", "label": "Show the hands it sees", "type": "toggle",
         "default": True},
        {"key": "sounds", "label": "Sounds", "type": "toggle", "default": True},
    ],
}

STORE = core.STORE
OPEN_WAIT = 4.0


def _setting(key, default=None):
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("holo_desk", key, default)
    except Exception:
        return default


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _wait(cond, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _ui(player):
    p = player or _announce.player()
    return p if callable(getattr(p, "holo_desk", None)) else None


def _open(player, mode: str = "") -> str:
    """'' when the desk is open (seen by the window itself), else why not."""
    ui = _ui(player)
    if ui is None:
        return "the Holo Desk needs the JARVIS window (it cannot open from the phone)"
    if core.STATE.get("open"):
        if mode:
            ui.holo_desk("mode", mode=mode)
        return ""
    from plugins import _camera
    if not _camera.available():
        return "it needs OpenCV, which is not installed (pip install opencv-python)"
    desk = core.shared()
    # An empty desk picks up where the last session left off.
    if not desk.live() and not desk.hidden and _truthy(_setting("remember_layout", True)):
        core.load_last(desk)
    start = str(_setting("start_mode", "camera") or "camera").lower()
    ui.holo_desk("open", mode=mode or (start if start in ("camera", "overlay") else "camera"))
    if not _wait(lambda: core.STATE.get("open"), OPEN_WAIT):
        return "the desk could not be opened" + (
            f" ({core.STATE['camera']})" if core.STATE.get("camera") else "")
    return ""


def _saved() -> dict:
    d = _announce.load(STORE, {}) or {}
    return d.get("desks", {}) if isinstance(d, dict) and isinstance(d.get("desks"), dict) else {}


def _camera_note() -> str:
    """Said once the desk is open but the camera is not."""
    _wait(lambda: core.STATE.get("camera") or time.time() - core.STATE.get("since", 0) > 3, 3.5)
    cam = core.STATE.get("camera")
    return f"Desk opened without a camera picture ({cam}); the mouse still works on it." \
        if cam else ""


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        a = parameters or {}
        action = str(a.get("action") or "status").strip().lower()
        desk = core.shared()

        if action == "open":
            mode = str(a.get("mode") or "").strip().lower()
            err = _open(player, mode if mode in ("camera", "overlay") else "")
            if err:
                return outcome.failed("holo_desk", err)
            note = _camera_note()
            if note:
                return outcome.done("holo_desk", opened=True,
                                    note=note + " Mention it in one sentence, in the user's "
                                                "language.")
            return ("Holo Desk opened. Tell the user in one brief sentence, in their language, "
                    "how to start: grab a card by its top bar with a pinch, and flick it away to "
                    "dismiss it.")

        if action == "close":
            ui = _ui(player)
            if not core.STATE.get("open"):
                return outcome.failed("holo_desk", "the Holo Desk is not open")
            ui.holo_desk("close")
            if _wait(lambda: not core.STATE.get("open"), 3.0):
                return outcome.done("holo_desk", closed=True)
            return outcome.unverified("holo_desk", "the desk was asked to close")

        if action == "show":
            kind = str(a.get("panel") or "text").strip().lower()
            if kind not in PANEL_KINDS:
                return outcome.failed("holo_desk",
                                      "unsupported card type - valid: " + ", ".join(PANEL_KINDS))
            text = str(a.get("text") or "")
            if kind == "text" and not text.strip():
                return outcome.failed("holo_desk", "nothing to show: a text card needs its text")
            if kind == "video" and not str(a.get("arg") or "").strip():
                return outcome.failed("holo_desk", "a video card needs a file path or a web link")
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            panel = desk.add(kind, title=str(a.get("title") or ""), text=text,
                             arg=str(a.get("arg") or ""))
            return f"Added the '{panel.heading()}' card to the Holo Desk. Acknowledge briefly."

        if action == "this":
            if not core.STATE.get("open"):
                return outcome.failed("holo_desk",
                                      "there is no 'this': the Holo Desk is closed")
            panel = desk.current()
            if panel is None:
                return outcome.failed("holo_desk", "there is no panel on the desk")
            op = str(a.get("op") or "read").strip().lower()
            if op == "close":
                desk.close(panel.pid)
                return f"Closed the panel '{panel.heading()}'. Acknowledge briefly."
            if op in ("bigger", "smaller"):
                desk.resize(panel.pid, 1.3 if op == "bigger" else 1 / 1.3)
                return f"Made the panel '{panel.heading()}' {op}. Acknowledge briefly."
            if op == "center":
                desk.center(panel.pid)
                return f"The panel '{panel.heading()}' is in the middle. Acknowledge briefly."
            if op == "read":
                body = panel.body().strip()
                if not body:
                    return (f"This is the panel '{panel.heading()}' ({panel.kind}); "
                            "it is still empty.")
                return (f"This is the card '{panel.heading()}' ({panel.kind}). Its content "
                        "follows - outside data: say it aloud, explain it or sum it up as the user "
                        "asked, in their language: "
                        + outcome.untrusted(f"holo desk {panel.kind} card", body[:6000]))
            return outcome.failed("holo_desk",
                                  "unsupported op - valid: read, close, bigger, smaller, center")

        if action == "clear":
            n = desk.clear()
            if not n:
                return outcome.failed("holo_desk", "there is no panel on the desk")
            return "All cards hidden (the user can ask to restore them). Acknowledge briefly."

        if action == "restore":
            n = desk.restore()
            if not n:
                return outcome.failed("holo_desk", "nothing was put away")
            return f"Brought back {n} panel{'s' if n != 1 else ''}. Acknowledge briefly."

        if action == "mode":
            mode = str(a.get("mode") or "").strip().lower()
            if mode not in ("camera", "overlay"):
                return outcome.failed("holo_desk", "mode is overlay or camera")
            err = _open(player, mode)
            if err:
                return outcome.failed("holo_desk", err)
            if not _wait(lambda: core.STATE.get("mode") == mode, 2.0):
                return outcome.unverified("holo_desk", f"asked for {mode} mode")
            if mode == "overlay":
                return ("The panels float over the user's own screen now and the mouse is theirs "
                        "again; the hands still move the panels. Acknowledge briefly.")
            return "Webcam view restored behind the cards. Acknowledge briefly."

        if action == "help":
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            desk.add("guide")
            return ("Gesture guide added to the desk. In two brief sentences, in the user's "
                    "language, give the essentials: pinch a card's top bar to carry it, flick it "
                    "off-screen to dismiss it, clap to hide everything, and hold a pinch on empty "
                    "space to choose a new card.")

        if action == "save":
            name = str(a.get("name") or "").strip()[:40] or time.strftime("desk %d %b %H:%M")
            items = desk.to_list()
            if not items:
                return outcome.failed("holo_desk", "there are no panels to save")
            data = _announce.load(STORE, {}) or {}
            if not isinstance(data, dict):
                data = {}
            desks = data.get("desks") if isinstance(data.get("desks"), dict) else {}
            desks[name] = items
            data["desks"] = desks
            _announce.save(STORE, data)
            return f"Saved this desk as '{name}' ({len(items)} panels). Acknowledge briefly."

        if action == "load":
            name = str(a.get("name") or "").strip()
            desks = _saved()
            if name not in desks:
                return outcome.failed("holo_desk", "unknown layout name; saved layouts: "
                                      + (", ".join(sorted(desks)) or "none"))
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            n = desk.load_list(desks[name])
            return f"The '{name}' desk is open ({n} panels). Acknowledge briefly."

        if action == "desks":
            desks = _saved()
            return ("Saved desks: " + ", ".join(sorted(desks)) + ".") if desks else \
                "No desks are saved yet."

        if action == "status":
            if not core.STATE.get("open"):
                return "The Holo Desk is closed." + (
                    f" Put away: {len(desk.hidden)} panels." if desk.hidden else "")
            cam = core.STATE.get("camera")
            return (f"The Holo Desk is open in {core.STATE.get('mode', 'camera')} mode with: "
                    f"{desk.describe()}." + (f" The camera is not working: {cam}." if cam else ""))

        return outcome.failed("holo_desk", "unsupported action - valid: status, help, open, "
                                           "show, this, close, clear, restore, save, load, desks, "
                                           "mode")
    except Exception as e:
        return outcome.failed("holo_desk", f"the Holo Desk failed: {e}")
