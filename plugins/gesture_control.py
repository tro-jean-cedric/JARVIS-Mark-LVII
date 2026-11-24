"""
gesture_control — drive the assistant and the music with hand gestures.

  open palm, held still  -> stop talking        (player.on_interrupt)
  swipe right / left     -> next / previous track (computer_settings.media_*)
  two fingers, held      -> play / pause
  pinch, moved up / down -> volume up / down    (computer_settings.volume_*)
  thumbs up / down       -> yes / no to the confirmation on screen (core.confirm)

With MediaPipe installed (optional) every gesture works. Without it an OpenCV
fallback follows the hand by skin colour and recognises swipes and the open
palm only. The watcher runs on its own thread at about 12 frames a second on a
small picture, sharing the one webcam through plugins/_camera.py.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from plugins import _announce

PLUGIN = {
    "name": "gesture_control",
    "description": (
        "Hand-gesture control through the WEBCAM: an open palm stops the assistant "
        "talking, swiping right/left skips to the next/previous track, two fingers "
        "play/pause, a pinch moved up/down changes the volume, thumbs up/down answer "
        "the confirmation on screen. action='start' ('turn on gesture control', 'let "
        "me control you with my hands'), 'stop' ('turn off gesture control'), "
        "'status' (whether it is on and which gestures work)."
    ),
    "brief": "Webcam hand-gesture control: start / stop / status.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, stop or status"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "gesture_control",
    "title": "Gesture control",
    "fields": [
        {"key": "show_preview", "label": "Show my hand on the HUD while it watches",
         "type": "toggle", "default": False},
        {"key": "pinch_volume", "label": "Pinch and move to set the volume",
         "type": "toggle", "default": True},
        {"key": "volume_step", "label": "Volume step per pinch move (%)", "type": "text",
         "default": "5"},
    ],
}

FPS = 12.0
GESTURE_HELP = ("open palm = stop talking, swipe right/left = next/previous track, "
                "two fingers = play/pause, pinch moved up/down = volume, "
                "thumbs up/down = yes/no")
FALLBACK_HELP = ("open palm = stop talking, swipe right/left = next/previous track "
                 "(install mediapipe for the other gestures)")


def _setting(key, default):
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("gesture_control", key, default)
    except Exception:
        return default


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ── Executing a gesture ──────────────────────────────────────────────────────

def execute(event: str, player=None) -> str:
    """Do what a gesture means. Returns a short log line ('' when nothing done).
    Never raises."""
    try:
        if event == "stop":
            fn = getattr(player, "on_interrupt", None) if player is not None else None
            if callable(fn):
                fn()
                return "stop talking"
            return ""
        if event in ("next", "previous", "play_pause"):
            from actions import computer_settings as cs
            {"next": cs.media_next, "previous": cs.media_previous,
             "play_pause": cs.media_play_pause}[event]()
            return {"next": "next track", "previous": "previous track",
                    "play_pause": "play/pause"}[event]
        if event in ("volume_up", "volume_down"):
            if not _truthy(_setting("pinch_volume", True)):
                return ""
            try:
                step = max(1, min(25, int(float(_setting("volume_step", "5") or 5))))
            except (TypeError, ValueError):
                step = 5
            from actions import computer_settings as cs
            (cs.volume_up if event == "volume_up" else cs.volume_down)(step)
            return f"volume {'up' if event == 'volume_up' else 'down'}"
        if event in ("yes", "no"):
            from core import confirm
            if not confirm.pending_title():
                return ""
            confirm.resolve(event == "yes")
            return "confirmed" if event == "yes" else "cancelled"
    except Exception as e:
        print(f"[gesture_control] {event} failed: {e}")
        return f"{event} failed ({e})"
    return ""


# ── Watcher ──────────────────────────────────────────────────────────────────

class _Watcher:
    def __init__(self):
        self.stop_ev = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.kind = ""
        self.state = "starting"
        self.events = 0
        self.error = ""

    def _preview(self, frame, tracker) -> None:
        p = _announce.player()
        if p is None or not hasattr(p, "show_camera_frame"):
            return
        try:
            import cv2
            img = cv2.flip(frame, 1)
            h, w = img.shape[:2]
            pts = getattr(tracker, "last_points", None)
            if pts:
                for x, y in pts:
                    cv2.circle(img, (int((1 - x) * w), int(y * h)), 3, (255, 200, 0), -1)
            from plugins import _camera
            p.show_camera_frame(_camera.encode_jpeg(img, 480, 60))
        except Exception:
            pass

    def run(self) -> None:
        from plugins import _camera, _hands
        try:
            try:
                tracker = _hands.make_tracker()
            except Exception as e:
                self.error = f"the hand tracker could not start ({e})"
                self.state = "failed"
                _announce.say(f"Gesture control could not start: {self.error}. Say so briefly.")
                return
            self.kind = tracker.kind
            if not _camera.subscribe("gesture_control"):
                self.error, self.state = "OpenCV is not installed", "failed"
                return
            if _camera.wait_frame(8.0) is None:
                self.error = _camera.last_error() or "no camera shows a live picture"
                self.state = "failed"
                _announce.say(f"Gesture control could not start: {self.error}. Say so briefly.")
                return
            self.state = "watching"
            interp = _hands.GestureInterpreter()
            preview = _truthy(_setting("show_preview", False))
            n = 0
            while not self.stop_ev.wait(1.0 / FPS):
                frame = _camera.latest(1.0)
                if frame is None:
                    if _camera.last_error():
                        self.error, self.state = _camera.last_error(), "failed"
                        _announce.say(f"Gesture control stopped: {self.error}. Say so briefly.")
                        return
                    continue
                small = frame
                h, w = frame.shape[:2]
                if w > 480:
                    import cv2
                    small = cv2.resize(frame, (480, int(h * 480 / w)))
                obs = tracker.process(small)
                event = interp.feed(obs, time.time())
                if event:
                    done = execute(event, _announce.player())
                    if done:
                        self.events += 1
                        _announce.log(f"Gesture: {done}")
                n += 1
                if preview and n % 3 == 0:
                    self._preview(small, tracker)
        except Exception as e:
            self.error, self.state = str(e), "failed"
            print(f"[gesture_control] watcher crashed: {e}")
            _announce.log(f"Gesture control stopped after an error: {e}")
        finally:
            from plugins import _camera as cam
            cam.unsubscribe("gesture_control")
            if self.state != "failed":
                self.state = "stopped"


_watcher: Optional[_Watcher] = None
_lock = threading.Lock()


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _watcher
    try:
        _announce.remember(player)
        action = str((parameters or {}).get("action") or "status").strip().lower()
        with _lock:
            w = _watcher
            alive = w is not None and w.thread is not None and w.thread.is_alive()
            if action == "start":
                from plugins import _camera
                if not _camera.available():
                    return ("Gesture control needs OpenCV, which is not installed "
                            "(pip install opencv-python).")
                if alive:
                    return "Gesture control is already on."
                _watcher = _Watcher()
                _watcher.thread = threading.Thread(target=_watcher.run, name="gesture-control",
                                                   daemon=True)
                _watcher.thread.start()
                try:
                    import mediapipe  # noqa: F401
                    gestures = GESTURE_HELP
                except Exception:
                    gestures = FALLBACK_HELP
                return ("Gesture control is on. Tell the user briefly which gestures work: "
                        f"{gestures}.")
            if action == "stop":
                if not alive:
                    return "Gesture control was not on."
                w.stop_ev.set()
                return "Gesture control is off; the camera is free."
            if action == "status":
                if not alive:
                    if w is not None and w.error:
                        return f"Gesture control is off; it stopped because {w.error}."
                    return "Gesture control is off."
                gestures = GESTURE_HELP if w.kind == "mediapipe" else FALLBACK_HELP
                return (f"Gesture control is {w.state}, using {w.kind or 'the camera'}; "
                        f"{w.events} gesture(s) so far. Gestures: {gestures}.")
        return "Unknown action for gesture control; use start, stop or status."
    except Exception as e:
        return f"Gesture control failed: {e}"
