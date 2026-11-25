"""
plugins/_gesture_core.py — tracked hands -> steady cursors, robust pinches, poses.

Helper module (leading underscore: the plugin loader skips it). Shared by the
Holo Desk (plugins/_holo_desk_core.py wraps HandFilter(profile="desk")) and Air
Control (plugins/_air_control_core.py, profile="air"). Standard library only and
Qt-free, so it is tested without a camera: python -m plugins._gesture_tests.

Why each piece looks the way it does (all measured on the old code):

- Distances are 3D and in SQUARE units. Hand.pts are (x/w, y/h) of a 16:9
  frame, so a 2D ratio on them shrank every horizontal gap by h/w = 0.5625: the
  old "pinched" threshold of 0.33 fired at a real gap of ~0.59 palm sizes, i.e.
  on a clearly open finger. `features()` uses iso points (x, y/aspect, 0.6*z)
  and, when the model's world landmarks (metres) look sane, takes
  r = max(r_world, r_img): a hand turned side-on foreshortens the picture gap
  but not the metric one, so turning the hand can no longer fake a pinch.
- A pinch is never decided from one frame. PinchDetector takes the median of
  the last 3 ratios, wants a deliberate closing motion (the SMOOTHED gap must
  have been `came_delta` wider, and nearly open - at least `came_from` - within
  the last 0.5 s, so a hand that appears already pinched does not click and a
  steady, slightly open hand whose landmarks jitter around ON does not either;
  comparing raw peaks with the median let one noisy frame fake the "close"),
  the thumb at the index TIP (not along its side) and
  an index that is not tucked into a fist, and ignores frames the landmark
  model is unsure about.
- The cursor is the knuckle anchor (mean of the four finger bases), which does
  not move when the fingers close. The desk profile adds the fingertip offset
  but freezes it while the fingers are closing, and the press is retro-dated
  to where the hand was aiming when the approach started (`t_armed`).
- The filter runs once per CAMERA frame (a repeated t returns the cached
  result); a One Euro filter whose speed term is the derivative of the raw
  samples (the old filter used its own residual error, so the cursor moved in
  steps) plus a tiny dead zone for a hand held still.
- Poses only change when the new one wins 3 of the last 4 frames; each finger
  has its own hysteresis band. Pinches bypass voting (they are already
  debounced and must be immediate).
- Slots are matched on the anchor with a penalty for the other hand's side,
  so two hands crossing keep their slots.

Times are seconds (time.monotonic() in the app). Picture coords: 0..1 of the
camera frame, x mirrored. Nothing here raises across the module boundary.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from statistics import median
from typing import Callable, Optional

POSES = ("none", "relaxed", "point", "v", "open", "fist", "pinch", "pinch_mid")
# (ON, OFF) thresholds on the fused ratio r (thumb-index gap / palm length).
SENSITIVITY = {"gentle": (0.36, 0.52), "normal": (0.30, 0.45), "firm": (0.24, 0.38)}
N_ON = {"gentle": 1, "normal": 1, "firm": 2}
# Thumb-index gap (palm lengths, 3D) that counts as tip contact whatever the
# joint distances say: see features().
TIGHT_PINCH = 0.24
PROFILES = {
    "desk": {"min_cutoff": 0.8, "beta": 8.0, "d_cutoff": 1.2, "dead": 0.0012, "offset": True},
    "air": {"min_cutoff": 0.6, "beta": 6.0, "d_cutoff": 1.0, "dead": 0.0008, "offset": False},
}

_FINGERS = ((5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20))


# ─────────────────────────────────────────────────────────────────────────────
#  Small vector helpers (tuples; no numpy here)
# ─────────────────────────────────────────────────────────────────────────────
def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def _sub(a, b) -> tuple:
    return tuple(x - y for x, y in zip(a, b))


def _norm(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def _dist(a, b) -> float:
    return _norm(_sub(a, b))


def _cos(u, v) -> Optional[float]:
    nu, nv = _norm(u), _norm(v)
    if nu < 1e-12 or nv < 1e-12:
        return None
    return sum(x * y for x, y in zip(u, v)) / (nu * nv)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ─────────────────────────────────────────────────────────────────────────────
#  One Euro filter
# ─────────────────────────────────────────────────────────────────────────────
class OneEuro:
    """Casiez, Roussel & Vogel (2012). Smooths hard when the signal is still and
    hardly at all when it moves fast: cutoff = min_cutoff + beta * |speed|.
    The speed is the derivative of the RAW samples (low-passed at d_cutoff)."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = float(min_cutoff), float(beta), float(d_cutoff)
        self.value: Optional[float] = None
        self.dx: float = 0.0
        self._raw: Optional[float] = None
        self._t: Optional[float] = None

    @staticmethod
    def _alpha(fc: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * max(fc, 1e-6))
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float) -> float:
        x, t = float(x), float(t)
        if self.value is None or self._t is None or t < self._t:
            self.reset(x, t)
            return x
        if t == self._t:
            return self.value
        dt = _clamp(t - self._t, 1e-3, 0.1)
        raw_dx = (x - self._raw) / dt
        self.dx += self._alpha(self.d_cutoff, dt) * (raw_dx - self.dx)
        fc = self.min_cutoff + self.beta * abs(self.dx)
        self.value += self._alpha(fc, dt) * (x - self.value)
        self._raw, self._t = x, t
        return self.value

    def reset(self, x: Optional[float] = None, t: Optional[float] = None) -> None:
        self.value = None if x is None else float(x)
        self._raw = self.value
        self._t = None if t is None else float(t)
        self.dx = 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Per-frame hand features
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Features:
    valid3d: bool = False          # computed from 21 pts (and world if valid)
    r: float = 1.0                 # fused thumb-index gap / palm length
    r_img: float = 1.0
    r_world: Optional[float] = None
    r_mid: float = 1.0             # thumb tip to middle tip, same normalisation
    index_reach: float = 2.0       # |8-0| / |5-0|: fist ~0.9, pinch ~1.1-1.6, open ~1.8
    tip_contact: bool = True       # thumb at the index TIP, not along its side
    tip_q: float = 0.0             # |4-8| / min(|4-6|, |4-7|): < 1 = nearer the tip
    fingers: tuple = ("mid", "mid", "mid", "mid")   # index, middle, ring, pinky
    spread_deg: float = -1.0       # index vs pinky direction; -1 = unknown
    anchor: tuple = (0.5, 0.5)     # picture coords: mean of the finger bases
    tip: tuple = (0.5, 0.5)        # picture coords: thumb-index midpoint
    size: float = 0.0
    angle: float = 0.0
    conf: float = 1.0
    side: str = ""


