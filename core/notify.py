"""
core/notify.py — a desktop notification (the system's own banner) for things
that happen while the user is in another window.

WHY
    A background task can finish, or a reminder fire, while the user is
    working in something else with the sound off or the headphones out. The
    spoken sentence is then lost, and the result panel sits behind the window
    they are using. The operating system's notification is the one thing that
    is seen there, and it stays in the notification centre afterwards.

    When JARVIS's own window is in front, nothing is shown: the panel and the
    voice already say it, and a banner on top would only repeat them.

HOW, PER SYSTEM
    macOS    osascript "display notification". The title and message are
             passed as ARGUMENTS to a fixed script, never written into it, so
             no text - a task result, a reminder the user dictated - can
             become AppleScript.
    Linux    notify-send (libnotify), normal urgency, 15 seconds.
    Windows  win10toast (already in requirements.txt), run in a short-lived
             child process like Torvis does: its toast loop must not run on a
             thread of this app, and a child cannot hang the session.
    Without any of these, it says so and shows nothing.

`show()` reports what it observed: "shown" when the system command said it
worked, "requested" when only the start of a child process could be
checked (Windows), else why not. It never raises.

    python -m core.notify "Title" "Message"     # try it on this computer
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import threading
from typing import Callable, Optional

_TITLE_MAX = 60
_MESSAGE_MAX = 240
TIMEOUT_S = 10.0

# Fixed AppleScript: the text arrives as argv, so it is data, never code.
_APPLESCRIPT = ("on run argv\n"
                "display notification (item 2 of argv) with title (item 1 of argv)\n"
                "end run")
_WIN_CHILD = ("import sys\n"
              "from win10toast import ToastNotifier\n"
              "ToastNotifier().show_toast(sys.argv[1], sys.argv[2], duration=10, threaded=False)\n")


def enabled() -> bool:
    try:
        from memory.config_manager import get_desktop_notifications_enabled
        return get_desktop_notifications_enabled()
    except Exception:
        return True


def _clean(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def method(system: Optional[str] = None) -> tuple[str, str]:
    """(how notifications would be shown here, or '' and why not)."""
    system = system or platform.system()
    if system == "Darwin":
        return ("osascript", "") if shutil.which("osascript") else ("", "osascript is missing")
    if system == "Linux":
        if shutil.which("notify-send"):
            return "notify-send", ""
        return "", "notify-send is not installed (install libnotify-bin or libnotify)"
    if system == "Windows":
        try:
            import importlib.util
            if importlib.util.find_spec("win10toast"):
                return "win10toast", ""
        except Exception:
            pass
        return "", "win10toast is not installed (pip install win10toast)"
    return "", f"no notification method for {system}"


def show(title: str, message: str, *, system: Optional[str] = None,
         run: Callable = subprocess.run, spawn: Callable = subprocess.Popen) -> tuple[str, str]:
    """Show one notification now: ("shown" | "requested" | "failed", detail).
    Blocking for at most TIMEOUT_S; use `away()` from code that must not wait."""
    title, message = _clean(title, _TITLE_MAX) or "JARVIS", _clean(message, _MESSAGE_MAX)
    how, why = method(system)
    if not how:
        return "failed", why
    try:
        if how == "osascript":
            r = run(["osascript", "-e", _APPLESCRIPT, title, message],
                    capture_output=True, text=True, timeout=TIMEOUT_S)
            return ("shown", "macOS notification") if r.returncode == 0 else \
                ("failed", (r.stderr or "").strip()[:160] or f"osascript exited {r.returncode}")
        if how == "notify-send":
            r = run(["notify-send", "--app-name=JARVIS", "--urgency=normal",
                     "--expire-time=15000", title, message],
                    capture_output=True, text=True, timeout=TIMEOUT_S)
            return ("shown", "notify-send") if r.returncode == 0 else \
                ("failed", (r.stderr or "").strip()[:160] or f"notify-send exited {r.returncode}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        spawn([sys.executable, "-c", _WIN_CHILD, title, message],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        return "requested", "Windows toast started (it cannot report back)"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"


def user_is_here() -> bool:
    """Whether JARVIS's own window is the one in front (then no banner)."""
    try:
        from core import screenview
        pid = screenview.foreground_pid()
        return bool(pid) and screenview.is_own_pid(pid)
    except Exception:
        return False


def away(title: str, message: str, *, here: Optional[Callable[[], bool]] = None,
         shower: Optional[Callable] = None) -> bool:
    """Show a notification on a background thread, unless notifications are
    off or the user is looking at JARVIS already. True when one was started.
    Never raises, never blocks."""
    try:
        if not enabled() or (here or user_is_here)():
            return False

        def work():
            status, detail = (shower or show)(title, message)
            if status == "failed":
                print(f"[Notify] not shown: {detail}")

        threading.Thread(target=work, daemon=True, name="desktop-notification").start()
        return True
    except Exception as e:
        print(f"[Notify] {e}")
        return False


if __name__ == "__main__":
    args = sys.argv[1:] or ["JARVIS", "A test notification from core/notify.py."]
    print(show(args[0], args[1] if len(args) > 1 else ""))
