"""
plugins/_hands.py — one hand seen through the webcam, and what it is doing.

Helper module (leading underscore: the plugin loader skips it). Used by
gesture_control and holo_desk.

Trackers (best first), both returning a HandObs per frame:
  * MediaPipe Hands (optional: pip install mediapipe) — 21 landmarks, so every
    pose is recognised: open palm, two fingers, pinch, thumbs up / down.
  * OpenCV fallback — skin colour segmentation with the face masked out; the
    biggest skin blob is the hand. Knows only 'open_palm' (several finger gaps
    in the outline) and 'other', plus where the hand is, which is enough for
    swipes and the open-palm stop.

GestureInterpreter turns a stream of HandObs into events:
  stop, next, previous, play_pause, volume_up, volume_down, yes, no
It is pure logic, unit-tested with synthetic observations.

x and y are 0..1 in the picture, x already MIRRORED so that moving the hand to
the user's right increases x (the camera sees them the other way round).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class HandObs:
    pose: str             # open_palm | two_fingers | pinch | thumbs_up | thumbs_down | other
    x: float
    y: float
    size: float = 0.0     # palm size, 0..1 of the picture height (bigger = nearer)


# ── Landmark classification (MediaPipe's 21-point layout) ────────────────────

def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify_landmarks(pts) -> str:
    """pts: 21 (x, y) points, y growing downwards."""
    wrist = pts[0]
    palm = _d(wrist, pts[9]) or 1e-6

    def extended(tip, pip):
        return _d(pts[tip], wrist) > _d(pts[pip], wrist) * 1.15

    idx, mid = extended(8, 6), extended(12, 10)
    ring, pinky = extended(16, 14), extended(20, 18)
    thumb_out = _d(pts[4], pts[5]) > 0.55 * palm and _d(pts[4], pts[17]) > _d(pts[3], pts[17])
    fingers = sum((idx, mid, ring, pinky))

    if fingers == 0 and thumb_out:
        dy = pts[4][1] - pts[2][1]
        if dy < -0.35 * palm:
            return "thumbs_up"
        if dy > 0.35 * palm:
            return "thumbs_down"
    # A pinch: thumb and index tips together, with the index reaching out past
    # its knuckle (in a fist the thumb also rests on a curled index).
    if _d(pts[4], pts[8]) < 0.28 * palm and _d(pts[8], wrist) > _d(pts[5], wrist) * 1.1:
        return "pinch"
    if fingers == 4 and thumb_out:
        return "open_palm"
    if idx and mid and not ring and not pinky:
        return "two_fingers"
    return "other"


# ── Trackers ─────────────────────────────────────────────────────────────────

class MediaPipeTracker:
    kind = "mediapipe"

    def __init__(self):
        import mediapipe as mp  # optional dependency
        self._hands = mp.solutions.hands.Hands(max_num_hands=1, model_complexity=0,
                                               min_detection_confidence=0.6,
                                               min_tracking_confidence=0.5)
        self.last_points = None

    def process(self, frame) -> Optional[HandObs]:
        import cv2
        res = self._hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        hands = getattr(res, "multi_hand_landmarks", None)
        if not hands:
            self.last_points = None
            return None
        pts = [(p.x, p.y) for p in hands[0].landmark]
        self.last_points = pts
        pose = classify_landmarks(pts)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        if pose == "pinch":
            cx, cy = (pts[4][0] + pts[8][0]) / 2, (pts[4][1] + pts[8][1]) / 2
        return HandObs(pose, 1.0 - cx, cy, _d(pts[0], pts[9]))


class SkinTracker:
    """No-dependency fallback: skin blob minus the face."""
    kind = "opencv"

    def __init__(self):
        import cv2
        from pathlib import Path
        self.cv2 = cv2
        self._face = cv2.CascadeClassifier(
            str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        self._faces = []
        self._n = 0
        self.last_contour = None

    def process(self, frame) -> Optional[HandObs]:
        cv2 = self.cv2
        import numpy as np
        h0, w0 = frame.shape[:2]
        s = 240.0 / max(w0, 1)
        small = cv2.resize(frame, (240, max(1, int(h0 * s))), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        ycc = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycc, (0, 135, 85), (255, 175, 130))
        # Faces are skin too: find them every few frames and blank them out,
        # with the neck below.
        if self._n % 4 == 0 and not self._face.empty():
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            self._faces = self._face.detectMultiScale(gray, 1.2, 4, minSize=(24, 24))
        self._n += 1
        for (fx, fy, fw, fh) in self._faces:
            x1, x2 = max(0, int(fx - 0.25 * fw)), min(w, int(fx + 1.25 * fw))
            y1, y2 = max(0, int(fy - 0.3 * fh)), min(h, int(fy + 1.9 * fh))
            mask[y1:y2, x1:x2] = 0
        k = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
        return self.from_mask(mask)

    def from_mask(self, mask) -> Optional[HandObs]:
        """Classify the biggest blob of a binary mask (unit-tested directly)."""
        cv2 = self.cv2
        import numpy as np
        h, w = mask.shape[:2]
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            self.last_contour = None
            return None
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        if area < 0.015 * w * h:
            self.last_contour = None
            return None
        self.last_contour = c
        m = cv2.moments(c)
        cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
        x, y, bw, bh = cv2.boundingRect(c)
        gaps = 0
        hull = cv2.convexHull(c, returnPoints=False)
        if hull is not None and len(hull) > 3:
            try:
                defects = cv2.convexityDefects(c, hull)
            except cv2.error:
                defects = None
            if defects is not None:
                for sidx, eidx, fidx, depth in defects[:, 0]:
                    a, b, f = c[sidx][0], c[eidx][0], c[fidx][0]
                    if depth / 256.0 < 0.12 * max(bw, bh):
                        continue
                    v1, v2 = a - f, b - f
                    n = float(np.linalg.norm(v1) * np.linalg.norm(v2))
                    if n and math.degrees(math.acos(max(-1, min(1, float(v1 @ v2) / n)))) < 90:
                        gaps += 1
        pose = "open_palm" if gaps >= 3 else "other"
        return HandObs(pose, 1.0 - cx / w, cy / h, bh / float(h))


def make_tracker(prefer_mediapipe: bool = True):
    if prefer_mediapipe:
        try:
            return MediaPipeTracker()
        except Exception:
            pass
    return SkinTracker()


# ── From observations to gestures ────────────────────────────────────────────

class GestureInterpreter:
    """Feed one HandObs (or None) per frame; returns an event name or None."""

    HOLD = {"open_palm": ("stop", 0.6), "two_fingers": ("play_pause", 0.8),
            "thumbs_up": ("yes", 0.8), "thumbs_down": ("no", 0.8)}

    def __init__(self, swipe_dx: float = 0.28, swipe_window: float = 0.6,
                 still: float = 0.08, cooldown: float = 1.2, volume_step: float = 0.07):
        self.swipe_dx, self.swipe_window = swipe_dx, swipe_window
        self.still, self.cooldown, self.volume_step = still, cooldown, volume_step
        self._track: deque = deque()            # (t, x, y)
        self._pose = None
        self._pose_since = 0.0
        self._pose_pts: list = []
        self._held_fired = False
        self._quiet_until = 0.0
        self._pinch_anchor: Optional[float] = None
        self._pinch_lost = 0.0

    def _fire(self, t: float, name: str) -> str:
        self._quiet_until = t + self.cooldown
        self._track.clear()
        self._held_fired = True
        return name

    def feed(self, obs: Optional[HandObs], t: float) -> Optional[str]:
        if obs is None:
            if self._pinch_anchor is not None and t - self._pinch_lost > 0.4:
                self._pinch_anchor = None
            self._pose = None
            self._held_fired = False
            while self._track and t - self._track[0][0] > self.swipe_window:
                self._track.popleft()
            return None

        # Pinch moved up / down -> volume steps, no cooldown between steps.
        if obs.pose == "pinch":
            self._pinch_lost = t
            if self._pinch_anchor is None:
                self._pinch_anchor = obs.y
                self._track.clear()
                return None
            moved = self._pinch_anchor - obs.y          # up is positive
            if abs(moved) >= self.volume_step:
                self._pinch_anchor = obs.y
                return "volume_up" if moved > 0 else "volume_down"
            return None
        if self._pinch_anchor is not None and t - self._pinch_lost > 0.4:
            self._pinch_anchor = None

        # Swipe: a quick, mostly horizontal run across the picture.
        self._track.append((t, obs.x, obs.y))
        while self._track and t - self._track[0][0] > self.swipe_window:
            self._track.popleft()
        if t >= self._quiet_until and len(self._track) >= 3:
            dx = self._track[-1][1] - self._track[0][1]
            dy = self._track[-1][2] - self._track[0][2]
            if abs(dx) >= self.swipe_dx and abs(dy) < 0.6 * abs(dx):
                self._pose = None
                return self._fire(t, "next" if dx > 0 else "previous")

        # Held poses: the same pose, held still for a moment.
        if obs.pose != self._pose:
            self._pose, self._pose_since, self._pose_pts = obs.pose, t, []
            self._held_fired = False
        self._pose_pts.append((obs.x, obs.y))
        if obs.pose in self.HOLD and not self._held_fired and t >= self._quiet_until:
            name, hold = self.HOLD[obs.pose]
            if t - self._pose_since >= hold:
                xs = [p[0] for p in self._pose_pts]
                ys = [p[1] for p in self._pose_pts]
                if max(xs) - min(xs) <= self.still and max(ys) - min(ys) <= self.still:
                    return self._fire(t, name)
                # moving too much: restart the hold from here
                self._pose_since, self._pose_pts = t, [(obs.x, obs.y)]
        return None
