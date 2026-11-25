"""
plugins/_gesture_synth.py — synthetic hands for the gesture tests.

Helper module (leading underscore: the plugin loader skips it), standard
library only. `make_hand()` builds a plugins._hands_onnx.Hand the way the
tracker would report it: 21 picture points (x mirrored, x / width, y / height
of a frame with the given aspect), relative depths, and the model's metric
"world" landmarks. The geometry is a simple but anatomically plausible hand
(palm 9 cm, finger segments 4.5 / 2.5 / 2 cm), so the same pose can be shown
turned (yaw), rolled, near or far, on any frame shape. That is what lets the
tests prove a pinch is decided in real, square distances: on the old 2D ratio
an open finger 0.5 palms from the thumb read as 0.28, a pinch.

Hand frame used while building (metres): wrist at the origin, fingers up
(-Y, picture y grows downwards), the palm facing the camera, fingers curling
towards it (-Z, smaller z = nearer, as the model reports). L is the sideways
axis with the thumb side positive; a right hand seen in the mirrored picture
has its thumb on the left, so X = -L for "right" and X = +L for "left".
`(x, y)` is where Hand.anchor (the mean of the finger bases) lands.
"""
from __future__ import annotations

import math

from plugins._hands_onnx import Hand

PALM = 0.09                       # wrist to middle-finger base, metres
SEG = (0.045, 0.025, 0.02)        # proximal, middle, distal phalanx
# Per finger: base sideways position L, length scale, open-hand spread (deg).
_BASES = ((0.024, 1.0, 12.0), (0.004, 1.08, 4.0), (-0.014, 1.0, -4.0), (-0.030, 0.8, -12.0))

OPEN = (0.0, 5.0, 5.0)            # flexion at MCP, PIP, DIP in degrees
RELAXED = (25.0, 30.0, 20.0)
FIST = (90.0, 100.0, 70.0)
PINCHING = (35.0, 40.0, 30.0)     # the finger meeting the thumb
SOFT = (15.0, 20.0, 15.0)         # the fingers left out of a pinch
NEAR = (15.0, 20.0, 10.0)         # an open index the thumb is reaching for

POSES = ("open", "relaxed", "point", "v", "fist", "pinch", "pinch_mid")

_CMC = (0.022, -0.018, -0.004)
_THUMB_TIPS = {
    "open": (0.078, -0.082, -0.020),
    "relaxed": (0.055, -0.075, -0.035),
    "tucked": (0.008, -0.070, -0.055),   # across the curled fingers
}


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _mul(a, k):
    return tuple(x * k for x in a)


def _lerp(a, b, k):
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def _finger(k: int, curl: tuple, spread_scale: float) -> list:
    """MCP, PIP, DIP, TIP of finger k (0 index .. 3 pinky) in (L, Y, Z)."""
    base_l, scale, spread = _BASES[k]
    mcp = (base_l, -math.sqrt(max(PALM ** 2 - base_l ** 2, 1e-6)), 0.0)
    sp = math.radians(spread * spread_scale)
    up = (math.sin(sp), -math.cos(sp), 0.0)
    fwd = (0.0, 0.0, -1.0)
    out, p, ang = [mcp], mcp, 0.0
    for seg, c in zip(SEG, curl):
        ang += math.radians(c)
        d = _add(_mul(up, math.cos(ang)), _mul(fwd, math.sin(ang)))
        p = _add(p, _mul(d, seg * scale))
        out.append(p)
    return out


def _thumb(tip: tuple) -> list:
    """CMC, MCP, IP, TIP reaching `tip`, bowed slightly outwards."""
    return [_CMC,
            _add(_lerp(_CMC, tip, 0.42), (0.012, 0.0, 0.0)),
            _add(_lerp(_CMC, tip, 0.74), (0.008, 0.0, 0.0)),
            tip]


