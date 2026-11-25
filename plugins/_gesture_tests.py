"""
Self-test for the shared gesture core: hand geometry, filters, pinches, poses,
swipes, the crush, and the camera -> hands stream.

    python -m plugins._gesture_tests

Standard library only: no camera, no window, no network, no models. Hands come
from plugins/_gesture_synth.make_hand() and are fed at 30 fps, one call per
camera frame, the way plugins/_hand_stream.py feeds the windows.
(Leading underscore: the plugin loader skips this file.)
"""
from __future__ import annotations

import math
import random
import sys
import threading
import time
from types import SimpleNamespace

from plugins._gesture_core import (N_ON, POSES, PROFILES, SENSITIVITY, CrushDetector,
                                   HandFilter, HandState, OneEuro, PinchDetector,
                                   PoseClassifier, SwipeDetector, features)
from plugins._gesture_synth import make_hand

_ok = _fail = 0
DT = 1 / 30
IDENT = lambda x, y: (x, y)   # noqa: E731


def check(name: str, cond, detail="") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


def feed(f: HandFilter, frames, t0: float = 100.0, to_screen=IDENT):
    """frames: list of hand lists. Returns (t, states) per frame."""
    out, t = [], t0
    for hands in frames:
        t += DT
        out.append((t, f.update(hands, t, to_screen)))
    return out


def first(states):
    return states[0] if states else None


class FakeHand:
    """The old test stand-in: no landmarks at all."""

    def __init__(self, x, y, ratio, size=0.12):
        self.pinch_point = (x, y)
        self.pinch_ratio = ratio
        self.size = size
        self.angle = 0.0
        self.open_palm = ratio > 0.8
        self.pointing = False


# ─────────────────────────────────────────────────────────────────────────────
def test_aspect_regression():
    print("\n[A1] an open finger is not a pinch any more (the 16:9 bug)")
    h = make_hand("open", gap=0.5, aspect=16 / 9)
    old = math.dist(h.pts[4], h.pts[8]) / math.dist(h.pts[0], h.pts[9])
    f = features(h)
    check(f"old 2D ratio read the 0.5 gap as {old:.2f} (< 0.33: counted as a pinch)", old < 0.33,
          old)
    check(f"Hand.pinch_ratio is square now ({h.pinch_ratio:.2f} >= 0.42)", h.pinch_ratio >= 0.42,
          h.pinch_ratio)
    check(f"features().r agrees ({f.r:.2f} >= 0.42)", f.r >= 0.42 and f.valid3d, f)
    turned = features(make_hand("open", gap=0.45, yaw=70))
    check(f"side-on hand: the metric gap wins (r={turned.r:.2f}, picture {turned.r_img:.2f})",
          turned.r_world is not None and turned.r >= 0.44 and turned.r_img < turned.r, turned)
    check("Hand.anchor is where make_hand put it",
          max(abs(a - b) for a, b in zip(h.anchor, (0.5, 0.6))) < 1e-9, h.anchor)
    check("Hand.extended uses joint angles (fist curled, open straight)",
          make_hand("open").open_palm and not make_hand("fist").open_palm
          and make_hand("point").pointing)


def test_false_pinches():
    print("\n[B] what must never press")
    rnd = random.Random(7)
    f = HandFilter("normal", "desk")
    frames = [[make_hand("open", gap=0.5 + rnd.uniform(-0.06, 0.06),
                         x=0.5 + rnd.uniform(-0.003, 0.003), y=0.6 + rnd.uniform(-0.003, 0.003))]
              for _ in range(60)]
    downs = [s.down for _, ss in feed(f, frames) for s in ss]
    check("open finger at 0.5 +/- 0.06 palms: never pressed (60 frames)",
          downs and not any(downs), downs.count(True))
    f = HandFilter("normal", "desk")
    downs = [s.down for _, ss in feed(f, [[make_hand("open", gap=0.45, yaw=70)]] * 60) for s in ss]
    check("gap 0.45 turned 70 deg side-on: never pressed", downs and not any(downs),
          downs.count(True))
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("fist")]] * 60)
    downs = [s.down for _, ss in res for s in ss]
    check("fist: never pressed", downs and not any(downs), downs.count(True))
    early = [first(ss).pose for _, ss in res[:4] if ss]
    check("fist: voted 'fist' within 4 frames", early and early[0] == "fist", early)


