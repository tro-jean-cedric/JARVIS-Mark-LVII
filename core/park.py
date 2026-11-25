"""
core/park.py — put the computer to sleep now and have it wake by itself at a
set time ("sleep until 7 and be ready when I get up").

    ok, why = park.supported()
    status, text = park.park(wake_at, on_resume=say)   # after the user confirmed

Asleep, the computer draws a few watts and the assistant is offline; at the
set time the computer's own clock wakes it, and the session reconnects as it
does after any network loss. Hibernate (core/remote_mode.py) is the other
choice: no power at all, but only a person, Wake-on-LAN or a smart plug can
switch it on again.

PER SYSTEM
    Windows  a waitable timer allowed to wake the computer (kernel32
             CreateWaitableTimerW + SetWaitableTimer with resume), then
             SetSuspendState. Windows also needs "Allow wake timers" on in
             the power plan; when it is off, the computer sleeps and the
             timer is ignored — reported after the fact (see RESUME).
    Linux    rtcwake sets the hardware clock's alarm (root: run directly as
             root, or through sudo without a password), then
             `systemctl suspend`.
    macOS    `pmset schedule wake` — only an administrator may schedule a
             wake, so it works when `sudo -n` is allowed, and otherwise the
             answer gives the one Terminal command that sets a daily wake
             time instead. Then `pmset sleepnow`.

RESUME
    A thread watches the wall clock. A jump of more than SLEPT_GAP_S between
    two one-second ticks means the computer was asleep; the time it came back
    is compared with the time asked for:
        "woken"            within WAKE_SLACK_S after it — the timer did it
        "woken-otherwise"  before it — a person, a key, the network
        "late"             well after it — the timer was ignored (wake
                           timers off, lid closed, battery saver)
        "no-sleep"         no gap within NO_SLEEP_S — the system refused
    and on_resume(text) is called with a sentence saying which.

Never raises; every outcome is a (status, sentence) pair.
"""
from __future__ import annotations

import datetime as _dt
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

_OS = platform.system()

MIN_SECONDS = 120               # shorter than this is not worth a sleep
MAX_SECONDS = 7 * 24 * 3600
SLEPT_GAP_S = 20.0
WAKE_SLACK_S = 180.0
NO_SLEEP_S = 90.0

# Tests replace these.
_run = subprocess.run
_now = time.time
_tick = time.sleep


def _log(text: str) -> None:
    print(f"[Park] {text}")


# ── when ─────────────────────────────────────────────────────────────────────

