"""
Self-test for the Holo Desk's logic: panels, physics and gestures.

    python -m plugins._holo_desk_tests

No camera, no window, no network. Hands are synthetic Pointer sequences fed
frame by frame at 30 fps, the way gui/holo_desk.py feeds the real ones.
(Leading underscore: the plugin loader skips this file.)

Tests [15], [16] and the crush tests need plugins/_gesture_core.py (stdlib
only); they print SKIP if it cannot be imported. They check behaviour (presses
within a few frames, no press from a one-frame spike), not exact frame
indices: plugins/_gesture_tests.py pins those down.
"""
from __future__ import annotations

import math
import sys

from plugins._holo_desk_core import (DOCK, HEADER, Desk, Input, Pointer)

_ok = _fail = 0


def check(name: str, cond, detail="") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def setup():
    clk = Clock()
    desk = Desk(clock=clk)
    return clk, desk, Input(desk)


def run(inp, clk, frames, dt=1 / 30):
    """frames: list of pointer lists. Returns every event."""
    out = []
    for ps in frames:
        clk.t += dt
        out += inp.update(ps, clk.t)
        out += inp.desk.step(dt)
    return out


def hand(x, y, down, pid="h0", size=0.12, angle=0.0, open_palm=False, pointing=False):
    return Pointer(pid, x, y, down, size=size, angle=angle, open_palm=open_palm,
                   pointing=pointing)


def line(a, b, n):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1))
            for i in range(n)]


def test_carry_and_drop():
    print("\n[1] pinch the title bar, carry, let go")
    clk, desk, inp = setup()
    p = desk.add("clock")
    grab = (p.x + 0.05, p.y + HEADER / 2)
    path = line(grab, (0.6, 0.5), 30)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(0.6, 0.5, True)]] * 8
             + [[hand(0.6, 0.5, False)]])
    check("grab event", ("grab", p.pid) in ev, ev)
    check("panel followed the hand", abs(p.x + 0.05 - 0.6) < 0.01 and abs(p.y + HEADER / 2 - 0.5) < 0.01,
          (p.x, p.y))
    check("slow release is a drop, not a throw", ("drop", p.pid) in ev and p.vx == 0, ev)


def test_throw_closes():
    print("\n[2] fling it off the screen")
    clk, desk, inp = setup()
    p = desk.add("weather", arg="Paris")
    grab = (p.x + 0.1, p.y + 0.02)
    path = line(grab, (grab[0] + 0.35, grab[1]), 6)        # fast: 0.35 in 0.2 s
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], False)]])
    check("thrown", ("throw", p.pid) in ev, ev)
    ev = run(inp, clk, [[]] * 60)
    check("off screen -> closed", ("closed", p.pid) in ev and desk.get(p.pid) is None, ev)
    run(inp, clk, [[]] * 15)
    check("removed after the fade", p not in desk.panels)


def test_short_throw_stays():
    print("\n[3] a gentle push slides and stops on screen")
    clk, desk, inp = setup()
    p = desk.add("text", text="hello")
    p.x, p.y = 0.3, 0.3
    grab = (p.x + 0.1, p.y + 0.02)
    path = line(grab, (grab[0] + 0.12, grab[1]), 4)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], False)]])
    run(inp, clk, [[]] * 90)
    check("thrown then stopped on screen", ("throw", p.pid) in ev and desk.get(p.pid) is not None
          and p.vx == 0 and 0 <= p.x <= 1 - p.w * 0.5, (p.x, ev))


def test_scroll_list():
    print("\n[4] pinch inside a list and move to scroll")
    clk, desk, inp = setup()
    p = desk.add("news", arg="tech")
    p.rows = [(f"headline {i}", f"https://example.com/{i}") for i in range(30)]
    start = (p.x + 0.1, p.y + 0.3)
    path = line(start, (start[0], start[1] - 0.2), 15)
    run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], False)]])
    check("scrolled about 6 rows", 5 <= p.scroll <= 7, p.scroll)
    check("the panel did not move", abs(p.y - 0.06) < 0.05, p.y)


def test_diagonal_list_pull():
    print("\n[4b] a diagonal pinch-drag on a list card always does something")
    clk, desk, inp = setup()
    p = desk.add("news", arg="tech")
    p.rows = [(f"headline {i}", f"https://example.com/{i}") for i in range(30)]
    x0, y0 = p.x, p.y
    start = (p.x + 0.1, p.y + 0.3)
    path = line(start, (start[0] + 0.12, start[1] - 0.10), 10)     # |dx| = 1.2 |dy|
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], True)]] * 30
             + [[hand(*path[-1], False)]])
    moved = math.hypot(p.x - x0, p.y - y0) > 0.05
    check("a 1.2:1 diagonal pull carries the card (no silent 'decide')",
          moved or p.scroll > 0.5, ((p.x - x0, p.y - y0), p.scroll, ev))