def test_real_pinch():
    print("\n[B2] a real pinch presses quickly")
    f = HandFilter("normal", "desk")
    gaps = [1.0] * 10 + [0.6, 0.3] + [0.12] * 8
    res = feed(f, [[make_hand("pinch", gap=g)] for g in gaps])
    reach = gaps.index(0.12)
    pressed = next((i for i, (_, ss) in enumerate(res) if ss and ss[0].down), None)
    check(f"pressed {None if pressed is None else pressed - reach} frame(s) after reaching 0.12",
          pressed is not None and 0 <= pressed - reach <= 2, pressed)
    st = res[-1][1][0]
    check("pinch pose and closeness 1 while pressed", st.pose == "pinch" and st.closeness == 1.0, st)
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("pinch_mid", gap=1.0)]] * 8 + [[make_hand("pinch_mid", gap=0.12)]] * 5)
    st = res[-1][1][0]
    check("thumb on middle tip: pinch_mid pressed, index pinch not",
          st.pinch_mid == "pressed" and st.pose == "pinch_mid" and not st.down, st)


def test_pinch_detector_rule():
    print("\n[C] pinch rule: median, deliberate close, release")
    on, off = SENSITIVITY["normal"]
    d = PinchDetector(on, off)
    t, states = 10.0, []
    for r in [1.0] * 5 + [0.1] + [1.0] * 3:
        t += DT
        states.append(d.update(r, True, 0.97, t))
    check("a one-frame dip to 0.1 does not press", "pressed" not in states, states)
    d = PinchDetector(on, off)
    for r in [1.0] * 3 + [0.12] * 5:
        t += DT
        d.update(r, True, 0.97, t)
    check("held at 0.12 after a close: pressed", d.state == "pressed", d.state)
    t += DT
    d.update(0.8, True, 0.97, t)
    check("a one-frame spike to 0.8 does not release", d.state == "pressed", d.state)
    for _ in range(3):                  # let the spike leave the 3-frame median
        t += DT
        d.update(0.12, True, 0.97, t)
    t += DT
    first_frame = d.update(0.8, True, 0.97, t)
    t += DT
    second = d.update(0.8, True, 0.97, t)
    check("two frames at 0.8 release (on the second)",
          first_frame == "pressed" and second == "open" and d.t_release == t, (first_frame, second))
    d = PinchDetector(on, off)
    for r in [1.0] * 3 + [0.12] * 5:
        t += DT
        d.update(r, False, 0.97, t)
    check("wrong hand shape: no press", d.state != "pressed", d.state)
    d = PinchDetector(on, off)
    for r in [1.0] * 3:
        t += DT
        d.update(r, True, 0.97, t)
    for r in [0.12] * 5:
        t += DT
        d.update(r, True, 0.5, t)
    check("unsure landmarks (conf 0.5): no state change", d.state == "open", d.state)
    check("... but the closeness still shows the fingers closing", d.closeness == 1.0 or
          d.closeness > 0.9, d.closeness)
    lag = {}
    for sens in ("normal", "firm"):
        d = PinchDetector(*SENSITIVITY[sens], n_on=N_ON[sens])
        seq = [d.update(r, True, 0.97, t + i * DT) for i, r in enumerate([1.0] * 3 + [0.12] * 4)]
        lag[sens] = seq.index("pressed") if "pressed" in seq else None
    check(f"'firm' wants one more frame under ON ({lag})",
          lag["normal"] is not None and lag["firm"] == lag["normal"] + 1, lag)

    # a steady, slightly open hand with landmark jitter around ON: one noisy
    # frame above the median must not count as "the fingers came together"
    on, off = SENSITIVITY["normal"]
    pressed = 0
    for base in (on + 0.01, on + 0.02, on + 0.03):
        for sd in (0.02, 0.03):
            for seed in range(40):
                rnd = random.Random(seed)
                d = PinchDetector(on, off)
                for i in range(90):
                    if d.update(base + rnd.gauss(0, sd), True, 0.95, 10.0 + i * DT) == "pressed":
                        pressed += 1
                        break
    check(f"steady hand at ON+0.01..0.03, noise sd 0.02-0.03: no press ({pressed}/240 runs)",
          pressed <= 2, pressed)

    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("pinch", gap=0.12)]] * 20)
    check("a hand that appears already pinched does not press",
          not any(s.down for _, ss in res for s in ss))
    res = feed(f, [[make_hand("pinch", gap=1.0)]] * 5 + [[make_hand("pinch", gap=0.12)]] * 4,
               t0=res[-1][0])
    check("... until it opens and closes again", res[-1][1] and res[-1][1][0].down, res[-1])