def wake_time(at: str = "", minutes=None, now: Optional[float] = None) -> Optional[float]:
    """The wake time as a timestamp, from minutes from now or a clock time
    ("07:00", "7:30", "2026-11-26 07:00", ISO). A clock time already past
    today means tomorrow. None when it cannot be read."""
    now = _now() if now is None else now
    if minutes not in (None, ""):
        try:
            return now + float(minutes) * 60.0
        except (TypeError, ValueError):
            return None
    text = str(at or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2})[:h.](\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        base = _dt.datetime.fromtimestamp(now)
        t = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t.timestamp() <= now:
            t += _dt.timedelta(days=1)
        return t.timestamp()
    try:
        return _dt.datetime.fromisoformat(text.replace("T", " ")).timestamp()
    except ValueError:
        return None


def _clock(ts: float) -> str:
    t = _dt.datetime.fromtimestamp(ts)
    today = _dt.datetime.fromtimestamp(_now()).date()
    day = "" if t.date() == today else (
        " tomorrow" if t.date() == today + _dt.timedelta(days=1) else t.strftime(" on %a %d %b"))
    return t.strftime("%H:%M") + day


# ── can this computer do it ──────────────────────────────────────────────────

def _sudo_ok(cmd: list[str]) -> bool:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if not shutil.which("sudo"):
        return False
    try:
        r = _run(["sudo", "-n", "-l", *cmd], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _as_root(cmd: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    return ["sudo", "-n", *cmd]


MAC_DAILY_HINT = ("To have it wake every day instead, run this once in Terminal (it asks for "
                  "your password): sudo pmset repeat wakeorpoweron MTWRFSU 07:00:00")


def supported() -> tuple[bool, str]:
    """(True, how) or (False, why not and what to do)."""
    if _OS == "Windows":
        try:
            import ctypes
            ctypes.WinDLL("powrprof")
            return True, "a Windows wake timer"
        except Exception as e:
            return False, f"Windows' sleep functions are not available ({e})"
    if _OS == "Linux":
        if not shutil.which("rtcwake"):
            return False, "rtcwake is not installed (package util-linux)"
        if not shutil.which("systemctl"):
            return False, "systemctl is not available to put it to sleep"
        if not _sudo_ok(["rtcwake", "-m", "no", "-s", "60"]):
            return False, ("setting the wake alarm needs administrator rights: run the assistant "
                           "as a user allowed `sudo rtcwake` without a password")
        return True, "the hardware clock's alarm (rtcwake)"
    if _OS == "Darwin":
        if not _sudo_ok(["pmset", "schedule", "wake", "01/01/30 07:00:00"]):
            return False, ("macOS lets only an administrator schedule a wake. " + MAC_DAILY_HINT)
        return True, "a scheduled wake (pmset)"
    return False, f"not available on {_OS or 'this system'}"


# ── doing it ─────────────────────────────────────────────────────────────────

class _WinTimer:
    """A waitable timer that may wake the computer (Windows only)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k.CreateWaitableTimerW.restype = wintypes.HANDLE
        self._h = self._k.CreateWaitableTimerW(None, True, None)
        if not self._h:
            raise OSError(f"could not create a wake timer (error {ctypes.get_last_error()})")

    def arm(self, seconds: float) -> None:
        import ctypes
        due = ctypes.c_longlong(-int(seconds * 10_000_000))   # relative, 100 ns units
        if not self._k.SetWaitableTimer(self._h, ctypes.byref(due), 0, None, None, True):
            raise OSError(f"could not arm the wake timer (error {ctypes.get_last_error()})")

    def fired(self) -> bool:
        return self._k.WaitForSingleObject(self._h, 0) == 0

    def close(self) -> None:
        try:
            self._k.CancelWaitableTimer(self._h)
            self._k.CloseHandle(self._h)
        except Exception:
            pass


def _win_sleep() -> tuple[bool, str]:
    import ctypes
    try:
        powr = ctypes.WinDLL("powrprof", use_last_error=True)
        # (hibernate=False, force=False, wake events allowed=False -> True keeps timers)
        if powr.SetSuspendState(False, False, False):
            return True, ""
        return False, f"Windows refused to sleep (error {ctypes.get_last_error()})"
    except Exception as e:
        return False, f"Windows refused to sleep ({e})"


def _set_alarm(seconds: float, at: float) -> tuple[bool, str]:
    """Set the wake on Linux / macOS. (ok, why not)."""
    if _OS == "Linux":
        cmd = _as_root(["rtcwake", "-m", "no", "-s", str(int(seconds))])
    else:
        stamp = _dt.datetime.fromtimestamp(at).strftime("%m/%d/%y %H:%M:%S")
        cmd = _as_root(["pmset", "schedule", "wake", stamp])
    try:
        r = _run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return False, f"the wake could not be set ({type(e).__name__})"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:160]
        return False, f"the wake could not be set{': ' + err if err else ''}"
    return True, ""


def _cancel_alarm(at: float) -> None:
    """Take back a wake that was set for a sleep that did not happen."""
    if _OS == "Linux":
        cmd = _as_root(["rtcwake", "-m", "disable"])
    else:
        stamp = _dt.datetime.fromtimestamp(at).strftime("%m/%d/%y %H:%M:%S")
        cmd = _as_root(["pmset", "schedule", "cancel", "wake", stamp])
    try:
        _run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def _posix_sleep() -> tuple[bool, str]:
    cmd = ["systemctl", "suspend"] if _OS == "Linux" else ["pmset", "sleepnow"]
    try:
        r = _run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, f"it would not go to sleep ({type(e).__name__})"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:160]
        return False, f"it would not go to sleep{': ' + err if err else ''}"
    return True, ""


def judge(asked: float, back: Optional[float]) -> tuple[str, str]:
    """What happened, from the time asked for and the time it came back."""
    if back is None:
        return "no-sleep", "The computer did not go to sleep; it stayed on."
    if back < asked - 60:
        return "woken-otherwise", (f"The computer was woken at {_clock(back)}, before the "
                                   f"{_clock(asked)} wake time — by someone or something else.")
    if back <= asked + WAKE_SLACK_S:
        return "woken", f"Awake again at {_clock(back)}, as planned."
    return "late", (f"The computer slept past its wake time and only came back at "
                    f"{_clock(back)}. Its wake timer was ignored — on Windows check 'Allow wake "
                    f"timers' in the power options; a closed laptop lid also stops it.")


def watch_resume(asked: float, started: float) -> Optional[float]:
    """Block until a sleep has been seen and ended; the time it came back,
    or None when no sleep happened within NO_SLEEP_S."""
    last = _now()
    while True:
        _tick(1.0)
        now = _now()
        if now - last > SLEPT_GAP_S:
            return now
        last = now
        if now - started > NO_SLEEP_S:
            return None


def park(wake_at: float, on_resume: Optional[Callable[[str], None]] = None,
         wait: bool = False) -> tuple[str, str]:
    """Set the wake, then sleep. Returns ("asleep", text) once the system
    was asked to sleep (the resume is reported to on_resume from a thread;
    with wait=True this call blocks until then and returns that outcome), or
    ("unsupported" | "failed", why). Only ever call after the user agreed."""
    now = _now()
    seconds = wake_at - now
    if seconds < MIN_SECONDS:
        return "failed", f"The wake time has to be at least {MIN_SECONDS // 60} minutes from now."
    if seconds > MAX_SECONDS:
        return "failed", "The wake time has to be within a week."
    ok, why = supported()
    if not ok:
        return "unsupported", f"A timed wake is not possible on this computer: {why}"

    timer = None
    try:
        if _OS == "Windows":
            timer = _WinTimer()
            timer.arm(seconds)
        else:
            ok, why = _set_alarm(seconds, wake_at)
            if not ok:
                return "failed", f"Not put to sleep: {why}."
    except Exception as e:
        if timer:
            timer.close()
        return "failed", f"Not put to sleep: {e}."

    result: dict = {}
    started = _now()

    def after():
        back = watch_resume(wake_at, started)
        if result.get("cancelled"):
            return
        status, text = judge(wake_at, back)
        if timer is not None:
            if status == "late" and timer.fired():
                status, text = "woken", f"Awake again at {_clock(back)}, woken by its timer."
            timer.close()
        result.update(status=status, text=text)
        _log(f"{status}: {text}")
        if on_resume:
            try:
                on_resume(text)
            except Exception:
                pass

    watcher = threading.Thread(target=after, daemon=True, name="park-resume")
    watcher.start()
    _tick(0.5)            # let the watcher take its first reading before the sleep
    ok, why = _win_sleep() if _OS == "Windows" else _posix_sleep()
    if not ok:
        result["cancelled"] = True
        if timer is not None:
            timer.close()
        else:
            _cancel_alarm(wake_at)
        return "failed", f"Not put to sleep: {why}."
    if wait:
        watcher.join()
        return result.get("status", "no-sleep"), result.get("text", "")
    return "asleep", f"Going to sleep now; it wakes by itself at {_clock(wake_at)}."
