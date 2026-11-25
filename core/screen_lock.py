"""
core/screen_lock.py — is the computer's screen locked right now?

    from core.screen_lock import is_locked
    is_locked()   # True, False, or None when this system cannot tell

Used by the Holo Desk, which closes when the user locks the computer — a
full-screen camera window left over the lock screen would show the room to
whoever sits down next. Cheap enough to poll every couple of seconds, and it
never raises.

  macOS    CGSessionCopyCurrentDictionary() through ctypes: the session
           carries "CGSSessionScreenIsLocked" = true only while locked (the key
           is absent otherwise — checked on macOS 26).
  Windows  the lock screen is LogonUI.exe; it runs only while locked.
  Linux    systemd-logind's LockedHint for this session (GNOME, KDE and most
           desktops set it); None where loginctl is missing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

_mac = None


def _mac_api():
    global _mac
    if _mac is None:
        import ctypes
        import ctypes.util
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        _mac = (cg, cf, key)
    return _mac


def _mac_locked() -> Optional[bool]:
    cg, cf, key = _mac_api()
    d = cg.CGSessionCopyCurrentDictionary()
    if not d:
        return None                 # no GUI session (ssh, a daemon)
    try:
        v = cf.CFDictionaryGetValue(d, key)
        return bool(v) and bool(cf.CFBooleanGetValue(v))
    finally:
        cf.CFRelease(d)


def _windows_locked() -> Optional[bool]:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == "logonui.exe":
            return True
    return False


def _linux_locked() -> Optional[bool]:
    sid = os.environ.get("XDG_SESSION_ID", "")
    cmd = ["loginctl", "show-session"] + ([sid] if sid else ["self"]) + ["-p", "LockedHint"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    if "LockedHint=" not in out:
        return None
    return out.strip().endswith("yes")


def is_locked() -> Optional[bool]:
    try:
        if sys.platform == "darwin":
            return _mac_locked()
        if sys.platform == "win32":
            return _windows_locked()
        return _linux_locked()
    except Exception:
        return None


if __name__ == "__main__":
    print("locked:", is_locked())