def _anchor_of(hand) -> tuple:
    """Hand.anchor without trusting the object to have it (older Hand, fakes)."""
    pts = getattr(hand, "pts", None)
    try:
        if pts and len(pts) >= 21:
            ids = (5, 9, 13, 17)
            return (sum(float(pts[i][0]) for i in ids) / 4, sum(float(pts[i][1]) for i in ids) / 4)
    except Exception:
        pass
    try:
        p = hand.pinch_point
        return (float(p[0]), float(p[1]))
    except Exception:
        return (0.5, 0.5)


def _fallback(hand) -> Features:
    """For objects without landmarks (test fakes, other trackers)."""
    try:
        r = float(getattr(hand, "pinch_ratio", 1.0))
    except Exception:
        r = 1.0
    try:
        p = hand.pinch_point
        tip = (float(p[0]), float(p[1]))
    except Exception:
        tip = (0.5, 0.5)
    if getattr(hand, "open_palm", False):
        fingers = ("ext",) * 4
    elif getattr(hand, "pointing", False):
        fingers = ("ext", "curl", "curl", "curl")
    else:
        fingers = ("mid",) * 4
    try:
        size = float(getattr(hand, "size", 0.0) or 0.0)
    except Exception:
        size = 0.0
    try:
        angle = float(getattr(hand, "angle", 0.0) or 0.0)
    except Exception:
        angle = 0.0
    try:
        conf = float(getattr(hand, "conf", 1.0) or 1.0)
    except Exception:
        conf = 1.0
    return Features(valid3d=False, r=r, r_img=r, r_world=None, r_mid=1.0, index_reach=2.0,
                    tip_contact=True, fingers=fingers, spread_deg=-1.0, anchor=tip, tip=tip,
                    size=size, angle=angle, conf=conf, side=str(getattr(hand, "side", "") or ""))


def _world_of(hand) -> Optional[list]:
    """The model's metric landmarks, or None when absent or implausible."""
    w = getattr(hand, "world", None)
    try:
        if not w or len(w) < 21:
            return None
        pts = [(float(p[0]), float(p[1]), float(p[2])) for p in w[:21]]
        scale = _dist(pts[0], pts[9])
        if not 0.05 <= scale <= 0.14:      # a real palm is ~7-11 cm
            return None
        return pts
    except Exception:
        return None


