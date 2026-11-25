"""
Self-test for the Holo Desk's logic: panels, physics and gestures.

    python -m plugins._holo_desk_tests

No camera, no window, no network. Hands are synthetic Pointer sequences fed
frame by frame at 30 fps, the way gui/holo_desk.py feeds the real ones.
(Leading underscore: the plugin loader skips this file.)
"""
from __future__ import annotations

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
    run(inp, clk, [[Pointer("mouse", g[0], g[1], True)], [Pointer("mouse", 0.5, 0.5, True)]] * 3
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
    def __init__(self, x, y, ratio, size=0.12):
        self.pinch_point = (x, y)
        self.pinch_ratio = ratio
        self.size = size
        self.angle = 0.0
        self.open_palm = ratio > 0.8
        self.pointing = False


def test_sensitivity_and_smoothing():
    print("\n[16] pinch sensitivity, speed-aware smoothing, pinch feedback")
    from plugins._holo_desk_core import HandFilter
    ident = lambda x, y: (x, y)
    loose = 0.37                         # a loose pinch: counts only when 'gentle'
    got = {}
    for sens in ("gentle", "normal", "firm"):
        f = HandFilter(sens)
        got[sens] = f.update([FakeHand(0.5, 0.5, loose)], ident, 0.0)[0].down
    check("a loose pinch counts as gentle only", got == {"gentle": True, "normal": False,
                                                         "firm": False}, got)
    # Still hand with jitter: the cursor moves much less than the jitter.
    f = HandFilter()
    t = 0.0
    f.update([FakeHand(0.5, 0.5, 1.0)], ident, t)
    xs = []
    for i in range(20):
        t += 1 / 30
        xs.append(f.update([FakeHand(0.5 + (0.004 if i % 2 else -0.004), 0.5, 1.0)], ident, t)[0].x)
    spread = max(xs[5:]) - min(xs[5:])
    check(f"still-hand jitter damped ({spread:.4f} of 0.008)", spread < 0.004, spread)
    # A fast sweep: the cursor stays close behind the hand.
    for i in range(10):
        t += 1 / 30
        p = f.update([FakeHand(0.5 + 0.06 * (i + 1), 0.5, 1.0)], ident, t)[0]
    check(f"fast hand followed closely (lag {0.5 + 0.6 - p.x:.3f})", 0.5 + 0.6 - p.x < 0.03,
          p.x)
    f = HandFilter()
    vals = [f.update([FakeHand(0.5, 0.5, r)], ident, 0.1 * i)[0].pinch
            for i, r in enumerate((1.0, 0.8, 0.6, 0.45, 0.30))]
    check("pinch closeness rises to 1 as fingers close",
          vals == sorted(vals) and vals[-1] == 1.0 and vals[0] == 0.0, vals)


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


def test_hand_filter():
    print("\n[15] hands to pointers: stable slots, pinch hysteresis, dropouts")
    from plugins._holo_desk_core import HandFilter
    f = HandFilter()
    ident = lambda x, y: (x, y)
    t = 0.0
    ps = f.update([FakeHand(0.2, 0.5, 1.0), FakeHand(0.8, 0.5, 1.0)], ident, t)
    left = next(p.pid for p in ps if p.x < 0.5)
    t += 0.03
    ps = f.update([FakeHand(0.79, 0.5, 1.0), FakeHand(0.21, 0.5, 1.0)], ident, t)  # order swapped
    check("slots follow the hands, not the list order",
          next(p.pid for p in ps if p.x < 0.5) == left, ps)
    seq = [1.0, 0.4, 0.30, 0.45, 0.48, 0.55, 0.40]
    downs = []
    for r in seq:
        t += 0.03
        downs.append(f.update([FakeHand(0.2, 0.5, r)], ident, t)[0].down)
    check("pinch: on below 0.33, stays on until above 0.5",
          downs == [False, False, True, True, True, False, False], downs)
    t += 0.03
    f.update([FakeHand(0.2, 0.5, 0.2)], ident, t)
    t += 0.05
    ps = f.update([], ident, t)
    check("a hand lost for a frame keeps its pinch", ps and ps[0].down, ps)
    t += 0.5
    check("a hand gone for good is released", f.update([], ident, t) == [])


def main() -> int:
    print("holo desk self-test")
    for fn in (test_carry_and_drop, test_throw_closes, test_short_throw_stays, test_scroll_list,
               test_tap_row, test_two_hand_resize, test_pull_nearer, test_clap_and_spread,
               test_ring, test_dock, test_point_focus_and_close_button, test_rotate_model,
               test_mouse_and_lost_hand, test_save_load, test_hand_filter,
               test_sensitivity_and_smoothing, test_hold_progress_and_last_layout):
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