def test_click_drift():
    print("\n[D] the click lands where you aimed")
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("pinch", gap=0.9)]] * 20)
    pre = res[-1][1][0]
    res = feed(f, [[make_hand("pinch", gap=g)] for g in (0.45, 0.25) + (0.12,) * 6], t0=res[-1][0])
    moved = max(math.hypot(ss[0].x - pre.x, ss[0].y - pre.y) for _, ss in res)
    st = res[-1][1][0]
    check(f"cursor still while the fingertips close (moved {moved:.4f})", moved < 0.003, moved)
    check("pressed", st.down, st)
    check(f"press point = the pre-pinch cursor ({st.press_x - pre.x:+.4f}, {st.press_y - pre.y:+.4f})",
          abs(st.press_x - pre.x) < 0.003 and abs(st.press_y - pre.y) < 0.003, st)


def test_retro_press():
    print("\n[E] the press is dated back to the start of the approach")
    f = HandFilter("normal", "air")
    gaps = [1.0] * 8 + [0.8, 0.65, 0.5, 0.45, 0.4, 0.36, 0.33, 0.28, 0.2, 0.12, 0.12, 0.12]
    xs, t, st_press = {}, 100.0, None
    for i, g in enumerate(gaps):
        t += DT
        ss = f.update([make_hand("pinch", gap=g, x=0.3 + 0.005 * i)], t)
        if ss:
            xs[round(ss[0].t, 6)] = ss[0].x
            if ss[0].down and st_press is None:
                st_press = ss[0]
    slot = f.slots["h0"]
    ok = st_press is not None
    check("pressed", ok)
    if ok:
        check(f"aim dated to t_armed ({st_press.t_press - st_press.t_aim:.3f} s before the press)",
              abs(st_press.t_aim - slot.pinch.t_armed) < 1e-9 and st_press.t_aim < st_press.t_press,
              (st_press.t_aim, slot.pinch.t_armed))
        check("press_x is the cursor at that moment",
              abs(st_press.press_x - xs.get(round(st_press.t_aim, 6), -1)) < 1e-9,
              (st_press.press_x, xs.get(round(st_press.t_aim, 6))))
        check("the cursor stays on the press point (LOCK)", st_press.x == st_press.press_x)


def test_lock_unlock():
    print("\n[F] lock on the press point, unlock when the hand really moves")
    f = HandFilter("normal", "air")
    res = feed(f, [[make_hand("pinch", gap=1.0)]] * 8 + [[make_hand("pinch", gap=0.12)]] * 2)
    st = res[-1][1][0]
    check("pressed", st.down, st)
    t = res[-1][0]
    small = feed(f, [[make_hand("pinch", gap=0.12, x=0.508)]] * 2, t0=t)
    check("a small wobble inside LOCK stays on the press point",
          all(ss[0].x == st.press_x for _, ss in small), [ss[0].x for _, ss in small])
    t = small[-1][0]
    big = feed(f, [[make_hand("pinch", gap=0.12, x=0.53)]] * 8, t0=t)
    last = big[-1][1][0]
    out = f.slots[last.slot].out
    check(f"moving 0.03 unlocks and the cursor catches up (gap {abs(last.x - out[0]):.4f})",
          last.x - st.press_x > 0.015 and abs(last.x - out[0]) < 0.002, (last.x, out))


