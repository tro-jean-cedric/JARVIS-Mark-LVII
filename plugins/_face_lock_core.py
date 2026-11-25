"""
plugins/_face_lock_core.py — the owner's face and the lock state.

Helper module (leading underscore: the plugin loader skips it). The face_lock
plugin drives it; other code (main.py's microphone callback, the dashboard)
only READS it:

    from plugins import _face_lock_core as face_lock
    face_lock.is_locked()          # True -> do not act on this computer's mic
    face_lock.is_owner_present()   # owner's face seen in the last few seconds

Both are plain attribute reads: cheap enough for an audio callback.

Faces are matched with OpenCV's SFace embeddings (cv2.FaceRecognizerSF) on
faces found by YuNet (cv2.FaceDetectorYN). Both models come from the official
OpenCV model zoo and are downloaded once into memory/plugin_data/models/.
The embeddings (numbers, not pictures) are kept in
memory/plugin_data/face_lock.json, which is git-ignored and never leaves this
computer.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from plugins import _announce

STORE = "face_lock"
COSINE_MATCH = 0.363          # SFace's published cosine threshold
CONSISTENT = 0.30             # enrollment views must agree at least this much
ENROLL_VIEWS = 5
ENROLL_TIMEOUT = 30.0
PRESENT_WINDOW = 8.0

_state_lock = threading.Lock()
_locked = False
_owner_seen_at = 0.0
_scan_thread: Optional[threading.Thread] = None
_scan_stop = threading.Event()
_listeners: list = []


# ── Public, cheap state checks ───────────────────────────────────────────────

def is_locked() -> bool:
    return _locked


def owner_seen_at() -> float:
    return _owner_seen_at


def is_owner_present(window: float = PRESENT_WINDOW) -> bool:
    """True when the owner's face was recognised in the last `window` seconds
    (by the lock's watcher or any check). Without a running watcher this goes
    False a few seconds after the last sighting."""
    return _owner_seen_at > 0 and time.time() - _owner_seen_at <= window


_view: dict = {"faces": [], "caption": "", "at": 0.0}


def last_view() -> dict:
    """What the scan or the watcher saw last, for drawing on the HUD:
    {"faces": [(x, y, w, h, label)], "caption": str, "at": time}. Boxes are
    fractions of the frame (0..1), so they fit a picture of any size; label is
    "owner", "unknown" or "scanning". Nothing here is a picture."""
    return dict(_view)


def _saw(frame, rows, labels, caption: str = "") -> None:
    global _view
    h, w = frame.shape[:2]
    faces = [(float(r[0]) / w, float(r[1]) / h, float(r[2]) / w, float(r[3]) / h, lab)
             for r, lab in zip(rows, labels)]
    _view = {"faces": faces, "caption": caption, "at": time.time()}


def on_change(fn: Callable[[bool], None]) -> None:
    """Register fn(locked) to hear about lock/unlock (called on a worker thread)."""
    _listeners.append(fn)


def _set_locked(value: bool) -> None:
    global _locked
    with _state_lock:
        changed = _locked != value
        _locked = value
    if changed:
        for fn in list(_listeners):
            try:
                fn(value)
            except Exception as e:
                print(f"[face_lock] listener failed: {e}")


# ── Stored data ──────────────────────────────────────────────────────────────

def load() -> dict:
    d = _announce.load(STORE, {}) or {}
    return d if isinstance(d, dict) else {}


def enrolled() -> bool:
    return bool(load().get("embeddings"))


def forget() -> bool:
    had = enrolled()
    _announce.save(STORE, {})
    stop_scan()
    _set_locked(False)
    return had


# ── Vectors (pure numpy, unit-tested) ────────────────────────────────────────

def cosine(a, b) -> float:
    import numpy as np
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    n = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / n if n else 0.0


def best_match(vec, stored: list) -> float:
    return max((cosine(vec, s) for s in stored), default=0.0)


def views_consistent(vecs: list, min_cos: float = CONSISTENT) -> bool:
    """All enrollment views look like one person (no one walked past)."""
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if cosine(vecs[i], vecs[j]) < min_cos:
                return False
    return True


# ── Models ───────────────────────────────────────────────────────────────────

class Eyes:
    """YuNet + SFace, one instance per thread."""

    def __init__(self):
        import cv2
        from plugins import _faces
        self.cv2 = cv2
        det = _faces.model_path(_faces.YUNET)
        rec = _faces.model_path(_faces.SFACE)
        self.det = cv2.FaceDetectorYN.create(str(det), "", (320, 240), 0.8, 0.3, 10)
        self.rec = cv2.FaceRecognizerSF.create(str(rec), "")
        self._size = None

    def faces(self, frame) -> list:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self.det.setInputSize((w, h))
            self._size = (w, h)
        _, rows = self.det.detect(frame)
        if rows is None:
            return []
        return sorted(list(rows), key=lambda r: -(r[2] * r[3]))

    def embed(self, frame, row):
        crop = self.rec.alignCrop(frame, row)
        return self.rec.feature(crop).ravel()


def _frame(max_side: int = 640):
    from plugins import _camera, _faces
    f = _camera.latest(1.0)
    if f is None:
        return None
    return _faces.shrink(f, max_side)[0]


# ── Enrollment ───────────────────────────────────────────────────────────────

def enroll(stop: threading.Event, prompt: Callable[[str], None] = lambda s: None) -> str:
    """Collect several views of exactly one face and store them.
    Blocking; call from a worker thread. Returns '' on success or the reason."""
    from plugins import _camera
    try:
        eyes = Eyes()
    except Exception as e:
        return f"the face models could not be loaded ({e})"
    if not _camera.subscribe("face_lock_enroll"):
        return "OpenCV is not installed"
    try:
        if _camera.wait_frame(8.0) is None:
            return ("no picture came from the webcam - it may be in use by another "
                    "program or covered")
        vecs = []
        hints = ["", "Now turn your head a little to the left",
                 "Now turn your head a little to the right"]
        tip = ["Look at the camera"]
        coach = prompt

        def prompt(text: str) -> None:          # also shown on the HUD
            tip[0] = text
            coach(text)

        end = time.time() + ENROLL_TIMEOUT
        last = 0.0
        while time.time() < end and len(vecs) < ENROLL_VIEWS and not stop.is_set():
            stop.wait(0.4)
            frame = _frame()
            if frame is None:
                continue
            rows = eyes.faces(frame)
            _saw(frame, rows[:3], ["scanning"] * len(rows[:3]),
                 f"Face scan {len(vecs)}/{ENROLL_VIEWS} - {tip[0]}")
            if len(rows) > 1:
                prompt("Only the owner should be in view")
                continue
            if not rows:
                continue
            r = rows[0]
            if r[2] < frame.shape[1] * 0.12:
                prompt("Come a little closer to the camera")
                continue
            if time.time() - last < 0.8:
                continue
            vecs.append(eyes.embed(frame, r))
            last = time.time()
            idx = len(vecs) * len(hints) // ENROLL_VIEWS
            if idx < len(hints) and hints[idx] and len(vecs) < ENROLL_VIEWS:
                prompt(hints[idx])
        if stop.is_set():
            return "the scan was cancelled"
        if len(vecs) < 3:
            return ("the scan ran out of time without seeing the face clearly - "
                    "good light and looking at the camera help")
        if not views_consistent(vecs):
            return "different faces were seen during the scan; only the owner should be in view"
        data = load()
        data["embeddings"] = [[round(float(x), 6) for x in v] for v in vecs]
        data["enrolled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _announce.save(STORE, data)
        global _owner_seen_at
        _owner_seen_at = time.time()
        return ""
    finally:
        _camera.unsubscribe("face_lock_enroll")


# ── Lock and the watcher that lifts it ───────────────────────────────────────

def lock(on_unlock: Callable[[], None] = lambda: None,
         on_stranger: Callable[[], None] = lambda: None) -> str:
    """Lock now and watch the camera until the owner's face is seen.
    Returns '' when locked, or the reason it did not lock."""
    global _scan_thread
    stored = load().get("embeddings") or []
    if not stored:
        return "no face is saved yet"
    old = _scan_thread
    if old is not None and old.is_alive() and _scan_stop.is_set():
        old.join(2.0)           # a watcher that was just told to stop
    with _state_lock:
        if _scan_thread is not None and _scan_thread.is_alive():
            busy = True
        else:
            busy = False
            _scan_stop.clear()
            _scan_thread = threading.Thread(target=_watch, args=(stored, on_unlock, on_stranger),
                                            name="face-lock-watch", daemon=True)
    _set_locked(True)
    if not busy:
        _scan_thread.start()
    return ""


def unlock() -> None:
    stop_scan()
    _set_locked(False)


# ── The PIN: the way in when the camera cannot see the owner ─────────────────
# Shared by the lock screen (gui/lock_screen.py) and the voice tool, so both
# count the same wrong tries. Five wrong in a row and it waits half a minute.
PIN_TRIES = 5
PIN_WAIT = 30.0
_pin_state = {"wrong": 0, "until": 0.0}


def pin_set() -> bool:
    return bool(_pin())


def _pin() -> str:
    try:
        from memory.config_manager import get_plugin_setting
        return str(get_plugin_setting("face_lock", "unlock_pin", "") or "").strip()
    except Exception:
        return ""


def pin_wait() -> float:
    """Seconds before the next try is allowed (0 = now)."""
    return max(0.0, _pin_state["until"] - time.time())


def try_pin(given: str) -> tuple[bool, str]:
    """(unlocked, what to tell the user). Unlocks on the right PIN."""
    import hmac
    wait = pin_wait()
    if wait > 0:
        return False, f"Too many wrong tries - wait {int(wait) + 1} s."
    pin = _pin()
    if not pin:
        return False, "No unlock PIN is set (Face lock's settings)."
    given = str(given or "").strip()
    if not given:
        return False, "Type the unlock PIN."
    if hmac.compare_digest(given.encode(), pin.encode()):
        _pin_state.update(wrong=0, until=0.0)
        unlock()
        return True, "Unlocked with the PIN."
    _pin_state["wrong"] += 1
    if _pin_state["wrong"] >= PIN_TRIES:
        _pin_state.update(wrong=0, until=time.time() + PIN_WAIT)
        return False, f"Wrong PIN, {PIN_TRIES} times - wait {int(PIN_WAIT)} s."
    left = PIN_TRIES - _pin_state["wrong"]
    return False, f"That PIN is not right ({left} tr{'ies' if left != 1 else 'y'} left)."


def stop_scan() -> None:
    _scan_stop.set()


def _watch(stored: list, on_unlock, on_stranger) -> None:
    global _owner_seen_at
    from plugins import _camera
    try:
        eyes = Eyes()
    except Exception as e:
        _announce.log(f"Face lock: the models could not be loaded ({e}); still locked.")
        return
    if not _camera.subscribe("face_lock"):
        return
    stranger_said = 0.0
    try:
        while _locked and not _scan_stop.wait(0.5):
            frame = _frame()
            if frame is None:
                continue
            rows = eyes.faces(frame)[:3]
            labels = ["owner" if best_match(eyes.embed(frame, r), stored) >= COSINE_MATCH
                      else "unknown" for r in rows]
            owner = "owner" in labels
            _saw(frame, rows, labels,
                 "Owner recognised - unlocked" if owner
                 else "Not the owner - still locked" if rows
                 else "Locked - look at the camera")
            if owner:
                _owner_seen_at = time.time()
                _set_locked(False)
                on_unlock()
                return
            if rows and time.time() - stranger_said > 60:
                stranger_said = time.time()
                _announce.log("Face lock: a face that is not the owner's")
                on_stranger()
    except Exception as e:
        print(f"[face_lock] watcher error: {e}")
        _announce.log(f"Face lock watcher stopped after an error: {e}; still locked.")
    finally:
        _camera.unsubscribe("face_lock")


def check_owner(timeout: float = 4.0) -> bool:
    """One-off look: is the owner in front of the camera right now?
    Blocking for up to `timeout` seconds; call from a worker thread."""
    global _owner_seen_at
    stored = load().get("embeddings") or []
    if not stored:
        return False
    from plugins import _camera
    try:
        eyes = Eyes()
    except Exception:
        return False
    name = f"face_lock_check_{time.time()}"
    if not _camera.subscribe(name):
        return False
    try:
        end = time.time() + timeout
        while time.time() < end:
            frame = _frame()
            if frame is not None:
                for r in eyes.faces(frame)[:3]:
                    if best_match(eyes.embed(frame, r), stored) >= COSINE_MATCH:
                        _owner_seen_at = time.time()
                        return True
            time.sleep(0.3)
        return False
    finally:
        _camera.unsubscribe(name)
