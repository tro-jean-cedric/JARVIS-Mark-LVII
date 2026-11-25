"""
gui/whatsapp_web.py — WhatsApp Web inside the JARVIS window.

The WHATSAPP panel under the head holds a Qt WebEngine view of
web.whatsapp.com with its own profile in config/whatsapp_web_embedded/
(git-ignored: once the QR code is scanned it IS a linked WhatsApp session).

Everything here runs on the Qt thread. plugins/_whatsapp_core.py drives the
page from a worker thread through JarvisUI.whatsapp_web(op, ...), which emits
MainWindow._wa_sig; WhatsAppWeb.handle() runs the step and fills in the answer:

    ensure [show]        create the view if needed (and show the panel)
    show                 show the panel
    js  <fn-source> [arg] run `(fn)(arg)` in the page, return its JSON value
    text <str>           type text into the focused field (an IME commit)
    key <name>           press Enter / Backspace / Escape on the focused field

Locked down compared with a normal browser:
  * the page only ever shows whatsapp.com — any other link opens in the
    system browser, and pop-up windows never open here;
  * microphone and camera are granted to web.whatsapp.com only (calls); every
    other permission request is refused;
  * no remote-debugging port is opened — nothing outside this process can
    reach the session.

Qt WebEngine (pip install PyQt6-WebEngine) is optional: without it the panel
says so and plugins/_whatsapp_core.py falls back to WhatsApp Web in Chrome.
"""
from __future__ import annotations

import importlib.util
import json
import platform

from PyQt6.QtCore import QUrl, Qt, QEvent
from PyQt6.QtGui import QDesktopServices, QInputMethodEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from gui.style import px_font
from gui.theme import C, CONFIG_DIR

WEB_URL = "https://web.whatsapp.com/"
PROFILE_DIR = CONFIG_DIR / "whatsapp_web_embedded"
_HOSTS = ("whatsapp.com", "whatsapp.net")
# WhatsApp Web switches to a one-column layout below roughly this many CSS
# pixels, and the driver needs the chat list and the chat side by side; a
# narrow panel zooms out instead.
_MIN_CSS_WIDTH = 900

_KEYS = {
    "Enter": (Qt.Key.Key_Return, "\r"),
    "Backspace": (Qt.Key.Key_Backspace, "\b"),
    "Escape": (Qt.Key.Key_Escape, "\x1b"),
}


def available() -> bool:
    """Is Qt WebEngine installed? (Checked without importing it.)"""
    try:
        return importlib.util.find_spec("PyQt6.QtWebEngineWidgets") is not None
    except Exception:
        return False


def _allowed(url: QUrl) -> bool:
    if url.scheme() in ("blob", "data", "about"):
        return True
    host = (url.host() or "").lower()
    return url.scheme() == "https" and any(host == h or host.endswith("." + h) for h in _HOSTS)


def _user_agent() -> str:
    """A plain Chrome user agent. WhatsApp Web refuses browsers it does not
    recognise, and Qt's own agent string names QtWebEngine."""
    from PyQt6.QtWebEngineCore import qWebEngineChromiumVersion
    major = (qWebEngineChromiumVersion() or "140").split(".")[0]
    plat = {"Darwin": "Macintosh; Intel Mac OS X 10_15_7",
            "Windows": "Windows NT 10.0; Win64; x64"}.get(platform.system(), "X11; Linux x86_64")
    return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


_CLASSES = None