def features(hand, prev: Optional[Features] = None) -> Features:
    """Everything the gesture code needs from one tracked hand. Never raises."""
    try:
        pts = getattr(hand, "pts", None)
        if not pts or len(pts) < 21:
            return _fallback(hand)
        aspect = float(getattr(hand, "aspect", 16 / 9) or 16 / 9)
        zs = getattr(hand, "z", None) or []
        iso = []
        for i in range(21):
            z = float(zs[i]) if i < len(zs) else 0.0
            iso.append((float(pts[i][0]), float(pts[i][1]) / aspect, 0.6 * z))
        scale_img = _dist(iso[0], iso[9])
        if scale_img < 1e-6:
            return _fallback(hand)
        world = _world_of(hand)
        r_img = _dist(iso[4], iso[8]) / scale_img
        rmid_img = _dist(iso[4], iso[12]) / scale_img
        r_world = None
        r, r_mid = r_img, rmid_img
        if world is not None:
            sw = _dist(world[0], world[9])
            r_world = _dist(world[4], world[8]) / sw
            r = max(r_world, r_img)
            r_mid = max(_dist(world[4], world[12]) / sw, rmid_img)
        geo = world if world is not None else iso
        index_reach = _dist(geo[8], geo[0]) / max(_dist(geo[5], geo[0]), 1e-9)
        d48 = _dist(geo[4], geo[8])
        tip_q = d48 / max(min(_dist(geo[4], geo[6]), _dist(geo[4], geo[7])), 1e-9)
        # Thumb at the index tip, not lying along the finger's side. A pinch
        # made with a CURLED index folds the tip back next to joints 6/7, so
        # the ratio alone refused real pinches (live trace, Nov 2026: r_world
        # 0.21-0.23 held 0.4 s, never pressed). Tips this close always count.
        tight = d48 / max(_dist(geo[0], geo[9]), 1e-9) <= TIGHT_PINCH
        tip_contact = tip_q <= 1.0 or tight
        old = prev.fingers if prev is not None and len(getattr(prev, "fingers", ())) == 4 \
            else ("mid",) * 4
        fingers = []
        for k, (mcp, pip, dip, tip) in enumerate(_FINGERS):
            c = _cos(_sub(geo[pip], geo[mcp]), _sub(geo[tip], geo[dip]))
            ratio = _dist(geo[tip], geo[0]) / max(_dist(geo[mcp], geo[0]), 1e-9)
            if c is not None and c > 0.75 and ratio > 1.35:
                fingers.append("ext")
            elif (c is not None and c < 0.3) or ratio < 1.15:
                fingers.append("curl")
            else:
                fingers.append(old[k])     # inside the band: keep the last call
        u = (iso[8][0] - iso[5][0], iso[8][1] - iso[5][1])
        v = (iso[20][0] - iso[17][0], iso[20][1] - iso[17][1])
        c2 = _cos(u, v)
        spread = math.degrees(math.acos(_clamp(c2, -1.0, 1.0))) if c2 is not None else -1.0
        ids = (5, 9, 13, 17)
        anchor = (sum(float(pts[i][0]) for i in ids) / 4, sum(float(pts[i][1]) for i in ids) / 4)
        tip = ((float(pts[4][0]) + float(pts[8][0])) / 2, (float(pts[4][1]) + float(pts[8][1])) / 2)
        try:
            size = float(hand.size)
        except Exception:
            size = scale_img
        try:
            angle = float(hand.angle)
        except Exception:
            angle = math.atan2(pts[9][1] - pts[0][1], pts[9][0] - pts[0][0])
        try:
            conf = float(getattr(hand, "conf", 1.0) or 1.0)
        except Exception:
            conf = 1.0
        return Features(valid3d=True, r=r, r_img=r_img, r_world=r_world, r_mid=r_mid,
                        index_reach=index_reach, tip_contact=bool(tip_contact),
                        tip_q=float(tip_q),
                        fingers=tuple(fingers), spread_deg=spread, anchor=anchor, tip=tip,
                        size=size, angle=angle, conf=conf,
                        side=str(getattr(hand, "side", "") or ""))
    except Exception as e:
        print(f"[gesture] features failed: {e}")
        try:
            return _fallback(hand)
        except Exception:
            return Features()