def test_tap_row():
    print("\n[5] a quick pinch on a headline presses it")
    clk, desk, inp = setup()
    p = desk.add("news")
    p.rows = [(f"headline {i}", f"https://example.com/{i}") for i in range(10)]
    y = p.y + HEADER + 0.01 + Input.ROW_H * 2.5
    ev = run(inp, clk, [[hand(p.x + 0.1, y, True)]] * 3 + [[hand(p.x + 0.1, y, False)]])
    check("row 2 pressed", ("row", p.pid, 2) in ev, ev)


def test_two_hand_resize():
    print("\n[6] both hands on one panel, pull apart")
    clk, desk, inp = setup()
    p = desk.add("image", arg="x.png")
    p.x, p.y = 0.3, 0.3
    w0 = p.w
    a0, b0 = (0.35, 0.32), (0.55, 0.32)
    frames = [[hand(*a0, True, "h0")]] * 3 + [[hand(*a0, True, "h0"), hand(*b0, True, "h1")]] * 3
    for i in range(15):
        frames.append([hand(a0[0] - 0.005 * i, a0[1], True, "h0"),
                       hand(b0[0] + 0.005 * i, b0[1], True, "h1")])
    ev = run(inp, clk, frames + [[hand(0.28, 0.32, False, "h0"), hand(0.62, 0.32, False, "h1")]])
    check("resize events", any(e[0] == "resize" for e in ev), ev[:5])
    # hands went from 0.20 apart to 0.34 apart
    check("1.7x wider, like the hands' distance", 1.65 < p.w / w0 < 1.75, p.w / w0)


def test_resize_from_the_x():
    print("\n[6b] a pinch on the x, then the other hand: a resize, never a close")
    clk, desk, inp = setup()
    p = desk.add("image", arg="x.png")
    p.x, p.y = 0.3, 0.3
    cx, cy = p.close_center()
    other = (p.x + 0.05, p.y + 0.1)
    w0 = p.w
    frames = [[hand(cx, cy, True, "h0")]] * 2
    for i in range(6):
        frames.append([hand(cx, cy, True, "h0"), hand(other[0] - 0.01 * i, other[1], True, "h1")])
    frames.append([hand(cx, cy, False, "h0"), hand(other[0] - 0.05, other[1], True, "h1")])
    frames.append([hand(other[0] - 0.05, other[1], False, "h1")])
    ev = run(inp, clk, frames)
    check("the card is not closed by letting go of the x hand",
          ("closed", p.pid) not in ev and desk.get(p.pid) is not None, ev)
    check("and it was resized", any(e[0] == "resize" for e in ev) or abs(p.w - w0) > 1e-6,
          (p.w, w0, ev))

    clk, desk, inp = setup()
    p = desk.add("image", arg="x.png")
    p.x, p.y = 0.3, 0.3
    cx, cy = p.close_center()
    other = (p.x + 0.05, p.y + 0.1)
    frames = [[hand(cx, cy, True, "h0")]] * 2 + [[hand(cx, cy, True, "h0"),
                                                   hand(*other, True, "h1")]] * 3
    for i in range(10):
        frames.append([hand(cx + 0.01 * i, cy, True, "h0"), hand(*other, True, "h1")])
    ev = run(inp, clk, frames)
    check("moving the x hand keeps resizing (it does not turn into a carry)",
          all(g.mode == "resize" for g in inp.grips.values()) and len(inp.grips) == 2,
          {k: g.mode for k, g in inp.grips.items()})


def test_pull_nearer():
    print("\n[7] a held card brought nearer grows")
    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    w0 = p.w
    g = (p.x + 0.05, p.y + 0.02)
    frames = [[hand(*g, True, size=0.12 + 0.004 * i)] for i in range(12)]
    run(inp, clk, frames + [[hand(*g, False, size=0.17)]])
    check("grew when the hand came nearer", p.w > w0 * 1.3, p.w / w0)


