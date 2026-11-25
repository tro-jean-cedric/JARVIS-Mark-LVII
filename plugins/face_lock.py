"""
face_lock — the assistant works only for its owner's face.

enroll : a short webcam scan learns the owner's face (several views).
lock   : lock now; the assistant ignores this computer's microphone until the
         owner's face is seen again (the watcher then unlocks by itself).
unlock : unlock with the PIN from the settings, or by a quick face check.
forget : delete the saved face; nothing locks any more.
status : whether a face is saved and whether it is locked.

The lock state lives in plugins/_face_lock_core.py (is_locked(),
is_owner_present()) so main.py can gate the microphone with a cheap check.
Faces are stored as SFace embeddings in memory/plugin_data/face_lock.json —
never a photo, never committed, never uploaded.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from plugins import _announce
from plugins import _face_lock_core as core

PLUGIN = {
    "name": "face_lock",
    "description": (
        "Face Lock: the assistant works only for its owner, recognised through the "
        "WEBCAM. action='enroll' learns (or re-learns) the user's face with a short "
        "scan - 'set up face lock', 'remember my face'. action='lock' locks the "
        "assistant until the owner's face is seen - 'lock yourself'. action='unlock' "
        "with pin unlocks with the PIN from the settings. action='forget' deletes the "
        "saved face - 'forget my face', 'turn face recognition off'. action='status' "
        "says whether a face is saved and whether it is locked. Do not use the screen "
        "or camera snapshot tools for these."
    ),
    "brief": "Owner face lock: enroll / lock / unlock / forget / status.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "enroll, lock, unlock, forget or status"},
            "pin": {"type": "STRING", "description": "unlock PIN, only for action='unlock'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "face_lock",
    "title": "Face lock",
    "fields": [
        {"key": "owner_name", "label": "Name to greet you with (empty: none)", "type": "text",
         "default": ""},
        {"key": "lock_at_start", "label": "Lock at every start until it sees my face",
         "type": "toggle", "default": False},
        {"key": "unlock_pin", "label": "Unlock PIN (needed to lock at start)",
         "type": "password", "default": ""},
        {"key": "show_camera", "label": "Show the camera on the HUD while scanning or locked",
         "type": "toggle", "default": True},
    ],
}

_enroll_thread: Optional[threading.Thread] = None
_enroll_stop = threading.Event()
_lock = threading.Lock()
_last_prompt = {"text": "", "at": 0.0}


def _setting(key, default=None):
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("face_lock", key, default)
    except Exception:
        return default


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _prompt(text: str) -> None:
    """Coaching during the scan: on the log, and spoken at most every 6 s."""
    now = time.time()
    if text == _last_prompt["text"] and now - _last_prompt["at"] < 6:
        return
    _last_prompt.update(text=text, at=now)
    _announce.log(f"Face lock: {text}")
    if text.startswith("Now turn") or text.startswith("Come"):
        _announce.say(f"Face scan in progress. Tell the user, in a few words: {text}.")


def _greeting() -> str:
    name = str(_setting("owner_name", "") or "").strip()
    return f" Greet them as {name}." if name else ""


# ── The camera on the HUD ────────────────────────────────────────────────────
#
# While a face scan runs or the assistant is locked, the large HUD area shows the
# webcam picture with a box round each face the lock is looking at: green for
# the owner, red for anyone else, blue while scanning. A separate thread draws it
# at ~12 fps from the shared camera stream; the scan and the watcher only look
# every half second, and drawing from their loops would stutter. The pictures go
# to this computer's window and nowhere else, and none is kept.

_VIEW_FPS = 12
_COLOURS = {"owner": (120, 220, 0), "unknown": (60, 60, 255), "scanning": (255, 200, 0)}  # BGR
_viewer: Optional[threading.Thread] = None
_viewer_lock = threading.Lock()


def _viewing() -> bool:
    enrolling = _enroll_thread is not None and _enroll_thread.is_alive()
    return enrolling or core.is_locked()


def _draw(frame, view: dict, caption: str, colour=None):
    """The mirrored picture with face boxes and a status bar. Pure OpenCV."""
    import cv2
    img = cv2.flip(frame, 1)                 # a mirror, the way people expect to see themselves
    h, w = img.shape[:2]
    fresh = time.time() - float(view.get("at") or 0) < 1.5
    for x, y, bw, bh, label in (view.get("faces") or []) if fresh else []:
        col = _COLOURS.get(label, _COLOURS["scanning"])
        x0, y0 = int((1 - x - bw) * w), int(y * h)
        x1, y1 = int((1 - x) * w), int((y + bh) * h)
        cv2.rectangle(img, (x0, y0), (x1, y1), col, 2)
        tag = {"owner": "OWNER", "unknown": "NOT THE OWNER"}.get(label, "")
        if tag:
            cv2.putText(img, tag, (x0, max(14, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, col, 2, cv2.LINE_AA)
    bar = max(28, h // 14)
    cv2.rectangle(img, (0, 0), (w, bar), (10, 6, 0), -1)
    text = "FACE LOCK  |  " + caption
    scale = bar / 48
    # Shrink a long coaching hint to fit; past half size, cut it instead.
    while cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0] > w - 20:
        if scale > bar / 96:
            scale *= 0.92
        elif len(text) > 12:
            text = text[:-4] + "..."
        else:
            break                            # a picture too small for any text
    cv2.putText(img, text, (10, int(bar * 0.68)), cv2.FONT_HERSHEY_SIMPLEX, scale,
                colour or (255, 220, 120), 1, cv2.LINE_AA)
    return img


def _caption() -> str:
    view = core.last_view()
    if time.time() - float(view.get("at") or 0) < 1.5 and view.get("caption"):
        return str(view["caption"])
    if _enroll_thread is not None and _enroll_thread.is_alive():
        return "Face scan - look at the camera"
    return "Locked - look at the camera"


def _view_loop() -> None:
    from plugins import _camera
    p = _announce.player()
    send = getattr(p, "show_hud_frame", None)
    if not callable(send) or not _camera.subscribe("face_lock_view"):
        return
    shown = False
    last = None
    try:
        while _viewing() and _truthy(_setting("show_camera", True)):
            time.sleep(1.0 / _VIEW_FPS)
            frame = _camera.latest(1.0)
            if frame is None or frame is last:
                continue
            last = frame
            send(_camera.encode_jpeg(_draw(frame, core.last_view(), _caption()), 960, 70))
            shown = True
        # One last picture saying how it ended, so the HUD does not just snap back.
        if shown and _truthy(_setting("show_camera", True)) and not core.is_locked():
            end, ok = _ending()
            frame = _camera.latest(1.0)
            if frame is not None:
                send(_camera.encode_jpeg(_draw(
                    frame, core.last_view(), end,
                    _COLOURS["owner"] if ok else _COLOURS["unknown"]), 960, 70))
                time.sleep(1.5)
    except Exception as e:
        print(f"[face_lock] camera view stopped: {e}")
    finally:
        _camera.unsubscribe("face_lock_view")
        if shown:
            stop = getattr(p, "stop_hud_frames", None)
            if callable(stop):
                stop()


# How the last face scan ended: None while none has finished, '' on success,
# else the reason it failed. Set by _enroll_worker.
_scan_result: list = [None]


def _ending() -> tuple[str, bool]:
    """(closing caption, good news?) for the last frame of the view."""
    if _scan_result[0] is not None:
        err = _scan_result[0]
        _scan_result[0] = None
        return ("Face saved", True) if not err else ("Face scan failed", False)
    if time.time() - core.owner_seen_at() < 5:
        return "Owner recognised - unlocked", True
    return "Unlocked", True


def _show_camera() -> None:
    """Start the HUD camera view if it is wanted and not already running."""
    global _viewer
    if not _truthy(_setting("show_camera", True)):
        return
    with _viewer_lock:
        if _viewer is not None and _viewer.is_alive():
            return
        _viewer = threading.Thread(target=_view_loop, name="face-lock-view", daemon=True)
        _viewer.start()


def _on_lock_change(locked: bool) -> None:
    if locked:
        _show_camera()


core.on_change(_on_lock_change)


def _on_unlock() -> None:
    _announce.log("Face lock: owner recognised - unlocked.")
    _announce.say("The owner's face was recognised, so the assistant is unlocked and "
                  "listening again. Welcome them back in a few words." + _greeting())


def _on_stranger() -> None:
    _announce.log("Face lock: access denied - a face that is not the owner's.")


def _enroll_worker() -> None:
    try:
        err = core.enroll(_enroll_stop, _prompt)
    except Exception as e:
        err = str(e)
    _scan_result[0] = err or ""
    if err:
        _announce.say(f"The face scan failed: {err}. Tell the user briefly.")
        return
    lock_at_start = _truthy(_setting("lock_at_start", False))
    pin = str(_setting("unlock_pin", "") or "")
    if lock_at_start and pin:
        extra = "From the next start the assistant will not listen until it sees them."
    elif lock_at_start:
        extra = ("Locking at start is switched on but no unlock PIN is set, so it will not "
                 "lock at start yet - suggest setting a PIN in Face lock's settings.")
    else:
        extra = "It locks only when asked ('lock yourself')."
    _announce.say("The user's face is saved (kept only on this computer). " + extra +
                  " Tell them briefly.")


def on_startup(player=None) -> bool:
    """Called once by main.py after the UI exists. Locks when the user asked
    for that and a PIN is set as the way in when the camera cannot see them.
    Returns True when it locked."""
    try:
        _announce.remember(player)
        if not _truthy(_setting("lock_at_start", False)) or not core.enrolled():
            return False
        if not str(_setting("unlock_pin", "") or ""):
            print("[face_lock] not locking at start: no unlock PIN is set")
            return False
        if core.lock(_on_unlock, _on_stranger):
            return False
        _announce.log("Face lock: locked until the owner's face is seen.")
        return True
    except Exception as e:
        print(f"[face_lock] startup lock failed: {e}")
        return False


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _enroll_thread
    try:
        _announce.remember(player)
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        from plugins import _camera
        if action in ("enroll", "lock") and not _camera.available():
            return "Face lock needs OpenCV, which is not installed (pip install opencv-python)."

        if action == "enroll":
            with _lock:
                if _enroll_thread is not None and _enroll_thread.is_alive():
                    return "A face scan is already running."
                _enroll_stop.clear()
                _enroll_thread = threading.Thread(target=_enroll_worker, name="face-enroll",
                                                  daemon=True)
                _enroll_thread.start()
            _show_camera()
            tip = "" if _setting("unlock_pin") else (
                " Suggest setting an unlock PIN in Face lock's settings, for when the camera "
                "cannot see them.")
            return ("Face scan started: ask the user to look at the camera, then turn their "
                    "head slightly left and right. The first run downloads the face models, "
                    "which can take a moment. I'll report when it is done." + tip)

        if action == "lock":
            if not core.enrolled():
                return "No face is saved yet. Offer to learn the user's face first (action enroll)."
            err = core.lock(_on_unlock, _on_stranger)
            if err:
                return f"Could not lock: {err}."
            return ("Locked: this computer's microphone is ignored until the owner's face is "
                    "seen. Confirm in a few words.")

        if action == "unlock":
            if not core.is_locked():
                return "Face lock is not locked."
            pin = str(_setting("unlock_pin", "") or "")
            given = str(params.get("pin") or "").strip()
            if pin and given and given == pin:
                core.unlock()
                return "Unlocked with the PIN."
            if given:
                return "That PIN is not right; still locked."
            return "Still locked: it unlocks when the owner's face is seen, or with the PIN."

        if action == "forget":
            _enroll_stop.set()
            if core.forget():
                return "The saved face is deleted; nothing locks any more."
            return "No face was saved, so there was nothing to delete."

        if action == "status":
            if not core.enrolled():
                return "No face is saved. Face lock locks nothing until the user's face is learnt."
            state = "locked" if core.is_locked() else "unlocked"
            at_start = _truthy(_setting("lock_at_start", False))
            pin = bool(_setting("unlock_pin", ""))
            extra = (" It locks at every start." if at_start and pin else
                     " Locking at start is on, but needs an unlock PIN first." if at_start else
                     " It locks only when asked.")
            seen = " The owner was seen just now." if core.is_owner_present() else ""
            return f"A face is saved; the assistant is {state}.{extra}{seen}"

        return "Unknown action for face lock; use enroll, lock, unlock, forget or status."
    except Exception as e:
        return f"Face lock failed: {e}"
