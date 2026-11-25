"""
plugins/_hand_stream.py — camera frames -> tracked hands, on one worker thread.

Helper module (leading underscore: the plugin loader skips it). The Holo Desk
and Air Control windows both need the same loop: join the shared webcam
(plugins/_camera.py), load the OpenCV hand tracker (plugins/_hands_onnx.py),
and for every NEW camera frame call back with the hands and the frame's
capture time. It used to live inside gui/holo_desk.py; it moved here so the
two windows share it and it can be tested without Qt.

Why "once per new frame, with the capture time": the gesture filter
(plugins/_gesture_core.py) measures speed and pinch timing per camera frame.
Feeding it the same frame twice, stamped with the screen refresh time, is what
made the old cursor move in steps.

Threading: `on_frame` and `on_status` run on this module's worker thread.
A GUI passes functions that only emit a pyqtSignal. A RuntimeError from either
(the Qt object behind the signal was deleted) ends the loop quietly; any other
error is printed and the loop goes on. Nothing here touches Qt, and start() /
stop() never raise.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable, Optional

TRACK_WIDTH = 640        # the hand models gain nothing from a bigger picture
PICTURE_WIDTH = 1280     # the picture handed back for display

LOADING = "Loading the hand models (the first time downloads ~8 MB)..."


def _shrink(frame, max_w: int):
    """The frame scaled to at most max_w wide (a new array), or itself."""
    try:
        h, w = frame.shape[:2]
    except Exception:
        return frame
    if w <= max_w:
        return frame
    try:
        import cv2  # type: ignore
        return cv2.resize(frame, (max_w, max(1, int(h * max_w / w))))
    except Exception:
        return frame


class HandStream:
    """One camera reader + hand tracker on a daemon thread "hand-stream-<name>"."""

    def __init__(self, name: str, on_frame: Callable[[object, list, float], None], *,
                 max_hands: int = 2, detect_every: float = 1.0,
                 want_picture: Callable[[], bool] = lambda: False,
                 on_status: Callable[[str], None] = lambda s: None,
                 tracker_factory: Optional[Callable[[], object]] = None,
                 camera=None):
        self.name = str(name)
        self.on_frame = on_frame
        self.max_hands = max(1, int(max_hands))
        self.detect_every = float(detect_every)
        self.want_picture = want_picture
        self.on_status = on_status
        self.tracker_factory = tracker_factory
        self.camera = camera
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tracker = None

    # ── public ──────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> None:
        try:
            if self.running:
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name=f"hand-stream-{self.name}", daemon=True)
            self._thread.start()
        except Exception as e:
            print(f"[HandStream] {self.name}: could not start: {e}")

    def stop(self) -> None:
        """Ask the thread to end; it leaves the camera itself. Idempotent."""
        try:
            self._stop.set()
        except Exception:
            pass

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the thread to end (tests, shutdown). True when it has."""
        t = self._thread
        if t is None:
            return True
        try:
            t.join(timeout)
        except Exception:
            pass
        return not t.is_alive()

    def set_max_hands(self, n: int) -> None:
        try:
            self.max_hands = max(1, int(n))
            if self._tracker is not None:
                self._tracker.max_hands = self.max_hands
        except Exception as e:
            print(f"[HandStream] {self.name}: max_hands not changed: {e}")

    # ── the worker ──────────────────────────────────────────────────────────
    def _camera(self):
        if self.camera is not None:
            return self.camera
        from plugins import _camera
        return _camera

    def _status(self, text: str) -> bool:
        """False when the receiver is gone (the loop should end)."""
        try:
            self.on_status(text)
        except RuntimeError:
            return False
        except Exception as e:
            print(f"[HandStream] {self.name}: status callback failed: {e}")
        return True

    def _make_tracker(self):
        if self.tracker_factory is not None:
            return self.tracker_factory()
        from plugins import _hands_onnx
        return _hands_onnx.Tracker(max_hands=self.max_hands, detect_every=self.detect_every)

    @staticmethod
    def _latest(cam) -> tuple:
        fn = getattr(cam, "latest_stamped", None)
        if fn is not None:
            got = fn(1.0)
            if isinstance(got, tuple) and len(got) == 2:
                return got
            return None, 0.0
        frame = cam.latest(1.0)          # an older camera module: stamp it now
        return frame, time.monotonic()

    def _wants_picture(self) -> bool:
        try:
            return bool(self.want_picture())
        except Exception:
            return False

    def _run(self, stop: threading.Event) -> None:
        cam = None
        subscribed = False
        try:
            cam = self._camera()
            if not cam.subscribe(self.name):
                self._status("camera:OpenCV is not installed")
                return
            subscribed = True
            if cam.wait_frame(8.0) is None:
                why = ""
                try:
                    why = cam.last_error() or ""
                except Exception:
                    pass
                if not self._status("camera:" + (why or "no camera shows a live picture")):
                    return
            if stop.is_set():
                return
            if not self._status(LOADING):
                return
            try:
                tracker = self._make_tracker()
                try:
                    tracker.max_hands = self.max_hands
                except Exception:
                    pass
                self._tracker = tracker
                if not self._status(""):
                    return
            except Exception as e:
                self._tracker = None
                if not self._status(f"hands:the hand models could not be loaded ({e})"):
                    return
            last_frame, last_stamp = None, None
            last_err, last_err_at = "", 0.0
            while not stop.wait(1 / 60):
                try:
                    frame, stamp = self._latest(cam)
                except Exception as e:
                    print(f"[HandStream] {self.name}: camera read failed: {e}")
                    continue
                if frame is None or frame is last_frame or (stamp and stamp == last_stamp):
                    continue
                last_frame, last_stamp = frame, stamp
                hands: list = []
                tracker = self._tracker
                if tracker is not None:
                    try:
                        hands = list(tracker.process(_shrink(frame, TRACK_WIDTH)) or [])
                    except Exception as e:
                        # Printed once per new message (or every 5 s), not 30x a second.
                        msg, now = str(e), time.monotonic()
                        if msg != last_err or now - last_err_at > 5.0:
                            print(f"[HandStream] {self.name}: hand tracking error: {msg}")
                            last_err, last_err_at = msg, now
                        hands = []
                img = _shrink(frame, PICTURE_WIDTH) if self._wants_picture() else None
                if stop.is_set():
                    break
                try:
                    self.on_frame(img, hands, float(stamp) if stamp else time.monotonic())
                except RuntimeError:
                    break                # the window went away with a frame in flight
                except Exception as e:
                    print(f"[HandStream] {self.name}: frame callback failed: {e}")
                    traceback.print_exc()
        except Exception as e:
            print(f"[HandStream] {self.name}: stopped: {e}")
        finally:
            if subscribed and cam is not None:
                try:
                    cam.unsubscribe(self.name)
                except Exception:
                    pass
            self._tracker = None