def test_clap_and_spread():
    print("\n[8] clap closes everything, pulling apart brings it back")
    clk, desk, inp = setup()
    for k in ("clock", "system", "notes"):
        desk.add(k)
    frames = []
    for i in range(8):                                   # hands come together fast
        d = 0.35 - 0.04 * i
        frames.append([hand(0.5 - d / 2, 0.5, False, "h0", open_palm=True),
                       hand(0.5 + d / 2, 0.5, False, "h1", open_palm=True)])
    ev = run(inp, clk, frames)
    check("clap put 3 panels away", ("clap", 3) in ev and not desk.live(), ev)
    frames = [[hand(0.47, 0.5, False, "h0", open_palm=True),
               hand(0.53, 0.5, False, "h1", open_palm=True)]] * 20
    for i in range(12):
        d = 0.06 + 0.035 * i
        frames.append([hand(0.5 - d / 2, 0.5, False, "h0", open_palm=True),
                       hand(0.5 + d / 2, 0.5, False, "h1", open_palm=True)])
    ev = run(inp, clk, frames)
    check("spread brought them back", ("restore", 3) in ev and len(desk.live()) == 3, ev)


def test_ring():
    print("\n[9] pinch and hold on empty space opens the ring")
    clk, desk, inp = setup()
    c = (0.5, 0.6)
    ev = run(inp, clk, [[hand(*c, True)]] * 25)
    check("ring opened", any(e[0] == "ring" for e in ev) and inp.ring is not None, ev)
    target = next(it for it in inp.ring["items"] if it[0] == "weather")
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in line(c, target[1:], 6)]
             + [[hand(*target[1:], False)]])
    added = [desk.get(e[1]) for e in ev if e[0] == "added"]
    check("released on 'weather' adds a weather panel", added and added[0].kind == "weather", ev)
    check("ring closed", inp.ring is None)


def test_dock():
    print("\n[10] the dock: a quick pinch adds, pulling up carries it out")
    clk, desk, inp = setup()
    items = {k: (x, y) for k, x, y in Input.dock_items()}
    check("dock has every kind", list(items) == list(DOCK))
    ev = run(inp, clk, [[hand(*items["notes"], True)]] * 3 + [[hand(*items["notes"], False)]])
    check("tap on Notes adds a notes panel", ("press", "notes") in ev
          and desk.live()[-1].kind == "notes", ev)
    start = items["clock"]
    path = line(start, (0.4, 0.4), 12)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(0.4, 0.4, False)]])
    clock = [p for p in desk.live() if p.kind == "clock"]
    check("pulled out a clock that followed the hand",
          clock and clock[0].contains(0.4, 0.4) and any(e[0] == "grab" for e in ev), ev)


def test_point_focus_and_close_button():
    print("\n[11] pointing makes 'this'; the x closes")
    clk, desk, inp = setup()
    a = desk.add("clock")
    b = desk.add("text", text="b")
    ev = run(inp, clk, [[hand(a.x + 0.05, a.y + 0.1, False, pointing=True)]] * 2)
    check("pointed panel is 'this'", desk.current() is a and ("focus", a.pid) in ev, ev)
    cx, cy = b.x + b.w - HEADER / 2, b.y + HEADER / 2
    ev = run(inp, clk, [[hand(cx, cy, True)]] * 2 + [[hand(cx, cy, False)]])
    check("x button closed it", ("closed", b.pid) in ev and desk.get(b.pid) is None, ev)


def test_rotate_model():
    print("\n[12] turn the hand while holding the model")
    clk, desk, inp = setup()
    m = desk.add("model")
    yaw0 = m.yaw
    g = (m.x + 0.05, m.y + 0.02)
    run(inp, clk, [[hand(*g, True, angle=0.05 * i)] for i in range(10)] + [[hand(*g, False)]])
    check("yaw changed with the hand", abs(m.yaw - yaw0 - 0.45 * 1.6) < 0.05, m.yaw - yaw0)


def test_mouse_and_lost_hand():
    print("\n[13] the mouse works like a hand; a hand that vanishes lets go")
    clk, desk, inp = setup()
    p = desk.add("system")
    g = (p.x + 0.05, p.y + 0.02)
    # (A steady carry: a mouse that teleports back and forth every frame
    # would read as a throw now that the release speed skips the last 50 ms.)
    run(inp, clk, [[Pointer("mouse", g[0], g[1], True)]] + [[Pointer("mouse", 0.5, 0.5, True)]] * 8
        + [[Pointer("mouse", 0.5, 0.5, False)]])
    check("mouse carried it", p.contains(0.5, 0.5))
    run(inp, clk, [[hand(0.51, 0.51, True)]] * 3 + [[]])
    check("lost hand released the panel", not p.held and not inp.grips)


def test_save_load():
    print("\n[14] save and load a desk")
    clk, desk, inp = setup()
    desk.add("weather", arg="Paris")
    desk.add("text", title="Recipe", text="eggs")
    saved = desk.to_list()
    d2 = Desk(clock=clk)
    n = d2.load_list(saved + [{"kind": "bogus"}])
    check("2 panels back, the bogus one skipped", n == 2 and d2.live()[0].arg == "Paris", saved)
    old = [{"kind": "clock", "title": "", "text": "", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}]
    check("old saved desks still load", d2.load_list(old) == 1)