# ─────────────────────────────────────────────────────────────────────────────
#  Pinch detector
# ─────────────────────────────────────────────────────────────────────────────
class PinchDetector:
    """open -> armed -> pressed -> open on the ratio r (see module docstring).

    rm = median of the last 3 r. ARM = off + 0.10. Frames under min_conf change
    no state. Press: rm < on for n_on frames, the hand shape agrees, and within
    came_window the SMOOTHED rm was both at least came_delta wider than now and
    at least came_from (default off - 0.04: the fingers were seen open, not
    just one noisy frame above a resting, nearly closed hand).
    Release: rm > off for n_off frames, or rm > off + fast_release once."""

    def __init__(self, on: float, off: float, n_on: int = 1, n_off: int = 2,
                 fast_release: float = 0.20, min_conf: float = 0.88,
                 came_window: float = 0.5, came_delta: float = 0.08,
                 came_from: Optional[float] = None):
        self.on, self.off = float(on), float(off)
        self.n_on, self.n_off = max(1, int(n_on)), max(1, int(n_off))
        self.fast_release, self.min_conf = float(fast_release), float(min_conf)
        self.came_window, self.came_delta = float(came_window), float(came_delta)
        self.came_from = self.off - 0.04 if came_from is None else float(came_from)
        self.reset()

    def reset(self) -> None:
        self.state = "open"
        self.closeness = 0.0
        self.t_press = 0.0
        self.t_release = 0.0
        self.t_armed = 0.0
        self._last3: deque = deque(maxlen=3)
        self._hist: deque = deque()          # (t, median r) inside came_window
        self._n_on = 0
        self._n_off = 0

    def update(self, r: float, shape_ok: bool, conf: float, t: float) -> str:
        try:
            r, t = float(r), float(t)
            if math.isnan(r):
                return self.state
            self._last3.append(r)
            rm = median(self._last3)
            self._hist.append((t, rm))
            while self._hist and self._hist[0][0] <= t - self.came_window:
                self._hist.popleft()
            if float(conf) >= self.min_conf:
                self._step(rm, bool(shape_ok), t)
            if self.state == "pressed":
                self.closeness = 1.0
            else:
                top = self.off + 0.25
                self.closeness = _clamp((top - rm) / max(top - self.on, 1e-6), 0.0, 1.0)
        except Exception as e:
            print(f"[gesture] pinch update failed: {e}")
        return self.state

    def _step(self, rm: float, shape_ok: bool, t: float) -> None:
        arm = self.off + 0.10
        if self.state == "pressed":
            self._n_off = self._n_off + 1 if rm > self.off else 0
            if self._n_off >= self.n_off or rm > self.off + self.fast_release:
                self.state = "open"
                self.t_release = t
                self.t_armed = 0.0
                self._n_off = self._n_on = 0
            return
        if self.state == "open" and rm < arm:
            self.state, self.t_armed = "armed", t
        elif self.state == "armed" and rm > arm + 0.05:
            self.state, self.t_armed = "open", 0.0
        self._n_on = self._n_on + 1 if rm < self.on else 0
        peak = max(v for _, v in self._hist)
        came = peak - rm >= self.came_delta and peak >= self.came_from
        if self._n_on >= self.n_on and shape_ok and came:
            if self.t_armed <= 0.0:
                self.t_armed = t
            self.state = "pressed"
            self.t_press = t
            self._n_off = 0


# ─────────────────────────────────────────────────────────────────────────────
#  Pose classifier
# ─────────────────────────────────────────────────────────────────────────────
class PoseClassifier:
    """Raw pose per frame, then a vote: a new pose needs `votes` of the last
    `window` raw labels. Pinches bypass the vote (already debounced)."""

    def __init__(self, votes: int = 3, window: int = 4):
        self.votes = max(1, int(votes))
        self._win: deque = deque(maxlen=max(self.votes, int(window)))
        self._voted = "none"
        self.pose = "none"
        self.raw = "none"
        self.since = 0.0

    @staticmethod
    def classify(f) -> str:
        fi = tuple(getattr(f, "fingers", ("mid",) * 4) or ("mid",) * 4)
        if len(fi) != 4:
            fi = ("mid",) * 4
        reach = float(getattr(f, "index_reach", 2.0))
        spread = float(getattr(f, "spread_deg", -1.0))
        if all(s == "curl" for s in fi) and reach < 1.05:
            return "fist"
        if all(s == "ext" for s in fi) and (spread < 0 or spread > 12):
            return "open"
        if fi[0] == "ext" and fi[1] == "ext" and fi[2] == "curl" and fi[3] == "curl":
            return "v"
        if fi[0] == "ext" and fi[1] == "curl" and fi[2] == "curl" and fi[3] == "curl":
            return "point"
        if float(getattr(f, "conf", 1.0)) < 0.8:
            return "none"
        return "relaxed"                 # includes the undecided 'mid' fingers

    def update(self, f, pinch: str, pinch_mid: str, t: float) -> str:
        try:
            raw = self.classify(f)
        except Exception:
            raw = "none"
        self.raw = raw
        self._win.append(raw)
        if self._win.count(raw) >= self.votes:
            self._voted = raw
        if pinch == "pressed":
            out = "pinch"
        elif pinch_mid == "pressed":
            out = "pinch_mid"
        else:
            out = self._voted
        if out != self.pose:
            self.pose, self.since = out, float(t)
        return self.pose


# ─────────────────────────────────────────────────────────────────────────────
#  Hands -> HandStates
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class HandState:
    slot: str = "h0"
    x: float = 0.0                 # cursor in to_screen space
    y: float = 0.0
    vx: float = 0.0                # filtered velocity, to_screen units / s
    vy: float = 0.0
    pose: str = "none"             # voted pose from POSES
    pinch: str = "open"            # index pinch: open | armed | pressed
    pinch_mid: str = "open"        # thumb-middle pinch
    closeness: float = 0.0         # 0..1
    down: bool = False             # pinch == "pressed"
    press_x: float = 0.0           # retro-dated aim of the current or last press
    press_y: float = 0.0
    t_aim: float = 0.0
    t_press: float = 0.0           # last press edge (0 = never)
    size: float = 0.0
    angle: float = 0.0
    conf: float = 1.0
    side: str = ""
    age: int = 0                   # camera frames since the slot was born
    lost: bool = False             # held through a dropout
    pts: Optional[list] = None     # 21 smoothed points in to_screen space
    t: float = 0.0                 # capture time of the frame this comes from


