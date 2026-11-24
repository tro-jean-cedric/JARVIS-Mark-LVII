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