class FakeHand:
    """A tracked hand without landmarks: gesture_core reads pinch_ratio and
    pinch_point directly (its documented fallback), so these tests check the
    desk's wrapper and the filter's behaviour, not landmark geometry."""

    def __init__(self, x, y, ratio, size=0.12):
        self.pinch_point = (x, y)
        self.anchor = (x, y)
        self.pinch_ratio = ratio
        self.size = size
        self.angle = 0.0
        self.open_palm = ratio > 0.8
        self.pointing = False
        self.conf = 1.0
        self.side = ""


def _gesture_core():
    """plugins/_gesture_core.py, or None (it ships in another work package)."""
    try:
        from plugins import _gesture_core
        return _gesture_core
    except Exception as e:
        print(f"  SKIP  plugins._gesture_core is not importable: {e}")
        return None


IDENT = lambda x, y: (x, y)          # noqa: E731
FPS = 1 / 30


class Feed:
    """One HandFilter fed one camera frame at a time (30 fps)."""

    def __init__(self, sens="normal"):
        from plugins._holo_desk_core import HandFilter
        self.f = HandFilter(sens)
        self.t = 0.0

    def __call__(self, hands):
        self.t += FPS
        return self.f.update(hands, IDENT, self.t)

    def one(self, r, x=0.2, y=0.5):
        ps = self([FakeHand(x, y, r)])
        return ps[0] if ps else None

    def warm(self, n=5, x=0.2, y=0.5):
        for _ in range(n):
            self.one(1.0, x, y)

    def press(self):
        """Warm up open, then close to 0.2 until pressed; frames it took."""
        self.warm()
        for i in range(8):
            p = self.one(0.2)
            if p is not None and p.down:
                return i + 1
        return 99


def test_hand_filter():
    print("\n[15] hands to pointers: stable slots, a debounced pinch, dropouts")
    if _gesture_core() is None:
        return
    f = Feed()
    for _ in range(5):
        ps = f([FakeHand(0.2, 0.5, 1.0), FakeHand(0.8, 0.5, 1.0)])
    check("two hands give two pointers", len(ps) == 2, ps)
    left = next((p.pid for p in ps if p.x < 0.5), None)
    ps = f([FakeHand(0.79, 0.5, 1.0), FakeHand(0.21, 0.5, 1.0)])        # order swapped
    check("slots follow the hands, not the list order",
          left is not None and next((p.pid for p in ps if p.x < 0.5), None) == left, ps)
    check("pointers carry pose / state / conf",
          all(isinstance(p.pose, str) and p.state in ("open", "armed", "pressed") for p in ps), ps)

    f = Feed()
    n = f.press()
    check(f"r held at 0.2 presses within 4 frames (took {n})", n <= 4, n)
    downs = [f.one(0.40).down for _ in range(6)]
    check("0.40 keeps the pinch (hysteresis)", all(downs), downs)
    rel = [f.one(0.5).down for _ in range(4)]
    check("r >= 0.5 lets go within 4 frames", not rel[-1], rel)

    f = Feed()
    f.press()
    rel = [f.one(0.9).down for _ in range(2)]
    check("wide open (0.9) lets go within 2 frames", not rel[-1], rel)

    f = Feed()
    f.warm()
    downs = [f.one(r).down for r in (0.1, 1.0, 1.0, 1.0, 1.0)]
    check("a one-frame 0.1 spike does not press", not any(downs), downs)

    f = Feed()
    f.press()
    f.t += 0.05 - FPS
    ps = f([])
    check("a hand lost for a frame keeps its pinch", ps and ps[0].down, ps)
    f.t += 0.5 - FPS
    ps = f([])
    check("a hand gone for 0.5 s is released", not any(p.down for p in ps), ps)


