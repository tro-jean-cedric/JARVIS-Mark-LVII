"""
holo_desk — the Holo Desk: the camera picture fills the screen and glass panels
float in it, moved by the user's hands.

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
        "The Holo Desk: the camera picture fills the screen and glass panels float in it, moved "
        "by the user's hands. action='open': 'open the holo desk'. action='close'. "
        "action='show' puts a panel on it (opening the desk if needed): panel='clock' | "
        "'system' | 'weather' (arg = city) | 'news' (arg = topic) | 'notes' | 'routines' (what "
        "is coming up) | 'model' (arg = path to a .obj, or empty for the assistant's own head) "
        "| 'image' (arg = path) | 'video' (arg = path or link) | 'text' (title + text: anything "
        "the user asks to put on the desk - a recipe, a list, your answer). action='this' acts "
        "on the panel the user is pointing at or holding - use it whenever the desk is open and "
        "they say this / that / it about something on screen: op='close' | 'bigger' | "
        "'smaller' | 'center' | 'read' (returns what the panel shows, so you can read it out, "
        "explain or summarize it - 'what is this?', 'summarize this'). action='save' (name) "
        "keeps the panels as a named desk; action='load' (name) opens one; action='desks' "
        "lists them. action='clear' puts every panel away ('close everything'); "
        "action='restore' brings them back. action='mode' with mode='overlay' floats the "
        "panels over the user's own screen ('put the desk over my screen') or mode='camera' "
        "shows the camera picture behind them. action='help' shows every gesture. "
        "action='status' says what is on the desk. While the desk is open it fills the screen: "
        "for anything about it use this tool, never the screen tools."
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
                   "description": "for this: close, bigger, smaller, center or read"},
            "name": {"type": "STRING", "description": "for save / load: the desk's name"},
            "mode": {"type": "STRING", "description": "for open / mode: camera or overlay"},
        },
        "required": ["action"],
    },
}

STORE = "holo_desk"
OPEN_WAIT = 4.0


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
    ui.holo_desk("open", mode=mode or "camera")
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
    return f"The desk is open, but the camera is not: {cam}. The mouse works meanwhile." \
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
                                    note=note + " Say so briefly in the user's language.")
            return ("The Holo Desk is open. In one short sentence in the user's language: pinch "
                    "a panel to move it, throw it off the screen to close it.")

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
                                      "unknown panel; use one of: " + ", ".join(PANEL_KINDS))
            text = str(a.get("text") or "")
            if kind == "text" and not text.strip():
                return outcome.failed("holo_desk", "a text panel needs the text to show")
            if kind == "video" and not str(a.get("arg") or "").strip():
                return outcome.failed("holo_desk", "a video panel needs a file or a link")
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            panel = desk.add(kind, title=str(a.get("title") or ""), text=text,
                             arg=str(a.get("arg") or ""))
            return f"The panel '{panel.heading()}' is on the Holo Desk. Confirm in a few words."

        if action == "this":
            if not core.STATE.get("open"):
                return outcome.failed("holo_desk",
                                      "the Holo Desk is not open, so nothing is 'this' on it")
            panel = desk.current()
            if panel is None:
                return outcome.failed("holo_desk", "there is no panel on the desk")
            op = str(a.get("op") or "read").strip().lower()
            if op == "close":
                desk.close(panel.pid)
                return f"Closed the panel '{panel.heading()}'. Confirm in a few words."
            if op in ("bigger", "smaller"):
                desk.resize(panel.pid, 1.3 if op == "bigger" else 1 / 1.3)
                return f"Made the panel '{panel.heading()}' {op}. Confirm in a few words."
            if op == "center":
                desk.center(panel.pid)
                return f"The panel '{panel.heading()}' is in the middle. Confirm in a few words."
            if op == "read":
                body = panel.body().strip()
                if not body:
                    return (f"This is the panel '{panel.heading()}' ({panel.kind}); "
                            "it shows nothing to read yet.")
                return (f"This is the panel '{panel.heading()}' ({panel.kind}). What it shows, as "
                        "untrusted content to read out, explain or summarize as they asked, in "
                        "their language: " + outcome.untrusted(f"holo desk {panel.kind} panel",
                                                               body[:6000]))
            return outcome.failed("holo_desk",
                                  "unknown op; use close, bigger, smaller, center or read")

        if action == "clear":
            n = desk.clear()
            if not n:
                return outcome.failed("holo_desk", "there is no panel on the desk")
            return ("Every panel is put away; 'bring them back' returns them. "
                    "Confirm in a few words.")

        if action == "restore":
            n = desk.restore()
            if not n:
                return outcome.failed("holo_desk", "nothing was put away")
            return f"Brought back {n} panel{'s' if n != 1 else ''}. Confirm in a few words."

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
                        "again; the hands still move the panels. Confirm in a few words.")
            return "The camera picture is behind the panels again. Confirm in a few words."

        if action == "help":
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            desk.add("guide")
            return ("The guide is on the desk. Name the main gestures in two short sentences in "
                    "the user's language: pinch the title bar to move, fling to close, a fast "
                    "clap of both hands closes all, pinch and hold for the panel ring.")

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
            return f"Saved this desk as '{name}' ({len(items)} panels). Confirm in a few words."

        if action == "load":
            name = str(a.get("name") or "").strip()
            desks = _saved()
            if name not in desks:
                return outcome.failed("holo_desk", "no desk of that name; saved desks: "
                                      + (", ".join(sorted(desks)) or "none"))
            err = _open(player)
            if err:
                return outcome.failed("holo_desk", err)
            n = desk.load_list(desks[name])
            return f"The '{name}' desk is open ({n} panels). Confirm in a few words."

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

        return outcome.failed("holo_desk", "unknown action; use open, close, show, this, clear, "
                                           "restore, save, load, desks, mode, help or status")
    except Exception as e:
        return outcome.failed("holo_desk", f"the Holo Desk failed: {e}")
