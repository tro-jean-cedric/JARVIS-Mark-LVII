"""
Self-test for Air Control's logic: python -m plugins._air_control_tests

Stdlib only: no camera, window, mouse or network. Hands are SimpleNamespace
stand-ins for gesture_core.HandState (every C4 field, see hs()), driven at 30
frames a second on a fake clock, with a 16:10 screen and a 16:9 camera. The
OS side is a fake pump and a fake os_input; nothing reaches the real mouse.

Swipes and the crush use the real plugins/_gesture_core detectors when that
module imports, and are skipped with a note otherwise; their wiring into the
engine is also checked with scripted stand-in detectors, which always run.
"""
from __future__ import annotations

import math
import sys
import types
from types import SimpleNamespace

from plugins import _air_control_core as ac
from plugins._air_control_core import AirEngine, AirSettings, Dispatcher, FieldInfo
from plugins._air_words import WordPredictor

FPS = 30.0
DT = 1.0 / FPS
SA = 16 / 10
FA = 16 / 9

_results = {"pass": 0, "fail": 0, "skip": 0}


def check(name: str, cond, detail="") -> bool:
    ok = bool(cond)
    _results["pass" if ok else "fail"] += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok or not detail else f"  -- {detail}"))
    return ok


def skip(name: str, why: str) -> None:
    _results["skip"] += 1
    print(f"SKIP {name}  -- {why}")


def hs(**kw) -> SimpleNamespace:
    """A HandState stand-in with a default for every contract field."""
    d = dict(slot="h0", x=0.5, y=0.6, vx=0.0, vy=0.0, pose="relaxed", pinch="open",
             pinch_mid="open", closeness=0.0, down=False, press_x=0.5, press_y=0.6, t_aim=0.0,
             t_press=0.0, size=0.18, angle=0.0, conf=0.97, side="right", age=10, lost=False,
             pts=None, t=0.0)
    d.update(kw)
    return SimpleNamespace(**d)


class FakeSwipe:
    def __init__(self):
        self.next = None

    def update(self, x, y, pose, t):
        d, self.next = self.next, None
        return d

    def reset(self):
        pass


