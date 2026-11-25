"""gui/air_control.py — Air Control: a see-through overlay over the whole
computer, through which one hand drives the real mouse and keyboard.

Qt main thread only. Built and driven by MainWindow (`_air_sig`), which the
voice tool reaches through `JarvisUI.air_control(cmd, **kw)`; nothing outside
gui/ creates it. The gesture logic lives in plugins/_air_control_core.py
(AirEngine, Qt-free and tested on its own); this file only feeds it, sends
what it decides to the OS, and draws what it shows.

WHY THE WINDOW IS BUILT THE WAY IT IS
  The overlay covers every program the user works in, so it must never take a
  click, the keyboard focus or a place in the app switcher. Qt's flags
  (WindowTransparentForInput, WindowDoesNotAcceptFocus, Tool) ask for that,
  but on macOS they do not survive everything (a Tool window hides when the
  app is deactivated; a window above full-screen apps needs a collection
  behaviour), so `_mac_overlay_native` sets the NSWindow up directly after the
  window is shown. Nothing is sent to the OS until that step has confirmed the
  window is click-through (`input_ok`): an overlay that swallowed the clicks it
  sends would click itself. On Windows the extended styles are set and read
  back; elsewhere Qt's flag is trusted.

  It never calls activateWindow() or raise_(): taking focus would move the
  keyboard away from the field the user is typing into.

WHAT RUNS WHERE
  worker   HandStream (plugins/_hand_stream.py): camera + hand tracking, hands
           arrive through `_hands_sig` once per camera frame
  worker   focus poller (4 Hz): the focused text field, through `_field_sig`
  worker   Esc poller (20 Hz) and the screen-lock poller: `_exit_sig` / `_status_sig`
  worker   core.dictation events, through `_dict_sig`
  worker   the input pump (core/os_input.InputPump) posts the OS events; the
           Qt thread only enqueues on it (Dispatcher.run never blocks)
  Qt       a 60 fps timer: hands -> HandFilter (once per NEW frame) -> AirEngine
           -> Dispatcher -> repaint
"""
from __future__ import annotations

import math
import sys
import threading
import time
from types import SimpleNamespace

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .style import chamfer, hud_font, px_font, with_alpha
from .theme import C

FPS = 60
HINT_S = 10.0                   # the gesture hint shows this long after opening
STALE = 0.4                     # no camera frame for this long: the hands are gone
TOAST_S = 3.0
_IDLE_PAINT = 0.25              # seconds between repaints while nothing is shown

_HINT = ("Relaxed hand moves  ·  pinch clicks  ·  two fingers scroll  ·  open hand pauses  ·  "
         "both open hands exit")
_NO_ESC = "Esc is not available here: say stop air control or show both open hands"
_STATUS_WORD = {"active": "ACTIVE", "no_hand": "NO HAND", "paused_mouse": "MOUSE PAUSE",
                "blocked": "BLOCKED", "exiting": "EXITING"}
_BONES = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10),
          (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18),
          (18, 19), (19, 20), (0, 17))
# Engine events -> gui.holo_desk._Sounds names.
_EVENT_SOUND = {"click": "press", "right_click": "press", "pinch_tick": "tick",
                "swipe": "swipe", "crush": "closed", "menu_open": "ring",
                "drag_start": "grab", "drag_end": "drop", "exit": "restore",
                "ui_press": "press"}


def _platform() -> str:
    """sys.platform, behind a function so the tests can pretend to be on macOS."""
    return sys.platform


# ─────────────────────────────────────────────────────────────────────────────
#  Native window set-up (shared with gui/holo_desk.py's overlay mode)
# ─────────────────────────────────────────────────────────────────────────────
def _ignored_numbers():
    """core.screenview.IGNORED_WINDOW_NUMBERS, or None while it does not exist:
    the window numbers screen reading must look straight through."""
    try:
        from core import screenview
        nums = getattr(screenview, "IGNORED_WINDOW_NUMBERS", None)
        return nums if isinstance(nums, set) else None
    except Exception:
        return None


def _mac_overlay_native(widget) -> str:
    """Make a shown Qt window a true overlay on macOS: mouse events pass
    through, it stays on every Space and over full-screen apps, it is never
    key, never in the app cycle and never in screen captures. Returns '' or
    the reason it could not be done (pyobjc missing, no native window).

    The window number is remembered on the widget (`_ns_window_number`) and
    added to core.screenview.IGNORED_WINDOW_NUMBERS, so JARVIS reading the
    screen does not read its own overlay."""
    # winId() is an NSView* only on the "cocoa" platform plugin. Under any
    # other (offscreen, minimal: tests, headless runs) it is some other handle,
    # and reinterpreting it as an Objective-C object crashes the process.
    try:
        from PyQt6.QtGui import QGuiApplication
        plugin = QGuiApplication.platformName()
    except Exception:
        plugin = ""
    if plugin != "cocoa":
        return f"not a native macOS window (Qt platform '{plugin}')"
    try:
        import objc  # type: ignore
        import AppKit  # type: ignore
    except Exception as e:
        return f"pyobjc is not available ({e})"
    try:
        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        if win is None:
            return "the overlay has no native window yet"
        win.setIgnoresMouseEvents_(True)
        win.setHidesOnDeactivate_(False)
        win.setLevel_(int(getattr(AppKit, "NSStatusWindowLevel", 25)) + 1)
        behaviour = (int(getattr(AppKit, "NSWindowCollectionBehaviorCanJoinAllSpaces", 1 << 0))
                     | int(getattr(AppKit, "NSWindowCollectionBehaviorStationary", 1 << 4))
                     | int(getattr(AppKit, "NSWindowCollectionBehaviorIgnoresCycle", 1 << 6))
                     | int(getattr(AppKit, "NSWindowCollectionBehaviorFullScreenAuxiliary",
                                   1 << 8)))
        win.setCollectionBehavior_(behaviour)
        panel_cls = getattr(AppKit, "NSPanel", None)
        if panel_cls is not None and win.isKindOfClass_(panel_cls):
            nonactivating = int(getattr(AppKit, "NSWindowStyleMaskNonactivatingPanel", 1 << 7))
            win.setStyleMask_(int(win.styleMask()) | nonactivating)
            win.setBecomesKeyOnlyIfNeeded_(True)
        win.setSharingType_(int(getattr(AppKit, "NSWindowSharingNone", 0)))
        if not win.ignoresMouseEvents():
            return "macOS did not make the overlay click-through"
        number = int(win.windowNumber())
        old = getattr(widget, "_ns_window_number", None)
        nums = _ignored_numbers()
        if nums is not None:
            if old is not None and old != number:
                nums.discard(old)
            nums.add(number)
        widget._ns_window_number = number
        return ""
    except Exception as e:
        return f"the overlay could not be set up ({e})"


