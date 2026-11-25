"""
Self-test for the Air Control overlay window and the Holo Desk window.

    QT_QPA_PLATFORM=offscreen python -m gui._air_gui_tests

Needs only PyQt6. No camera, no OS events, no network: the hand stream, the
input pump, os_input and dictation are fakes that record what they are asked
to do. The engine (plugins/_air_control_core.py) and the gesture core are used
when they import; until they exist, small stand-ins take their place and the
run says so, so this file passes while the other packages are being written.
(Leading underscore: nothing imports this.)
"""
from __future__ import annotations

import os
import sys
import time
import types
from dataclasses import dataclass
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_ok = _fail = 0


def check(name: str, cond, detail="") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


# ─────────────────────────────────────────────────────────────────────────────
#  Fakes
# ─────────────────────────────────────────────────────────────────────────────
class FakeStream:
    made: list = []

    def __init__(self, name, on_frame, **kw):
        self.name, self.on_frame, self.kw = name, on_frame, kw
        self.started = self.stopped = 0
        FakeStream.made.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def set_max_hands(self, n):
        pass

    running = True


class FakePump:
    def __init__(self, *a, **kw):
        self.calls: list = []
        self.closed = 0
        self.last_error = ""

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def post(self, fn, *args):
        self.calls.append(("post", getattr(fn, "__name__", str(fn)), args))

    def slow(self, fn, *args, done=None):
        self.calls.append(("slow", getattr(fn, "__name__", str(fn)), args))

    def cursor_sample(self):
        return None

    def close(self):
        self.closed += 1


class FakeOsInput(types.ModuleType):
    """os_input with no OS behind it. Unknown functions record and do nothing."""

    def __init__(self):
        super().__init__("fake_os_input")
        self.log: list = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def fn(*a, **kw):
            self.log.append((name, a))
            return (True, "") if name not in ("open_browser",) else "done: opened"
        return fn

    def cursor(self):
        return (100.0, 100.0)

    def monitor_for_qt(self, geo, dpr):
        x, y, w, h = geo
        return {"left": x, "top": y, "width": w, "height": h}

    def to_mouse(self, u, v, mon):
        return (mon["left"] + u * mon["width"], mon["top"] + v * mon["height"])

    def from_mouse(self, x, y, mon):
        return ((x - mon["left"]) / mon["width"], (y - mon["top"]) / mon["height"])

    def key_down(self, name):
        return None

    def focused_text_field(self):
        return None

    def own_app_focused(self):
        return False

    def InputPump(self, *a, **kw):
        return FakePump()


class FakeDictation(types.ModuleType):
    def __init__(self):
        super().__init__("fake_dictation")
        self.subs, self.unsubbed, self.stopped = [], 0, []
        self.on = False

    def active(self):
        return self.on

    def on_event(self, cb):
        self.subs.append(cb)

        def unsub():
            self.unsubbed += 1
        return unsub

    def stop(self, commit=True):
        self.stopped.append(commit)
        self.on = False
        return "done: stopped"

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **kw: "done"


class StubFilter:
    """Counts calls; hands in -> the same objects out (they are HandStates)."""

    def __init__(self):
        self.calls: list = []

    def update(self, hands, t, to_screen=None):
        self.calls.append(t)
        return list(hands)