class _Slot:
    """Everything one tracked hand carries from frame to frame."""

    def __init__(self, name: str, prof: dict, on: float, off: float, n_on: int):
        self.name = name
        self.pinch = PinchDetector(on, off, n_on=n_on)
        self.mid = PinchDetector(on, off, n_on=n_on)
        self.poses = PoseClassifier()
        self.fx = OneEuro(prof["min_cutoff"], prof["beta"], prof["d_cutoff"])
        self.fy = OneEuro(prof["min_cutoff"], prof["beta"], prof["d_cutoff"])
        self.fsize = OneEuro(0.5, 2.0)
        self.fangle = OneEuro(1.0, 3.0)
        self.out: Optional[tuple] = None       # filtered + dead zone
        self.offset: Optional[tuple] = None    # desk: fingertip - anchor
        self.hist: deque = deque()             # (t, x, y) of `out`, ~0.4 s
        self.sides: deque = deque(maxlen=10)
        self.seen = 0.0
        self.age = 0
        self.feat: Optional[Features] = None
        self.raw_anchor: Optional[tuple] = None
        self.pts_ema: Optional[list] = None
        self.angle_un: Optional[float] = None
        self.was_down = False
        self.press = (0.0, 0.0)
        self.t_aim = 0.0
        self.t_press = 0.0
        self.locked = False
        self.t_unlock: Optional[float] = None
        self.state: Optional[HandState] = None

    def side(self) -> str:
        if not self.sides:
            return ""
        r = sum(1 for s in self.sides if s == "right")
        l = sum(1 for s in self.sides if s == "left")
        return "right" if r > l else "left" if l > r else ""


