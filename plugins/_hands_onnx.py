"""
plugins/_hands_onnx.py — two hands, 21 points each, with OpenCV alone.

Helper module (leading underscore: the plugin loader skips it). Used by the
Holo Desk. No MediaPipe package and no onnxruntime: the two MediaPipe hand
models (palm detection, then hand landmarks) run on cv2.dnn, which ships with
opencv-python. The models come from the official OpenCV model zoo and are
downloaded once into memory/plugin_data/models/ by plugins/_faces.py.

The pre- and post-processing is adapted from the OpenCV model zoo's
mp_palmdet.py and mp_handpose.py (Copyright OpenCV, Apache License 2.0,
https://github.com/opencv/opencv_zoo). Changes: the 2016 SSD anchors are
generated instead of listed; a hand seen in the last frame is followed by
cropping round its previous landmarks, so the slower palm detector runs only
when a hand is missing; results are returned mirrored and normalised.

Coordinates: x, y are 0..1 of the picture, x MIRRORED (moving the hand to the
user's right increases x), the convention plugins/_hands.py already uses.
Landmark layout is MediaPipe's: 0 wrist, 4 thumb tip, 8 index tip, 12 middle
tip, 16 ring tip, 20 pinky tip; 5/9/13/17 the finger bases.

Nothing here touches Qt. A Tracker is not thread-safe: one per worker thread.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import cv2  # type: ignore
    import numpy as np
    _CV2 = True
except Exception:  # pragma: no cover - optional dependency
    cv2 = None
    np = None
    _CV2 = False

PALM_MODEL = "palm_detection_mediapipe_2023feb.onnx"
HAND_MODEL = "handpose_estimation_mediapipe_2023feb.onnx"


def available() -> bool:
    return _CV2


@dataclass
class Hand:
    pts: list                    # 21 (x, y), mirrored, 0..1 of the picture
    z: list = field(default_factory=list)   # relative depth per point (smaller = nearer)
    side: str = ""               # "right" | "left" as the user would say it
    conf: float = 0.0

    @property
    def size(self) -> float:
        """Wrist to middle-finger base: grows as the hand comes closer."""
        return _dist(self.pts[0], self.pts[9])

    @property
    def center(self) -> tuple:
        ids = (0, 5, 9, 13, 17)
        return (sum(self.pts[i][0] for i in ids) / 5, sum(self.pts[i][1] for i in ids) / 5)

    @property
    def pinch_point(self) -> tuple:
        a, b = self.pts[4], self.pts[8]
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    @property
    def pinch_ratio(self) -> float:
        """Thumb tip to index tip, in palm sizes. ~0.2 pinched, >0.6 open."""
        return _dist(self.pts[4], self.pts[8]) / max(self.size, 1e-6)

    def extended(self, tip: int, pip: int) -> bool:
        w = self.pts[0]
        return _dist(self.pts[tip], w) > _dist(self.pts[pip], w) * 1.15

    @property
    def open_palm(self) -> bool:
        return all(self.extended(t, p) for t, p in ((8, 6), (12, 10), (16, 14), (20, 18)))

    @property
    def pointing(self) -> bool:
        """Index out, the other three curled."""
        return (self.extended(8, 6) and not self.extended(12, 10)
                and not self.extended(16, 14) and not self.extended(20, 18))

    @property
    def angle(self) -> float:
        """Direction wrist -> middle base, radians; turning the hand changes it."""
        w, m = self.pts[0], self.pts[9]
        return math.atan2(m[1] - w[1], m[0] - w[0])


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def make_anchors() -> "np.ndarray":
    """The palm model's 2016 anchors: SSD on a 192 px input, strides 8, 16,
    16, 16 with two anchors per cell, same-stride layers merged (MediaPipe's
    ssd_anchors_calculator). Identical to the table in mp_palmdet.py."""
    out = []
    for stride, per_cell in ((8, 2), (16, 6)):
        n = 192 // stride
        for y in range(n):
            for x in range(n):
                for _ in range(per_cell):
                    out.append(((x + 0.5) / n, (y + 0.5) / n))
    return np.array(out, dtype=np.float32)


class _PalmDetector:
    SIZE = 192

    def __init__(self, path: str, score: float = 0.5, nms: float = 0.3):
        self.net = cv2.dnn.readNet(path)
        self.score, self.nms = score, nms
        self.anchors = make_anchors()

    def infer(self, image) -> "np.ndarray":
        """rows of [x1, y1, x2, y2, 7 x (x, y), score] in image pixels."""
        h, w = image.shape[:2]
        ratio = min(self.SIZE / h, self.SIZE / w)
        rh, rw = int(h * ratio), int(w * ratio)
        img = cv2.resize(image, (rw, rh))
        left, top = (self.SIZE - rw) // 2, (self.SIZE - rh) // 2
        img = cv2.copyMakeBorder(img, top, self.SIZE - rh - top, left, self.SIZE - rw - left,
                                 cv2.BORDER_CONSTANT, None, (0, 0, 0))
        blob = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)[None] / 255.0
        pad = np.array([left / ratio, top / ratio]).astype(np.int32)
        self.net.setInput(blob)
        out = self.net.forward(self.net.getUnconnectedOutLayersNames())
        score = 1 / (1 + np.exp(-out[1][0, :, 0].astype(np.float64)))
        delta = out[0][0]
        scale = max(w, h)
        cxy = delta[:, :2] / self.SIZE
        wh = delta[:, 2:4] / self.SIZE
        xy1 = (cxy - wh / 2 + self.anchors) * scale
        xy2 = (cxy + wh / 2 + self.anchors) * scale
        boxes = np.concatenate([xy1, xy2], axis=1) - [pad[0], pad[1], pad[0], pad[1]]
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), score.tolist(), self.score, self.nms, top_k=5000)
        if len(keep) == 0:
            return np.empty((0, 19))
        keep = np.array(keep).reshape(-1)
        lms = delta[keep, 4:].reshape(-1, 7, 2) / self.SIZE + self.anchors[keep][:, None, :]
        lms = lms * scale - pad
        return np.c_[boxes[keep], lms.reshape(-1, 14), score[keep]]


class _HandPose:
    SIZE = 224
    PALM_BASE, MIDDLE_BASE = 0, 2

    def __init__(self, path: str, conf: float = 0.8):
        self.net = cv2.dnn.readNet(path)
        self.conf = conf

    @staticmethod
    def _crop(image, box, rotation: bool):
        wh = box[1] - box[0]
        box = box + (np.array([0, 0]) if rotation else np.array([0, -0.4])) * wh
        center = box.sum(axis=0) / 2
        half = (box[1] - box[0]) * (4 if rotation else 3) / 2
        box = np.array([center - half, center + half]).astype(np.int32)
        box[:, 0] = np.clip(box[:, 0], 0, image.shape[1])
        box[:, 1] = np.clip(box[:, 1], 0, image.shape[0])
        image = image[box[0][1]:box[1][1], box[0][0]:box[1][0], :]
        side = int(np.linalg.norm(image.shape[:2]) if rotation else max(image.shape[:2]))
        ph, pw = side - image.shape[0], side - image.shape[1]
        left, top = pw // 2, ph // 2
        image = cv2.copyMakeBorder(image, top, ph - top, left, pw - left,
                                   cv2.BORDER_CONSTANT, None, (0, 0, 0))
        return image, box, box[0] - [left, top]

    def infer(self, image, palm) -> Optional["np.ndarray"]:
        """21 x (x, y, z) in image pixels, handedness, confidence — or None."""
        box = palm[0:4].reshape(2, 2)
        img, box, bias = self._crop(image, box, True)
        if img.size == 0:
            return None
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pad = np.array(bias, dtype=np.int32)
        box = box - pad
        lm7 = palm[4:18].reshape(7, 2) - pad
        p1, p2 = lm7[self.PALM_BASE], lm7[self.MIDDLE_BASE]
        rad = np.pi / 2 - np.arctan2(-(p2[1] - p1[1]), p2[0] - p1[0])
        rad = rad - 2 * np.pi * np.floor((rad + np.pi) / (2 * np.pi))
        angle = np.rad2deg(rad)
        center = box.sum(axis=0) / 2
        rot = cv2.getRotationMatrix2D(tuple(map(float, center)), angle, 1.0)
        rotated = cv2.warpAffine(img, rot, (img.shape[1], img.shape[0]))
        hom = np.c_[lm7, np.ones(7)]
        rl = np.array([hom @ rot[0], hom @ rot[1]])
        rbox = np.array([rl.min(axis=1), rl.max(axis=1)])
        crop, rbox, _ = self._crop(rotated, rbox, False)
        if crop.size == 0:
            return None
        blob = cv2.resize(crop, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA)
        self.net.setInput(blob.astype(np.float32)[None] / 255.0)
        lms, conf, handed, _world = self.net.forward(self.net.getUnconnectedOutLayersNames())
        conf = float(conf[0][0])
        if conf < self.conf:
            return None
        lms = lms[0].reshape(-1, 3).astype(np.float64)
        sf = max((rbox[1] - rbox[0]) / self.SIZE)
        lms[:, :2] = (lms[:, :2] - self.SIZE / 2) * sf
        lms[:, 2] *= sf
        r = cv2.getRotationMatrix2D((0, 0), angle, 1.0)
        lms[:, :2] = lms[:, :2] @ r[:, :2]
        rc = np.array([[rot[0][0], rot[1][0]], [rot[0][1], rot[1][1]]])
        tc = np.array([rot[0][2], rot[1][2]])
        inv = np.c_[rc, [-rc[0] @ tc, -rc[1] @ tc]]
        c = np.append(rbox.sum(axis=0) / 2, 1)
        lms[:, :2] += np.array([c @ inv[0], c @ inv[1]]) + pad
        return lms, float(handed[0][0]), conf


class Tracker:
    """Up to two hands per frame. `process(frame)` -> list[Hand]."""

    kind = "onnx"

    def __init__(self, max_hands: int = 2, detect_every: float = 0.35):
        from plugins import _faces
        self.palm = _PalmDetector(str(_faces.model_path(PALM_MODEL)))
        self.pose = _HandPose(str(_faces.model_path(HAND_MODEL)))
        self.max_hands = max_hands
        self.detect_every = detect_every
        self._prev: list = []            # pixel landmark arrays from the last frame
        self._last_detect = 0.0

    @staticmethod
    def _palm_from_landmarks(lms) -> "np.ndarray":
        """A palm-detector-shaped row built from last frame's landmarks, so the
        hand can be followed without running the detector again."""
        ids = [0, 5, 9, 13, 17, 1, 2]
        pts = lms[ids, :2]
        x1, y1 = pts.min(axis=0)
        x2, y2 = pts.max(axis=0)
        return np.r_[[x1, y1, x2, y2], pts.reshape(-1), [1.0]]

    @staticmethod
    def _overlap(a, b) -> bool:
        ca, cb = a[:, :2].mean(axis=0), b[:, :2].mean(axis=0)
        span = max(np.ptp(a[:, 0]), np.ptp(a[:, 1]), 1.0)
        return np.linalg.norm(ca - cb) < span * 0.5

    def process(self, frame) -> list:
        h, w = frame.shape[:2]
        found = []
        for lms in self._prev:
            got = self.pose.infer(frame, self._palm_from_landmarks(lms))
            if got is not None:
                found.append(got)
        now = time.monotonic()
        if len(found) < self.max_hands and now - self._last_detect >= (
                0.0 if not found else self.detect_every):
            self._last_detect = now
            for palm in self.palm.infer(frame)[: self.max_hands + 1]:
                got = self.pose.infer(frame, palm)
                if got is None or any(self._overlap(got[0], f[0]) for f in found):
                    continue
                found.append(got)
                if len(found) >= self.max_hands:
                    break
        self._prev = [f[0] for f in found]
        hands = []
        for lms, handed, conf in found:
            pts = [(1.0 - float(x) / w, float(y) / h) for x, y in lms[:, :2]]
            # The model's handedness is for the un-mirrored picture; mirrored,
            # what it calls left is the user's right hand.
            side = "right" if handed < 0.5 else "left"
            hands.append(Hand(pts=pts, z=[float(v) / w for v in lms[:, 2]], side=side, conf=conf))
        return hands