def hand_state(x=0.5, y=0.5, t=0.0, age=20, pose="relaxed"):
    pts = [(x + 0.01 * (i % 5), y + 0.01 * (i // 5)) for i in range(21)]
    return SimpleNamespace(slot="h0", x=x, y=y, vx=0.0, vy=0.0, pose=pose, pinch="open",
                           pinch_mid="open", closeness=0.0, down=False, press_x=x, press_y=y,
                           t_aim=t, t_press=0.0, size=0.18, angle=0.0, conf=0.97, side="right",
                           age=age, lost=False, pts=pts, t=t)


# ─────────────────────────────────────────────────────────────────────────────
#  Stand-ins for packages still being written
# ─────────────────────────────────────────────────────────────────────────────
def _standin_air_core():
    mod = types.ModuleType("plugins._air_control_core")
    mod.STATE = {"open": False, "since": 0.0, "status": "", "error": "", "input_ok": False}

    @dataclass
    class AirSettings:
        sensitivity: str = "normal"
        hand: str = "any"
        pointer_speed: str = "normal"
        precision: bool = True
        scroll_natural: bool = True
        scroll_speed: float = 1.0
        space_swipes: bool = True
        type_bar: bool = True
        keyboard_layout: str = "qwerty"
        show_hands: bool = True
        sounds: bool = True

    @dataclass
    class FieldInfo:
        rect: tuple
        caret: tuple | None
        secure: bool
        role: str = ""
        pid: int = 0

    class AirEngine:
        def __init__(self, settings, screen_aspect, frame_aspect=16 / 9, clock=time.monotonic):
            self.ok, self.events, self.field, self.kb = False, [], None, False

        def update(self, hands, t, real_cursor=None):
            if not self.ok or not hands:
                return []
            return [("move", hands[0].x, hands[0].y)]

        def set_field(self, f, t):
            self.field = f

        def set_input_ok(self, ok):
            self.ok = ok

        def set_frame_aspect(self, a):
            pass

        def open_keyboard(self, t):
            self.kb = True

        def on_dictation(self, kind, data):
            pass

        def request_exit(self, why):
            return [("release_all",), ("exit", why)]

        def view(self, t):
            return SimpleNamespace(status="active", cursor=(0.5, 0.5), hand=None, ring="track",
                                   pinch=0.0, progress=0.0, scroll_axis="", menu=None, bar=None,
                                   keyboard=None, exit_progress=0.0, hint="", toast=None,
                                   hands_seen=0, hand_pts=None)

        def take_events(self):
            ev, self.events = self.events, []
            return ev

    class Dispatcher:
        def __init__(self, pump, mon, os_input_mod=None, dictation_mod=None,
                     on_feedback=lambda text: None):
            self.pump, self.mon, self.osi = pump, mon, os_input_mod

        def run(self, actions):
            for a in actions:
                if a[0] == "move":
                    self.pump.move(*self.osi.to_mouse(a[1], a[2], self.mon))

    mod.AirSettings, mod.FieldInfo, mod.AirEngine, mod.Dispatcher = (AirSettings, FieldInfo,
                                                                     AirEngine, Dispatcher)
    mod.load_settings = lambda: AirSettings()
    return mod


def _standin_gesture_core():
    mod = types.ModuleType("plugins._gesture_core")

    class HandFilter:
        def __init__(self, sensitivity="normal", profile="desk", max_hands=2):
            pass

        def update(self, hands, t, to_screen=lambda x, y: (x, y)):
            return list(hands)

    mod.HandFilter = HandFilter
    return mod


STANDINS: list = []


def _ensure(modname: str, maker) -> None:
    try:
        __import__(modname)
    except Exception as e:
        sys.modules[modname] = maker()
        import plugins
        setattr(plugins, modname.split(".")[-1], sys.modules[modname])
        STANDINS.append(f"{modname} ({type(e).__name__})")


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────
def pump_events(app, n=3):
    for _ in range(n):
        app.processEvents()


def make_air(app, platform="linux", mac_result=""):
    from gui import air_control as ac
    ac._platform = lambda: platform
    ac._mac_overlay_native = lambda w: mac_result
    import plugins._air_control_core as air
    real_load = air.load_settings

    def quiet():
        s = real_load()
        s.sounds = False                           # no QSoundEffect in the test
        return s
    air.load_settings = quiet
    osi, dic = FakeOsInput(), FakeDictation()
    closed = []
    w = ac.AirControlWindow(on_close=None, log=lambda s: None, stream_factory=FakeStream,
                            pump_factory=FakePump, os_input_mod=osi, dictation_mod=dic)
    w.resize(1280, 800)
    calls = []
    w.activateWindow = lambda: calls.append("activateWindow")
    w.raise_ = lambda: calls.append("raise_")
    w.open_on(app.primaryScreen())
    pump_events(app)
    air.load_settings = real_load
    return w, osi, dic, calls, closed


def test_air(app) -> None:
    print("Air Control window")
    from gui import air_control as ac
    import plugins._air_control_core as air
    w, osi, dic, calls, _ = make_air(app)
    flags = w.windowFlags()
    check("flags: transparent for input", bool(flags & Qt.WindowType.WindowTransparentForInput))
    check("flags: does not accept focus", bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus))
    check("flags: tool, on top, frameless",
          bool(flags & Qt.WindowType.Tool) and bool(flags & Qt.WindowType.WindowStaysOnTopHint)
          and bool(flags & Qt.WindowType.FramelessWindowHint))
    check("attributes set", all(w.testAttribute(a) for a in (
        Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_NoSystemBackground,
        Qt.WidgetAttribute.WA_ShowWithoutActivating,
        Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)))
    check("no focus policy", w.focusPolicy() == Qt.FocusPolicy.NoFocus)
    check("open_on never activates or raises", calls == [], calls)
    check("STATE open", air.STATE.get("open") is True, air.STATE)
    check("input confirmed off macOS", w.input_ok and air.STATE.get("input_ok") is True)
    check("stream started with the contract arguments",
          w.stream.started == 1 and w.stream.name == "air_control"
          and w.stream.kw.get("max_hands") == 2 and w.stream.kw["want_picture"]() is False)
    check("dictation events subscribed", len(dic.subs) == 1)

    # Hands -> filter (once per frame) -> engine -> dispatcher -> pump moves.
    w.filter = stub = StubFilter()
    t0 = time.monotonic()
    for i in range(30):
        t = t0 + i / 30
        w._hands_sig.emit([hand_state(0.40 + 0.004 * i, 0.5, t, age=10 + i)], t)
        pump_events(app, 1)
        w._tick()
        w._tick()                                  # a second tick on the same frame
    moves = [c for c in w.pump.calls if c[0] == "move"]
    check("hands reach the pump as moves", len(moves) > 0, w.pump.calls[:5])
    check("filter called once per new frame, not per tick", len(stub.calls) == 30,
          len(stub.calls))

    # Painting every state must not raise.
    errors = []

    def view(**kw):
        base = dict(status="active", cursor=(0.5, 0.5), hand=(0.52, 0.55), ring="track",
                    pinch=0.4, progress=0.5, scroll_axis="", menu=None, bar=None, keyboard=None,
                    exit_progress=0.0, hint="hint", toast=("Clicked", 0.3), hands_seen=1,
                    hand_pts=[(0.4 + 0.01 * (i % 5), 0.5 + 0.02 * (i // 5)) for i in range(21)])
        base.update(kw)
        return SimpleNamespace(**base)

    menu = {"x": 0.5, "y": 0.5, "r": 0.11, "hover": "browser",
            "items": [(k, k.title(), "◎", 0.5 + 0.08 * i / 8, 0.42) for i, k in enumerate(
                ("right_click", "keyboard", "back", "forward", "browser", "launcher",
                 "overview", "exit"))]}
    buttons = [("dictate", "🎙", "Dictate", 0.30, 0.61, 0.04, 0.05),
               ("keys", "⌨", "Keys", 0.345, 0.61, 0.04, 0.05),
               ("backspace", "⌫", "Delete", 0.39, 0.61, 0.04, 0.05),
               ("enter", "⏎", "Enter", 0.435, 0.61, 0.04, 0.05),
               ("close", "✕", "Close", 0.48, 0.61, 0.04, 0.05)]
    bar = {"x": 0.29, "y": 0.6, "w": 0.4, "h": 0.07, "state": "idle", "buttons": buttons,
           "hover": "keys", "level": 0.6, "preview": "hello there how are you", "note": ""}
    keys = [(c, c, 0.2 + 0.05 * i, 0.7, 0.045, 0.06) for i, c in enumerate("qwertyuiop")]
    kb = {"keys": keys, "hover": "e", "pressed": "r", "layer": "abc", "shift": True,
          "suggest": [("hello", 0.2, 0.63, 0.1, 0.05), ("help", 0.31, 0.63, 0.1, 0.05)]}
    views = [view(ring=r) for r in ("track", "armed", "pressed", "drag", "hold", "clutch",
                                     "crush", "lost", "ui")]
    views += [view(ring="scroll", scroll_axis=a) for a in ("", "v", "h", "both")]
    views += [view(menu=menu), view(bar=bar), view(bar=dict(bar, state="secure", buttons=[],
                                                             note="🔒 Type this one on your keyboard")),
              view(bar=dict(bar, state="dictating")), view(bar=dict(bar, state="keys"), keyboard=kb),
              view(exit_progress=0.6, status="exiting"), view(status="blocked", cursor=None),
              view(status="no_hand", hands_seen=0, hand_pts=None, toast=None)]
    w._toast = ("Opened the browser", time.monotonic())
    w._effects.append((0.5, 0.5, time.monotonic(), "#ffcc00"))
    for v in views:
        w._view = v
        w._tick_error = ""
        img = QImage(w.size(), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        w.render(img)
        if w._tick_error:
            errors.append((v.ring, w._tick_error))
    check("every ring, the menu, the bar states and the keyboard paint", not errors, errors)

    # The engine itself, driven to show the keyboard and a field.
    w._view = None
    w.open_keyboard()
    w._field_sig.emit(air.FieldInfo(rect=(0.3, 0.4, 0.3, 0.05), caret=(0.35, 0.4, 0.002, 0.04),
                                    secure=False))
    w._field_sig.emit(air.FieldInfo(rect=(0.3, 0.4, 0.3, 0.05), caret=None, secure=False))
    pump_events(app)
    for _ in range(5):
        w._tick()
    w._tick_error = ""
    img = QImage(w.size(), QImage.Format.Format_ARGB32_Premultiplied)
    w.render(img)
    check("real engine view (keyboard + field) paints", not w._tick_error, w._tick_error)
    if not any("_air_control_core" in x for x in STANDINS):
        v = w._view
        check("engine shows the keyboard and the type bar",
              v is not None and bool(getattr(v, "keyboard", None)) and bool(getattr(v, "bar", None)),
              (getattr(v, "keyboard", None) is not None, getattr(v, "bar", None)))
    w._field_sig.emit(air.FieldInfo(rect=(0.3, 0.4, 0.3, 0.05), caret=None, secure=True))
    pump_events(app)
    w._tick()
    w.render(img)
    check("secure field view paints", not w._tick_error, w._tick_error)

    # An ('exit', 'gesture') event from the engine closes the window.
    pump = w.pump
    closed = []
    w.on_close = lambda already_closed=False: (closed.append(already_closed), w.shutdown())
    w.engine.take_events = lambda: [("exit", "gesture")]
    w._tick()
    pump_events(app)
    check("an exit event closes the window", closed == [False] and w._closing, closed)
    check("pump closed (everything released)", pump.closed == 1, pump.closed)
    check("stream stopped", w.stream.stopped == 1)
    check("dictation unsubscribed", dic.unsubbed == 1)
    check("STATE closed", air.STATE.get("open") is False and air.STATE.get("input_ok") is False)
    w._teardown()
    check("teardown is idempotent", pump.closed == 1)

    # macOS set-up that fails: paused, banner shown, nothing sent.
    w2, osi2, dic2, calls2, _ = make_air(app, platform="darwin", mac_result="no window")
    check("darwin failure: input_ok False", w2.input_ok is False and air.STATE["input_ok"] is False)
    check("darwin failure: error recorded", air.STATE.get("error") == "no window", air.STATE)
    check("darwin failure: banner shown", w2.native_done and not w2.input_ok)
    w2.filter = StubFilter()
    t = time.monotonic()
    for i in range(10):
        w2._hands_sig.emit([hand_state(0.5, 0.5, t + i / 30, age=20 + i)], t + i / 30)
        pump_events(app, 1)
        w2._tick()
    check("darwin failure: nothing sent", not [c for c in w2.pump.calls if c[0] == "move"],
          w2.pump.calls[:3])
    img = QImage(w2.size(), QImage.Format.Format_ARGB32_Premultiplied)
    w2._tick_error = ""
    w2.render(img)
    check("banner paints", not w2._tick_error, w2._tick_error)
    check("open_on (darwin) never activates", calls2 == [], calls2)

    # Esc unavailable -> hint; exit signal -> close with request_exit actions.
    w2._status_sig.emit("hint:" + ac._NO_ESC)
    pump_events(app)
    check("Esc-unavailable note shown", w2.note == ac._NO_ESC)
    w2._exit_sig.emit("esc")
    pump_events(app)
    check("Esc exit closes", w2._closing and w2.pump.closed == 1)

    # A failure while opening stops what started and hides the window.
    real_engine = air.AirEngine

    def broken(*a, **kw):
        raise ValueError("no engine")
    air.AirEngine = broken
    w3 = ac.AirControlWindow(stream_factory=FakeStream, pump_factory=FakePump,
                             os_input_mod=FakeOsInput(), dictation_mod=FakeDictation())
    try:
        w3.open_on(app.primaryScreen())
        raised = False
    except ValueError:
        raised = True
    finally:
        air.AirEngine = real_engine
    pump_events(app)
    check("failed open raises, hides and closes", raised and w3._closing and not w3.isVisible()
          and air.STATE.get("open") is False)


def test_holo(app) -> None:
    print("Holo Desk window")
    from plugins import _holo_desk_core as core
    from gui import holo_desk as hd
    from gui import air_control as ac
    ac._platform = lambda: "linux"
    hd._Sounds = lambda: None
    desk = core.Desk()
    a = desk.add("clock")
    b = desk.add("text", title="Note", text="Some words")
    FakeStream.made.clear()
    w = hd.HoloDeskWindow(desk, mode="camera", stream_factory=FakeStream)
    w.sounds = None
    w.resize(1280, 800)
    check("built with the stream factory", FakeStream.made and FakeStream.made[-1].started == 1
          and FakeStream.made[-1].name == "holo_desk")
    check("picture wanted in camera mode", FakeStream.made[-1].kw["want_picture"]() is True)
    w.set_mode("overlay")
    check("picture not wanted in overlay mode", FakeStream.made[-1].kw["want_picture"]() is False)
    flags = w.windowFlags()
    check("overlay: transparent and focus-less",
          bool(flags & Qt.WindowType.WindowTransparentForInput)
          and bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
          and w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
    calls = []
    w.activateWindow = lambda: calls.append("activateWindow")
    w.raise_ = lambda: calls.append("raise_")
    w.open_on(app.primaryScreen())
    pump_events(app)
    check("overlay open_on never activates", calls == [], calls)
    w.set_mode("camera")
    check("camera mode activates (Esc)", "activateWindow" in calls, calls)

    # The filter runs once per NEW camera frame.
    stub = SimpleNamespace(calls=[])

    def upd(hands, to_screen, t):
        stub.calls.append(t)
        return [core.Pointer("h0", 0.5, 0.5, False)] if hands else []
    stub.update = upd
    w.filter = stub
    t = time.monotonic()
    w._on_frame(None, [object()], t)
    for _ in range(5):
        w._tick()
    w._on_frame(None, [object()], t + 0.033)
    for _ in range(5):
        w._tick()
    check("holo filter once per new frame", len(stub.calls) == 2, stub.calls)
    w._on_frame(None, [], t - 1.0)               # a stale, empty frame
    w._tick()
    n = len(stub.calls)
    w._tick()
    check("stale hands let go once", len(stub.calls) == n, (n, len(stub.calls)))

    # Crush marks, the undo chip, a lit ✕ and a V-scroll cursor paint.
    now = time.monotonic()
    w.inp.crush_marks = lambda t: [{"pointer": "h0", "panel": a.pid, "phase": "closing",
                                    "progress": 0.6},
                                   {"pointer": "h1", "panel": b.pid, "phase": "armed",
                                    "progress": 0.0}]
    w.inp.undo_chip = lambda t: {"x": 0.45, "y": 0.8, "w": 0.1, "h": 0.04, "label": "Undo close",
                                 "left": 5.0}
    w.inp.cursor_at = lambda pid: (0.31, 0.32)
    w.inp.v_scroll = {"h1": b.pid}
    w.inp.hover = {"h0": (0.3, 0.3, f"close:{b.pid}"), "h1": (0.46, 0.81, "chip")}
    w.pointers = [core.Pointer("h0", 0.3, 0.3, True, pinch=1.0,
                               pts=[(0.3 + 0.01 * (i % 5), 0.3 + 0.01 * (i // 5)) for i in range(21)]),
                  core.Pointer("h1", 0.6, 0.6, False, pinch=0.5)]
    try:
        w.pointers[1].state = "armed"
    except Exception:
        pass
    img = QImage(w.size(), QImage.Format.Format_ARGB32_Premultiplied)
    try:
        w.render(img)
        ok, err = True, ""
    except Exception as e:                       # render swallows; paintEvent errors print
        ok, err = False, str(e)
    check("crush marks, undo chip, lit ✕, V-scroll paint", ok, err)
    w.set_mode("overlay")
    w.render(img)
    for ev in (("armed", a.pid), ("crushed", a.pid), ("reopened", b.pid)):
        w._event(ev, now)
    check("crushed / reopened ripples", len([e for e in w.effects if e[0] == "ripple"]) >= 2,
          w.effects)
    stream = FakeStream.made[-1]
    w.shutdown()
    pump_events(app)
    check("holo shutdown stops the stream", stream.stopped == 1)


def test_wiring(app) -> None:
    """MainWindow's _on_air / _on_holo slots and JarvisUI.air_control, run on a
    stand-in `self` with fake windows: one camera mode at a time, a second
    'open' never builds a second overlay, a failed open is cleaned up."""
    print("\n[W] MainWindow / JarvisUI wiring")
    try:
        from gui import window as gw
        import gui.air_control as ac
        import gui.holo_desk as hd
        import ui as jui
        from plugins import _air_control_core as air_core
    except Exception as e:
        check("wiring modules import", False, e)
        return
    made: list = []

    class FakeAir:
        fail = False

        def __init__(self, on_close=None, log=None):
            self.on_close, self.shut, self.kb = on_close, 0, 0
            made.append(self)

        def open_on(self, screen):
            if FakeAir.fail:
                raise RuntimeError("boom")

        def open_keyboard(self):
            self.kb += 1

        def shutdown(self):
            self.shut += 1

    class FakeHolo:
        def __init__(self, *a, **kw):
            self.shut, self.mode = 0, kw.get("mode") or "camera"
            made.append(self)

        def open_on(self, screen):
            pass

        def shutdown(self):
            self.shut += 1

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    old_air, old_holo = ac.AirControlWindow, hd.HoloDeskWindow
    ac.AirControlWindow, hd.HoloDeskWindow = FakeAir, FakeHolo
    try:
        logs: list = []
        me = SimpleNamespace(_air=None, _holo=None, screen=lambda: None,
                             _log=SimpleNamespace(append_log=logs.append))
        me._air_closed = lambda already_closed=False: gw.MainWindow._air_closed(me, already_closed)
        me._holo_closed = lambda already_closed=False: gw.MainWindow._holo_closed(
            me, already_closed)
        gw.MainWindow._on_holo(me, "open", {})
        holo = me._holo
        check("holo opens", isinstance(holo, FakeHolo))
        gw.MainWindow._on_air(me, "open", {})
        air = me._air
        check("opening Air Control closes the Holo Desk",
              isinstance(air, FakeAir) and me._holo is None and holo.shut == 1)
        gw.MainWindow._on_air(me, "open", {})
        check("a second open keeps the same overlay", me._air is air
              and sum(isinstance(m, FakeAir) for m in made) == 1)
        gw.MainWindow._on_air(me, "keyboard", {})
        check("keyboard reaches the overlay", air.kb == 1)
        gw.MainWindow._on_holo(me, "open", {})
        check("opening the Holo Desk closes Air Control",
              me._air is None and air.shut == 1 and isinstance(me._holo, FakeHolo))
        gw.MainWindow._on_holo(me, "close", {})
        gw.MainWindow._on_air(me, "close", {})
        check("close with nothing open is harmless", me._air is None and me._holo is None)
        FakeAir.fail = True
        gw.MainWindow._on_air(me, "open", {})
        bad = made[-1]
        check("a failed open is shut down and reported",
              me._air is None and bad.shut == 1 and air_core.STATE.get("open") is False
              and "boom" in str(air_core.STATE.get("error")), air_core.STATE)
        sent: list = []
        fake_ui = SimpleNamespace(_win=SimpleNamespace(
            _air_sig=SimpleNamespace(emit=lambda *a: sent.append(a))))
        jui.JarvisUI.air_control(fake_ui, "keyboard", x=1)
        check("JarvisUI.air_control only emits _air_sig", sent == [("keyboard", {"x": 1})], sent)
    finally:
        FakeAir.fail = False
        ac.AirControlWindow, hd.HoloDeskWindow = old_air, old_holo
        air_core.STATE.update(open=False, error="", input_ok=False)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    _ensure("plugins._air_control_core", _standin_air_core)
    _ensure("plugins._gesture_core", _standin_gesture_core)
    if STANDINS:
        print("Stand-ins (not written yet): " + ", ".join(STANDINS))
    test_air(app)
    test_holo(app)
    test_wiring(app)
    print(f"\n{_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