class FakeCrush:
    """Open for 3 frames arms; a fist kept 4 frames fires."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.open_n = 0
        self.fist_n = 0
        self.phase = "idle"

    def update(self, pose, x, y, speed, t):
        if self.phase in ("fired", "cancelled"):
            self.phase = "idle"
        if pose == "open":
            self.open_n += 1
            self.fist_n = 0
            if self.open_n >= 3:
                self.phase = "armed"
            return (self.phase, 0.0)
        if pose == "fist" and self.phase in ("armed", "closing"):
            self.fist_n += 1
            self.phase = "closing"
            if self.fist_n >= 4:
                self.phase = "fired"
                return ("fired", 1.0)
            return ("closing", self.fist_n / 4)
        self.open_n = 0
        self.phase = "idle"
        return ("idle", 0.0)


class Sim:
    """One engine, one hand, a fake clock. The 'OS cursor' follows sent moves."""

    def __init__(self, settings: AirSettings | None = None, real: bool = False,
                 fake_detectors: bool = False):
        self.t = 100.0
        s = settings or AirSettings(precision=False)
        AirEngine.WORD_STORE = False
        self.eng = AirEngine(s, SA, FA, clock=lambda: self.t)
        self.eng.words = WordPredictor("en", store=False)
        if fake_detectors:
            self.eng._swipe = FakeSwipe()
            self.eng._crush = FakeCrush()
        self.h = hs(t=self.t)
        self.hands = [self.h]
        self.real = (0.5, 0.5) if real else None
        self.acts: list = []
        self.events: list = []

    @property
    def bw(self):
        return self.eng.bw

    @property
    def bh(self):
        return self.eng.bh

    def frame(self, **changes):
        self.t += DT
        for k, v in changes.items():
            setattr(self.h, k, v)
        self.h.t = self.t
        self.h.age += 1
        for other in self.hands:
            if other is not self.h:
                other.t = self.t
                other.age += 1
        acts = self.eng.update(list(self.hands), self.t, self.real)
        for a in acts:
            if a[0] == "move" and self.real is not None:
                self.real = (a[1], a[2])
        self.acts.extend(acts)
        self.events.extend(self.eng.take_events())
        return acts

    def hold(self, n: int, **changes):
        out = []
        for _ in range(n):
            out += self.frame(vx=0.0, vy=0.0, **changes)
        return out

    def move(self, dx: float, dy: float, n: int, **changes):
        """Move the anchor by (dx, dy) picture units over n frames."""
        out = []
        vx, vy = dx / (n * DT), dy / (n * DT)
        for _ in range(n):
            out += self.frame(x=self.h.x + dx / n, y=self.h.y + dy / n, vx=vx, vy=vy, **changes)
        return out

    def point_at(self, u: float, v: float, n: int = 12):
        cu, cv = self.eng._cursor
        self.move((u - cu) * self.bw, (v - cv) * self.bh, n)
        return self.hold(6)

    def press(self, t_aim: float | None = None, **changes):
        """A pinch edge this frame; aim retro-dated to t_aim (default: 2 frames back)."""
        ta = self.t + DT - 2 * DT if t_aim is None else t_aim
        return self.frame(down=True, pinch="pressed", pose="pinch", t_press=self.t + DT,
                          t_aim=ta, closeness=1.0, vx=changes.pop("vx", 0.0),
                          vy=changes.pop("vy", 0.0), **changes)

    def release(self, **changes):
        return self.frame(down=False, pinch="open", pose="relaxed", closeness=0.0,
                          vx=0.0, vy=0.0, **changes)

    def start(self, frames: int = 15):
        """First sight, then wait out the transition guard."""
        return self.hold(frames)

    def kinds(self, acts=None):
        return [a[0] for a in (self.acts if acts is None else acts)]

    def clear(self):
        self.acts = []
        self.events = []


def near(a, b, tol=0.004):
    return a is not None and b is not None and abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def last_move(acts):
    ms = [a for a in acts if a[0] == "move"]
    return (ms[-1][1], ms[-1][2]) if ms else None


# ── Mapping ──────────────────────────────────────────────────────────────────
def test_mapping():
    s = Sim()
    s.start()
    check("first hand: cursor at the screen centre", near(s.eng._cursor, (0.5, 0.5), 1e-6),
          s.eng._cursor)
    check("box shape: bh = bw / screen_aspect * frame_aspect",
          abs(s.bh - s.bw / SA * FA) < 1e-9)
    s.clear()
    s.move(0.45 * s.bw, 0.0, 15)
    s.hold(3)
    check("moving the hand 0.45 x box width moves the cursor 0.45 of the screen",
          near(last_move(s.acts), (0.95, 0.5)), last_move(s.acts))
    s.clear()
    s.move(-0.2 * s.bh * 0 - 0.0, -0.3 * s.bh, 12)
    s.hold(2)
    check("vertical mapping follows the box height", near(last_move(s.acts), (0.95, 0.2)),
          last_move(s.acts))


def test_rubber_box():
    s = Sim()
    s.start()
    s.move(1.0 * s.bw, 0.0, 20)
    s.hold(2)
    check("rubber box: the cursor stops at the right edge",
          abs(s.eng._cursor[0] - ac.CLAMP[1]) < 1e-6, s.eng._cursor)
    s.clear()
    s.move(-0.1 * s.bw, 0.0, 10)
    s.hold(2)
    check("rubber box: coming back responds at once (no dead travel)",
          abs(s.eng._cursor[0] - 0.9) < 0.005, s.eng._cursor)
    s.move(-2.0 * s.bw, 0.0, 30)
    s.hold(2)
    check("rubber box: left edge", abs(s.eng._cursor[0] - ac.CLAMP[0]) < 1e-6, s.eng._cursor)


def test_precision():
    s = Sim(AirSettings(precision=True))
    s.start()
    u0 = s.eng._cursor[0]
    # 0.1 screen in 1 s = 0.1 screen/s: under the 0.15 precision threshold.
    s.move(0.1 * s.bw, 0.0, 30)
    du = s.eng._cursor[0] - u0
    check("precision: slow motion moves the cursor half as far", abs(du - 0.05) < 0.004, du)
    # Fast strokes back and forth, ~1.2 s above 0.4 screen/s: the offset decays.
    for _ in range(3):
        s.move(0.3 * s.bw, 0.0, 6)
        s.move(-0.3 * s.bw, 0.0, 6)
    off = math.hypot(*s.eng._off)
    check("precision: the lost distance comes back while moving fast", off < 0.002, off)
    raw = s.eng._map_raw(s.h.x, s.h.y)
    check("precision: cursor back on the hand's mapping", near(s.eng._cursor, raw, 0.003),
          (s.eng._cursor, raw))


def test_clutch():
    s = Sim()
    s.start()
    s.move(0.1 * s.bw, 0.05 * s.bh, 8)
    before = s.eng._cursor
    s.clear()
    s.hold(4, pose="open")
    s.move(0.3 * s.bw, 0.2 * s.bh, 10, pose="open")
    s.hold(3, pose="open")
    check("clutch: an open hand sends no moves", "move" not in s.kinds(), s.acts)
    check("clutch: the cursor stays put", near(s.eng._cursor, before, 1e-9))
    s.clear()
    s.hold(3, pose="relaxed")
    check("clutch: leaving it does not jump the cursor", near(s.eng._cursor, before, 1e-6),
          (s.eng._cursor, before))
    s.move(0.1 * s.bw, 0.0, 6)
    check("clutch: afterwards the cursor moves relative to the new hand position",
          near(s.eng._cursor, (before[0] + 0.1, before[1]), 0.003), s.eng._cursor)


# ── Clicks ───────────────────────────────────────────────────────────────────
def test_quick_pinch_retro():
    s = Sim()
    s.start()
    s.point_at(0.3, 0.4)
    t_aim = s.t
    aim = s.eng._cursor
    # The fingers closing drag the knuckles: the cursor drifts before the press.
    s.move(0.02 * s.bw, 0.01 * s.bh, 3)
    s.hold(2)
    drifted = s.eng._cursor
    s.clear()
    s.press(t_aim=t_aim)
    s.hold(2, down=True, pinch="pressed", pose="pinch")
    s.release()
    clicks = [a for a in s.acts if a[0] == "click"]
    check("quick pinch: one left click", clicks == [("click", "left", 1)], s.acts)
    i = s.acts.index(("click", "left", 1)) if clicks else -1
    mv = s.acts[i - 1] if i > 0 else None
    check("quick pinch: sent at the RETRO point, not where the cursor drifted",
          mv is not None and mv[0] == "move" and near((mv[1], mv[2]), aim, 1e-6)
          and not near(drifted, aim, 0.005), (mv, aim, drifted))
    check("quick pinch: click event", any(e[0] == "click" for e in s.events), s.events)
    check("quick pinch: cursor stays at the click point", near(s.eng._cursor, aim, 1e-6))


def test_double_click():
    s = Sim()
    s.start()
    s.point_at(0.6, 0.5)
    s.clear()
    s.press()
    s.hold(2, down=True, pinch="pressed", pose="pinch")
    s.release()
    s.hold(3)
    s.press()
    s.hold(1, down=True, pinch="pressed", pose="pinch")
    s.release()
    clicks = [a for a in s.acts if a[0] == "click"]
    check("double click: second pinch within 0.40 s sends count 2",
          clicks == [("click", "left", 1), ("click", "left", 2)], clicks)


def test_void_after_reacquire_and_clutch():
    s = Sim()
    s.start()
    s.hands.clear()
    for _ in range(15):
        s.frame()
    s.hands.append(s.h)
    s.hold(1)
    s.clear()
    s.press()
    s.hold(1, down=True, pinch="pressed", pose="pinch")
    s.release()
    check("void: no click within 0.3 s of the hand coming back", "click" not in s.kinds(), s.acts)
    check("void: a 'blocked' event says why", ("blocked", "moving") in s.events, s.events)

    s = Sim()
    s.start()
    s.hold(6, pose="open")
    s.hold(2, pose="relaxed")
    s.clear()
    s.press()
    s.hold(1, down=True, pinch="pressed", pose="pinch")
    s.release()
    check("void: no click within 0.3 s of leaving the clutch", "click" not in s.kinds(), s.acts)
    s.hold(12)
    s.clear()
    s.press()
    s.release()
    check("void: after the guard, clicks work again", ("click", "left", 1) in s.acts, s.acts)


def test_void_fast():
    s = Sim()
    s.start()
    s.clear()
    s.press(vx=0.6 * s.bw * 1.5)
    s.release()
    check("void: a press while the hand moves fast does not click", "click" not in s.kinds(),
          s.acts)
    check("void: fast press reported", ("blocked", "moving") in s.events)


def test_void_young_and_pose_change():
    s = Sim()
    s.h.age = 0
    s.hold(5)          # ages 1..5; primary from age 3, still under 6
    s.clear()
    s.press()
    s.release()
    check("void: a hand younger than 6 frames cannot click", "click" not in s.kinds(), s.acts)
    s = Sim()
    s.start()
    s.hold(1, pose="point")
    s.clear()
    s.press()
    s.release()
    check("void: a press within 0.1 s of a pose change does not click",
          "click" not in s.kinds(), s.acts)


def test_drag():
    s = Sim()
    s.start()
    s.point_at(0.4, 0.4)
    origin = s.eng._cursor
    s.clear()
    s.press()
    s.move(0.15 * s.bw, 0.05 * s.bh, 8, down=True, pinch="pressed", pose="pinch")
    s.release()
    k = s.kinds()
    i = k.index("button") if "button" in k else -1
    check("drag: button goes down", ("button", True, "left") in s.acts, s.acts)
    check("drag: at the origin first", i > 0 and s.acts[i - 1][0] == "move"
          and near(s.acts[i - 1][1:3], origin, 1e-6), s.acts[:i + 1])
    after = [a for a in s.acts[i + 1:] if a[0] == "move"]
    check("drag: moves follow while held", len(after) >= 3, s.acts)
    check("drag: button up on release", s.acts[-1] == ("button", False, "left")
          or ("button", False, "left") in s.acts[-2:], s.acts[-3:])
    check("drag: no click", "click" not in k)
    check("drag: drag_start / drag_end events",
          any(e[0] == "drag_start" for e in s.events) and any(e[0] == "drag_end" for e in s.events))


def test_drag_through_pump():
    """The engine's drag start through a REAL InputPump: the press lands at the
    pinch point, not at the drifted hand (the pump keeps move/job order)."""
    try:
        from core import os_input
    except Exception as e:
        skip("drag through the pump", f"core.os_input not importable: {e}")
        return
    import threading
    import time as _time

    class Rec:
        def __init__(self):
            self.pos = (0.0, 0.0)
            self.log = []
            self.lock = threading.Lock()

        def move(self, x, y):
            with self.lock:
                self.pos = (x, y)
                self.log.append(("move", x, y))

        def cursor(self):
            return self.pos

        def release_all(self):
            pass

    rec = Rec()
    pump = os_input.InputPump(backend=rec, cursor_hz=50)
    try:
        mon = {"left": 0, "top": 0, "width": 1000, "height": 500}
        osm = fake_os()
        osm.button = lambda down, which="left": rec.log.append(("button", down, rec.pos))
        d = Dispatcher(pump, mon, os_input_mod=osm)
        s = Sim()
        s.start()
        s.point_at(0.4, 0.4)
        origin = s.eng._cursor
        d.run(s.press())
        dx, dy, n = 0.15 * s.bw, 0.05 * s.bh, 8
        for _ in range(n):
            d.run(s.frame(x=s.h.x + dx / n, y=s.h.y + dy / n, vx=dx / (n * DT),
                          vy=dy / (n * DT), down=True, pinch="pressed", pose="pinch"))
        d.run(s.release())
        end = _time.monotonic() + 1.0
        while _time.monotonic() < end and not any(e[0] == "button" and e[1] is False
                                                   for e in rec.log):
            _time.sleep(0.01)
        downs = [e for e in rec.log if e[0] == "button" and e[1] is True]
        want = (origin[0] * 1000, origin[1] * 500)
        check("drag through the pump: mouse-down at the pinch point",
              len(downs) == 1 and abs(downs[0][2][0] - want[0]) < 0.6
              and abs(downs[0][2][1] - want[1]) < 0.6, (downs, want))
        ups = [e for e in rec.log if e[0] == "button" and e[1] is False]
        check("drag through the pump: mouse-up after the hand moved",
              len(ups) == 1 and ups[0][2][0] > want[0] + 5, (ups, want))
    finally:
        pump.close()


def test_drag_idle_release():
    s = Sim()
    s.start()
    s.press()
    s.move(0.1 * s.bw, 0.0, 5, down=True, pinch="pressed", pose="pinch")
    s.clear()
    s.hold(int(8.3 * FPS), down=True, pinch="pressed", pose="pinch")
    check("drag: released by itself after 8 s without movement",
          s.acts.count(("button", False, "left")) == 1, s.acts[-5:])
    s.clear()
    s.release()
    check("drag: the later finger release sends nothing more",
          ("button", False, "left") not in s.acts and "click" not in s.kinds(), s.acts)


def test_right_click():
    s = Sim()
    s.start()
    s.point_at(0.7, 0.3)
    s.clear()
    s.hold(4, pinch_mid="pressed", pose="pinch_mid")
    s.hold(1, pinch_mid="open", pose="relaxed")
    check("right click: thumb on middle finger", ("click", "right", 1) in s.acts, s.acts)
    check("right click: no left click", ("click", "left", 1) not in s.acts)
    check("right click: event", any(e[0] == "right_click" for e in s.events))


def test_menu():
    s = Sim()
    s.start()
    s.point_at(0.5, 0.5)
    s.clear()
    s.press()
    s.hold(int(0.95 * FPS), down=True, pinch="pressed", pose="pinch")
    check("menu: opens after 0.9 s of a still pinch", s.eng._menu is not None
          and any(e[0] == "menu_open" for e in s.events), s.events)
    items = {i[0]: (i[3], i[4]) for i in s.eng._menu["items"]} if s.eng._menu else {}
    check("menu: has the eight items", set(items) == set(ac.MENU_ITEMS), items)
    if "browser" in items:
        cu, cv = s.eng._cursor
        bu, bv = items["browser"]
        s.clear()
        s.move((bu - cu) * s.bw, (bv - cv) * s.bh, 8, down=True, pinch="pressed", pose="pinch")
        s.hold(2, down=True, pinch="pressed", pose="pinch")
        check("menu: hovering 'browser'", s.eng._menu and s.eng._menu["hover"] == "browser",
              s.eng._menu and s.eng._menu["hover"])
        check("menu: no OS moves while choosing", "move" not in s.kinds(), s.acts)
        s.release()
        check("menu: releasing on 'browser' opens the browser", ("browser",) in s.acts, s.acts)
        check("menu: no click", "click" not in s.kinds())
        check("menu: ('menu', 'browser') event", ("menu", "browser") in s.events, s.events)
    s = Sim()
    s.start()
    s.press()
    s.hold(int(0.95 * FPS), down=True, pinch="pressed", pose="pinch")
    s.clear()
    s.move(0.0, -0.4 * s.bh, 8, down=True, pinch="pressed", pose="pinch")
    s.release()
    check("menu: releasing on empty space cancels", s.acts == [] and s.eng._menu is None, s.acts)


# ── Scrolling ────────────────────────────────────────────────────────────────
def _scroll(s, dy, dx=0.0, n=15):
    s.hold(6, pose="v")
    s.move(dx, dy, n, pose="v")
    return [a for a in s.acts if a[0] == "scroll"]


def test_scroll():
    s = Sim()
    s.start()
    s.clear()
    sc = _scroll(s, -0.2 * s.bh)
    total = sum(a[2] for a in sc)
    check("scroll: two fingers scroll", len(sc) > 3, sc)
    check("scroll: natural - hand up gives dy < 0", total < 0, total)
    check("scroll: about 2500 px per screen unit (0.2 -> ~500 px less the axis buffer)",
          380 <= -total <= 520, total)
    check("scroll: the cursor stays frozen", "move" not in s.kinds())
    s = Sim(AirSettings(precision=False, scroll_natural=False))
    s.start()
    s.clear()
    total = sum(a[2] for a in _scroll(s, -0.2 * s.bh))
    check("scroll: natural off - hand up gives dy > 0", total > 0, total)
    s = Sim(AirSettings(precision=False, scroll_speed=2.0))
    s.start()
    s.clear()
    total2 = sum(a[2] for a in _scroll(s, -0.2 * s.bh))
    check("scroll: scroll_speed 2 doubles it", total2 < -800, total2)


def test_axis_lock():
    s = Sim()
    s.start()
    s.clear()
    sc = _scroll(s, -0.25 * s.bh, dx=0.04 * s.bw, n=20)
    check("axis lock: mostly vertical locks to 'v'", s.eng._axis == "v", s.eng._axis)
    check("axis lock: dx is zero", sc and all(a[1] == 0 for a in sc), sc)
    v = s.eng.view(s.t)
    check("axis lock: view reports the axis", v.scroll_axis == "v", v.scroll_axis)


def test_inertia():
    s = Sim()
    s.start()
    s.clear()
    s.hold(6, pose="v")
    s.move(0.0, -0.3 * s.bh, 6, pose="v")       # fast: 1.5 screen/s
    s.clear()
    s.hold(1, pose="relaxed")
    check("inertia: starts when V ends fast", s.eng._inertia is not None)
    s.hold(int(2.0 * FPS), pose="relaxed")
    sc = [a for a in s.acts if a[0] == "scroll"]
    check("inertia: keeps scrolling the same way", len(sc) > 5 and all(a[2] <= 0 for a in sc), sc)
    first = sum(abs(a[2]) for a in sc[:3])
    last = sum(abs(a[2]) for a in sc[-3:])
    check("inertia: decays", last < first, (first, last))
    check("inertia: stops by itself", s.eng._inertia is None)


# ── Swipes, crush, exit ──────────────────────────────────────────────────────
def test_swipes_fake():
    s = Sim(fake_detectors=True)
    s.start()
    s.hold(4, pose="open")
    s.clear()
    s.eng._swipe.next = "left"
    s.hold(1, pose="open")
    check("swipe wiring: hand left -> Space to the right", ("space", "right") in s.acts, s.acts)
    check("swipe wiring: event with label", any(e[0] == "swipe" and e[1] == "left"
                                                 for e in s.events), s.events)
    check("swipe wiring: toast shown", s.eng.view(s.t).toast is not None)
    for d, want in (("right", ("space", "left")), ("up", ("overview",))):
        s.clear()
        s.eng._swipe.next = d
        s.hold(1, pose="open")
        check(f"swipe wiring: {d}", want in s.acts, s.acts)
    s.clear()
    s.eng._swipe.next = "down"
    s.hold(1, pose="open")
    want = ("app_windows",) if sys.platform == "darwin" else ("show_desktop",)
    check("swipe wiring: down -> app windows (mac) / show desktop", want in s.acts, s.acts)
    s = Sim(AirSettings(precision=False, space_swipes=False), fake_detectors=True)
    s.start()
    s.eng._swipe.next = "left"
    s.clear()
    s.hold(1, pose="open")
    check("swipe wiring: space_swipes off sends nothing", s.acts == [], s.acts)


def test_swipe_real():
    try:
        from plugins._gesture_core import SwipeDetector  # noqa: F401
    except Exception as e:
        skip("swipe (real detector)", f"plugins._gesture_core not importable: {e}")
        return
    s = Sim()
    s.start()
    s.hold(8, pose="open")
    s.clear()
    s.move(-0.30, 0.0, 6, pose="open")          # 0.3 of the frame in 0.2 s
    s.hold(4, pose="open")
    check("swipe: open hand left -> ('space', 'right') once",
          s.acts.count(("space", "right")) == 1, s.acts)
    s.clear()
    s.move(0.30, 0.0, 6, pose="open")           # the hand coming back
    s.hold(4, pose="open")
    check("swipe: the return stroke is ignored", not any(a[0] == "space" for a in s.acts),
          s.acts)
    # a clutch reposition: smooth (min-jerk) open-hand strokes that start and
    # end at rest, as when lifting a mouse to recentre it
    for dist, dur in ((0.30, 0.45), (0.25, 0.30), (0.23, 0.25), (0.40, 0.45)):
        s = Sim()
        s.start()
        s.hold(8, pose="open")
        s.clear()
        n = int(round(dur * FPS))
        prev = 0.0
        for i in range(1, n + 1):
            tau = i / n
            p = dist * (10 * tau ** 3 - 15 * tau ** 4 + 6 * tau ** 5)
            s.frame(x=s.h.x - (p - prev), vx=-(p - prev) / DT, vy=0.0, pose="open")
            prev = p
        s.hold(10, pose="open")
        check(f"swipe: a {dist} / {dur} s clutch reposition does not change desktop",
              not any(a[0] in ("space", "overview", "app_windows", "show_desktop")
                      for a in s.acts), s.acts)


def test_crush():
    s = Sim(fake_detectors=True)
    s.start()
    s.clear()
    s.hold(5, pose="open")
    s.hold(6, pose="fist")
    check("crush wiring: open then fist -> close_window", ("close_window",) in s.acts, s.acts)
    check("crush wiring: event", ("crush",) in s.events)
    try:
        from plugins._gesture_core import CrushDetector  # noqa: F401
    except Exception as e:
        skip("crush (real detector)", f"plugins._gesture_core not importable: {e}")
        return
    s = Sim()
    s.start()
    s.clear()
    s.hold(int(0.75 * FPS), pose="open")
    s.hold(int(1.0 * FPS), pose="fist")
    check("crush: open palm still 0.6 s, then a fist held 0.8 s -> close_window",
          s.acts.count(("close_window",)) == 1, s.acts)
    s = Sim()
    s.start()
    s.clear()
    s.hold(int(0.45 * FPS), pose="open")
    s.hold(int(1.0 * FPS), pose="fist")
    check("crush: a palm held only 0.45 s does not arm", ("close_window",) not in s.acts, s.acts)
    s = Sim()
    s.start()
    s.clear()
    s.hold(int(0.75 * FPS), pose="open")
    s.hold(int(0.5 * FPS), pose="fist")
    s.hold(5, pose="open")
    check("crush: opening before 0.8 s cancels", ("close_window",) not in s.acts, s.acts)
    # the clutch: open hand, reposition, stop, then curl the hand to rest it
    s = Sim()
    s.start()
    s.clear()
    s.hold(4, pose="open")
    s.move(0.15, 0.05, 10, pose="open")
    s.hold(int(1.0 * FPS), pose="open")
    s.hold(int(1.2 * FPS), pose="fist")
    check("crush: a clutch reposition, then a resting fist, closes nothing",
          ("close_window",) not in s.acts, s.acts)
    # after leaving the open pose, a fresh palm in place arms again
    s.hold(5, pose="relaxed")
    s.hold(int(0.75 * FPS), pose="open")
    s.hold(int(1.0 * FPS), pose="fist")
    check("crush: ...a fresh palm afterwards still works",
          s.acts.count(("close_window",)) == 1, s.acts)


def test_crush_suspended_when_typing():
    s = Sim(fake_detectors=True)
    s.start()
    s.eng.open_keyboard(s.t)
    s.clear()
    s.hold(5, pose="open")
    s.hold(6, pose="fist")
    check("crush: off while the keyboard is open", ("close_window",) not in s.acts, s.acts)


def test_exit():
    s = Sim()
    s.start()
    other = hs(slot="h1", x=0.2, y=0.6, side="left", pose="open", age=10, t=s.t)
    s.hands.append(other)
    s.clear()
    s.hold(int(0.5 * FPS), pose="open")
    v = s.eng.view(s.t)
    check("exit: progress fills", 0.3 < v.exit_progress < 0.7, v.exit_progress)
    s.hold(int(0.6 * FPS), pose="open")
    check("exit: both hands open and still 1 s -> release_all + exit",
          ("release_all",) in s.acts and ("exit", "gesture") in s.acts, s.acts)
    check("exit: event", ("exit", "gesture") in s.events)
    check("exit: status exiting", s.eng.view(s.t).status == "exiting")
    s.clear()
    s.hold(3)
    check("exit: nothing after exiting", s.acts == [])
    s = Sim()
    s.start()
    out = s.eng.request_exit("esc")
    check("request_exit: immediate", out == [("release_all",), ("exit", "esc")], out)


def test_lost_while_dragging():
    s = Sim()
    s.start()
    s.press()
    s.move(0.1 * s.bw, 0.0, 5, down=True, pinch="pressed", pose="pinch")
    s.clear()
    s.hands.clear()
    s.hold(int(0.2 * FPS))
    check("lost: nothing released before 0.35 s", ("button", False, "left") not in s.acts)
    s.hold(int(0.25 * FPS))
    check("lost: button up and release_all after 0.35 s",
          ("button", False, "left") in s.acts and ("release_all",) in s.acts, s.acts)
    check("lost: status no_hand", s.eng.view(s.t).status == "no_hand")
    check("lost: ring 'lost'", s.eng.view(s.t).ring == "lost")


def test_physical_mouse():
    s = Sim(real=True)
    s.start()
    s.move(0.1 * s.bw, 0.0, 6)
    s.hold(6)
    check("mouse: our own moves do not pause anything", s.eng.view(s.t).status == "active")
    s.real = (0.1, 0.9)          # the user grabs the trackpad
    s.clear()
    s.hold(2)
    check("mouse: real cursor moved -> paused_mouse", s.eng.view(s.t).status == "paused_mouse")
    s.move(0.2 * s.bw, 0.1 * s.bh, int(1.2 * FPS))
    check("mouse: nothing sent while paused", s.acts == [], s.acts[:5])
    s.hold(int(0.5 * FPS))
    s.clear()
    s.move(0.05 * s.bw, 0.0, 6)
    check("mouse: after 1.5 s the hand drives again", s.eng.view(s.t).status == "active"
          and "move" in s.kinds(), s.acts)
    check("mouse: resumes from where the user left the cursor",
          near(last_move(s.acts), (0.15, 0.9), 0.01), last_move(s.acts))


def test_input_blocked():
    s = Sim()
    s.start()
    s.eng.set_input_ok(False)
    s.clear()
    s.move(0.2 * s.bw, 0.0, 10)
    s.press()
    s.release()
    check("blocked: no actions while input is not OK", s.acts == [], s.acts)
    check("blocked: status", s.eng.view(s.t).status == "blocked")
    check("blocked: the view still follows the hand", s.eng.view(s.t).cursor[0] > 0.6)


# ── Text entry ───────────────────────────────────────────────────────────────
FIELD = FieldInfo(rect=(0.3, 0.3, 0.4, 0.05), caret=(0.42, 0.305, 0.002, 0.04),
                  secure=False, role="AXTextField", pid=42)


def _with_bar(s, field=FIELD):
    s.eng.set_field(field, s.t)
    s.eng.set_field(field, s.t)
    return s.eng.view(s.t).bar


def _button(bar, bid):
    for b in bar["buttons"]:
        if b[0] == bid:
            return (b[3] + b[5] / 2, b[4] + b[6] / 2)
    return None


def test_type_bar_hysteresis():
    s = Sim()
    s.start()
    s.eng.set_field(FIELD, s.t)
    check("bar: not after one focus check", s.eng.view(s.t).bar is None)
    s.eng.set_field(FIELD, s.t)
    bar = s.eng.view(s.t).bar
    check("bar: shown after two", bar is not None and bar["state"] == "idle", bar)
    check("bar: placed ABOVE the field, clear of its drop-down suggestions",
          bar and bar["y"] + bar["h"] <= FIELD.rect[1], bar and bar["y"])
    check("bar: centred on the caret", bar and abs(bar["x"] + bar["w"] / 2 - 0.421) < 0.01,
          bar and (bar["x"], bar["w"]))
    check("bar: five buttons", bar and [b[0] for b in bar["buttons"]] ==
          ["dictate", "keys", "backspace", "enter", "close"], bar and bar["buttons"])
    s.eng.set_field(None, s.t)
    s.eng.set_field(None, s.t)
    check("bar: still there after two misses", s.eng.view(s.t).bar is not None)
    s.eng.set_field(None, s.t)
    check("bar: hidden after three", s.eng.view(s.t).bar is None)
    low = FieldInfo(rect=(0.3, 0.9, 0.4, 0.05), caret=None, role="AXTextArea", pid=7)
    bar = _with_bar(s, low)
    check("bar: above a field near the bottom too", bar and bar["y"] + bar["h"] <= 0.9,
          bar and bar["y"])
    top = FieldInfo(rect=(0.3, 0.02, 0.4, 0.03), caret=None, role="AXTextField", pid=8)
    bar = _with_bar(s, top)
    check("bar: below a field with no room above it", bar and bar["y"] >= 0.05,
          bar and bar["y"])
    # only a pinch squarely on a button belongs to the bar: the background and
    # the gaps go through to the app (its own suggestion list, say)
    b0 = bar["buttons"][0]
    check("bar: a point on the bar's background is not a bar hit",
          s.eng._ui_hit(bar["x"] + 0.002, bar["y"] + 0.002) is None)
    check("bar: just outside a button is not a hit either",
          s.eng._ui_hit(b0[3] + b0[5] / 2, b0[4] + b0[6] * 1.1) is None)
    check("bar: on the button is", s.eng._ui_hit(b0[3] + b0[5] / 2, b0[4] + b0[6] / 2)
          == "bar:" + b0[0])
    s2 = Sim(AirSettings(precision=False, type_bar=False))
    check("bar: type_bar off never shows it", _with_bar(s2) is None)


def test_secure_field():
    s = Sim()
    s.start()
    bar = _with_bar(s, FieldInfo(rect=(0.3, 0.3, 0.3, 0.05), secure=True, role="AXSecureTextField",
                                 pid=9))
    check("secure: state 'secure'", bar and bar["state"] == "secure", bar)
    check("secure: only the close button", bar and [b[0] for b in bar["buttons"]] == ["close"],
          bar and bar["buttons"])
    check("secure: note asks for the real keyboard", bar and "keyboard" in bar["note"])
    s.eng.open_keyboard(s.t)
    check("secure: the air keyboard is refused", s.eng.view(s.t).keyboard is None)


def test_bar_dictate():
    s = Sim()
    s.start()
    bar = _with_bar(s)
    s.point_at(*_button(bar, "dictate"))
    s.clear()
    s.press()
    s.hold(2, down=True, pinch="pressed", pose="pinch")
    s.release()
    check("bar: a tap on Dictate toggles dictation", ("dictate", "toggle") in s.acts, s.acts)
    check("bar: a UI press clicks nothing in the OS", "click" not in s.kinds()
          and "button" not in s.kinds(), s.acts)
    check("bar: ui_press event", ("ui_press", "dictate") in s.events, s.events)
    s.hold(12)
    s.clear()
    s.press()
    s.hold(int(0.5 * FPS), down=True, pinch="pressed", pose="pinch")
    check("bar: holding Dictate starts walkie-talkie dictation",
          ("dictate", "hold_start") in s.acts, s.acts)
    s.release()
    check("bar: releasing it stops", s.acts[-1] == ("dictate", "hold_stop"), s.acts)
    check("bar: hold never toggles", ("dictate", "toggle") not in s.acts)


def test_bar_buttons():
    s = Sim()
    s.start()
    bar = _with_bar(s)
    s.point_at(*_button(bar, "enter"))
    s.clear()
    s.press()
    s.release()
    check("bar: Enter presses Enter, no click", s.acts == [("key", "enter", (), 1)]
          or (("key", "enter", (), 1) in s.acts and "click" not in s.kinds()), s.acts)
    s.hold(12)
    s.point_at(*_button(bar, "backspace"))
    s.clear()
    s.press()
    s.hold(int(1.0 * FPS), down=True, pinch="pressed", pose="pinch")
    s.release()
    n = s.acts.count(("key", "backspace", (), 1))
    check("bar: ⌫ held repeats (1 + ~8/s after 0.5 s)", 4 <= n <= 7, n)
    s.hold(12)
    s.point_at(*_button(bar, "close"))
    s.press()
    s.release()
    check("bar: ✕ hides the bar", s.eng.view(s.t).bar is None)
    s.eng.set_field(FIELD, s.t)
    check("bar: stays hidden for the same field", s.eng.view(s.t).bar is None)
    other = FieldInfo(rect=(0.1, 0.1, 0.3, 0.05), role="AXTextField", pid=43)
    _with_bar(s, other)
    check("bar: comes back for another field", s.eng.view(s.t).bar is not None)


def test_dictation_events():
    s = Sim(fake_detectors=True)
    s.start()
    _with_bar(s)
    s.eng.on_dictation("listening", {})
    bar = s.eng.view(s.t).bar
    check("dictation: bar state 'dictating'", bar and bar["state"] == "dictating", bar)
    s.eng.on_dictation("level", {"level": 0.6})
    s.eng.on_dictation("typed", {"chars": 11, "verified": True, "preview": "hello world"})
    bar = s.eng.view(s.t).bar
    check("dictation: level and preview", bar["level"] == 0.6 and bar["preview"] == "hello world",
          bar)
    s.clear()
    s.eng._swipe.next = "left"
    s.hold(1, pose="open")
    check("dictation: open swipe left undoes the last phrase", ("dictate", "undo") in s.acts
          and not any(a[0] == "space" for a in s.acts), s.acts)
    s.clear()
    s.hold(int(0.4 * FPS), pose="fist")
    check("dictation: a fist held 0.3 s stops it", s.acts.count(("dictate", "stop")) == 1, s.acts)
    check("dictation: no crush while dictating", ("close_window",) not in s.acts)
    s.eng.on_dictation("failed", {"why": "no microphone"})
    check("dictation: failure shows a toast", s.eng.view(s.t).toast[0] == "no microphone")
    s.eng.on_dictation("stopped", {"why": "user"})
    check("dictation: stopped -> idle", s.eng.view(s.t).bar["state"] == "idle")


def _key_centre(s, kid):
    for k in s.eng.view(s.t).keyboard["keys"]:
        if k[0] == kid:
            return (k[2] + k[4] / 2, k[3] + k[5] / 2)
    return None


def _tap_key(s, kid, drift_to=None):
    s.point_at(*_key_centre(s, kid))
    t_aim = s.t
    if drift_to:
        u, v = _key_centre(s, drift_to)
        cu, cv = s.eng._cursor
        s.move((u - cu) * s.bw, (v - cv) * s.bh, 3)
    s.press(t_aim=t_aim)
    s.release()
    s.hold(12)


def test_keyboard():
    s = Sim()
    s.start()
    s.eng.open_keyboard(s.t)
    kb = s.eng.view(s.t).keyboard
    check("keyboard: opens with QWERTY keys", kb and [k[0] for k in kb["keys"][:3]] ==
          ["q", "w", "e"], kb and kb["keys"][:3])
    check("keyboard: keys inside v 0.62..0.98", kb and all(0.619 <= k[3] and k[3] + k[5] <= 0.981
                                                           for k in kb["keys"]))
    s.clear()
    _tap_key(s, "h", drift_to="j")
    _tap_key(s, "i", drift_to="o")
    types_ = [a for a in s.acts if a[0] == "type"]
    check("keyboard: keys act at the rollback point despite drift onto a neighbour",
          types_ == [("type", "h"), ("type", "i")], types_)
    check("keyboard: pinches on keys never click the OS", "click" not in s.kinds(), s.acts)
    check("keyboard: prefix tracked", s.eng._prefix == "hi", s.eng._prefix)
    _tap_key(s, "shift")
    s.clear()
    _tap_key(s, "a")
    _tap_key(s, "b")
    check("keyboard: shift capitalises one letter", [a for a in s.acts if a[0] == "type"] ==
          [("type", "A"), ("type", "b")], s.acts)
    s.clear()
    _tap_key(s, "space")
    check("keyboard: space types a space and resets the word", ("type", " ") in s.acts
          and s.eng._prefix == "", (s.acts, s.eng._prefix))
    s2 = Sim(AirSettings(precision=False, keyboard_layout="azerty"))
    s2.eng.open_keyboard(s2.t)
    check("keyboard: AZERTY layout", [k[0] for k in s2.eng.view(s2.t).keyboard["keys"][:3]] ==
          ["a", "z", "e"])


class SpyWords:
    """A WordPredictor stand-in that records what it was asked to learn."""

    def __init__(self):
        self.learned = []

    def learn(self, text):
        self.learned.append(text)

    def suggest(self, prefix, n=3):
        return [prefix + "lo", prefix + "p", prefix + "ro"][:n]


def test_word_privacy():
    # dictation previews are never learned
    s = Sim(fake_detectors=True)
    s.start()
    spy = SpyWords()
    s.eng.words = spy
    _with_bar(s)
    s.eng.on_dictation("listening", {})
    s.eng.on_dictation("typed", {"chars": 20, "verified": True, "preview": "Dear Mrs Smithson"})
    check("privacy: dictated phrases are not learned", spy.learned == [], spy.learned)

    # no field: nothing learned (a terminal prompt that reports no field)
    s = Sim()
    s.start()
    spy = SpyWords()
    s.eng.words = spy
    s.eng.open_keyboard(s.t)
    for k in ("h", "u", "n", "t", "e", "r", "space"):
        _tap_key(s, k)
    check("privacy: with no text field, nothing is learned", spy.learned == [], spy.learned)

    # a normal field: space learns, Enter does not
    s = Sim()
    s.start()
    spy = SpyWords()
    s.eng.words = spy
    _with_bar(s)
    s.eng.open_keyboard(s.t)
    for k in ("h", "i", "space"):
        _tap_key(s, k)
    check("privacy: a word ended by space in a normal field is learned",
          spy.learned == ["hi"], spy.learned)
    for k in ("p", "w", "d", "enter"):
        _tap_key(s, k)
    check("privacy: a word ended by Enter is not learned (passwords, commands)",
          spy.learned == ["hi"], spy.learned)

    # the field turns out to be a password field: the letters so far are dropped
    for k in ("s", "e", "c"):
        _tap_key(s, k)
    sec = FieldInfo(rect=FIELD.rect, secure=True, role="AXTextField", pid=42)
    s.eng.set_field(sec, s.t)
    check("privacy: a secure field drops the word in progress",
          s.eng._prefix in ("", None) and s.eng.view(s.t).keyboard is None, s.eng._prefix)
    check("privacy: ...and learns nothing", spy.learned == ["hi"], spy.learned)

    # learning can be turned off
    s = Sim(AirSettings(precision=False, learn_words=False))
    s.start()
    spy = SpyWords()
    s.eng.words = spy
    _with_bar(s)
    s.eng.open_keyboard(s.t)
    for k in ("h", "i", "space"):
        _tap_key(s, k)
    check("privacy: learn_words off learns nothing", spy.learned == [], spy.learned)
    check("settings: learn_words parsed", not ac.settings_from({"learn_words": "off"}).learn_words
          and ac.settings_from({}).learn_words)


def test_stale_prefix():
    s = Sim()
    s.start()
    s.eng.words = SpyWords()
    _with_bar(s)
    s.eng.open_keyboard(s.t)
    for k in ("h", "e", "l"):
        _tap_key(s, k)
    check("prefix: 'hel' typed, suggestions up", s.eng._prefix == "hel" and s.eng._sugg,
          (s.eng._prefix, s.eng._sugg))
    s.point_at(0.5, 0.2)                 # a pinch-click somewhere else in the text
    s.press()
    s.release()
    s.hold(12)
    check("prefix: a click makes the word unknown and clears the suggestions",
          s.eng._prefix is None and s.eng._sugg == [], (s.eng._prefix, s.eng._sugg))
    s.clear()
    s.eng._ui_press(ac._Grip("ui", s.t, 0.5, 0.5, 0.5, 0.5, target="suggest:0"), s.t, s.acts)
    check("prefix: a stale suggestion never backspaces",
          not any(a[0] == "key" and a[1] == "backspace" for a in s.acts), s.acts)
    _tap_key(s, "x")
    check("prefix: letters after the click do not start a known word", s.eng._prefix is None)
    _tap_key(s, "space")
    _tap_key(s, "a")
    check("prefix: a new word after a space is tracked again", s.eng._prefix == "a",
          s.eng._prefix)

    # the caret jumps (physical keyboard, a click by mouse) while not typing
    s.hold(30)
    moved = FieldInfo(rect=FIELD.rect, caret=(0.30, 0.305, 0.002, 0.04), role="AXTextField",
                      pid=42)
    s.eng.set_field(moved, s.t)
    check("prefix: a caret jump makes the word unknown", s.eng._prefix is None, s.eng._prefix)
    other = FieldInfo(rect=(0.3, 0.5, 0.4, 0.05), role="AXTextField", pid=42)
    _tap_key(s, "space")
    _tap_key(s, "b")
    s.eng.set_field(other, s.t)
    check("prefix: another field of the same app makes it unknown too",
          s.eng._prefix is None, s.eng._prefix)


def test_keyboard_placement_and_echo():
    s = Sim()
    s.start()
    low = FieldInfo(rect=(0.2, 0.88, 0.6, 0.05), caret=(0.25, 0.885, 0.002, 0.04),
                    role="AXTextArea", pid=5)
    _with_bar(s, low)
    s.eng.open_keyboard(s.t)
    kb = s.eng.view(s.t).keyboard
    bottom = max(k[3] + k[5] for k in kb["keys"])
    check("keyboard: a field in the lower half puts the keyboard on top",
          kb["side"] == "top" and bottom < 0.5, (kb["side"], bottom))
    r = s.eng._kb_rect()
    check("keyboard: ...clear of the field", r[1] + r[3] < low.rect[1], r)
    for k in ("h", "i"):
        _tap_key(s, k)
    check("keyboard: the echo shows what was typed", s.eng.view(s.t).keyboard["echo"] == "hi",
          s.eng.view(s.t).keyboard["echo"])
    _tap_key(s, "backspace")
    check("keyboard: ⌫ takes it off the echo", s.eng.view(s.t).keyboard["echo"] == "h")
    er = s.eng.view(s.t).keyboard["echo_rect"]
    check("keyboard: echo strip between the keys and the field",
          er[1] > bottom and er[1] + er[3] < low.rect[1], er)
    s.eng.close_keyboard()
    _with_bar(s)
    s.eng.open_keyboard(s.t)
    kb = s.eng.view(s.t).keyboard
    check("keyboard: a field in the upper half keeps it at the bottom",
          kb["side"] == "bottom" and min(k[3] for k in kb["keys"]) >= 0.6, kb["side"])
    _tap_key(s, "123")
    kb = s.eng.view(s.t).keyboard
    check("keyboard: the 123 layer keeps the side", min(k[3] for k in kb["keys"]) >= 0.6)


def test_keyboard_hover_hysteresis():
    s = Sim()
    s.start()
    s.eng.open_keyboard(s.t)
    s.point_at(*_key_centre(s, "g"))
    check("keyboard: hover on the key under the cursor", s.eng._kb_hover == "g", s.eng._kb_hover)
    gu, gv = _key_centre(s, "g")
    s.point_at(gu + ac.KEY_W * 0.5 + ac.KEY_GAP + ac.KEY_W * 0.05, gv)
    check("keyboard: hover stays within 15% past the edge", s.eng._kb_hover == "g",
          s.eng._kb_hover)
    s.point_at(gu + ac.KEY_W * 0.5 + ac.KEY_GAP + ac.KEY_W * 0.3, gv)
    check("keyboard: hover switches further in", s.eng._kb_hover == "h", s.eng._kb_hover)


def test_suggestion():
    s = Sim()
    s.start()
    s.eng.open_keyboard(s.t)
    _tap_key(s, "t")
    _tap_key(s, "h")
    kb = s.eng.view(s.t).keyboard
    words = [w[0] for w in kb["suggest"]]
    check("suggest: three words for 'th'", len(words) == 3 and "the" in words, words)
    w0 = kb["suggest"][0]
    s.point_at(w0[1] + w0[3] / 2, w0[2] + w0[4] / 2)
    s.clear()
    s.press()
    s.release()
    check("suggest: tap = backspace the prefix, then type the word and a space",
          s.acts == [("key", "backspace", (), 2), ("type", w0[0] + " ")], s.acts)


def test_keyboard_swipes():
    s = Sim(fake_detectors=True)
    s.start()
    s.eng.open_keyboard(s.t)
    s.clear()
    s.eng._swipe.next = "left"
    s.hold(1, pose="open")
    check("keyboard: space swipes are suspended", not any(a[0] == "space" for a in s.acts),
          s.acts)
    s.eng._swipe.next = "down"
    s.hold(1, pose="open")
    check("keyboard: an open swipe down closes it", s.eng.view(s.t).keyboard is None)


def test_keyboard_follows_bar():
    s = Sim()
    s.start()
    bar = _with_bar(s)
    s.point_at(*_button(bar, "keys"))
    s.press()
    s.release()
    check("keyboard: the bar's ⌨ opens it", s.eng.view(s.t).keyboard is not None)
    for _ in range(3):
        s.eng.set_field(None, s.t)
    check("keyboard: closes with the bar when opened from it", s.eng.view(s.t).keyboard is None)
    s.eng.open_keyboard(s.t)
    _with_bar(s)
    for _ in range(3):
        s.eng.set_field(None, s.t)
    check("keyboard: opened from the menu, it stays", s.eng.view(s.t).keyboard is not None)


def test_view_misc():
    s = Sim()
    v = s.eng.view(s.t)
    check("view: no cursor before a hand", v.cursor is None and v.status == "no_hand")
    s.h.pts = [(0.5, 0.5)] * 21
    s.start()
    v = s.eng.view(s.t)
    check("view: active, cursor, hint, hand_pts", v.status == "active" and v.cursor is not None
          and v.hint and v.hand_pts and len(v.hand_pts) == 21 and v.hands_seen == 1, v)
    s.hold(int(10.5 * FPS))
    check("view: hint gone after 10 s", s.eng.view(s.t).hint == "")
    s.press()
    s.hold(int(0.5 * FPS), down=True, pinch="pressed", pose="pinch")
    v = s.eng.view(s.t)
    check("view: hold ring with progress", v.ring == "hold" and 0.2 < v.progress < 0.6,
          (v.ring, v.progress))


def test_hand_setting():
    s = Sim(AirSettings(precision=False, hand="left"))
    s.start()
    check("hand=left: a right hand does not drive", s.eng.view(s.t).status == "no_hand")
    s.h.side = "left"
    s.hold(3)
    check("hand=left: a left hand does", s.eng.view(s.t).status == "active")


def test_engine_never_raises():
    s = Sim()
    bad = SimpleNamespace(slot="h0", age="x", x=None, t="nan")
    try:
        out = s.eng.update([bad, None, object()], s.t + 1, real_cursor=("a", 1))
        ok = isinstance(out, list)
    except Exception as e:
        ok = False
        out = e
    check("engine: junk input never raises", ok, out)


# ── Dispatcher ───────────────────────────────────────────────────────────────
class FakePump:
    def __init__(self):
        self.moves, self.posted, self.slowed = [], [], []

    def move(self, x, y):
        self.moves.append((x, y))

    def post(self, fn, *args):
        self.posted.append((fn, args))

    def slow(self, fn, *args, done=None):
        self.slowed.append((fn, args, done))

    def run_all(self):
        for fn, args in self.posted:
            fn(*args)
        for fn, args, done in self.slowed:
            r = fn(*args)
            if done:
                done(r)
        self.posted, self.slowed = [], []


def fake_os(own=False):
    log = []
    m = types.SimpleNamespace(log=log, own=own)
    m.to_mouse = lambda u, v, mon: (mon["left"] + u * mon["width"], mon["top"] + v * mon["height"])
    m.button = lambda down, which="left": log.append(("button", down, which))
    m.click = lambda which="left", count=1: log.append(("click", which, count))
    m.scroll = lambda dx, dy: log.append(("scroll", dx, dy))
    m.release_all = lambda: log.append(("release_all",))
    m.key = lambda name, mods=(), count=1: (log.append(("key", name, mods, count)), (True, ""))[1]
    m.type_text = lambda text: (log.append(("type", text)), (True, ""))[1]
    m.close_window = lambda: (log.append(("close_window",)), (True, ""))[1]
    m.own_app_focused = lambda: m.own
    m.nav = lambda what: (log.append(("nav", what)), (True, ""))[1]
    m.space = lambda d: (log.append(("space", d)), (False, "no Spaces here"))[1]
    m.overview = lambda: (log.append(("overview",)), (True, ""))[1]
    m.app_windows = lambda: (log.append(("app_windows",)), (True, ""))[1]
    m.show_desktop = lambda: (log.append(("show_desktop",)), (True, ""))[1]
    m.launcher = lambda: (log.append(("launcher",)), (True, ""))[1]
    m.open_browser = lambda url="": (log.append(("browser",)),
                                     "status=failed action=browser reason=\"no browser\" -- x")[1]
    return m


class FakeDict:
    def __init__(self):
        self.on = False
        self.calls = []

    def active(self):
        return self.on

    def start(self, source="gesture", hold=False):
        self.calls.append(("start", source, hold))
        self.on = True
        return "status=started action=dictation"

    def stop(self, commit=True):
        self.calls.append(("stop", commit))
        self.on = False
        return "status=done action=dictation"

    def send(self):
        self.calls.append(("send",))
        return "status=done action=dictation"

    def undo_last(self):
        self.calls.append(("undo",))
        return "status=failed action=dictation reason=\"nothing to undo\" -- x"


def test_dispatcher():
    mon = {"left": 0, "top": 0, "width": 1000, "height": 500}
    pump, osm, dm, fb = FakePump(), fake_os(), FakeDict(), []
    d = Dispatcher(pump, mon, os_input_mod=osm, dictation_mod=dm, on_feedback=fb.append)
    d.run([("move", 0.5, 0.5), ("button", True, "left"), ("click", "left", 2),
           ("scroll", 3.0, -4.0), ("release_all",), ("key", "enter", (), 1), ("type", "hi"),
           ("close_window",), ("nav", "back"), ("space", "left"), ("overview",),
           ("app_windows",), ("show_desktop",), ("launcher",), ("browser",), ("exit", "x")])
    check("dispatcher: move -> pump.move in mouse space", pump.moves == [(500.0, 250.0)],
          pump.moves)
    check("dispatcher: pointer actions on the fast lane", len(pump.posted) == 7, pump.posted)
    check("dispatcher: OS shortcuts on the slow lane", len(pump.slowed) == 7, pump.slowed)
    pump.run_all()
    want = [("button", True, "left"), ("click", "left", 2), ("scroll", 3.0, -4.0),
            ("release_all",), ("key", "enter", (), 1), ("type", "hi"), ("close_window",),
            ("nav", "back"), ("space", "left"), ("overview",), ("app_windows",),
            ("show_desktop",), ("launcher",), ("browser",)]
    check("dispatcher: every action reaches os_input in order", osm.log == want, osm.log)
    check("dispatcher: failed Status and outcome lines become feedback",
          fb == ["no Spaces here", "no browser"], fb)

    pump, osm, fb = FakePump(), fake_os(own=True), []
    d = Dispatcher(pump, mon, os_input_mod=osm, dictation_mod=dm, on_feedback=fb.append)
    d.run([("type", "secret"), ("key", "a", ("cmd",), 1), ("close_window",), ("click", "left", 1)])
    pump.run_all()
    check("dispatcher: JARVIS focused blocks type, key and close_window",
          osm.log == [("click", "left", 1)], osm.log)
    check("dispatcher: ...and says why", fb.count(ac.OWN_FOCUS) == 3, fb)

    pump, osm, dm, fb = FakePump(), fake_os(), FakeDict(), []
    d = Dispatcher(pump, mon, os_input_mod=osm, dictation_mod=dm, on_feedback=fb.append)
    d.run([("dictate", "toggle")])
    pump.run_all()
    d.run([("dictate", "toggle")])
    pump.run_all()
    d.run([("dictate", "hold_start"), ("dictate", "hold_stop"), ("dictate", "send"),
           ("dictate", "undo")])
    pump.run_all()
    check("dispatcher: dictate toggle starts then stops, hold, send, undo",
          dm.calls == [("start", "gesture", False), ("stop", True), ("start", "gesture", True),
                       ("stop", True), ("send",), ("undo",)], dm.calls)
    check("dispatcher: a dictation problem is fed back", fb == ["nothing to undo"], fb)
    try:
        Dispatcher(FakePump(), mon, os_input_mod=osm, dictation_mod=dm).run(
            [("move", "x", None), (), ("nonsense",)])
        ok = True
    except Exception:
        ok = False
    check("dispatcher: bad actions never raise", ok)


# ── Words and settings ───────────────────────────────────────────────────────
def test_words():
    w = WordPredictor("en", store=False)
    s = w.suggest("th")
    check("words: prefix match, most frequent first", s[:1] == ["the"] and len(s) == 3, s)
    check("words: case follows the prefix", w.suggest("Th")[0] == "The"
          and w.suggest("TH")[0] == "THE")
    check("words: the prefix itself is not suggested", "the" not in w.suggest("the"))
    w.learn("Thermostat thermostat, thermostat!")
    check("words: learning ranks a used word first", w.suggest("th")[0] == "thermostat",
          w.suggest("th"))
    check("words: learned new words are suggested", w.suggest("therm")[0] == "thermostat")
    f = WordPredictor("fr", store=False)
    check("words: French list, accents folded", "été" in f.suggest("et") or "était" in
          f.suggest("eta"), (f.suggest("et"), f.suggest("eta")))
    check("words: empty prefix gives nothing", w.suggest("") == [])
    from plugins import _air_words
    check("words: list sizes", len(_air_words.COMMON["en"]) >= 700
          and len(_air_words.COMMON["fr"]) >= 350,
          (len(_air_words.COMMON["en"]), len(_air_words.COMMON["fr"])))
    check("words: azerty -> French", _air_words.lang_for("azerty") == "fr")


def test_settings():
    s = ac.settings_from({"pinch_sensitivity": "FIRM", "hand": "Left", "pointer_speed": "fast",
                          "precision": "false", "scroll_natural": "0", "scroll_speed": "1.5",
                          "space_swipes": "no", "type_bar": "yes", "keyboard_layout": "AZERTY",
                          "show_hands": 0, "sounds": "true"})
    check("settings: strings parsed", (s.sensitivity, s.hand, s.pointer_speed, s.precision,
                                       s.scroll_natural, s.scroll_speed, s.space_swipes,
                                       s.type_bar, s.keyboard_layout, s.show_hands, s.sounds) ==
          ("firm", "left", "fast", False, False, 1.5, False, True, "azerty", False, True), s)
    s = ac.settings_from({"scroll_speed": "9", "pointer_speed": "warp", "hand": 3})
    check("settings: out of range clamped, junk -> defaults",
          s.scroll_speed == 2.0 and s.pointer_speed == "normal" and s.hand == "any", s)
    check("settings: scroll_speed floor", ac.settings_from({"scroll_speed": "0.1"}).scroll_speed
          == 0.5)
    check("settings: not a dict", ac.settings_from(None) == AirSettings())
    try:
        import memory.config_manager as cm
        saved = cm.get_plugin_config
        cm.get_plugin_config = lambda ns: {"pointer_speed": "slow", "precision": "off"} \
            if ns == "air_control" else {}
        try:
            s = ac.load_settings()
        finally:
            cm.get_plugin_config = saved
        check("settings: load_settings reads namespace 'air_control'",
              s.pointer_speed == "slow" and s.precision is False, s)
    except Exception as e:
        skip("settings: load_settings", f"memory.config_manager not importable: {e}")
    eng = AirEngine(AirSettings(pointer_speed="fast"), SA)
    check("settings: pointer_speed sets the box", abs(eng.bw - 0.33) < 1e-9)


# ── The voice tool ───────────────────────────────────────────────────────────
class FakePlayer:
    def __init__(self, input_ok=True, opens=True, keyboard=True):
        self.calls = []
        self.keyboard = keyboard
        self.input_ok = input_ok
        self.opens = opens

    def write_log(self, text):
        pass

    def air_control(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd == "open" and self.opens:
            ac.STATE.update(open=True, since=0.0, input_ok=self.input_ok)
        elif cmd == "open":
            ac.STATE.update(error="camera: busy")
        elif cmd == "close":
            ac.STATE.update(open=False, input_ok=False)
        elif cmd == "keyboard":
            ac.STATE.update(keyboard=self.keyboard)


def tool_os(perm=None, check_ok=True):
    m = types.SimpleNamespace(requested=[])
    m.available = lambda: (True, "")
    m.permission_state = lambda: perm or {"accessibility": True, "post_events": True}
    m.request_permission = lambda: m.requested.append(1)
    m.self_check = lambda: (True, "") if check_ok else (False, "the cursor did not move")
    return m


def test_voice_tool():
    from plugins import air_control as tool
    from plugins import _camera
    saved = (tool._os_input, tool._dictation, _camera.available, tool.OPEN_WAIT,
             tool.INPUT_WAIT, tool.CLOSE_WAIT, _camera.wait_frame, _camera.last_error)
    tool.OPEN_WAIT = tool.INPUT_WAIT = tool.CLOSE_WAIT = 0.3
    _camera.available = lambda: True
    _camera.wait_frame = lambda timeout=5.0, max_age=1.0: object()
    try:
        ac.STATE.update(open=False, input_ok=False, error="")
        tool._os_input = tool_os(perm={"accessibility": False, "post_events": None})
        p = FakePlayer()
        r = tool.run({"action": "start"}, player=p)
        check("tool: start refused without the Accessibility permission",
              r.startswith("status=failed") and "Accessibility" in r and "Holo Desk" in r, r)
        check("tool: ...and the permission is requested", tool._os_input.requested == [1])
        check("tool: ...and nothing opened", p.calls == [])

        tool._os_input = tool_os()
        from plugins import _announce
        _announce._player = None
        r = tool.run({"action": "start"}, player=None)
        check("tool: start needs the window", r.startswith("status=failed"), r)

        ac.STATE.update(open=False, input_ok=False)
        p = FakePlayer()
        r = tool.run({"action": "start"}, player=p)
        check("tool: start done when open, click-through and self-check all confirm",
              r.startswith("status=done") and "cursor=verified" in r, r)
        check("tool: opened through player.air_control", p.calls == ["open"], p.calls)
        r = tool.run({"action": "status"}, player=p)
        check("tool: status says it is on", "Air Control is on" in r, r)
        r = tool.run({"action": "keyboard"}, player=p)
        check("tool: keyboard", r.startswith("status=done") and p.calls[-1] == "keyboard", r)
        p.keyboard = False
        r = tool.run({"action": "keyboard"}, player=p)
        check("tool: keyboard that never appears is not done",
              r.startswith("status=unverified"), r)
        p.keyboard = True
        r = tool.run({"action": "stop"}, player=p)
        check("tool: stop", r.startswith("status=done") and not ac.STATE["open"], r)

        _camera.wait_frame = lambda timeout=5.0, max_age=1.0: None
        _camera.last_error = lambda: "camera 0 could not be opened"
        ac.STATE.update(open=False, error="")
        r = tool.run({"action": "start"}, player=FakePlayer())
        check("tool: no camera picture -> unverified with the camera's reason, not done",
              r.startswith("status=unverified") and "camera 0 could not be opened" in r, r)
        tool.run({"action": "stop"}, player=FakePlayer())
        _camera.wait_frame = lambda timeout=5.0, max_age=1.0: object()

        tool._os_input = tool_os(check_ok=False)
        r = tool.run({"action": "start"}, player=FakePlayer())
        check("tool: unverified when the self-check fails",
              r.startswith("status=unverified") and "did not move" in r, r)
        ac.STATE.update(open=False)
        tool._os_input = tool_os()
        r = tool.run({"action": "start"}, player=FakePlayer(input_ok=False))
        check("tool: unverified when click-through is not confirmed",
              r.startswith("status=unverified"), r)
        ac.STATE.update(open=False)
        r = tool.run({"action": "start"}, player=FakePlayer(opens=False))
        check("tool: failed with the window's error when it does not open",
              r.startswith("status=failed") and "camera: busy" in r, r)

        dm = FakeDict()
        tool._dictation = dm
        r = tool.run({"action": "dictate", "op": "start"}, player=None)
        check("tool: dictate start passes through (source voice)",
              r == "status=started action=dictation" and dm.calls[-1] == ("start", "voice", False),
              (r, dm.calls))
        tool.run({"action": "dictate", "op": "send"})
        tool.run({"action": "dictate", "op": "undo"})
        tool.run({"action": "dictate", "op": "stop"})
        check("tool: dictate send / undo / stop", dm.calls[1:] == [("send",), ("undo",),
                                                                   ("stop", True)], dm.calls)
        r = tool.run({"action": "help"})
        check("tool: help lists the gestures", "pinch" in r and "two fingers" in r.lower(), r)
        r = tool.run({"action": "fly"})
        check("tool: unknown action fails politely", r.startswith("status=failed"), r)
        tool._os_input = types.SimpleNamespace()     # broken module: must not raise
        r = tool.run({"action": "start"}, player=FakePlayer())
        check("tool: never raises", r.startswith("status=failed"), r)
    finally:
        (tool._os_input, tool._dictation, _camera.available, tool.OPEN_WAIT, tool.INPUT_WAIT,
         tool.CLOSE_WAIT, _camera.wait_frame, _camera.last_error) = saved
        ac.STATE.update(open=False, input_ok=False, error="", status="")


def test_declaration():
    from plugins import air_control as tool
    from providers import schema
    import json
    import re
    decl = {k: v for k, v in tool.PLUGIN.items()}
    oa = schema.to_openai([decl])
    an = schema.to_anthropic([decl])
    blob = json.dumps(oa) + json.dumps(an)
    check("declaration: converts with no upper-case types left",
          not re.findall(r'"type":\s*"([A-Z_]+)"', blob), blob[:200])
    check("declaration: name", oa[0]["function"]["name"] == "air_control")
    keys = [f["key"] for f in tool.PLUGIN_SETTINGS["fields"]]
    check("declaration: C11 settings", keys == [
        "pinch_sensitivity", "hand", "pointer_speed", "precision", "scroll_natural",
        "scroll_speed", "space_swipes", "type_bar", "keyboard_layout", "learn_words",
        "dictation_engine",
        "show_hands", "sounds"] and tool.PLUGIN_SETTINGS["namespace"] == "air_control", keys)


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} ran", False, repr(e))
    print(f"\n{_results['pass']} passed, {_results['fail']} failed, {_results['skip']} skipped")
    return 1 if _results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