def _classes():
    """The WebEngine classes, imported on first use so the app starts as fast
    without the panel as before, and runs at all without the package."""
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEnginePermission, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    class _Outbound(QWebEnginePage):
        """A window the page asked to open — a link in a message. It goes to
        the system browser; nothing opens inside the app."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if url.scheme() in ("http", "https"):
                QDesktopServices.openUrl(url)
            self.deleteLater()
            return False

    class _Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if not is_main_frame:
                return url.scheme() in ("https", "blob", "data", "about")
            if _allowed(url):
                return True
            if url.scheme() in ("http", "https") and \
                    nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                QDesktopServices.openUrl(url)
            else:
                print(f"[WhatsApp] blocked a navigation to {url.toString()[:120]}")
            return False

        def createWindow(self, _type):
            return _Outbound(self.profile(), self)

        def javaScriptConsoleMessage(self, *_a):
            pass    # WhatsApp's own console chatter is not ours to print

    _CLASSES = (QWebEnginePage, QWebEnginePermission, QWebEngineProfile, QWebEngineView, _Page)
    return _CLASSES


def _run_js_source(fn_source: str, arg) -> str:
    """`(fn)(arg)` as JSON text, with a thrown error returned instead of lost."""
    return ("(function(){try{return JSON.stringify((" + fn_source + ")(" + json.dumps(arg) +
            "));}catch(e){return JSON.stringify({__error: String(e && e.message || e)});}})()")


class WhatsAppWeb(QWidget):
    """The view inside the WHATSAPP panel, built on first use."""

    def __init__(self, reveal, parent=None):
        super().__init__(parent)
        self._reveal = reveal
        self._view = self._page = self._profile = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setFont(px_font(12))
        self._note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._note.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._lay.addWidget(self._note)
        self._note.setText("WhatsApp Web opens here the first time it is needed.")

    # -- building -------------------------------------------------------------
    def ensure(self) -> str:
        """Build the view if needed. "" when it exists, else why it cannot."""
        if self._view is not None:
            return ""
        if not available():
            self._note.setText("WhatsApp Web runs inside JARVIS once Qt WebEngine is installed:\n"
                               "pip install PyQt6-WebEngine\n\nUntil then it opens in Chrome.")
            return "Qt WebEngine is not installed (pip install PyQt6-WebEngine)"
        try:
            _P, _Perm, Profile, View, Page = _classes()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            # Created in this order on purpose: Qt deletes children in creation
            # order, and the page (inside the view) must go before its profile.
            view = View(self)
            profile = Profile("jarvis_whatsapp", self)
            profile.setPersistentStoragePath(str(PROFILE_DIR))
            profile.setCachePath(str(PROFILE_DIR / "cache"))
            profile.setPersistentCookiesPolicy(Profile.PersistentCookiesPolicy.ForcePersistentCookies)
            profile.setHttpUserAgent(_user_agent())
            page = Page(profile, view)
            page.permissionRequested.connect(self._on_permission)
            view.setPage(page)
        except Exception as e:
            self._note.setText(f"WhatsApp Web could not start here: {e}")
            return f"the embedded browser could not start ({e})"
        self._view, self._page, self._profile = view, page, profile
        self._note.hide()
        self._lay.addWidget(view, stretch=1)
        self._fit()
        view.load(QUrl(WEB_URL))
        return ""

    def _on_permission(self, perm):
        _P, Perm, *_ = _classes()
        kinds = (Perm.PermissionType.MediaAudioCapture, Perm.PermissionType.MediaVideoCapture,
                 Perm.PermissionType.MediaAudioVideoCapture)
        if _allowed(perm.origin()) and perm.permissionType() in kinds:
            perm.grant()
        else:
            print(f"[WhatsApp] refused {perm.permissionType().name} for {perm.origin().toString()}")
            perm.deny()

    def _fit(self):
        if self._view is not None and self.width() > 0:
            self._view.setZoomFactor(max(0.6, min(1.0, self.width() / _MIN_CSS_WIDTH)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    # -- requests from worker threads ----------------------------------------
    def handle(self, req: dict) -> None:
        """Slot for MainWindow._wa_sig. Fills req["box"] and sets req["done"]."""
        box, done = req["box"], req["done"]
        op, args = req.get("op"), tuple(req.get("args") or ())

        def finish(value=None, error: Exception | None = None):
            if done.is_set():
                return
            if error is not None:
                box["error"] = error
            else:
                box["value"] = value
            done.set()

        try:
            if op == "ensure":
                why = self.ensure()
                if args and args[0]:
                    self._reveal()
                finish({"ok": not why, "why": why,
                        "url": self._page.url().toString() if self._page is not None else ""})
                return
            if op == "show":
                self._reveal()
                finish(True)
                return
            if self._view is None:
                finish(error=RuntimeError("WhatsApp Web is not open in the app"))
                return
            if op == "js":
                src = args[0]
                arg = args[1] if len(args) > 1 else None

                def done_js(raw):
                    try:
                        value = json.loads(raw) if isinstance(raw, str) else None
                    except ValueError:
                        value = None
                    if isinstance(value, dict) and "__error" in value:
                        finish(error=RuntimeError(f"page script failed: {value['__error']}"))
                    else:
                        finish(value)
                self._page.runJavaScript(_run_js_source(src, arg), 0, done_js)
                return
            target = self._view.focusProxy() or self._view
            if op == "text":
                self._view.setFocus()
                ev = QInputMethodEvent("", [])
                ev.setCommitString(str(args[0]))
                QApplication.sendEvent(target, ev)
                finish(True)
                return
            if op == "key":
                key, char = _KEYS[str(args[0])]
                self._view.setFocus()
                for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                    QApplication.sendEvent(target, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier, char))
                finish(True)
                return
            finish(error=ValueError(f"unknown WhatsApp panel request: {op!r}"))
        except Exception as e:
            finish(error=e)