def test_sensitivity_and_smoothing():
    print("\n[16] pinch sensitivity, steady cursor, pinch feedback")
    if _gesture_core() is None:
        return
    got = {}
    for sens in ("gentle", "normal", "firm"):
        f = Feed(sens)
        f.one(1.0)
        got[sens] = any(p is not None and p.down for p in (f.one(0.34) for _ in range(6)))
    check("r = 0.34 counts as a pinch with 'gentle' only",
          got == {"gentle": True, "normal": False, "firm": False}, got)
    check("PINCH_ON / PINCH_OFF mirror the filter", (Feed("normal").f.PINCH_ON,
                                                      Feed("normal").f.PINCH_OFF) == (0.30, 0.45))

    f = Feed()
    f.warm(x=0.5)
    xs = []
    for i in range(20):
        p = f.one(1.0, x=0.5 + (0.004 if i % 2 else -0.004))
        xs.append(p.x)
    spread = max(xs[5:]) - min(xs[5:])
    check(f"still-hand jitter damped ({spread:.4f} of 0.008)", spread < 0.004, spread)
    for i in range(10):
        p = f.one(1.0, x=0.5 + 0.06 * (i + 1))
    check(f"fast hand followed closely (lag {0.5 + 0.6 - p.x:.3f})", 0.5 + 0.6 - p.x < 0.03,
          p.x)

    f = Feed()
    f.warm(4)
    vals = [f.one(r).pinch for r in (1.0, 0.8, 0.6, 0.45, 0.3, 0.2, 0.2, 0.2, 0.2)]
    check("pinch closeness never falls as the fingers close, and reaches 1",
          all(b >= a - 1e-9 for a, b in zip(vals, vals[1:])) and vals[-1] == 1.0, vals)

def test_hold_progress_and_last_layout():
    print("\n[17] ring hold progress; the last layout comes back")
    clk, desk, inp = setup()
    run(inp, clk, [[hand(0.5, 0.6, True)]] * 12)
    prog = inp.hold_progress(clk.t)
    check("hold progress grows towards 1", prog and 0.4 < prog[0][2] < 0.8, prog)
    import tempfile
    from pathlib import Path
    from plugins import _announce, _holo_desk_core as core
    store = {}
    saved_load, saved_save = _announce.load, _announce.save
    _announce.load = lambda name, default=None: store.get(name, default)
    _announce.save = lambda name, data: store.__setitem__(name, data)
    try:
        d1 = Desk(clock=clk)
        d1.add("weather", arg="Oslo")
        d1.add("text", title="Todo", text="x")
        d1.add("clock")
        d1.clear()
        d1.add("system")
        store["holo_desk"] = {"desks": {"keep": []}}
        check("saved", core.save_last(d1) == "")
        d2 = Desk(clock=clk)
        n = core.load_last(d2)
        check("4 panels back: 1 shown, 3 put away (clap them back)",
              n == 4 and [p.kind for p in d2.live()] == ["system"] and len(d2.hidden) == 3,
              (n, [p.kind for p in d2.live()], len(d2.hidden)))
        check("restore brings back the put-away ones with their settings",
              d2.restore() == 3 and any(p.arg == "Oslo" for p in d2.live()))
        check("named desks are kept", "keep" in store["holo_desk"]["desks"])
        ids = [p.pid for p in d2.live()]
        check("panel ids stay unique", len(ids) == len(set(ids)), ids)
    finally:
        _announce.load, _announce.save = saved_load, saved_save


def gesture(x, y, pose, pid="h0", down=False, size=0.12, vx=0.0, vy=0.0):
    """A hand pointer with a voted pose, the way HandFilter makes them."""
    return Pointer(pid, x, y, down, size=size, pose=pose, vx=vx, vy=vy,
                   open_palm=pose == "open" and not down, pointing=pose == "point" and not down,
                   state="pressed" if down else "open")


def held(n_seconds, ps, dt=1 / 30):
    """The same pointer list for n seconds of frames."""
    return [ps] * (int(round(n_seconds / dt)) + 1)


def crush(x, y, open_s=0.35, fist_s=0.40):
    return held(open_s, [gesture(x, y, "open")]) + held(fist_s, [gesture(x, y, "fist")])


def test_crush_closes():
    print("\n[18] crush: open hand still over a card, then a fist, closes it")
    if _gesture_core() is None:
        return
    clk, desk, inp = setup()
    p = desk.add("text", text="crush me")
    c = (p.x + p.w / 2, p.y + p.h / 2)
    ev = run(inp, clk, held(0.35, [gesture(*c, "open")]))
    marks = inp.crush_marks(clk.t)
    check("armed after the open hand held still", ("armed", p.pid) in ev
          and marks and marks[0]["panel"] == p.pid and marks[0]["phase"] == "armed", (ev, marks))
    ev = run(inp, clk, held(0.12, [gesture(*c, "fist")]))
    marks = inp.crush_marks(clk.t)
    check("closing shows progress", marks and marks[0]["phase"] == "closing"
          and 0 < marks[0]["progress"] < 1 and desk.get(p.pid) is not None, marks)
    ev = run(inp, clk, held(0.3, [gesture(*c, "fist")]))
    check("the fist held closes it: crushed + closed",
          ("crushed", p.pid) in ev and ("closed", p.pid) in ev and desk.get(p.pid) is None, ev)
    check("nothing left to draw", not inp.crush_marks(clk.t), inp.crush_marks(clk.t))


