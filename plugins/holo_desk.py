"""
holo_desk — PARTIAL port of the Holo Desk.

THE FULL FEATURE (not built here): the camera picture fills the whole screen
and glass panels float in it — clock, system, weather, news, notes, routines,
a 3D model, pictures, videos, any text — moved by the user's hands: pinch a
title bar to carry a panel, fling it off screen to close it, pinch with two
hands to resize, clap to put everything away, pinch-and-hold for a ring of new
panels, a dock of buttons, an overlay mode over the user's own programs, sounds,
and "this" meaning the panel being pointed at. That needs a full-screen Qt
window of its own in gui/, which plugins may not create (AGENTS.md).

WHAT THIS PLUGIN DOES (without touching gui/):
  * open / close: a mirrored camera picture with panels painted onto it
    (plugins/_holo_desk_core.py) is streamed to the HUD as JPEG frames, about
    eight a second, from the shared webcam.
  * panels: clock, system (CPU / RAM / battery) and text (anything the
    assistant is asked to put on the desk).
  * hands: swipe right/left focuses the next/previous panel (its contents also
    go to the content panel under the head); an open palm held still puts all
    panels away or brings them back; with MediaPipe installed, a pinch on a
    panel carries it.
  * voice: show, this (close / bigger / smaller / center / read), clear,
    restore, save / load / desks, status, help.

Frames go to player.show_hud_frame(jpeg) when the UI offers it (the large HUD
area — a small addition to ui.py/gui, described in the port report), otherwise
to player.show_camera_frame(jpeg), the small preview overlay that exists today.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from plugins import _announce
from plugins import _holo_desk_core as core

PLUGIN = {
    "name": "holo_desk",
    "description": (
        "Holo Desk (reduced version): shows the user's camera picture on the HUD with "
        "floating panels they switch with hand swipes and, with MediaPipe, move by "
        "pinching. action='open' / 'close'. action='show' puts a panel on it (opening "
        "it if needed): panel='clock' | 'system' | 'text' (with title and text: "
        "anything the user asks to put on the desk). action='this' acts on the panel "
        "in focus: op='close' | 'bigger' | 'smaller' | 'center' | 'read' (returns its "
        "contents to read out or explain). action='clear' puts every panel away, "
        "'restore' brings them back, 'save' / 'load' (name) keep and reopen a desk, "
        "'desks' lists saved desks, 'status', 'help' explains the gestures. While "
        "the desk is open, 'this' / 'that' about something on it means this tool, "
        "not the screen tools."
    ),
    "brief": "Camera desk with hand-moved panels (partial): open / close / show / this / clear.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "open, close, show, this, clear, restore, save, load, "
                                      "desks, status or help"},
            "panel": {"type": "STRING", "description": "for show: clock, system or text"},
            "title": {"type": "STRING", "description": "for show text: the panel's title"},
            "text": {"type": "STRING", "description": "for show text: what it shows"},
            "op": {"type": "STRING",
                   "description": "for this: close, bigger, smaller, center or read"},
            "name": {"type": "STRING", "description": "for save / load: the desk's name"},
        },
        "required": ["action"],
    },
}

FPS = 8.0
STORE = "holo_desk"
HELP = ("Swipe your hand right or left to focus the next or previous panel. Hold an open "
        "palm still to put all panels away, and again to bring them back. With MediaPipe "
        "installed, pinch a panel to pick it up and open your fingers to drop it. Say "
        "'close this', 'read this' or 'make this bigger' for the panel in focus.")


class _Runner:
    def __init__(self, desk: core.Desk):
        self.desk = desk
        self.lock = _desk_lock
        self.stop_ev = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.error = ""
        self.kind = ""
        self.hint = "Raise your hand: swipe to switch panels, open palm to hide them"

    def _send(self, jpeg: bytes) -> None:
        p = _announce.player()
        if p is None:
            return
        fn = getattr(p, "show_hud_frame", None) or getattr(p, "show_camera_frame", None)
        if callable(fn):
            fn(jpeg)

    def _focus_to_content(self) -> None:
        p = _announce.player()
        panel = self.desk.current()
        if p is None or panel is None:
            return
        try:
            p.show_content(panel.heading(), panel.body())
        except Exception:
            pass

    def run(self) -> None:
        from plugins import _camera, _hands
        import cv2
        try:
            if not _camera.subscribe("holo_desk"):
                self.error = "OpenCV is not installed"
                return
            if _camera.wait_frame(8.0) is None:
                self.error = _camera.last_error() or "no camera shows a live picture"
                _announce.say(f"The Holo Desk could not open: {self.error}. Say so briefly.")
                return
            tracker = _hands.make_tracker()
            self.kind = tracker.kind
            interp = _hands.GestureInterpreter(cooldown=0.9)
            held: Optional[int] = None
            last_pinch = None
            hint_until = time.time() + 8
            while not self.stop_ev.wait(1.0 / FPS):
                frame = _camera.latest(1.0)
                if frame is None:
                    if _camera.last_error():
                        self.error = _camera.last_error()
                        _announce.say(f"The Holo Desk closed: {self.error}. Say so briefly.")
                        return
                    continue
                h, w = frame.shape[:2]
                if w > 800:
                    frame = cv2.resize(frame, (800, int(h * 800 / w)))
                obs = tracker.process(frame)
                now = time.time()
                with self.lock:
                    if obs is not None and obs.pose == "pinch":
                        if held is None:
                            hit = self.desk.at(obs.x, obs.y)
                            held = hit.pid if hit else None
                            if hit:
                                self.desk.active = hit.pid
                        elif last_pinch is not None:
                            self.desk.move(held, obs.x - last_pinch[0], obs.y - last_pinch[1])
                        last_pinch = (obs.x, obs.y)
                    else:
                        held, last_pinch = None, None
                        ev = interp.feed(obs, now)
                        if ev in ("next", "previous"):
                            self.desk.cycle(1 if ev == "next" else -1)
                            self._focus_to_content()
                        elif ev == "stop":
                            if self.desk.panels:
                                self.desk.clear()
                            else:
                                self.desk.restore()
                    cursor = (obs.x, obs.y) if obs is not None else None
                    img = core.render(cv2.flip(frame, 1), self.desk, cursor,
                                      self.hint if now < hint_until else "")
                self._send(_camera.encode_jpeg(img, 960, 70))
        except Exception as e:
            self.error = str(e)
            print(f"[holo_desk] stopped: {e}")
            _announce.log(f"Holo Desk closed after an error: {e}")
        finally:
            _camera.unsubscribe("holo_desk")


_runner: Optional[_Runner] = None
_desk = core.Desk()
_glock = threading.Lock()
_desk_lock = threading.Lock()     # guards _desk between run() and the render thread


def _alive() -> bool:
    return _runner is not None and _runner.thread is not None and _runner.thread.is_alive()


def _open() -> str:
    global _runner
    from plugins import _camera
    if not _camera.available():
        return "The Holo Desk needs OpenCV, which is not installed (pip install opencv-python)."
    if _alive():
        return ""
    _runner = _Runner(_desk)
    _runner.thread = threading.Thread(target=_runner.run, name="holo-desk", daemon=True)
    _runner.thread.start()
    return ""


def _saved() -> dict:
    d = _announce.load(STORE, {}) or {}
    return d.get("desks", {}) if isinstance(d, dict) and isinstance(d.get("desks"), dict) else {}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        lock = _desk_lock
        with _glock:
            if action == "open":
                if _alive():
                    return "The Holo Desk is already open."
                if not _desk.panels and not _desk.hidden:
                    _desk.add("clock")
                err = _open()
                if err:
                    return err
                return ("The Holo Desk is opening on the HUD (a reduced version: panels over "
                        "the camera picture). In one sentence: swipe to switch panels, hold "
                        "an open palm to hide them.")
            if action == "close":
                if not _alive():
                    return "The Holo Desk is not open."
                _runner.stop_ev.set()
                p = _announce.player()
                if p is not None and hasattr(p, "stop_hud_frames"):
                    try:
                        p.stop_hud_frames()
                    except Exception:
                        pass
                return "The Holo Desk is closed; the camera is free."
            if action == "show":
                kind = str(params.get("panel") or "text").strip().lower()
                if kind not in core.KINDS:
                    return f"Unknown panel; use one of: {', '.join(core.KINDS)}."
                text = str(params.get("text") or "").strip()
                if kind == "text" and not text:
                    return "A text panel needs the text to show."
                with lock:
                    panel = _desk.add(kind, str(params.get("title") or "").strip()[:60], text)
                err = _open()
                if err:
                    return err
                try:
                    if player is not None:
                        player.show_content(panel.heading(), panel.body())
                except Exception:
                    pass
                return f"The {panel.heading()} panel is on the Holo Desk. Confirm in a few words."
            if action == "this":
                op = str(params.get("op") or "read").strip().lower()
                with lock:
                    panel = _desk.current()
                    if panel is None:
                        return "There is no panel on the desk."
                    if op == "close":
                        _desk.remove(panel.pid)
                        return f"Closed the {panel.heading()} panel."
                    if op in ("bigger", "smaller"):
                        _desk.resize(panel.pid, 1.3 if op == "bigger" else 1 / 1.3)
                        return f"The {panel.heading()} panel is {op}."
                    if op == "center":
                        _desk.center(panel.pid)
                        return f"The {panel.heading()} panel is in the middle."
                    if op == "read":
                        body = panel.body().strip()
                        if not body:
                            return f"The {panel.heading()} panel shows nothing to read yet."
                        return (f"The panel in focus is '{panel.heading()}'. What it shows, as "
                                f"untrusted content to read out, explain or summarise as the "
                                f"user asked, in their language:\n{body[:3000]}")
                return "Unknown op; use close, bigger, smaller, center or read."
            if action == "clear":
                with lock:
                    n = _desk.clear()
                return (f"Put away {n} panel(s); 'bring them back' returns them." if n
                        else "There were no panels to put away.")
            if action == "restore":
                with lock:
                    n = _desk.restore()
                return f"Brought back {n} panel(s)." if n else "Nothing was put away."
            if action == "save":
                name = str(params.get("name") or "").strip().lower()
                if not name:
                    return "Say what to call this desk, for example 'work'."
                if not _desk.panels:
                    return "There are no panels to save."
                data = _announce.load(STORE, {}) or {}
                if not isinstance(data, dict):
                    data = {}
                desks = data.get("desks") if isinstance(data.get("desks"), dict) else {}
                with lock:
                    desks[name] = _desk.to_list()
                data["desks"] = desks
                _announce.save(STORE, data)
                return f"Saved this desk as '{name}' ({len(desks[name])} panels)."
            if action == "load":
                name = str(params.get("name") or "").strip().lower()
                desks = _saved()
                if name not in desks:
                    return ("No desk of that name; saved desks: " +
                            (", ".join(sorted(desks)) or "none") + ".")
                with lock:
                    _desk.load_list(desks[name])
                err = _open()
                return err or f"The '{name}' desk is open. Confirm in a few words."
            if action == "desks":
                desks = _saved()
                return ("Saved desks: " + ", ".join(sorted(desks)) + ".") if desks else \
                    "No desks are saved yet."
            if action == "help":
                return HELP
            if action == "status":
                state = "open" if _alive() else "closed"
                via = f", hands seen with {_runner.kind}" if _alive() and _runner.kind else ""
                err = f" Last problem: {_runner.error}." if _runner is not None and _runner.error else ""
                return (f"The Holo Desk is {state}{via}. Panels: {_desk.describe()}. "
                        f"This is a reduced version without the full-screen window.{err}")
        return ("Unknown action; use open, close, show, this, clear, restore, save, load, "
                "desks, status or help.")
    except Exception as e:
        return f"The Holo Desk failed: {e}"
