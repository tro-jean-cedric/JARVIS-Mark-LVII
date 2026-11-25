"""
air_control — Air Control: the user's hand, seen by the webcam, drives the
whole computer (cursor, clicks, drags, scrolling, desktops, typing).

This file is the voice side only. The overlay is a see-through, click-through
window (gui/air_control.py) opened through the thread-safe
`player.air_control(cmd)`; plugins may not build widgets (AGENTS.md). The
gesture logic is plugins/_air_control_core.py, the OS input core/os_input.py,
dictation core/dictation.py. The window keeps _air_control_core.STATE up to
date, and this tool reports only what STATE shows actually happened: the
overlay opening, the click-through being confirmed, the mouse answering.

WHY 'start' CHECKS SO MUCH BEFORE SAYING "done"
    On macOS a process that was never granted Accessibility can post mouse
    events and nothing happens: no error, no cursor movement. A user told "Air
    Control is on" would wave at a cursor that never moves. So start asks
    os_input for the permission state first, then for a self-check that moves
    the cursor by one point and reads it back, and says `done` only when that
    worked and the overlay confirmed it lets clicks through.
"""
from __future__ import annotations

import importlib
import time

from core import outcome
from plugins import _announce
from plugins import _air_control_core as core

PLUGIN = {
    "name": "air_control",
    "description": (
        "Air Control: whole-computer hand control through the webcam - a virtual mouse over "
        "everything on screen. The user's hand moves the real cursor, a pinch clicks, pinch and "
        "move drags, thumb on middle finger right-clicks, two fingers scroll, a fist pauses "
        "the cursor and a fist flick switches desktops, and a held pinch opens a menu "
        "(browser, back, forward, keyboard, close window...). When a text field has "
        "focus a type bar offers dictation and an air keyboard. Actions: start (turn it on), "
        "stop, status, dictate (`op` start|stop|send|undo: type what the user says into the "
        "focused text field), keyboard (show the air keyboard), help (the gestures). Use "
        "start for 'air control' said on its own (and 'open/turn on air control'), 'control my "
        "computer with my hand', 'virtual mouse', 'air mouse'; status only when asked whether it "
        "is on."
    ),
    "brief": "Hand-controlled mouse over the whole computer: start / stop / dictate / keyboard / help.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start (the default: a bare 'air control' means start), stop, "
                                      "status, dictate, keyboard or help"},
            "op": {"type": "STRING",
                   "description": "for dictate: start, stop, send (press Enter) or undo "
                                  "(remove the last phrase)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "air_control",
    "title": "Air Control",
    "fields": [
        {"key": "pinch_sensitivity", "label": "Pinch sensitivity", "type": "choice",
         "options": ["gentle", "normal", "firm"], "default": "normal"},
        {"key": "hand", "label": "Hand that drives the cursor", "type": "choice",
         "options": ["any", "right", "left"], "default": "any"},
        {"key": "pointer_speed", "label": "Pointer speed", "type": "choice",
         "options": ["slow", "normal", "fast"], "default": "normal"},
        {"key": "precision", "label": "Slow down small movements for precision",
         "type": "toggle", "default": True},
        {"key": "scroll_natural", "label": "Natural scrolling (content follows the hand)",
         "type": "toggle", "default": True},
        {"key": "scroll_speed", "label": "Scroll speed", "type": "choice",
         "options": ["0.5", "1", "1.5", "2"], "default": "1"},
        {"key": "space_swipes", "label": "Flick a fist to switch desktops",
         "type": "toggle", "default": True},
        {"key": "type_bar", "label": "Show the type bar in text fields", "type": "toggle",
         "default": True},
        {"key": "keyboard_layout", "label": "Air keyboard layout", "type": "choice",
         "options": ["qwerty", "azerty", "qwertz"], "default": "qwerty"},
        {"key": "learn_words", "label": "Learn the words I type on the air keyboard",
         "type": "toggle", "default": True},
        {"key": "dictation_engine", "label": "Dictation engine", "type": "choice",
         "options": ["auto", "whisper", "gemini"], "default": "auto"},
        {"key": "show_hands", "label": "Show the hand it sees", "type": "toggle",
         "default": True},
        {"key": "sounds", "label": "Sounds", "type": "toggle", "default": True},
    ],
}