def make_hand(pose: str = "open", *, gap: float | None = None, x: float = 0.5, y: float = 0.6,
              size: float = 0.18, roll: float = 0.0, yaw: float = 0.0, aspect: float = 16 / 9,
              world: bool = True, conf: float = 0.97, side: str = "right",
              closing: float | None = None, index_last: bool = False) -> Hand:
    """A Hand in `pose`. gap: thumb-tip to index-tip (middle tip for
    "pinch_mid") in palm lengths; default 0.12 for the pinch poses, and with
    "open" it brings the thumb that close to a still-open index (a near pinch).
    size: wrist to middle base in frame-HEIGHT units. roll / yaw in degrees:
    yaw turns the hand about the vertical axis (side-on to the camera), roll
    turns it in the picture plane.
    closing: 0..1 blends an open hand into a fist (fingers and thumb), for the
    in-between frames of a hand that closes; index_last keeps the index open
    until the other fingers are curled (it then curls in the second half)."""
    pose = pose if pose in POSES else "open"
    if closing is not None:
        return _closing_hand(float(closing), index_last, x=x, y=y, size=size, roll=roll,
                             yaw=yaw, aspect=aspect, world=world, conf=conf, side=side)
    curls = {
        "open": (OPEN, OPEN, OPEN, OPEN),
        "relaxed": (RELAXED,) * 4,
        "point": (OPEN, FIST, FIST, FIST),
        "v": (OPEN, OPEN, FIST, FIST),
        "fist": (FIST,) * 4,
        "pinch": (PINCHING, SOFT, SOFT, SOFT),
        "pinch_mid": (OPEN, PINCHING, SOFT, SOFT),
    }[pose]
    if pose == "open" and gap is not None:
        curls = (NEAR, OPEN, OPEN, OPEN)
    spread_scale = {"open": 1.0, "v": 1.6, "point": 0.5, "relaxed": 0.35}.get(pose, 0.2)
    fingers = [_finger(k, curls[k], spread_scale) for k in range(4)]
    if pose in ("pinch", "pinch_mid") or (pose == "open" and gap is not None):
        g = 0.12 if gap is None else float(gap)
        target = fingers[1 if pose == "pinch_mid" else 0][3]
        tip = _add(target, (g * PALM, 0.0, 0.0))      # beside the tip, thumb side
    elif pose in ("fist", "point", "v"):
        tip = _THUMB_TIPS["tucked"]
    elif pose == "relaxed":
        tip = _THUMB_TIPS["relaxed"]
    else:
        tip = _THUMB_TIPS["open"]
    return _build(fingers, tip, x=x, y=y, size=size, roll=roll, yaw=yaw, aspect=aspect,
                  world=world, conf=conf, side=side)


def _closing_hand(k: float, index_last: bool, **kw) -> Hand:
    k = max(0.0, min(1.0, k))
    others = _lerp(OPEN, FIST, k)
    if index_last:
        index = _lerp(OPEN, FIST, max(0.0, k * 2 - 1))
        others = _lerp(OPEN, FIST, min(1.0, k * 2))
    else:
        index = others
    curls = (index, others, others, others)
    spread_scale = 1.0 + (0.2 - 1.0) * k
    fingers = [_finger(i, curls[i], spread_scale) for i in range(4)]
    tip = _lerp(_THUMB_TIPS["open"], _THUMB_TIPS["tucked"], k)
    return _build(fingers, tip, **kw)


def _build(fingers, tip, *, x, y, size, roll, yaw, aspect, world, conf, side) -> Hand:
    pts3 = [(0.0, 0.0, 0.0)] + _thumb(tip)
    for f in fingers:
        pts3 += f
    # Centre on the anchor, then L -> mirrored picture X.
    ids = (5, 9, 13, 17)
    anchor = tuple(sum(pts3[i][k] for i in ids) / 4 for k in range(3))
    sx = -1.0 if side != "left" else 1.0
    pts3 = [((p[0] - anchor[0]) * sx, p[1] - anchor[1], p[2] - anchor[2]) for p in pts3]
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    turned = []
    for X, Y, Z in pts3:
        X, Z = X * cy + Z * sy, -X * sy + Z * cy        # yaw about the vertical axis
        X, Y = X * cr - Y * sr, X * sr + Y * cr         # roll in the picture plane
        turned.append((X, Y, Z))
    k = size / PALM
    pts = [(x + X * k / aspect, y + Y * k) for X, Y, _ in turned]
    zs = [Z * k / aspect for _, _, Z in turned]
    return Hand(pts=pts, z=zs, side=side, conf=conf, aspect=aspect,
                world=[tuple(p) for p in turned] if world else [])
