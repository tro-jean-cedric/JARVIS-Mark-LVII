"""
plugins/_camera.py — one webcam, shared by every plugin that needs it.

Helper module (leading underscore: the plugin loader skips it).

Why this exists: posture_guard, pushup_counter, gesture_control and face_lock
all watch the webcam for minutes at a time, and a camera can usually be opened
by only one VideoCapture at once. If each plugin opened its own, the second one
to start would get no frames. So there is ONE capture thread here; plugins
`subscribe()` to it and read `latest()` frames, and the camera is released when
the last subscriber leaves.

One-off pictures (fridge_chef, calorie_counter, paper_reminders...) use
`snapshot_jpeg()`, which reuses the running stream's frame when there is one and
otherwise opens the camera just for that picture.

Everything runs on worker threads; nothing here touches Qt.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

try:
    import cv2  # type: ignore
    import numpy as np  # noqa: F401  (cv2 frames are numpy arrays)
    _CV2 = True
except Exception:  # pragma: no cover - optional dependency
    cv2 = None
    _CV2 = False

_lock = threading.Lock()
_subscribers: set[str] = set()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_frame = None            # last BGR frame (numpy array)
_frame_at = 0.0
_error = ""


def available() -> bool:
    return _CV2


def last_error() -> str:
    return _error


def _camera_index_and_backend() -> tuple[int, int]:
    """Reuse the camera choice JARVIS already made (screen_processor saves it)."""
    try:
        from actions import screen_processor as sp
        return int(sp._get_camera_index()), int(sp._cv2_backend())
    except Exception:
        return 0, 0


def _open():
    idx, backend = _camera_index_and_backend()
    cap = cv2.VideoCapture(idx, backend) if backend else cv2.VideoCapture(idx)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(idx)
    return cap


def _loop() -> None:
    global _frame, _frame_at, _error
    cap = None
    try:
        cap = _open()
        if not cap.isOpened():
            _error = "the webcam could not be opened"
            return
        _error = ""
        misses = 0
        while not _stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                misses += 1
                if misses > 50:
                    _error = "the webcam stopped sending pictures"
                    return
                time.sleep(0.05)
                continue
            misses = 0
            _frame, _frame_at = frame, time.time()
            time.sleep(0.03)   # ~30 fps is plenty and keeps a core free
    except Exception as e:  # never let the thread die silently
        _error = f"webcam error: {e}"
        print(f"[camera] {_error}")
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        _frame = None


def subscribe(name: str) -> bool:
    """Start (or join) the shared stream. Returns False when there is no cv2."""
    global _thread
    if not _CV2:
        return False
    with _lock:
        _subscribers.add(name)
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_loop, name="shared-camera", daemon=True)
            _thread.start()
    return True


def unsubscribe(name: str) -> None:
    global _thread
    with _lock:
        _subscribers.discard(name)
        if not _subscribers and _thread is not None:
            _stop.set()
            _thread = None


def subscribers() -> set[str]:
    with _lock:
        return set(_subscribers)


def latest(max_age: float = 1.0):
    """The newest BGR frame, or None when the stream has nothing fresh."""
    if _frame is None or time.time() - _frame_at > max_age:
        return None
    return _frame


def wait_frame(timeout: float = 5.0, max_age: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        f = latest(max_age)
        if f is not None:
            return f
        if _error:
            return None
        time.sleep(0.05)
    return None


def encode_jpeg(frame, max_side: int = 1280, quality: int = 85) -> bytes:
    h, w = frame.shape[:2]
    scale = min(1.0, max_side / float(max(h, w)))
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("could not encode the picture")
    return buf.tobytes()


def snapshot_jpeg(max_side: int = 1280) -> bytes:
    """One picture from the webcam as JPEG bytes. Raises RuntimeError on failure."""
    if not _CV2:
        raise RuntimeError("OpenCV is not installed (pip install opencv-python)")
    f = latest(0.5)
    if f is not None:
        return encode_jpeg(f, max_side)
    name = f"snapshot-{time.time()}"
    subscribe(name)
    try:
        time.sleep(0.6)     # let auto-exposure settle
        f = wait_frame(6.0)
        if f is None:
            raise RuntimeError(_error or "the webcam returned no picture")
        return encode_jpeg(f, max_side)
    finally:
        unsubscribe(name)


def image_part(jpeg: bytes, mime: str = "image/jpeg"):
    """A google-genai Part for core.gemini calls (works on every provider rung)."""
    from google.genai import types
    return types.Part.from_bytes(data=jpeg, mime_type=mime)