OPEN_WAIT = 4.0
INPUT_WAIT = 1.5
CLOSE_WAIT = 3.0
CAMERA_WAIT = 3.0       # for the first live camera picture before saying "on"

PERMISSION_WHY = ("macOS has not allowed this app to control the mouse and keyboard yet. "
                  "Allow it in System Settings > Privacy & Security > Accessibility (the entry "
                  "for Python, Terminal or PyCharm, whichever started JARVIS), then restart "
                  "JARVIS.")

HELP = (
    "Air Control gestures - any open or relaxed hand moves the cursor. Quick pinch (thumb "
    "and index): click; two quick pinches: double click. Pinch and move: drag. Thumb on the "
    "middle finger: right click. Two fingers up (V): scroll. Fist: pauses the cursor so the "
    "hand can be repositioned (like lifting a mouse); a quick fist flick left or right "
    "switches desktop, up shows all windows, down shows the app's windows or the desktop. "
    "Pinch held still about a second: a menu (right click, keyboard, back, forward, browser, "
    "launcher, Mission Control, close window, exit). In a text field a bar offers Dictate "
    "and an air keyboard. To stop: both hands open and still for a second, Esc, or say "
    "'stop air control'. Explain the essentials in two or three sentences, in the user's "
    "language."
)

# Test hooks: modules used instead of core.os_input / core.dictation when set.
_os_input = None
_dictation = None


def _mod(name: str, override):
    return override if override is not None else importlib.import_module(name)