class HandFilter:
    """Tracked hands -> HandStates in stable slots. Call once per NEW camera
    frame with its capture time; repeating a t returns the cached list."""

    LOST = 0.25          # s a vanished, unpinched hand keeps its slot
    LOST_DOWN = 0.4      # ... a pinched one (and it keeps being reported as down)
    BIRTH_FRAMES = 3     # a new slot is silent for its first frames
    LOCK = 0.12          # s the cursor stays on the press point
    UNLOCK = 0.015       # ... unless the hand moves further than this
    MATCH = 0.25         # max matching cost (to_screen units)
    SIDE_COST = 0.15     # cost of giving a slot the other hand
    HIST = 0.4           # s of cursor history kept for the retro-dated aim
    EASE = 0.08          # s to glide from the press point back to the hand

    def __init__(self, sensitivity: str = "normal", profile: str = "desk", max_hands: int = 2):
        self.sensitivity = sensitivity if sensitivity in SENSITIVITY else "normal"
        self.on, self.off = SENSITIVITY[self.sensitivity]
        self.n_on = N_ON[self.sensitivity]
        self.profile = profile if profile in PROFILES else "desk"
        self.prof = PROFILES[self.profile]
        self.max_hands = max(1, int(max_hands))
        self.reset()

    def reset(self) -> None:
        self.slots: dict[str, _Slot] = {}
        self.states: list = []
        self._last_t: Optional[float] = None

    # ── public ──────────────────────────────────────────────────────────────
    def update(self, hands: list, t: float,
               to_screen: Callable[[float, float], tuple] = lambda x, y: (x, y)) -> list:
        try:
            t = float(t)
            if self._last_t is not None and t == self._last_t:
                return self.states
            self._last_t = t
            self.states = self._update(list(hands or []), t, to_screen)
        except Exception as e:
            print(f"[gesture] hand filter error: {e}")
        return self.states

    # ── internals ───────────────────────────────────────────────────────────
    def _names(self) -> list:
        return [f"h{i}" for i in range(max(2, self.max_hands))]

    def _update(self, hands: list, t: float, to_screen) -> list:
        def conf_of(h):
            try:
                return float(getattr(h, "conf", 1.0) or 1.0)
            except Exception:
                return 1.0
        hands = sorted(hands, key=conf_of, reverse=True)[: self.max_hands]
        anchors = []
        for h in hands:
            ax, ay = to_screen(*_anchor_of(h))
            anchors.append((float(ax), float(ay)))
        # Greedy matching on the raw anchor, with a penalty for the other side.
        cands = []
        for i, h in enumerate(hands):
            side = str(getattr(h, "side", "") or "")
            for name, s in self.slots.items():
                if s.raw_anchor is None:
                    continue
                d = math.hypot(anchors[i][0] - s.raw_anchor[0], anchors[i][1] - s.raw_anchor[1])
                ss = s.side()
                cost = d + (self.SIDE_COST if side and ss and side != ss else 0.0)
                if cost <= self.MATCH:
                    cands.append((cost, i, name))
        cands.sort()
        pairs: dict[int, str] = {}
        used: set = set()
        for cost, i, name in cands:
            if i in pairs or name in used:
                continue
            pairs[i] = name
            used.add(name)
        for i in range(len(hands)):
            if i in pairs:
                continue
            name = next((n for n in self._names() if n not in self.slots), None)
            if name is None:
                # Every name is taken: reuse the stalest slot not seen this frame.
                spare = sorted((s.seen, n) for n, s in self.slots.items() if n not in used)
                if not spare:
                    continue
                name = spare[0][1]
            self.slots[name] = _Slot(name, self.prof, self.on, self.off, self.n_on)
            pairs[i] = name
            used.add(name)
        out = []
        for i, name in pairs.items():
            try:
                st = self._step(self.slots[name], hands[i], anchors[i], t, to_screen)
            except Exception as e:
                print(f"[gesture] hand {name} skipped: {e}")
                st = None
            if st is not None:
                out.append(st)
        for name in list(self.slots):
            if name in used:
                continue
            s = self.slots[name]
            down = s.pinch.state == "pressed"
            if t - s.seen > (self.LOST_DOWN if down else self.LOST):
                del self.slots[name]
            elif down and s.state is not None:
                # Tracking drops a frame now and then; letting go there would
                # drop (or throw) whatever the hand is carrying.
                out.append(replace(s.state, lost=True, down=True))
        out.sort(key=lambda st: st.slot)
        return out

    def _step(self, s: _Slot, hand, anchor: tuple, t: float, to_screen) -> Optional[HandState]:
        f = features(hand, s.feat)
        s.feat = f
        s.raw_anchor = anchor
        s.seen = t
        if f.side:
            s.sides.append(f.side)
        # Every finger curled = a hand closing into a fist, whose thumb may
        # sweep past the index tip on the way: not a pinch (it would click,
        # and cancel the crush, on every unhurried fist).
        closing_fist = all(fs == "curl" for fs in f.fingers)
        pinch = s.pinch.update(f.r, f.index_reach > 1.0 and f.tip_contact and not closing_fist,
                               f.conf, t)
        mid = s.mid.update(f.r_mid, f.fingers[0] != "curl" and f.r > 0.45, f.conf, t)
        pose = s.poses.update(f, pinch, mid, t)
        down = pinch == "pressed"

        # Raw cursor: the knuckle anchor (+ the desk's frozen fingertip offset).
        ax, ay = anchor
        rx, ry = ax, ay
        if self.prof.get("offset"):
            tx, ty = to_screen(*f.tip)
            ox, oy = float(tx) - ax, float(ty) - ay
            if s.offset is None:
                s.offset = (ox, oy)
            elif (pinch == "open" and f.r > self.off + 0.10
                  and (s.pinch.t_release <= 0 or t - s.pinch.t_release > 0.25)):
                s.offset = (s.offset[0] + 0.3 * (ox - s.offset[0]),
                            s.offset[1] + 0.3 * (oy - s.offset[1]))
            rx, ry = ax + s.offset[0], ay + s.offset[1]
        fx, fy = s.fx(rx, t), s.fy(ry, t)
        vx, vy = s.fx.dx, s.fy.dx
        speed = math.hypot(vx, vy)
        dead = float(self.prof.get("dead", 0.0))
        if s.out is None or (down and speed > 0.3) or dead <= 0:
            s.out = (fx, fy)
        else:
            d = math.hypot(fx - s.out[0], fy - s.out[1])
            if d > dead:
                k = (d - dead) / d
                s.out = (s.out[0] + (fx - s.out[0]) * k, s.out[1] + (fy - s.out[1]) * k)
        s.hist.append((t, s.out[0], s.out[1]))
        while len(s.hist) > 3 and s.hist[0][0] < t - self.HIST:
            s.hist.popleft()

        # Press edge: retro-date the aim to where the approach started.
        if down and not s.was_down:
            ta = s.pinch.t_armed
            s.t_aim = ta if (ta > 0 and t - ta <= self.HIST) else t - 0.1
            near = min(s.hist, key=lambda h: abs(h[0] - s.t_aim))
            s.press = (near[1], near[2])
            s.t_press = t
            s.locked, s.t_unlock = True, None
        x, y = s.out
        if down:
            if s.locked:
                if (t - s.t_press <= self.LOCK
                        and math.hypot(s.out[0] - s.press[0], s.out[1] - s.press[1]) <= self.UNLOCK):
                    x, y = s.press
                else:
                    s.locked, s.t_unlock = False, t
            if not s.locked and s.t_unlock is not None:
                k = _clamp((t - s.t_unlock) / self.EASE, 0.0, 1.0)
                x = s.press[0] + (s.out[0] - s.press[0]) * k
                y = s.press[1] + (s.out[1] - s.press[1]) * k
                if k >= 1.0:
                    s.t_unlock = None
        elif s.was_down:
            # Opening the fingers moves the tips: report where the hand was
            # two frames ago for the release frame itself.
            h = s.hist[-3] if len(s.hist) >= 3 else s.hist[0]
            x, y = h[1], h[2]
            s.locked, s.t_unlock = False, None
        s.was_down = down

        size = s.fsize(f.size, t)
        s.angle_un = f.angle if s.angle_un is None else s.angle_un + _wrap(f.angle - s.angle_un)
        angle = s.fangle(s.angle_un, t)
        pts_screen = None
        pts = getattr(hand, "pts", None)
        if pts and len(pts) >= 21:
            cur = [(float(p[0]), float(p[1])) for p in pts[:21]]
            if s.pts_ema is None or len(s.pts_ema) != len(cur):
                s.pts_ema = cur
            else:
                s.pts_ema = [(a[0] + 0.5 * (b[0] - a[0]), a[1] + 0.5 * (b[1] - a[1]))
                             for a, b in zip(s.pts_ema, cur)]
            pts_screen = [tuple(to_screen(px, py)) for px, py in s.pts_ema]
        if s.t_press <= 0:
            px, py = x, y
        else:
            px, py = s.press
        age = s.age
        s.age += 1
        st = HandState(slot=s.name, x=x, y=y, vx=vx, vy=vy, pose=pose, pinch=pinch,
                       pinch_mid=mid, closeness=s.pinch.closeness, down=down,
                       press_x=px, press_y=py, t_aim=s.t_aim, t_press=s.t_press,
                       size=size, angle=angle, conf=f.conf, side=s.side() or f.side,
                       age=age, lost=False, pts=pts_screen, t=t)
        s.state = st
        return st if age >= self.BIRTH_FRAMES else None