def test_undo_chip():
    print("\n[19] the Undo chip appears after a close; a quick pinch on it reopens")
    if _gesture_core() is None:
        return
    clk, desk, inp = setup()
    p = desk.add("text", text="keep me")
    c = (p.x + p.w / 2, p.y + p.h / 2)
    run(inp, clk, crush(*c) + [[]])
    chip = inp.undo_chip(clk.t)
    check("chip shown with its time left",
          chip and chip["label"] == "Undo close" and 7 < chip["left"] <= Desk.TRASH_S, chip)
    cx, cy = chip["x"] + chip["w"] / 2, chip["y"] + chip["h"] / 2
    ev = run(inp, clk, [[hand(cx, cy, False)]] * 2 + [[hand(cx, cy, True)]] * 3
             + [[hand(cx, cy, False)]])
    check("pinching the chip reopens the card", ("reopened", p.pid) in ev
          and desk.get(p.pid) is p and desk.focused == p.pid, ev)
    check("the chip is gone", inp.undo_chip(clk.t) is None)
    # A chip left alone fades after TRASH_S.
    desk.close(p.pid)
    run(inp, clk, [[]])
    check("chip back after another close", inp.undo_chip(clk.t) is not None)
    clk.t += Desk.TRASH_S + 0.1
    check("and gone after TRASH_S", inp.undo_chip(clk.t) is None)


def test_crush_cancel_and_misses():
    print("\n[20] crush cancelled by opening; over empty space or a lone fist does nothing")
    if _gesture_core() is None:
        return
    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    c = (p.x + p.w / 2, p.y + p.h / 2)
    ev = run(inp, clk, held(0.35, [gesture(*c, "open")]) + held(0.1, [gesture(*c, "fist")])
             + held(0.5, [gesture(*c, "open")]))
    check("opening the hand while closing cancels", ("crushed", p.pid) not in ev
          and desk.get(p.pid) is not None, ev)

    clk, desk, inp = setup()
    p = desk.add("clock")
    spot = (0.9, 0.7)
    check("(test setup: the spot is empty)", desk.at(*spot) is None)
    ev = run(inp, clk, crush(*spot))
    check("crush over empty space does nothing",
          not any(e[0] in ("armed", "crushed", "closed") for e in ev) and desk.get(p.pid), ev)

    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    c = (p.x + p.w / 2, p.y + p.h / 2)
    ev = run(inp, clk, held(1.2, [gesture(*c, "fist")]))
    check("a fist alone never closes", not any(e[0] in ("crushed", "closed") for e in ev)
          and desk.get(p.pid) is not None, ev)

    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    ev = run(inp, clk, crush(p.x + p.w / 2, p.y + p.h / 2)[:5]
             + [[Pointer("mouse", 0.5, 0.5, False)]] * 30)
    check("the mouse never crushes", desk.get(p.pid) is not None, ev)


def test_v_scroll():
    print("\n[21] two fingers (V) over a list scroll it")
    clk, desk, inp = setup()
    p = desk.add("news", arg="tech")
    p.rows = [(f"headline {i}", "") for i in range(30)]
    start = (p.x + 0.1, p.y + 0.3)
    path = line(start, (start[0], start[1] - 0.2), 15)
    ev = run(inp, clk, held(0.2, [gesture(*start, "v")])
             + [[gesture(x, y, "v")] for x, y in path])
    check("scroll event once", ev.count(("scroll", p.pid)) == 1, ev)
    check("scrolled about 6 rows", 5 <= p.scroll <= 7, p.scroll)
    check("v_scroll names the panel", inp.v_scroll.get("h0") == p.pid, inp.v_scroll)
    before = p.scroll
    run(inp, clk, [[gesture(*start, "relaxed")]] * 3)
    check("leaving V ends it", not inp.v_scroll and p.scroll == before, (inp.v_scroll, p.scroll))
    check("the panel did not move", not p.held and abs(p.y - 0.085) < 0.05, p.y)


def test_hover_focus():
    print("\n[22] a hand resting over a card for 0.4 s makes it 'this'")
    clk, desk, inp = setup()
    a = desk.add("clock")
    b = desk.add("text", text="b")
    check("(setup) the newest card is 'this'", desk.current() is b)
    c = (a.x + a.w / 2, a.y + a.h / 2)
    check("(setup) the spot is on a", desk.at(*c) is a)
    ev = run(inp, clk, held(0.3, [gesture(*c, "relaxed")]))
    check("not yet after 0.3 s", desk.current() is b and ("focus", a.pid) not in ev, ev)
    ev = run(inp, clk, held(0.15, [gesture(*c, "relaxed")]))
    check("focused after 0.4 s, once", desk.current() is a and ev.count(("focus", a.pid)) == 1, ev)