def test_slots():
    print("\n[G] slots follow hands")
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("open", x=0.2), make_hand("open", x=0.8, side="left")]] * 5)
    left = next(s.slot for s in res[-1][1] if s.x < 0.5)
    res = feed(f, [[make_hand("open", x=0.79, side="left"), make_hand("open", x=0.21)]], t0=res[-1][0])
    check("swapped list order keeps the slots", next(s.slot for s in res[-1][1] if s.x < 0.5) == left,
          res[-1][1])
    f = HandFilter("normal", "air")
    res = feed(f, [[make_hand("relaxed", x=0.44, side="right"),
                    make_hand("relaxed", x=0.56, side="left")]] * 12)
    right = next(s.slot for s in res[-1][1] if s.side == "right")
    res = feed(f, [[make_hand("relaxed", x=0.53, side="right"),
                    make_hand("relaxed", x=0.47, side="left")]], t0=res[-1][0])
    after = {s.slot: s for s in res[-1][1]}
    check("hands crossing keep their slots (side penalty)",
          right in after and after[right].side == "right", after)
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("open")]] * 5)
    seen = [len(ss) for _, ss in res]
    check("a new hand is silent for 3 frames", seen == [0, 0, 0, 1, 1], seen)
    check("its age counts frames since birth", res[3][1][0].age == 3, res[3][1][0].age)


def test_lost():
    print("\n[H] a hand that drops out")
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("pinch", gap=1.0)]] * 8 + [[make_hand("pinch", gap=0.12)]] * 3)
    t = res[-1][0]
    check("pressed before the dropout", res[-1][1][0].down)
    held = [f.update([], t + d) for d in (0.1, 0.2, 0.3, 0.38)]
    check("pressed hand held as lost + down for LOST_DOWN",
          all(ss and ss[0].lost and ss[0].down for ss in held), held)
    check("... then dropped", f.update([], t + 0.45) == [] and not f.slots)
    f = HandFilter("normal", "desk")
    res = feed(f, [[make_hand("open")]] * 6)
    t, name = res[-1][0], res[-1][1][0].slot
    check("an open hand that vanishes shows nothing", f.update([], t + 0.1) == [])
    back = f.update([make_hand("open")], t + 0.2)
    check("back within LOST: same slot, no birth delay", back and back[0].slot == name, back)
    f.update([], t + 0.3)
    f.update([], t + 0.5)
    check("gone for more than LOST: the slot is freed", not f.slots, list(f.slots))
    again = f.update([make_hand("open")], t + 0.55)
    check("... and a returning hand is new (silent)", again == [], again)


def test_cache():
    print("\n[I] one update per camera frame")
    f = HandFilter()
    a = f.update([make_hand("open")], 5.0)
    b = f.update([make_hand("open", x=0.9)], 5.0)
    check("same t returns the cached list unchanged", a is b and f.states is a)
    st = HandState(slot="h1")
    check("HandState defaults", st.pose == "none" and st.pinch == "open" and not st.lost)
    check("contract constants", POSES[0] == "none" and set(PROFILES) == {"desk", "air"}
          and HandFilter.BIRTH_FRAMES == 3 and HandFilter.LOST == 0.25)


def test_one_euro():
    print("\n[J] One Euro filter (desk parameters)")
    p = PROFILES["desk"]
    f = OneEuro(p["min_cutoff"], p["beta"], p["d_cutoff"])
    t, outs = 0.0, []
    for i in range(30):
        t += DT
        outs.append(f(0.5 + (0.004 if i % 2 else -0.004), t))
    spread = max(outs[10:]) - min(outs[10:])
    check(f"+/-0.004 jitter at 30 Hz -> spread {spread:.4f} (< 0.002)", spread < 0.002, spread)
    x = 0.5
    for i in range(15):
        t += DT
        x += 1.8 * DT
        v = f(x, t)
    check(f"1.8 u/s sweep lags {x - v:.4f} (< 0.03)", x - v < 0.03, x - v)
    check(f"derivative tracks the speed ({f.dx:.2f} u/s)", 1.2 < f.dx < 2.2, f.dx)
    f.reset()
    check("reset forgets", f.value is None and f(0.2, 1.0) == 0.2)