# ─────────────────────────────────────────────────────────────────────────────
#  Swipes and the "crush" (open palm -> fist)
# ─────────────────────────────────────────────────────────────────────────────
class SwipeDetector:
    """A quick straight stroke of the hand in a given pose. Returns the
    direction the HAND moved (x right, y down) on exactly one frame."""

    _OPP = {"left": "right", "right": "left", "up": "down", "down": "up"}

    def __init__(self, min_dist=0.22, max_time=0.35, off_axis=0.5, min_peak=1.2, cooldown=0.8,
                 reverse_block=0.6, pose_ratio=0.8, poses=("open",), min_avg=0.0):
        self.min_dist, self.max_time, self.off_axis = float(min_dist), float(max_time), float(off_axis)
        # min_avg: the stroke's AVERAGE speed (picture units / s) over the
        # window that qualifies. A flick covers its distance at a high average
        # speed; repositioning the hand (Air Control's open-hand clutch) has a
        # similar peak but a slow start and end, so a peak alone mistook it
        # for a swipe.
        self.min_avg = float(min_avg)
        self.min_peak, self.cooldown, self.reverse_block = float(min_peak), float(cooldown), \
            float(reverse_block)
        self.pose_ratio, self.poses = float(pose_ratio), tuple(poses)
        self.reset()

    def reset(self) -> None:
        self._hist: deque = deque()       # (t, x, y, pose)
        self._block_all = -1e18
        self._block: dict[str, float] = {}

    def update(self, x: float, y: float, pose: str, t: float) -> Optional[str]:
        try:
            x, y, t = float(x), float(y), float(t)
            self._hist.append((t, x, y, pose))
            while self._hist and self._hist[0][0] < t - 0.5:
                self._hist.popleft()
            if t < self._block_all or len(self._hist) < 2:
                return None
            h = list(self._hist)
            end = h[-1]
            for i in range(len(h) - 1):
                st = h[i]
                if end[0] - st[0] > self.max_time:
                    continue
                dx, dy = end[1] - st[1], end[2] - st[2]
                main, off = (abs(dx), abs(dy)) if abs(dx) >= abs(dy) else (abs(dy), abs(dx))
                if main <= self.min_dist or off / main >= self.off_axis:
                    continue
                if self.min_avg > 0 and main / max(end[0] - st[0], 1e-6) < self.min_avg:
                    continue
                if abs(dx) >= abs(dy):
                    d = "right" if dx > 0 else "left"
                else:
                    d = "down" if dy > 0 else "up"
                if t < self._block.get(d, -1e18):
                    continue
                seg = h[i:]
                peak = 0.0
                for a, b in zip(seg, seg[1:]):
                    dt = b[0] - a[0]
                    if dt > 1e-6:
                        peak = max(peak, math.hypot(b[1] - a[1], b[2] - a[2]) / dt)
                if peak <= self.min_peak:
                    continue
                good = sum(1 for s in seg if s[3] in self.poses)
                if good < self.pose_ratio * len(seg):
                    continue
                self._hist.clear()
                self._block_all = t + self.cooldown
                self._block[self._OPP[d]] = t + self.cooldown + self.reverse_block
                return d
        except Exception as e:
            print(f"[gesture] swipe update failed: {e}")
        return None