def test_grab_anywhere():
    print("\n[23] pinch anywhere on a card carries it; a list decides scroll or carry")
    clk, desk, inp = setup()
    p = desk.add("text", text="hello")
    p.x, p.y = 0.3, 0.3
    g = (p.x + 0.1, p.y + 0.06)                   # below the title bar
    path = line(g, (0.6, 0.6), 12)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(0.6, 0.6, True)]] * 8
             + [[hand(0.6, 0.6, False)]])
    check("a pinch 0.06 below the top carries it", ("grab", p.pid) in ev
          and abs(p.x + 0.1 - 0.6) < 0.01 and abs(p.y + 0.06 - 0.6) < 0.01, (p.x, p.y, ev))

    clk, desk, inp = setup()
    n = desk.add("news")
    n.rows = [(f"headline {i}", "") for i in range(30)]
    x0, y0 = n.x, n.y
    s = (n.x + 0.1, n.y + 0.3)
    path = line(s, (s[0] + 0.12, s[1]), 10)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], True)]] * 8
             + [[hand(*path[-1], False)]])
    check("a list pulled sideways is carried", ("grab", n.pid) in ev
          and abs(n.x - x0 - 0.12) < 0.01 and n.scroll == 0, (n.x - x0, n.scroll, ev))
    x1 = n.x
    s = (n.x + 0.1, n.y + 0.3)
    path = line(s, (s[0], s[1] - 0.1), 10)
    ev = run(inp, clk, [[hand(x, y, True)] for x, y in path] + [[hand(*path[-1], False)]])
    check("pulled up and down it scrolls", ("grab", n.pid) not in ev and n.scroll > 2
          and abs(n.x - x1) < 1e-9 and abs(n.y - y0) < 0.03, (n.scroll, n.x, ev))
    s = (n.x + 0.1, n.y + 0.3)
    ev = run(inp, clk, held(0.45, [hand(*s, True)]) + [[hand(*s, False)]])
    check("held still on a list it is picked up", ("grab", n.pid) in ev, ev)


def test_close_hit_and_magnet():
    print("\n[24] the x has a bigger hit area; the cursor snaps onto nearby buttons")
    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    p.x, p.y = 0.3, 0.3
    cx, cy = p.close_center()
    ev = run(inp, clk, [[hand(cx - 0.02, cy + 0.02, True)]] * 2 + [[hand(cx - 0.02, cy + 0.02,
                                                                         False)]])
    check("a tap 0.02 off the x closes the card", ("closed", p.pid) in ev, ev)
    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    p.x, p.y = 0.3, 0.3
    cx, cy = p.close_center()
    ev = run(inp, clk, [[hand(cx - 0.025, cy, True)]] * 2 + [[hand(cx - 0.025, cy, False)]])
    check("... and 0.025 off (outside the old area)", ("closed", p.pid) in ev, ev)

    clk, desk, inp = setup()
    items = {k: (x, y) for k, x, y in Input.dock_items()}
    nx, ny = items["notes"]
    near = (nx, ny - 0.03)
    run(inp, clk, [[hand(*near, False)]] * 2)
    check("a hand 0.03 from a dock button lights it",
          inp.hover["h0"][2] == "dock:notes", inp.hover)
    cur = inp.cursor_at("h0")
    check("cursor_at is pulled toward it",
          cur and abs(cur[1] - (near[1] + 0.6 * 0.03)) < 1e-6 and cur[0] == nx, cur)
    run(inp, clk, [[hand(nx, ny - 0.045, False)]])
    check("it stays lit a little further out (no flicker)",
          inp.hover["h0"][2] == "dock:notes", inp.hover)
    run(inp, clk, [[hand(nx, ny - 0.06, False)]])
    check("and lets go beyond that", inp.hover["h0"][2] != "dock:notes"
          and inp.cursor_at("h0") == (nx, ny - 0.06), inp.hover)
    ev = run(inp, clk, [[hand(*near, False)]] + [[hand(*near, True)]] * 3
             + [[hand(*near, False)]])
    check("a pinch there presses the lit button", ("press", "notes") in ev, ev)
    check("cursor_at(unknown) is None", inp.cursor_at("h9") is None)

    clk, desk, inp = setup()
    p = desk.add("text", text="x")
    p.x, p.y = 0.3, 0.3
    cx, cy = p.close_center()
    spot = (cx - 0.033, cy)                        # outside the x's square, inside the magnet
    check("(setup) the spot is not on the x itself", not p.on_close(*spot))
    ev = run(inp, clk, [[hand(*spot, False)]] * 2 + [[hand(*spot, True)]] * 2
             + [[hand(*spot, False)]])
    check("the magnet lit the x, and the pinch closed the card",
          ("closed", p.pid) in ev, ev)


