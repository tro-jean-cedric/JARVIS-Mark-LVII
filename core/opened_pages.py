"""
core/opened_pages.py — the pages the assistant opened in the user's OWN browser,
so "close the browser" / "close that video" can close exactly those.

youtube_video and browser_control hand addresses to the system browser (`open
<url>`): the user's real browser, profile and tabs. browser_control's own
registry only knows the windows it drives itself, so a request to close what the
assistant had just opened found "No browser specified" / "none" and nothing
closed (observed: a YouTube video opened from Telegram could not be closed).

Quitting the user's browser would take every tab of theirs with it, so this
closes only tabs whose address is one the assistant opened. macOS only for now:
Chrome-family browsers and Safari are scriptable for that; elsewhere it says so.
"""
from __future__ import annotations

import platform
import subprocess
import threading
import time
from urllib.parse import urlsplit

_OS = platform.system()
_lock = threading.Lock()
_pages: list[tuple[float, str]] = []          # (when, url), newest last
_KEEP_S = 6 * 3600

_CHROMIUM = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Chromium", "Vivaldi", "Opera")


def record(url: str) -> None:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return
    now = time.time()
    with _lock:
        _pages[:] = [(t, u) for t, u in _pages if now - t < _KEEP_S and u != url]
        _pages.append((now, url))


def recent(limit: int = 10) -> list[str]:
    now = time.time()
    with _lock:
        return [u for t, u in reversed(_pages) if now - t < _KEEP_S][:limit]


def _key(url: str) -> str:
    """What identifies the page across redirects: host without www + path +
    query (YouTube's ?v=...), no fragment."""
    p = urlsplit(url)
    host = (p.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    return host + p.path.rstrip("/") + (("?" + p.query) if p.query else "")


def _running(app: str) -> bool:
    try:
        r = subprocess.run(["osascript", "-e", f'application "{app}" is running'],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _close_in(app: str, keys: list[str]) -> int:
    """Close the tabs of `app` whose URL contains one of `keys`. Count closed."""
    checks = " or ".join(f'u contains "{k}"' for k in keys)
    if app == "Safari":
        script = f'''
        set n to 0
        tell application "Safari"
          repeat with w in windows
            repeat with t in (tabs of w)
              set u to URL of t
              if u is not missing value and ({checks}) then
                close t
                set n to n + 1
              end if
            end repeat
          end repeat
        end tell
        return n'''
    else:
        script = f'''
        set n to 0
        tell application "{app}"
          repeat with w in windows
            set i to count of tabs of w
            repeat while i > 0
              set u to URL of tab i of w
              if ({checks}) then
                close tab i of w
                set n to n + 1
              end if
              set i to i - 1
            end repeat
          end repeat
        end tell
        return n'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"[OpenedPages] {app}: {(r.stderr or '').strip()[:200]}")
            return 0
        return int((r.stdout or "0").strip() or 0)
    except Exception as e:
        print(f"[OpenedPages] {app}: {e}")
        return 0


def close_opened(limit: int = 10) -> tuple[int, str]:
    """(tabs closed, explanation). Closes only pages the assistant opened."""
    urls = recent(limit)
    if not urls:
        return 0, "I have not opened any page in your browser that I could close"
    if _OS != "Darwin":
        return 0, "closing single tabs of your own browser is only supported on macOS so far"
    keys = sorted({_key(u) for u in urls if _key(u)})
    # Quotes would break the AppleScript string; such URLs are just skipped.
    keys = [k for k in keys if '"' not in k and "\\" not in k]
    total, where = 0, []
    for app in ("Safari",) + _CHROMIUM:
        if _running(app):
            n = _close_in(app, keys)
            if n:
                total += n
                where.append(app)
    if total:
        with _lock:
            _pages.clear()
        return total, f"closed {total} tab(s) I had opened, in {', '.join(where)}"
    return 0, ("none of the pages I opened is still open in a browser I can control "
               "(Safari, Chrome, Edge, Brave, Arc)")