def _wait(cond, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    try:
        return bool(cond())
    except Exception:
        return False


def _ui(player):
    p = player or _announce.player()
    return p if callable(getattr(p, "air_control", None)) else None


def _start(player) -> str:
    ui = _ui(player)
    if ui is None:
        return outcome.failed("air_control", "Air Control needs the JARVIS window on this "
                                             "computer (it cannot start from the phone)")
    try:
        osi = _mod("core.os_input", _os_input)
    except Exception as e:
        return outcome.failed("air_control", f"the mouse control module could not load ({e})")
    ok, why = osi.available()
    if not ok:
        return outcome.failed("air_control", why or "this computer does not allow mouse control")
    perm = osi.permission_state() or {}
    if perm.get("post_events") is False or perm.get("accessibility") is False:
        try:
            osi.request_permission()
        except Exception:
            pass
        return outcome.failed("air_control", PERMISSION_WHY, fallback="the Holo Desk still works")
    try:
        from plugins import _camera
        cam = _camera
        if not _camera.available():
            return outcome.failed("air_control", "it needs OpenCV for the webcam, which is not "
                                                 "installed (pip install opencv-python)")
    except Exception as e:
        return outcome.failed("air_control", f"the camera module could not load ({e})")

    if not core.STATE.get("open"):
        core.STATE["error"] = ""
        ui.air_control("open")
        if not _wait(lambda: core.STATE.get("open"), OPEN_WAIT):
            err = core.STATE.get("error") or "the overlay did not open"
            return outcome.failed("air_control", err)
    input_ok = _wait(lambda: core.STATE.get("input_ok"), INPUT_WAIT)
    try:
        ok, why = osi.self_check()
    except Exception as e:
        ok, why = False, str(e)
    if ok and input_ok:
        # Hands are seen through the camera: "on" is only true once a picture
        # has arrived (a denied permission or a webcam held by another app
        # shows up here, not seconds after the answer).
        cam_why = _camera_problem(cam)
        if cam_why:
            return outcome.unverified(
                "air_control", cam_why, on=True, cursor="verified",
                note="The overlay is up but no hand can be seen until the camera works.")
        return outcome.done(
            "air_control", on=True, cursor="verified",
            note="Tell the user in one or two sentences, in their language: any hand moves, "
                 "pinch clicks, two fingers scroll, a fist pauses and a fist flick switches "
                 "desktops; both open hands, Esc or saying stop ends it.")
    if ok and not input_ok:
        why = core.STATE.get("error") or "the overlay is up but could not be confirmed to let " \
                                         "clicks through, so nothing is sent yet"
    return outcome.unverified("air_control",
                              why or "the overlay is up but the mouse could not be confirmed")


def _camera_problem(cam) -> str:
    """'' once a live camera picture has arrived, else why not (bounded)."""
    try:
        frame = cam.wait_frame(CAMERA_WAIT)
    except Exception as e:
        return f"the camera could not be read ({e})"
    err = str(core.STATE.get("error") or "")
    if frame is None:
        try:
            last = cam.last_error()
        except Exception:
            last = ""
        return err or (f"no picture from the camera: {last}" if last else
                       "no picture from the camera yet (is it in use by another app, or is "
                       "camera access denied in the system settings?)")
    if err:
        return err                      # e.g. the hand models could not load
    return ""


def _stop(player) -> str:
    if not core.STATE.get("open"):
        return outcome.failed("air_control", "Air Control is not on")
    ui = _ui(player)
    if ui is None:
        return outcome.failed("air_control", "the JARVIS window is not reachable from here")
    ui.air_control("close")
    if _wait(lambda: not core.STATE.get("open"), CLOSE_WAIT):
        return outcome.done("air_control", on=False)
    return outcome.unverified("air_control", "asked the overlay to close")


def _status() -> str:
    s = core.STATE
    if not s.get("open"):
        err = s.get("error")
        return "Air Control is off." + (f" Last problem: {err}." if err else "")
    # STATE["since"] is wall-clock time.time(), written by the window.
    mins = max(0, int((time.time() - float(s.get("since") or time.time())) // 60))
    parts = [f"Air Control is on (for {mins} min)"]
    status = s.get("status") or ""
    if status:
        parts.append(f"state: {status}")
    if not s.get("input_ok"):
        parts.append("the mouse is not being driven yet (click-through not confirmed)")
    if s.get("error"):
        parts.append(f"problem: {s['error']}")
    return "; ".join(parts) + "."


def _dictate(op: str) -> str:
    try:
        dm = _mod("core.dictation", _dictation)
    except Exception as e:
        return outcome.failed("air_control", f"dictation could not load ({e})")
    if op == "start":
        return dm.start(source="voice")
    if op == "stop":
        return dm.stop()
    if op == "send":
        return dm.send()
    if op == "undo":
        return dm.undo_last()
    return outcome.failed("air_control", "unsupported op for dictate - valid: start, stop, "
                                         "send, undo")


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        a = parameters or {}
        action = str(a.get("action") or "status").strip().lower()
        if action in ("start", "open", "on"):
            return _start(player)
        if action in ("stop", "close", "off"):
            return _stop(player)
        if action == "status":
            return _status()
        if action == "dictate":
            return _dictate(str(a.get("op") or "start").strip().lower())
        if action == "keyboard":
            if not core.STATE.get("open"):
                return outcome.failed("air_control", "Air Control is not on; start it first")
            ui = _ui(player)
            if ui is None:
                return outcome.failed("air_control", "the JARVIS window is not reachable")
            core.STATE["keyboard"] = False
            ui.air_control("keyboard")
            if _wait(lambda: core.STATE.get("keyboard"), INPUT_WAIT):
                return outcome.done("air_control", keyboard="shown")
            return outcome.unverified("air_control", "asked for the air keyboard but it did "
                                                     "not appear (a password field refuses it)")
        if action == "help":
            return HELP
        return outcome.failed("air_control", "unsupported action - valid: start, stop, status, "
                                             "dictate, keyboard, help")
    except Exception as e:
        return outcome.failed("air_control", f"Air Control failed: {e}")
