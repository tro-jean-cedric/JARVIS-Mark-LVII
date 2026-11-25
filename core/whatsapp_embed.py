"""
core/whatsapp_embed.py — where worker threads find the WhatsApp panel inside
the JARVIS window.

Qt-free on purpose: plugins/_whatsapp_core.py imports this from a worker
thread. ui.py's JarvisUI registers itself here when the window exists and Qt
WebEngine is installed; JarvisUI.whatsapp_web() hands every request to the Qt
thread through a signal and waits for the answer (gui/whatsapp_web.py runs it).

    host = whatsapp_embed.host()      # None: no window, or no Qt WebEngine
    host.whatsapp_web("js", "() => document.title", timeout=10)
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_host = None


def register(host) -> None:
    """Called once by JarvisUI after the window is built."""
    global _host
    with _lock:
        _host = host


def host():
    """The JarvisUI that can run WhatsApp Web inside the window, or None."""
    with _lock:
        return _host
