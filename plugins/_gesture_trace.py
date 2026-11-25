"""
plugins/_gesture_trace.py — a flight recorder for hand control.

Helper module (leading underscore: the plugin loader skips it). Off unless the
environment variable JARVIS_GESTURE_TRACE names a folder; then the Holo Desk
and Air Control windows write there:

  trace.jsonl    one line per camera frame: what the tracker measured for each
                 hand (pinch gap, finger states, confidence), what the filter
                 decided (pose, pinch state, cursor), plus a line for every
                 event and OS action the mode produced.
  snap.jpg       the latest camera picture with the 21 landmarks drawn on it,
                 at most twice a second (overwritten in place).

Why: gestures that "don't feel smooth" cannot be fixed from a description;
this records the numbers behind each wrong decision so the thresholds can be
tuned against a real hand. Costs nothing when the variable is unset (every
function returns at once). Never raises: a recorder must not break the mode.
"""
from __future__ import annotations

import json
import os
import threading
import time

_DIR = os.environ.get("JARVIS_GESTURE_TRACE", "").strip()
_lock = threading.Lock()
_fh = None
_last_snap = 0.0
SNAP_EVERY = 0.5


def enabled() -> bool:
    return bool(_DIR)


def _write(obj: dict) -> None:
    global _fh
    try:
        with _lock:
            if _fh is None:
                os.makedirs(_DIR, exist_ok=True)
                _fh = open(os.path.join(_DIR, "trace.jsonl"), "a", buffering=1, encoding="utf-8")
            _fh.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _r(v, n=3):
    try:
        return round(float(v), n)
    except Exception:
        return v


def mark(text: str) -> None:
    """A free-form line (mode opened, closed, ...)."""
    if _DIR:
        _write({"w": time.time(), "mark": str(text)})


def frame(mode: str, t: float, hands: list, filt) -> None:
    """After HandFilter.update on a NEW frame: raw features + decided state."""
    if not _DIR:
        return
    try:
        out = []
        for name, s in list(getattr(filt, "slots", {}).items()):
            f, st = getattr(s, "feat", None), getattr(s, "state", None)
            d = {"slot": name}
            if f is not None:
                d.update(r=_r(f.r), ri=_r(f.r_img), rw=_r(f.r_world) if f.r_world is not None else None,
                         rm=_r(f.r_mid), reach=_r(f.index_reach), tip=bool(f.tip_contact),
                         fing="".join(x[0] for x in f.fingers), spread=_r(f.spread_deg, 0),
                         conf=_r(f.conf, 2), size=_r(f.size))
            if st is not None:
                d.update(pose=st.pose, pinch=st.pinch, pmid=st.pinch_mid, close=_r(st.closeness, 2),
                         x=_r(st.x), y=_r(st.y), vx=_r(st.vx, 2), vy=_r(st.vy, 2),
                         lost=bool(st.lost), age=st.age)
            out.append(d)
        _write({"w": round(time.time(), 3), "t": _r(t, 3), "m": mode, "n": len(hands or []),
                "h": out})
    except Exception as e:
        _write({"w": time.time(), "err": f"frame: {e}"})


def items(kind: str, mode: str, things: list) -> None:
    """Events or OS actions the mode produced this tick."""
    if not _DIR or not things:
        return
    try:
        _write({"w": round(time.time(), 3), "m": mode, kind: [list(a) if isinstance(a, (tuple, list))
                                                              else str(a) for a in things]})
    except Exception:
        pass


def snapshot(frame_bgr, hands: list) -> None:
    """The camera picture with landmarks, at most every SNAP_EVERY seconds."""
    global _last_snap
    if not _DIR:
        return
    now = time.monotonic()
    if now - _last_snap < SNAP_EVERY:
        return
    _last_snap = now
    try:
        import cv2  # type: ignore
        img = cv2.flip(frame_bgr, 1)          # landmarks are mirrored; so is the picture
        h, w = img.shape[:2]
        for hand in hands or []:
            pts = [(int(p[0] * w), int(p[1] * h)) for p in getattr(hand, "pts", [])[:21]]
            bones = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9),
                     (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
                     (13, 17), (0, 17), (17, 18), (18, 19), (19, 20))
            for a, b in bones:
                if a < len(pts) and b < len(pts):
                    cv2.line(img, pts[a], pts[b], (0, 255, 0), 2)
            for i, p in enumerate(pts):
                cv2.circle(img, p, 4 if i not in (4, 8) else 7, (0, 0, 255) if i in (4, 8) else (255, 0, 0), -1)
            try:
                cv2.putText(img, f"r={hand.pinch_ratio:.2f} c={hand.conf:.2f}", (pts[0][0], pts[0][1] + 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            except Exception:
                pass
        cv2.putText(img, time.strftime("%H:%M:%S"), (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 255), 2)
        tmp = os.path.join(_DIR, "snap.tmp.jpg")
        cv2.imwrite(tmp, img)
        os.replace(tmp, os.path.join(_DIR, "snap.jpg"))
    except Exception as e:
        _write({"w": time.time(), "err": f"snap: {e}"})