def test_release_jitter():
    print("\n[25] opening the fingers to let go does not throw the card")
    for jump in (0.04, 0.17):
        clk, desk, inp = setup()
        p = desk.add("text", text="x")
        p.x, p.y = 0.3, 0.3
        g = (p.x + 0.1, p.y + 0.02)
        ev = run(inp, clk, [[hand(*g, True)]] * 15 + [[hand(g[0] + jump, g[1], True)]]
                 + [[hand(g[0] + jump, g[1], False)]])
        check(f"still hold, last frame jumps {jump}: a drop, not a throw",
              ("drop", p.pid) in ev and ("throw", p.pid) not in ev and p.vx == 0, ev)


def test_reopen():
    print("\n[26] Desk.reopen brings back a closed card")
    clk, desk, inp = setup()
    a = desk.add("text", title="A", text="a")
    b = desk.add("clock")
    desk.close(a.pid)
    run(inp, clk, [[]] * 15)
    check("(setup) closed and faded out", a not in desk.panels and desk.trash[-1][0] is a)
    got = desk.reopen()
    check("reopen() brings the newest back, focused", got is a and desk.get(a.pid) is a
          and a.gone == 0 and desk.focused == a.pid and not desk.trash, got)
    check("nothing more to reopen", desk.reopen() is None)
    desk.close(b.pid)
    desk.close(a.pid)
    check("reopen(pid) picks that one", desk.reopen(b.pid) is b and desk.get(a.pid) is None)
    for i in range(7):
        desk.close(desk.add("text", text=str(i)).pid)
    check("the trash keeps the last 5", len(desk.trash) == 5, len(desk.trash))
    # thrown off the screen, it comes back fully on it
    clk, desk, inp = setup()
    p = desk.add("weather", arg="Paris")
    p.vx = 6.0
    ev = run(inp, clk, [[]] * 40)
    check("(setup) thrown off", ("closed", p.pid) in ev and desk.trash)
    got = desk.reopen()
    check("a thrown card comes back on screen", got is p and 0 <= p.x <= 1 - p.w
          and p.vx == 0, (p.x, p.vx))


def test_voice_tool():
    print("\n[27] the voice tool: reopen, and 'close this' goes on the undo stack")
    from core import undo
    from plugins import _holo_desk_core as hcore
    from plugins import holo_desk as tool
    clk = Clock()
    saved = hcore._shared, dict(hcore.STATE), undo.push_undo
    pushed = []
    hcore._shared = Desk(clock=clk)
    hcore.STATE["open"] = True
    undo.push_undo = lambda label, fn: pushed.append((label, fn))
    try:
        desk = hcore.shared()
        p = desk.add("text", title="Recipe", text="eggs")
        r = tool.run({"action": "this", "op": "close"})
        check("'close this' closed it", desk.get(p.pid) is None and "Closed" in r, r)
        check("and pushed an undo", len(pushed) == 1 and "Recipe" in pushed[0][0], pushed)
        r = pushed[0][1]() if pushed else ""
        check("the undo brings it back", desk.get(p.pid) is p and "Brought back" in r, r)
        desk.close(p.pid)
        r = tool.run({"action": "reopen"})
        check("'reopen' brings back the last closed card",
              r.startswith("status=done") and desk.get(p.pid) is p, r)
        r = tool.run({"action": "reopen"})
        check("'reopen' with nothing closed fails", r.startswith("status=failed")
              and "no card was closed recently" in r, r)
        check("the description points to air_control", "air_control" in tool.PLUGIN["description"])
    finally:
        hcore._shared = saved[0]
        hcore.STATE.clear()
        hcore.STATE.update(saved[1])
        undo.push_undo = saved[2]

def main() -> int:
    print("holo desk self-test")
    for fn in (test_carry_and_drop, test_throw_closes, test_short_throw_stays, test_scroll_list,
               test_diagonal_list_pull, test_resize_from_the_x,
               test_tap_row, test_two_hand_resize, test_pull_nearer, test_clap_and_spread,
               test_ring, test_dock, test_point_focus_and_close_button, test_rotate_model,
               test_mouse_and_lost_hand, test_save_load, test_hand_filter,
               test_sensitivity_and_smoothing, test_hold_progress_and_last_layout,
               test_crush_closes, test_undo_chip, test_crush_cancel_and_misses, test_v_scroll,
               test_hover_focus, test_grab_anywhere, test_close_hit_and_magnet,
               test_release_jitter, test_reopen, test_voice_tool):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, e)
    print(f"\n{_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