class CrushDetector:
    """Open palm held still, then a fist: "close this". (phase, progress).

    idle (progress = arming 0..1) -> armed -> closing (progress 0..1) ->
    fired; "cancelled" when the hand opens while closing, travels too far,
    the window runs out without a fist, or shows a clearly different pose
    (a V, a pinch) while armed. "fired" and "cancelled" are each returned on
    one call only.

    While armed, the in-between poses of a hand CLOSING into a fist
    ('relaxed': some fingers curled, some not yet; 'point': the index curls
    last) are tolerated: the pose fed in is the voted one, and an unhurried
    fist (300 ms or more) votes 'relaxed' for a few frames on its way.

    fresh=True (Air Control, where the open hand is also the clutch): the
    palm must be held still from the moment it opened. An open hand that has
    moved (a clutch reposition) cannot arm until it leaves the open pose."""

    ARM_TIME = 0.30
    ARM_SPEED = 0.25
    WINDOW = 0.8
    TRAVEL = 0.06
    CLOSING_POSES = ("relaxed", "point")
    TRANSIT = 0.40            # longest a closing pose is tolerated while armed

    def __init__(self, confirm: float = 0.25, arm_time: Optional[float] = None,
                 fresh: bool = False):
        self.confirm = float(confirm)
        self.arm_time = self.ARM_TIME if arm_time is None else float(arm_time)
        self.fresh = bool(fresh)
        self.reset()

    def reset(self) -> None:
        self.phase = "idle"
        self.arm_x = self.arm_y = 0.0
        self._hold: Optional[float] = None
        self._t_armed = 0.0
        self._t_close = 0.0
        self._rest = False        # timed out: wait for the hand to move or change
        self._open_moved = False  # fresh: this open hand has moved (a clutch)
        self._t_transit: Optional[float] = None

    def _cancel(self, rest: bool = False) -> tuple:
        self.phase = "cancelled"
        self._hold = None
        self._rest = rest
        return ("cancelled", 0.0)

    def update(self, pose: str, x: float, y: float, speed: float, t: float) -> tuple:
        try:
            x, y, speed, t = float(x), float(y), float(speed), float(t)
            if self.phase in ("fired", "cancelled"):
                self.phase, self._hold = "idle", None
            if self.phase == "idle":
                if self._rest and (pose != "open" or speed >= self.ARM_SPEED):
                    self._rest = False
                if pose != "open":
                    self._open_moved = False
                elif speed >= self.ARM_SPEED and self.fresh:
                    self._open_moved = True
                if pose == "open" and speed < self.ARM_SPEED and not self._rest \
                        and not self._open_moved:
                    if self._hold is None:
                        self._hold = t
                    held = t - self._hold
                    if held >= self.arm_time:
                        self.phase, self._t_armed = "armed", t
                        self._t_transit = None
                        self.arm_x, self.arm_y = x, y
                        self._hold = None
                        return ("armed", 0.0)
                    return ("idle", _clamp(held / max(self.arm_time, 1e-6), 0.0, 1.0))
                self._hold = None
                return ("idle", 0.0)
            travel = math.hypot(x - self.arm_x, y - self.arm_y)
            if travel > self.TRAVEL:
                return self._cancel()
            if self.phase == "armed":
                if pose == "fist":
                    self.phase, self._t_close = "closing", t
                    return ("closing", 0.0)
                if pose != "open" and pose not in self.CLOSING_POSES:
                    return self._cancel()
                if pose in self.CLOSING_POSES:
                    # on its way to a fist; a hand that just relaxes instead
                    # (back to moving the cursor) cancels after TRANSIT
                    if self._t_transit is None:
                        self._t_transit = t
                    elif t - self._t_transit > self.TRANSIT:
                        return self._cancel()
                else:
                    self._t_transit = None
                if t - self._t_armed > self.WINDOW:
                    # An open hand simply resting (Air Control's clutch) must
                    # not arm, time out and arm again forever.
                    return self._cancel(rest=True)
                return ("armed", 0.0)
            # closing
            if pose != "fist":
                return self._cancel()
            p = (t - self._t_close) / max(self.confirm, 1e-6)
            if p >= 1.0:
                self.phase = "fired"
                return ("fired", 1.0)
            return ("closing", _clamp(p, 0.0, 1.0))
        except Exception as e:
            print(f"[gesture] crush update failed: {e}")
            self.reset()
            return ("idle", 0.0)