def _mac_overlay_restore(widget) -> None:
    """Undo `_mac_overlay_native` enough for a normal window again (the Holo
    Desk going back to camera mode needs the mouse), and forget its number."""
    number = getattr(widget, "_ns_window_number", None)
    nums = _ignored_numbers()
    if nums is not None and number is not None:
        nums.discard(number)
    widget._ns_window_number = None
    if _platform() != "darwin":
        return
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":   # winId is no NSView*
            return
        import objc  # type: ignore
        win = objc.objc_object(c_void_p=int(widget.winId())).window()
        if win is not None:
            win.setIgnoresMouseEvents_(False)
            win.setSharingType_(1)                      # NSWindowSharingReadOnly, the default
    except Exception:
        pass


def _forget_window_number(widget) -> None:
    number = getattr(widget, "_ns_window_number", None)
    nums = _ignored_numbers()
    if nums is not None and number is not None:
        nums.discard(number)
    widget._ns_window_number = None


def _win_overlay_native(widget) -> str:
    """Windows: click-through, never activated, not in Alt+Tab, not in screen
    captures. Returns '' when the styles read back as set."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(widget.winId())
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
        # Qt's WindowTransparentForInput already made the window layered and
        # transparent; these bits only add to that (a layered window Qt did
        # not draw as one would be invisible).
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                              | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        try:
            user32.SetWindowDisplayAffinity(hwnd, 0x11)     # WDA_EXCLUDEFROMCAPTURE
        except Exception:
            pass
        back = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (back & WS_EX_TRANSPARENT and back & WS_EX_LAYERED):
            return "Windows did not make the overlay click-through"
        return ""
    except Exception as e:
        return f"the overlay could not be set up ({e})"


# ─────────────────────────────────────────────────────────────────────────────
#  The window
# ─────────────────────────────────────────────────────────────────────────────
class AirControlWindow(QWidget):
    _hands_sig = pyqtSignal(object, float)          # hands (picture coords), capture time
    _field_sig = pyqtSignal(object)                 # FieldInfo | None
    _status_sig = pyqtSignal(str)
    _dict_sig = pyqtSignal(str, object)             # dictation event kind, data
    _feedback_sig = pyqtSignal(str)
    _exit_sig = pyqtSignal(str)

    def __init__(self, on_close=None, log=None, stream_factory=None, pump_factory=None,
                 os_input_mod=None, dictation_mod=None):
        super().__init__(None)
        self.on_close = on_close
        self.log = log or (lambda s: None)
        self._stream_factory = stream_factory
        self._pump_factory = pump_factory
        self._osi = os_input_mod
        self._dictation = dictation_mod
        self.settings = None
        self.engine = None
        self.dispatcher = None
        self.pump = None
        self.stream = None
        self.filter = None
        self.sounds = None
        self.mon: dict = {}
        self.input_ok = False
        self.native_done = False                    # _native_setup has run at least once
        self.native_error = ""
        self.status = "Starting the camera..."
        self.hint = _HINT
        self.hint_until = time.monotonic() + HINT_S
        self.note = ""                              # a standing note (Esc unavailable)
        self._hands: list = []
        self._hands_at = 0.0
        self._filtered_at = 0.0
        self._states: list = []
        self._frame_aspect = 16 / 9
        self._view = None
        self._toast: tuple | None = None            # (text, t0)
        self._effects: list = []                    # (x, y, t0, colour)
        self._painted = 0.0
        self._tick_error = ""
        self._dict_unsub = None
        self._stop = threading.Event()
        self._poll_now = threading.Event()
        self._closing = False
        self._opened = False

        self.setWindowTitle("Air Control")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                            | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.NoDropShadowWindowHint)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground,
                     Qt.WidgetAttribute.WA_NoSystemBackground,
                     Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._hands_sig.connect(self._on_hands)
        self._field_sig.connect(self._on_field)
        self._status_sig.connect(self._on_status)
        self._dict_sig.connect(self._on_dictation)
        self._feedback_sig.connect(self._on_feedback)
        self._exit_sig.connect(self._on_exit)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ── modules (lazy: nothing here is imported until Air Control opens) ─────
    def _os_input(self):
        if self._osi is None:
            from core import os_input
            self._osi = os_input
        return self._osi

    def _dictation_mod(self):
        if self._dictation is None:
            try:
                from core import dictation
                self._dictation = dictation
            except Exception as e:
                print(f"[AirControl] dictation is not available: {e}")
                self._dictation = False
        return self._dictation or None

    # ── opening ──────────────────────────────────────────────────────────────
    def open_on(self, screen) -> None:
        """Cover one monitor and start. Raises only if the engine itself cannot
        be built (MainWindow turns that into a sentence); everything started
        before the failure is stopped first."""
        try:
            self._open_on(screen)
        except Exception:
            self.shutdown()                         # stops, releases, hides, deletes
            raise

    def _open_on(self, screen) -> None:
        from PyQt6.QtGui import QGuiApplication
        from plugins import _air_control_core as air
        osi = self._os_input()
        # The monitor the user is looking at: the one under the real cursor.
        # Only on macOS are os_input's points the same as Qt's logical pixels.
        if _platform() == "darwin":
            try:
                cur = osi.cursor()
                if cur is not None:
                    under = QGuiApplication.screenAt(QPoint(int(cur[0]), int(cur[1])))
                    if under is not None:
                        screen = under
            except Exception:
                pass
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        g = screen.geometry()
        self.setGeometry(g)
        self.show()                                     # never activateWindow / raise_
        self.mon = osi.monitor_for_qt((g.x(), g.y(), g.width(), g.height()),
                                      float(screen.devicePixelRatio()))

        self.settings = air.load_settings()
        self.engine = air.AirEngine(self.settings, screen_aspect=g.width() / max(1, g.height()))
        self.pump = (self._pump_factory or osi.InputPump)()
        self.dispatcher = air.Dispatcher(self.pump, self.mon, os_input_mod=osi,
                                         dictation_mod=self._dictation_mod(),
                                         on_feedback=self._feedback_sig.emit)
        from plugins import _gesture_core as gesture
        self.filter = gesture.HandFilter(self.settings.sensitivity, profile="air", max_hands=2)
        if getattr(self.settings, "sounds", True):
            try:
                from .holo_desk import _Sounds
                self.sounds = _Sounds()
            except Exception:
                self.sounds = None
        factory = self._stream_factory
        if factory is None:
            from plugins._hand_stream import HandStream
            factory = HandStream
        self.stream = factory("air_control", lambda img, hands, t: self._hands_sig.emit(hands, t),
                              max_hands=2, detect_every=1.0, want_picture=lambda: False,
                              on_status=self._status_sig.emit)
        self.stream.start()
        self._start_threads()
        self._opened = True
        air.STATE.update(open=True, since=time.time(), status="active", error="",
                         input_ok=False)
        self._timer.start(int(1000 / FPS))
        QTimer.singleShot(0, self._native_setup)
        try:
            handle = self.windowHandle()
            if handle is not None:
                handle.screenChanged.connect(self._on_screen_changed)
        except Exception:
            pass

    def _native_setup(self) -> None:
        """Confirm the overlay is click-through before anything is sent."""
        if self._closing:
            return
        plat = _platform()
        err = ""
        if plat == "darwin":
            err = _mac_overlay_native(self)
        elif plat == "win32":
            err = _win_overlay_native(self)
        self.native_error = err
        self.input_ok = err == ""
        self.native_done = True
        if self.engine is not None:
            try:
                self.engine.set_input_ok(self.input_ok)
            except Exception as e:
                print(f"[AirControl] set_input_ok failed: {e}")
        self._set_state(open=True, input_ok=self.input_ok, error=err)
        if err:
            self.log(f"Air Control is paused: {err}")
        self.update()

    def _on_screen_changed(self, screen) -> None:
        if self._closing or screen is None:
            return
        try:
            g = screen.geometry()
            self.mon = self._os_input().monitor_for_qt((g.x(), g.y(), g.width(), g.height()),
                                                       float(screen.devicePixelRatio()))
            if self.dispatcher is not None:
                self.dispatcher.mon = self.mon
        except Exception as e:
            print(f"[AirControl] monitor change: {e}")
        self._native_setup()

    # ── worker threads ───────────────────────────────────────────────────────
    def _start_threads(self) -> None:
        if getattr(self.settings, "type_bar", True):
            threading.Thread(target=self._focus_loop, daemon=True, name="air-focus").start()
        threading.Thread(target=self._esc_loop, daemon=True, name="air-esc").start()
        threading.Thread(target=self._lock_loop, daemon=True, name="air-lock").start()
        dictation = self._dictation_mod()
        if dictation is not None:
            try:
                self._dict_unsub = dictation.on_event(lambda k, d: self._safe_emit(
                    self._dict_sig, str(k), d))
            except Exception as e:
                print(f"[AirControl] dictation events: {e}")

    def _safe_emit(self, sig, *args) -> None:
        """Emit from a worker; a deleted window (RuntimeError) is not an error."""
        if self._stop.is_set():
            return
        try:
            sig.emit(*args)
        except RuntimeError:
            pass

    def _field_from(self, tf):
        """os_input.TextField (mouse space) -> FieldInfo (screen units)."""
        from plugins import _air_control_core as air
        osi, mon = self._os_input(), self.mon

        def box(r):
            x, y, w, h = r
            u0, v0 = osi.from_mouse(x, y, mon)
            u1, v1 = osi.from_mouse(x + w, y + h, mon)
            return (u0, v0, u1 - u0, v1 - v0)

        caret = getattr(tf, "caret", None)
        return air.FieldInfo(rect=box(tf.rect), caret=box(caret) if caret else None,
                             secure=bool(getattr(tf, "secure", False)),
                             role=str(getattr(tf, "role", "") or ""),
                             pid=int(getattr(tf, "pid", 0) or 0))

    def _focus_loop(self) -> None:
        """4 Hz, and once more 150 ms after a click: the focused text field."""
        osi = self._os_input()
        while not self._stop.is_set():
            if self._poll_now.wait(0.25):
                self._poll_now.clear()
                if self._stop.wait(0.15):
                    break
            if self._stop.is_set():
                break
            try:
                tf = osi.focused_text_field()
                info = self._field_from(tf) if tf is not None else None
            except Exception as e:
                print(f"[AirControl] focus check: {e}")
                info = None
            self._safe_emit(self._field_sig, info)

    def _esc_loop(self) -> None:
        """20 Hz: Esc held on two polls in a row closes Air Control. Esc must
        first be seen up, so an Esc still held from before does not."""
        osi = self._os_input()
        seen_up, hits = False, 0
        while not self._stop.wait(0.05):
            try:
                down = osi.key_down("esc")
            except Exception:
                down = None
            if down is None:
                self._safe_emit(self._status_sig, "hint:" + _NO_ESC)
                return
            if not down:
                seen_up, hits = True, 0
                continue
            hits = hits + 1 if seen_up else 0
            if hits >= 2:
                self._safe_emit(self._exit_sig, "esc")
                return

    def _lock_loop(self) -> None:
        """Close when the computer locks: nothing may be clicked behind the
        lock screen. Polled off the Qt thread (loginctl on Linux)."""
        try:
            from core.screen_lock import is_locked
        except Exception:
            return
        while not self._stop.wait(2.0):
            try:
                locked = is_locked()
            except Exception:
                locked = False
            if locked:
                self._safe_emit(self._status_sig, "locked")
                return

    # ── slots (Qt thread) ────────────────────────────────────────────────────
    def _on_hands(self, hands, t) -> None:
        self._hands, self._hands_at = list(hands or []), float(t)
        if self._hands and self.engine is not None:
            a = getattr(self._hands[0], "aspect", None)
            if a and abs(float(a) - self._frame_aspect) > 1e-3:
                self._frame_aspect = float(a)
                try:
                    self.engine.set_frame_aspect(self._frame_aspect)
                except Exception as e:
                    print(f"[AirControl] set_frame_aspect: {e}")

    def _on_field(self, field) -> None:
        if self.engine is not None:
            try:
                self.engine.set_field(field, time.monotonic())
            except Exception as e:
                print(f"[AirControl] set_field: {e}")

    def _on_dictation(self, kind, data) -> None:
        if self.engine is not None:
            try:
                self.engine.on_dictation(str(kind), dict(data or {}))
            except Exception as e:
                print(f"[AirControl] on_dictation: {e}")
        if kind == "failed":
            self._on_feedback(f"Dictation: {(data or {}).get('why') or 'it stopped working'}")
        elif kind == "command" and str((data or {}).get("name") or "") == "exit":
            # "stop air control" said while dictating: the microphone is not
            # the conversation's then, so the voice tool never hears it.
            self._on_exit("voice")

    def _on_feedback(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        self._toast = (text, time.monotonic())
        self.log(f"Air Control: {text}")
        self.update()

    def _on_status(self, text: str) -> None:
        if text == "locked":
            print("[AirControl] the computer was locked: closing")
            self.log("Air Control closed: the computer was locked.")
            self._on_exit("locked")
            return
        if text.startswith("hint:"):
            self.note = text[5:]
        elif text.startswith("camera:"):
            self.status = f"No camera: {text[7:]}"
            self._set_state(error=self.status)
        elif text.startswith("hands:"):
            self.status = text[6:]
            self._set_state(error=self.status)
        else:
            self.status = text
            if not text:
                # The camera and models are up again: clear a stale camera /
                # model problem, but keep a click-through failure on show.
                self._set_state(error=self.native_error)
        self.update()

    def _on_exit(self, why: str) -> None:
        if self._closing:
            return
        if self.engine is not None and self.dispatcher is not None:
            try:
                self.dispatcher.run(self.engine.request_exit(why))
            except Exception as e:
                print(f"[AirControl] exit: {e}")
        self.request_close(why)

    def open_keyboard(self) -> None:
        if self.engine is not None:
            try:
                now = time.monotonic()
                self.engine.open_keyboard(now)
                self._set_state(keyboard=self.engine.view(now).keyboard is not None)
            except Exception as e:
                print(f"[AirControl] open_keyboard: {e}")
            self.update()

    @staticmethod
    def _set_state(**kw) -> None:
        """plugins._air_control_core.STATE, which the voice tool reads back."""
        air = sys.modules.get("plugins._air_control_core")
        if air is None:
            try:
                from plugins import _air_control_core as air
            except Exception:
                return
        try:
            air.STATE.update(**kw)
        except Exception:
            pass

    # ── the frame tick ───────────────────────────────────────────────────────
    def _tick(self) -> None:
        if self._closing or self.engine is None:
            return
        try:
            self._step(time.monotonic())
        except Exception as e:                      # never take the app down
            msg = f"{type(e).__name__}: {e}"
            if msg != self._tick_error:
                self._tick_error = msg
                print(f"[AirControl] tick failed: {msg}")
                self.log(f"Air Control hit an error: {msg}")

    def _step(self, now: float) -> None:
        ident = lambda x, y: (x, y)  # noqa: E731
        # The filter runs once per NEW camera frame, timed by its capture.
        if self._hands_at != self._filtered_at:
            self._states = list(self.filter.update(self._hands, self._hands_at, ident))
            self._filtered_at = self._hands_at
        elif self._states and now - self._hands_at > STALE:
            self._states = list(self.filter.update([], now, ident))
        real = None
        sample = self.pump.cursor_sample() if self.pump is not None else None
        if sample:
            try:
                real = tuple(self._os_input().from_mouse(sample[0], sample[1], self.mon))
            except Exception:
                real = None
        actions = self.engine.update(self._states, now, real) or []
        if not self.input_ok:
            # Not confirmed click-through: never send anything but a release.
            actions = [a for a in actions if a and a[0] == "release_all"]
        if actions:
            self.dispatcher.run(actions)
        exit_why = None
        for ev in self.engine.take_events() or []:
            if not ev:
                continue
            kind = ev[0]
            if self.sounds is not None and kind in _EVENT_SOUND:
                try:
                    self.sounds.play(_EVENT_SOUND[kind])
                except Exception:
                    pass
            if kind == "click":
                self._poll_now.set()
                self._effects.append((ev[1], ev[2], now, C.ACC2))
            elif kind == "right_click":
                self._poll_now.set()
                self._effects.append((ev[1], ev[2], now, C.ACC))
            elif kind == "exit":
                exit_why = str(ev[1]) if len(ev) > 1 else "gesture"
        self._effects = [e for e in self._effects if now - e[2] < 0.6]
        self._view = self.engine.view(now)
        status = str(getattr(self._view, "status", "") or "")
        if status:
            self._set_state(status=status)
        self._set_state(keyboard=getattr(self._view, "keyboard", None) is not None)
        if exit_why is not None:
            self.request_close(exit_why)
            return
        busy = (getattr(self._view, "hands_seen", 0) or getattr(self._view, "menu", None)
                or getattr(self._view, "bar", None) or getattr(self._view, "keyboard", None)
                or getattr(self._view, "exit_progress", 0) or getattr(self._view, "toast", None)
                or self._effects or now < self.hint_until
                or (self._toast and now - self._toast[1] < TOAST_S))
        if busy or now - self._painted > _IDLE_PAINT:
            self._painted = now
            self.update()

    # ── closing ──────────────────────────────────────────────────────────────
    def request_close(self, why: str = "") -> None:
        if why:
            self._set_state(status=f"closed: {why}")
            print(f"[AirControl] closing ({why})")
        if callable(self.on_close):
            self.on_close()
        else:
            self.shutdown()

    def _teardown(self) -> None:
        """Stop everything once. Every held button and key is released before
        anyone is told Air Control is closed."""
        if self._closing:
            return
        self._closing = True
        self._stop.set()
        self._poll_now.set()
        try:
            self._timer.stop()
        except Exception:
            pass
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception as e:
                print(f"[AirControl] stream stop: {e}")
        if self.pump is not None:
            try:
                self.pump.close()
            except Exception as e:
                print(f"[AirControl] input pump close: {e}")
        dictation = self._dictation or None
        if dictation is not None:
            try:
                if dictation.active():
                    dictation.stop(commit=False)
            except Exception as e:
                print(f"[AirControl] dictation stop: {e}")
        if callable(self._dict_unsub):
            try:
                self._dict_unsub()
            except Exception:
                pass
            self._dict_unsub = None
        _forget_window_number(self)
        self._set_state(open=False, input_ok=False, keyboard=False)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._teardown()
        self.close()
        self.deleteLater()

    def closeEvent(self, e):
        if not self._closing:
            self._teardown()
            if callable(self.on_close):
                self.on_close(already_closed=True)
        super().closeEvent(e)

    # ─────────────────────────────────────────────────────────────────────────
    #  Painting
    # ─────────────────────────────────────────────────────────────────────────
    def paintEvent(self, _e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            now = time.monotonic()
            view = self._view
            if view is None and self.engine is not None:
                try:
                    view = self.engine.view(now)
                except Exception:
                    view = None
            if view is None:
                view = SimpleNamespace()
            self._paint(p, view, now)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self._tick_error:
                self._tick_error = msg
                print(f"[AirControl] paint failed: {msg}")
        finally:
            p.end()

    def _paint(self, p: QPainter, view, now: float) -> None:
        g = lambda name, d=None: getattr(view, name, d)  # noqa: E731
        kb = g("keyboard")
        if g("bar"):
            self._paint_bar(p, g("bar"))
        if kb:
            self._paint_keyboard(p, kb, g("cursor"))
        if g("menu"):
            self._paint_menu(p, g("menu"))
        self._paint_effects(p, now)
        self._paint_cursor(p, view)
        if g("exit_progress", 0.0):
            self._paint_exit(p, float(g("exit_progress", 0.0)))
        self._paint_status(p, view, now)
        if self.settings is None or getattr(self.settings, "show_hands", True):
            self._paint_hand(p, g("hand_pts"))
        if not self.input_ok and self.native_done:
            self._paint_banner(p)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _pt(self, u: float, v: float) -> QPointF:
        return QPointF(float(u) * self.width(), float(v) * self.height())

    def _box(self, x, y, w, h) -> QRectF:
        W, H = self.width(), self.height()
        return QRectF(float(x) * W, float(y) * H, float(w) * W, float(h) * H)

    def _label(self, p: QPainter, x: float, y: float, text: str, col=None) -> None:
        p.setFont(hud_font(9, QFont.Weight.Bold))
        fm = QFontMetricsF(p.font())
        w = fm.horizontalAdvance(text) + 14
        rect = QRectF(x - w / 2, y - 11, w, 22)
        p.setPen(QPen(QColor(col or C.PRI), 1))
        p.setBrush(with_alpha(C.BG, 0.88))
        p.drawRect(rect)
        p.setPen(QColor(C.TEXT))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _glass(p: QPainter, shape, edge, fill_a: float = 0.82, edge_a: float = 0.9) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(C.PANEL2, fill_a))
        p.drawPolygon(shape)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for width, a in ((5, 0.10 * edge_a), (1.3, edge_a)):
            p.setPen(QPen(with_alpha(edge, a), width))
            p.drawPolygon(shape)

    def _arc(self, p: QPainter, c: QPointF, rad: float, frac: float, col, width: float = 3.0,
             track: bool = True) -> None:
        rect = QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if track:
            p.setPen(QPen(with_alpha(col, 0.22), width))
            p.drawEllipse(rect)
        p.setPen(QPen(QColor(col), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, int(-360 * 16 * max(0.0, min(1.0, frac))))

    # ── the cursor ring (the OS draws the real arrow) ────────────────────────
    def _paint_cursor(self, p: QPainter, view) -> None:
        cur = getattr(view, "cursor", None)
        hand = getattr(view, "hand", None)
        if hand and (not cur or math.hypot((hand[0] - cur[0]) * self.width(),
                                           (hand[1] - cur[1]) * self.height()) > 6):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(C.PRI, 0.30))
            p.drawEllipse(self._pt(*hand[:2]), 5, 5)
        if not cur:
            return
        c = self._pt(*cur[:2])
        ring = str(getattr(view, "ring", "track") or "track")
        pinch = max(0.0, min(1.0, float(getattr(view, "pinch", 0.0) or 0.0)))
        progress = max(0.0, min(1.0, float(getattr(view, "progress", 0.0) or 0.0)))
        r = 18.0
        nb = Qt.BrushStyle.NoBrush
        if ring == "ui":
            p.setPen(QPen(QColor(C.WHITE), 1.2))
            p.setBrush(QColor(C.PRI))
            p.drawEllipse(c, 4, 4)
            return
        if ring == "lost":
            p.setPen(QPen(with_alpha(C.TEXT_DIM, 0.45), 2, Qt.PenStyle.DotLine))
            p.setBrush(nb)
            p.drawEllipse(c, r, r)
            return
        if ring == "clutch":
            p.setPen(QPen(with_alpha(C.PRI_DIM, 0.8), 2, Qt.PenStyle.DashLine))
            p.setBrush(with_alpha(C.BG, 0.25))
            p.drawEllipse(c, r, r)
            p.setPen(with_alpha(C.TEXT_MED, 0.9))
            p.setFont(px_font(14, QFont.Weight.Bold))
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, "⏸")
            return
        if ring == "crush":
            p.setPen(QPen(with_alpha(C.RED, 0.35), 6))
            p.setBrush(nb)
            p.drawEllipse(c, r, r)
            p.setPen(QPen(QColor(C.RED), 2))
            p.drawEllipse(c, r * 0.75, r * 0.75)
            p.setFont(px_font(14, QFont.Weight.Bold))
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, "✕")
            self._arc(p, c, r + 8, progress, C.RED, 3)
            return
        if ring == "scroll":
            axis = str(getattr(view, "scroll_axis", "") or "")
            horiz = axis == "h"
            w, h = (r * 3.0, r * 1.5) if horiz else (r * 1.5, r * 3.0)
            cap = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
            p.setPen(QPen(QColor(C.PRI), 2))
            p.setBrush(with_alpha(C.PRI_GHO, 0.7))
            p.drawRoundedRect(cap, min(w, h) / 2, min(w, h) / 2)
            p.setPen(QColor(C.WHITE))
            p.setFont(px_font(16, QFont.Weight.Bold))
            glyph = {"h": "↔", "v": "↕", "both": "✥"}.get(axis, "↕")
            p.drawText(cap, Qt.AlignmentFlag.AlignCenter, glyph)
            return
        # track / armed / pressed / drag / hold
        col = {"armed": C.ACC2, "pressed": C.ACC2, "drag": C.ACC2}.get(ring, C.PRI)
        p.setPen(QPen(with_alpha(col, 0.35), 6))
        p.setBrush(nb)
        p.drawEllipse(c, r, r)
        inner = r * (1.0 - 0.45 * (1.0 if ring in ("pressed", "drag") else pinch))
        p.setPen(QPen(QColor(col), 2))
        p.setBrush(with_alpha(col, 0.55) if ring in ("pressed", "drag") else nb)
        p.drawEllipse(c, inner, inner)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col))
        p.drawEllipse(c, 2.2, 2.2)
        if ring == "drag":
            p.setPen(QPen(QColor(col), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for k in range(4):
                a = k * math.pi / 2
                dx, dy = math.cos(a), math.sin(a)
                p.drawLine(QPointF(c.x() + dx * (r + 4), c.y() + dy * (r + 4)),
                           QPointF(c.x() + dx * (r + 11), c.y() + dy * (r + 11)))
        elif ring == "hold":
            self._arc(p, c, r + 9, progress, C.PRI, 3)

    def _paint_effects(self, p: QPainter, now: float) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for x, y, t0, col in self._effects:
            k = max(0.0, min(1.0, (now - t0) / 0.6))
            rad = 14 + 46 * k
            p.setPen(QPen(with_alpha(col, 0.8 * (1 - k)), 2))
            p.drawEllipse(self._pt(x, y), rad, rad)

    # ── the action menu ──────────────────────────────────────────────────────
    def _paint_menu(self, p: QPainter, menu: dict) -> None:
        H = self.height()
        c = self._pt(menu.get("x", 0.5), menu.get("y", 0.5))
        rad = float(menu.get("r", 0.11)) * H
        p.setPen(QPen(with_alpha(C.PRI, 0.5), 1.5, Qt.PenStyle.DashLine))
        p.setBrush(with_alpha(C.BG, 0.45))
        p.drawEllipse(c, rad * 1.25, rad * 1.25)
        hover = menu.get("hover") or ""
        for item in menu.get("items") or []:
            iid, label, glyph, x, y = item[:5]
            q = self._pt(x, y)
            on = hover == iid
            br = rad * (0.36 if on else 0.30)
            p.setPen(QPen(QColor(C.PRI if on else C.BORDER_B), 2 if on else 1.2))
            p.setBrush(with_alpha(C.PRI if on else C.PRI_GHO, 0.45 if on else 0.85))
            p.drawEllipse(q, br, br)
            p.setPen(QColor(C.WHITE if on else C.PRI))
            p.setFont(px_font(br * 0.95))
            p.drawText(QRectF(q.x() - br, q.y() - br, 2 * br, 2 * br), Qt.AlignmentFlag.AlignCenter,
                       str(glyph))
            if on:
                self._label(p, q.x(), q.y() - br - 16, str(label))

    # ── the type bar ─────────────────────────────────────────────────────────
    def _paint_bar(self, p: QPainter, bar: dict) -> None:
        r = self._box(bar.get("x", 0), bar.get("y", 0), bar.get("w", 0.2), bar.get("h", 0.05))
        if r.width() < 4 or r.height() < 4:
            return
        state = bar.get("state") or "idle"
        edge = {"secure": C.ACC2, "dictating": C.RED}.get(state, C.PRI)
        cut = min(14.0, r.height() * 0.3)
        self._glass(p, chamfer(r, cut, ("tl", "br")), edge)
        hover = bar.get("hover") or ""
        right = r.left() + cut
        first = r.right() - cut                     # left edge of the first button
        for btn in bar.get("buttons") or []:
            bid, glyph, label, x, y, w, h = btn[:7]
            br = self._box(x, y, w, h)
            right = max(right, br.right())
            first = min(first, br.left())
            on = hover == bid
            live = state == "dictating" and bid in ("stop", "dictate")
            col = C.RED if live else (C.PRI if on else C.BORDER_B)
            p.setPen(QPen(QColor(col), 2 if on else 1.2))
            p.setBrush(with_alpha(C.PRI if on else C.PRI_GHO, 0.45 if on else 0.8))
            p.drawPolygon(chamfer(br, min(8.0, br.height() * 0.25), ("tl", "br")))
            p.setPen(QColor(C.WHITE if on else (C.RED if live else C.PRI)))
            p.setFont(px_font(max(10.0, br.height() * 0.5)))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, str(glyph))
            if on and label:
                self._label(p, br.center().x(), br.top() - 16, str(label))
        if state == "secure":
            # Only a note (and the ✕): no dictation, no keys into a password.
            p.setPen(QColor(C.ACC2))
            p.setFont(hud_font(max(9.0, r.height() * 0.20), QFont.Weight.Bold))
            note = str(bar.get("note") or "🔒 Type this one on your keyboard")
            fm = QFontMetricsF(p.font())
            area = QRectF(r.left() + cut, r.top(), max(10.0, first - r.left() - cut - 6),
                          r.height())
            p.drawText(area, Qt.AlignmentFlag.AlignCenter,
                       fm.elidedText(note, Qt.TextElideMode.ElideRight, area.width()))
        elif state == "dictating":
            area = QRectF(right + 10, r.top() + 6, max(0.0, r.right() - cut - right - 10),
                          r.height() - 12)
            if area.width() > 20:
                level = max(0.0, min(1.0, float(bar.get("level", 0.0) or 0.0)))
                meter = QRectF(area.left(), area.bottom() - 5, area.width(), 4)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(C.BAR_BG, 0.95))
                p.drawRect(meter)
                p.setBrush(QColor(C.RED if level > 0.85 else C.GREEN))
                p.drawRect(QRectF(meter.left(), meter.top(), meter.width() * level, meter.height()))
                text = str(bar.get("preview") or 'Listening... say "stop" when done')
                p.setPen(QColor(C.TEXT if bar.get("preview") else C.TEXT_DIM))
                p.setFont(hud_font(max(8.0, r.height() * 0.18)))
                fm = QFontMetricsF(p.font())
                # The end of the phrase is what is being said now: elide the start.
                p.drawText(QRectF(area.left(), area.top(), area.width(), area.height() - 8),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(text, Qt.TextElideMode.ElideLeft, area.width()))
        elif bar.get("note"):
            p.setPen(QColor(C.TEXT_MED))
            p.setFont(hud_font(8))
            p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 18),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, str(bar["note"]))

    # ── the air keyboard ─────────────────────────────────────────────────────
    def _paint_keyboard(self, p: QPainter, kb: dict, cursor=None) -> None:
        keys = kb.get("keys") or []
        if not keys:
            return
        boxes = [self._box(*k[2:6]) for k in keys]
        left = min(b.left() for b in boxes)
        top = min(b.top() for b in boxes)
        right = max(b.right() for b in boxes)
        bottom = max(b.bottom() for b in boxes)
        suggest = kb.get("suggest") or []
        for s in suggest:
            sb = self._box(*s[1:5])
            top = min(top, sb.top())
            bottom = max(bottom, sb.bottom())        # below the keys when on top
        er = kb.get("echo_rect")
        echo_box = self._box(*er[:4]) if er and len(er) >= 4 else None
        if echo_box is not None:
            top = min(top, echo_box.top())
            bottom = max(bottom, echo_box.bottom())
        pad = 12.0
        frame = QRectF(left - pad, top - pad, right - left + 2 * pad, bottom - top + 2 * pad)
        self._glass(p, chamfer(frame, 16, ("tl", "br")), C.PRI, fill_a=0.72)
        hover, pressed = kb.get("hover") or "", kb.get("pressed") or ""
        lit = None
        for (kid, label, *_r), b in zip(keys, boxes):
            on, down = kid == hover, kid == pressed
            col = C.ACC2 if down else (C.PRI if on else C.BORDER_B)
            p.setPen(QPen(QColor(col), 1.8 if (on or down) else 1.0))
            p.setBrush(with_alpha(C.ACC2 if down else (C.PRI if on else C.PRI_GHO),
                                  0.55 if down else (0.35 if on else 0.82)))
            shape = chamfer(b.adjusted(2, 2, -2, -2), min(6.0, b.height() * 0.2), ("tl", "br"))
            p.drawPolygon(shape)
            text = str(label)
            if kb.get("shift") and len(text) == 1:
                text = text.upper()
            p.setPen(QColor(C.WHITE if (on or down) else C.TEXT))
            p.setFont(px_font(max(9.0, b.height() * (0.42 if len(text) <= 2 else 0.26))))
            p.drawText(b, Qt.AlignmentFlag.AlignCenter, text)
            if on:
                lit = (text, b)
        for s in suggest:
            word = str(s[0])
            sb = self._box(*s[1:5])
            on = bool(cursor) and sb.contains(self._pt(*cursor[:2]))
            p.setPen(QPen(QColor(C.PRI if on else C.BORDER_A), 1.2))
            p.setBrush(with_alpha(C.PRI if on else C.PANEL, 0.4 if on else 0.75))
            p.drawRect(sb.adjusted(2, 2, -2, -2))
            p.setPen(QColor(C.WHITE if on else C.TEXT_MED))
            p.setFont(hud_font(max(8.0, sb.height() * 0.28)))
            fm = QFontMetricsF(p.font())
            p.drawText(sb, Qt.AlignmentFlag.AlignCenter,
                       fm.elidedText(word, Qt.TextElideMode.ElideRight, sb.width() - 8))
        if echo_box is not None:
            # what the keyboard typed so far: typos show before Enter sends
            p.setPen(QPen(QColor(C.BORDER_A), 1.0))
            p.setBrush(with_alpha(C.BG, 0.85))
            p.drawRect(echo_box.adjusted(1, 1, -1, -1))
            echo = str(kb.get("echo") or "")
            p.setPen(QColor(C.TEXT if echo else C.TEXT_DIM))
            p.setFont(hud_font(max(8.0, echo_box.height() * 0.40)))
            fm = QFontMetricsF(p.font())
            area = echo_box.adjusted(10, 0, -10, 0)
            p.drawText(area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(echo + "▏" if echo else "Type with a pinch on a key",
                                     Qt.TextElideMode.ElideLeft, area.width()))
        layer = str(kb.get("layer") or "abc")
        p.setPen(QColor(C.TEXT_DIM))
        p.setFont(hud_font(8, QFont.Weight.Bold))
        tag = layer.upper() + ("  ⇧" if kb.get("shift") else "")
        p.drawText(QRectF(frame.left() + 8, frame.bottom() - 16, 80, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tag)
        if lit is not None:                          # the hovered key, enlarged above it
            text, b = lit
            w, h = b.width() * 1.25, b.height() * 1.25
            bub = QRectF(b.center().x() - w / 2, b.top() - h - 6, w, h)
            self._glass(p, chamfer(bub, min(8.0, h * 0.2), ("tl", "br")), C.PRI, fill_a=0.95)
            p.setPen(QColor(C.WHITE))
            p.setFont(px_font(max(10.0, h * (0.5 if len(text) <= 2 else 0.3)), QFont.Weight.Bold))
            p.drawText(bub, Qt.AlignmentFlag.AlignCenter, text)

    # ── exit, status, hand, banner ───────────────────────────────────────────
    def _paint_exit(self, p: QPainter, frac: float) -> None:
        c = QPointF(self.width() / 2, 70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(C.BG, 0.7))
        p.drawEllipse(c, 40, 40)
        self._arc(p, c, 34, frac, C.ACC2, 4)
        p.setPen(QColor(C.WHITE))
        p.setFont(px_font(20))
        p.drawText(QRectF(c.x() - 34, c.y() - 34, 68, 68), Qt.AlignmentFlag.AlignCenter, "✋✋")
        self._label(p, c.x(), c.y() + 56, "Hold to leave Air Control", C.ACC2)

    def _paint_status(self, p: QPainter, view, now: float) -> None:
        W = self.width()
        status = str(getattr(view, "status", "") or "active")
        n = int(getattr(view, "hands_seen", 0) or 0)
        head = (f"◈  AIR CONTROL  ·  {_STATUS_WORD.get(status, status.upper())}  ·  "
                f"{n} HAND{'S' if n != 1 else ''}")
        p.setFont(hud_font(10, QFont.Weight.Bold))
        fm = QFontMetricsF(p.font())
        box = QRectF(16, 10, fm.horizontalAdvance(head) + 20, 26)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(C.BG, 0.55))
        p.drawRect(box)
        p.setPen(QColor(C.RED if status == "blocked" else C.PRI))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, head)
        line, col = "", C.TEXT_MED
        if self.status:
            line, col = self.status, C.ACC2
        elif now < self.hint_until:
            line = self.hint
        else:
            line = str(getattr(view, "hint", "") or "") or self.note
        if line:
            p.setFont(hud_font(9))
            fm = QFontMetricsF(p.font())
            text = fm.elidedText(line, Qt.TextElideMode.ElideRight, W * 0.8)
            box = QRectF(16, 40, fm.horizontalAdvance(text) + 20, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(C.BG, 0.5))
            p.drawRect(box)
            p.setPen(QColor(col))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        # Toasts: the engine's own, or feedback from the dispatcher (newest wins).
        toast, alpha = None, 0.0
        t = getattr(view, "toast", None)
        if t:
            toast, alpha = str(t[0]), 1.0 - max(0.0, min(1.0, float(t[1])))
        if self._toast and now - self._toast[1] < TOAST_S:
            toast, alpha = self._toast[0], 1.0 - (now - self._toast[1]) / TOAST_S
        if toast and alpha > 0.02:
            p.setFont(hud_font(11, QFont.Weight.Bold))
            fm = QFontMetricsF(p.font())
            text = fm.elidedText(toast, Qt.TextElideMode.ElideRight, W * 0.7)
            w = fm.horizontalAdvance(text) + 36
            r = QRectF((W - w) / 2, self.height() * 0.86, w, 34)
            p.setOpacity(min(1.0, alpha * 2))
            self._glass(p, chamfer(r, 10, ("tl", "br")), C.PRI, fill_a=0.9)
            p.setPen(QColor(C.TEXT))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            p.setOpacity(1.0)

    def _paint_hand(self, p: QPainter, pts) -> None:
        """A small skeleton at the bottom right: which hand the camera sees."""
        if not pts or len(pts) < 21:
            return
        size = 170.0
        box = QRectF(self.width() - size - 18, self.height() - size - 18, size, size)
        p.setPen(QPen(with_alpha(C.BORDER_B, 0.6), 1))
        p.setBrush(with_alpha(C.BG, 0.45))
        p.drawPolygon(chamfer(box, 12, ("tl", "br")))
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        a = self._frame_aspect or 16 / 9
        # Picture coords are 0..1 of a wide frame: scale x by the aspect so
        # the hand keeps its shape, then fit the hand into the box.
        span = max((max(xs) - min(xs)) * a, max(ys) - min(ys), 1e-3)
        k = (size - 30) / span
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

        def q(i):
            return QPointF(box.center().x() + (pts[i][0] - cx) * a * k,
                           box.center().y() + (pts[i][1] - cy) * k)

        p.setPen(QPen(with_alpha(C.PRI, 0.7), 2))
        p.drawLines([q(i) for bone in _BONES for i in bone])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(C.WHITE, 0.8))
        for i in (4, 8, 12, 16, 20):
            p.drawEllipse(q(i), 3, 3)

    def _paint_banner(self, p: QPainter) -> None:
        W = self.width()
        text = ("Air Control is paused: the overlay could not be made click-through"
                + (f" ({self.native_error})" if self.native_error else ""))
        p.setFont(hud_font(10, QFont.Weight.Bold))
        fm = QFontMetricsF(p.font())
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, W * 0.8)
        w = fm.horizontalAdvance(text) + 40
        r = QRectF((W - w) / 2, 110, w, 36)
        p.setPen(QPen(QColor(C.RED), 1.5))
        p.setBrush(with_alpha(C.BG, 0.92))
        p.drawPolygon(chamfer(r, 10, ("tl", "br")))
        p.setPen(QColor(C.RED))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