def test_poses():
    print("\n[K] poses, voted")
    for name in ("v", "point", "open", "relaxed", "fist"):
        f = HandFilter()
        res = feed(f, [[make_hand(name)]] * 6)
        got = res[-1][1][0].pose if res[-1][1] else None
        check(f"{name} is recognised", got == name, got)
    f = HandFilter()
    frames = [[make_hand("open")]] * 8 + [[make_hand("fist")]] + [[make_hand("open")]] * 4
    poses = [ss[0].pose for _, ss in feed(f, frames) if ss]
    check("a one-frame flicker is ignored", set(poses) == {"open"}, poses)
    pc = PoseClassifier()
    fake = SimpleNamespace(fingers=("ext",) * 4, index_reach=2.0, spread_deg=20.0, conf=1.0)
    pc.update(fake, "open", "open", 1.0)
    check("pinch bypasses the vote", pc.update(fake, "pressed", "open", 1.1) == "pinch"
          and pc.since == 1.1)


def _swipe(sd, xs, ys, pose, t):
    hits = []
    for x, y in zip(xs, ys):
        t += DT
        r = sd.update(x, y, pose, t)
        if r:
            hits.append(r)
    return hits, t


def _line(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_swipes():
    print("\n[L] swipes")
    sd = SwipeDetector()
    hits, t = _swipe(sd, _line(0.7, 0.4, 7), [0.5] * 7, "open", 0.0)
    back, t = _swipe(sd, _line(0.4, 0.7, 7), [0.5] * 7, "open", t)
    check("open swipe left fires 'left' once", hits == ["left"], hits)
    check("the hand coming back is ignored", back == [], back)
    sd = SwipeDetector()
    hits, _ = _swipe(sd, _line(0.2, 0.8, 60), [0.5] * 60, "open", 0.0)
    check("slow drift does not fire", hits == [], hits)
    sd = SwipeDetector()
    hits, _ = _swipe(sd, _line(0.7, 0.4, 7), [0.5] * 7, "relaxed", 0.0)
    check("a relaxed hand does not swipe", hits == [], hits)
    sd = SwipeDetector()
    hits, _ = _swipe(sd, [0.5] * 7, _line(0.7, 0.4, 7), "open", 0.0)
    check("upwards is 'up'", hits == ["up"], hits)
    sd = SwipeDetector()
    h1, t = _swipe(sd, _line(0.7, 0.4, 7), [0.5] * 7, "open", 0.0)
    _, t = _swipe(sd, [0.7] * 3, [0.5] * 3, "open", t)
    h2, t = _swipe(sd, _line(0.7, 0.4, 7), [0.5] * 7, "open", t)
    _, t = _swipe(sd, [0.7] * 20, [0.5] * 20, "open", t)
    h3, t = _swipe(sd, _line(0.7, 0.4, 7), [0.5] * 7, "open", t)
    check("cooldown: a second swipe too soon is ignored, later it fires",
          h1 == ["left"] and h2 == [] and h3 == ["left"], (h1, h2, h3))


def _crush(cd, seq, t, x=0.5, y=0.5, speed=0.0):
    out = []
    for pose in seq:
        t += DT
        out.append(cd.update(pose, x, y, speed, t))
    return out, t


def test_crush():
    print("\n[M] crush: open palm, then a fist")
    cd = CrushDetector()
    res, t = _crush(cd, ["open"] * 11, 0.0)
    phases = [p for p, _ in res]
    check("arming shows progress, then armed", phases[-1] == "armed" and 0 < res[5][1] < 1
          and phases[0] == "idle", res)
    res, t = _crush(cd, ["fist"] * 10, t)
    phases = [p for p, _ in res]
    check("fist: closing then fired exactly once",
          phases[0] == "closing" and phases.count("fired") == 1 and phases[-1] == "idle", phases)
    cd = CrushDetector()
    _, t = _crush(cd, ["open"] * 11 + ["fist"] * 3, 0.0)
    res, t = _crush(cd, ["open", "open"], t)
    check("opening the hand cancels", res[0][0] == "cancelled" and res[1][0] == "idle", res)
    cd = CrushDetector()
    res, t = _crush(cd, ["open"] * 40, 0.0)
    phases = [p for p, _ in res]
    check("the window expires without a fist",
          "armed" in phases and phases.count("cancelled") == 1, phases)
    check("... and a resting open hand does not re-arm",
          "cancelled" in phases and "armed" not in phases[phases.index("cancelled"):], phases)
    cd = CrushDetector()
    _, t = _crush(cd, ["open"] * 11, 0.0)
    res, t = _crush(cd, ["open"], t, x=0.58)
    check("too much travel cancels", res[0][0] == "cancelled", res)
    cd = CrushDetector()
    _, t = _crush(cd, ["open"] * 11, 0.0)
    res, t = _crush(cd, ["pinch"], t)
    check("a pinch while armed cancels", res[0][0] == "cancelled", res)
    cd = CrushDetector()
    res, t = _crush(cd, ["open"] * 20, 0.0, speed=0.5)
    check("a moving open hand never arms", all(p == "idle" for p, _ in res), res)
    cd = CrushDetector(confirm=0.5)
    _, t = _crush(cd, ["open"] * 11, 0.0)
    res, t = _crush(cd, ["fist"] * 10, t)
    check("confirm=0.5 (Air Control) needs longer", all(p == "closing" for p, _ in res), res)


def test_crush_end_to_end():
    print("\n[M2] crush through the real chain: synthetic hand -> HandFilter -> CrushDetector")
    for n_close, index_last in ((2, False), (6, False), (10, False), (14, False),
                                (10, True)):
        f = HandFilter("normal", "desk")
        cd = CrushDetector(confirm=0.25)
        frames = [[make_hand("open")]] * 15
        frames += [[make_hand(closing=(i + 1) / n_close, index_last=index_last)]
                   for i in range(n_close)]
        frames += [[make_hand("fist")]] * 15
        phases, poses = [], []
        for t, ss in feed(f, frames):
            if not ss:
                continue
            st = ss[0]
            poses.append(st.pose)
            phases.append(cd.update(st.pose, st.x, st.y, math.hypot(st.vx, st.vy), t)[0])
        label = f"{n_close} frames{', index last' if index_last else ''}"
        check(f"an open hand closing into a fist over {label} fires",
              phases.count("fired") == 1, list(zip(poses, phases))[12:])


def test_fake_hand():
    print("\n[N] objects without landmarks still work")
    f = HandFilter()
    fh = FakeHand(0.4, 0.5, 1.0)
    feat = features(fh)
    check("features fall back", not feat.valid3d and feat.anchor == (0.4, 0.5)
          and feat.fingers == ("ext",) * 4, feat)
    res = feed(f, [[FakeHand(0.4, 0.5, 1.0)]] * 6 + [[FakeHand(0.4, 0.5, 0.2)]] * 3)
    st = res[-1][1][0]
    check("a FakeHand pinch presses", st.down and st.pts is None and abs(st.x - 0.4) < 0.01, st)
    check("garbage does not raise", isinstance(f.update([object(), None], 200.0), list))
    check("features(None) does not raise", features(None).r == 1.0)


class _Frame:
    def __init__(self, i, w=320, h=180):
        self.i = i
        self.shape = (h, w, 3)


class _FakeCam:
    """The parts of plugins._camera the stream uses. Each frame is served for
    two polls, like a 30 fps camera read at 60 Hz."""

    def __init__(self, n):
        self.frames = [_Frame(i) for i in range(n)]
        self.i, self.calls = -1, 0
        self.subs: set = set()
        self.lock = threading.Lock()

    def subscribe(self, name):
        self.subs.add(name)
        return True

    def unsubscribe(self, name):
        self.subs.discard(name)

    def wait_frame(self, timeout=5.0, max_age=1.0):
        return self.frames[0]

    def last_error(self):
        return ""

    def latest_stamped(self, max_age=1.0):
        with self.lock:
            self.calls += 1
            if self.calls % 2 == 1 and self.i < len(self.frames) - 1:
                self.i += 1
            return self.frames[self.i], 100.0 + self.i / 30


class _FakeTracker:
    max_hands = 2

    def process(self, frame):
        if frame.i == 2:
            raise ValueError("synthetic tracker failure")
        return [f"hand{frame.i}"]


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_hand_stream():
    print("\n[O] the camera -> hands stream")
    from plugins._hand_stream import LOADING, HandStream
    cam, got, status = _FakeCam(6), [], []
    hs = HandStream("test", lambda img, hands, t: got.append((img, hands, t)),
                    want_picture=lambda: len(got) % 2 == 0, on_status=status.append,
                    tracker_factory=_FakeTracker, camera=cam)
    hs.start()
    _wait(lambda: len(got) >= 6)
    time.sleep(0.1)
    hs.stop()
    hs.stop()
    ended = hs.join(2.0)
    check("one on_frame per new frame", len(got) == 6, len(got))
    check("with the capture time", [round(t, 6) for _, _, t in got]
          == [round(100.0 + i / 30, 6) for i in range(6)], [t for _, _, t in got])
    check("the picture only when wanted", all((img is not None) == (k % 2 == 0)
                                              for k, (img, _, _) in enumerate(got)),
          [img is not None for img, _, _ in got])
    check("a tracker error gives no hands and the loop goes on",
          got[2][1] == [] and got[3][1] == ["hand3"], [h for _, h, _ in got])
    check("loading status, then ready", status == [LOADING, ""], status)
    check("stop() ends the thread and leaves the camera", ended and not hs.running
          and cam.subs == set(), cam.subs)

    cam, status = _FakeCam(4), []

    def broken():
        raise OSError("no model file")
    got2 = []
    hs = HandStream("t2", lambda img, hands, t: got2.append(hands), on_status=status.append,
                    tracker_factory=broken, camera=cam)
    hs.start()
    _wait(lambda: len(got2) >= 4)
    hs.stop()
    hs.join(2.0)
    check("models that fail to load: 'hands:' status, frames still flow",
          any(s.startswith("hands:") for s in status) and len(got2) == 4
          and all(h == [] for h in got2), (status, got2))

    cam = _FakeCam(10)

    def gone(img, hands, t):
        raise RuntimeError("wrapped C/C++ object has been deleted")
    hs = HandStream("t3", gone, tracker_factory=_FakeTracker, camera=cam)
    hs.start()
    check("a deleted receiver ends the loop quietly", hs.join(2.0) and cam.subs == set())
    hs = HandStream("t4", lambda *a: None, tracker_factory=_FakeTracker, camera=cam)
    hs.set_max_hands(1)
    check("set_max_hands before start is kept", hs.max_hands == 1)
    hs.stop()


def test_camera_stamp():
    print("\n[P] _camera.latest_stamped")
    from plugins import _camera
    saved = (_camera._frame, _camera._frame_at, _camera._frame_mono)
    try:
        frame = object()
        _camera._frame, _camera._frame_at, _camera._frame_mono = frame, time.time(), 123.5
        f, stamp = _camera.latest_stamped(1.0)
        check("fresh frame with its monotonic stamp", f is frame and stamp == 123.5, (f, stamp))
        _camera._frame_at = time.time() - 5.0
        check("stale frame: (None, 0.0)", _camera.latest_stamped(1.0) == (None, 0.0))
        _camera._frame = None
        check("no frame: (None, 0.0)", _camera.latest_stamped() == (None, 0.0))
    finally:
        _camera._frame, _camera._frame_at, _camera._frame_mono = saved


def main() -> int:
    print("gesture core self-test")
    for fn in (test_aspect_regression, test_false_pinches, test_real_pinch,
               test_pinch_detector_rule, test_click_drift, test_retro_press, test_lock_unlock,
               test_slots, test_lost, test_cache, test_one_euro, test_poses, test_swipes,
               test_crush, test_crush_end_to_end, test_fake_hand, test_hand_stream,
               test_camera_stamp):
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
