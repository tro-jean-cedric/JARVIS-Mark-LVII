"""
plugins/_faces.py — face detection shared by the webcam watchers.

Helper module (leading underscore: the plugin loader skips it).

Used by posture_guard, pushup_counter, face_lock and gesture_control.

Two detectors, best first:
  * YuNet (cv2.FaceDetectorYN) — small ONNX model from the official OpenCV
    model zoo, downloaded once on first use into memory/plugin_data/models/
    (git-ignored). Gives eye/nose/mouth landmarks, so head tilt can be measured.
  * Haar cascade shipped inside opencv-python (cv2.data.haarcascades) — used
    when the model cannot be downloaded. Boxes only, no landmarks.

Nothing here touches Qt; everything runs on the caller's worker thread. A
detector object is NOT shared between threads — each watcher builds its own.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import cv2  # type: ignore
    import numpy as np
    _CV2 = True
except Exception:  # pragma: no cover - optional dependency
    cv2 = None
    np = None
    _CV2 = False

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    # filename: (url, minimum plausible size in bytes)
    "face_detection_yunet_2023mar.onnx":
        (f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx", 100_000),
    "face_recognition_sface_2021dec.onnx":
        (f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx", 10_000_000),
}
YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"

_dl_lock = threading.Lock()


def available() -> bool:
    return _CV2


def models_dir() -> Path:
    try:
        from plugins import _announce
        base = _announce.data_dir()
    except Exception:
        base = Path(__file__).resolve().parent.parent / "memory" / "plugin_data"
    d = base / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(name: str, download: bool = True) -> Path:
    """Path to a cached model, downloading it first when allowed.
    Raises RuntimeError with a readable reason when it is not there."""
    if name not in MODELS:
        raise RuntimeError(f"unknown model {name}")
    url, min_size = MODELS[name]
    path = models_dir() / name
    with _dl_lock:
        if path.exists() and path.stat().st_size >= min_size:
            return path
        if not download:
            raise RuntimeError(f"the model {name} has not been downloaded yet")
        part = path.with_suffix(".part")
        try:
            import requests
            with requests.get(url, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            fh.write(chunk)
            size = part.stat().st_size
            if size < min_size:
                raise RuntimeError(f"download was too small ({size} bytes)")
            part.replace(path)
            print(f"[faces] downloaded {name} ({size // 1024} KB)")
            return path
        except Exception as e:
            try:
                part.unlink()
            except Exception:
                pass
            raise RuntimeError(f"could not download the face model {name}: {e}")


@dataclass
class Face:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0
    # (x, y) of the right and left eye (as seen by the camera), YuNet only
    eye_r: Optional[tuple] = None
    eye_l: Optional[tuple] = None
    row: object = None          # raw YuNet row (needed by FaceRecognizerSF)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def roll_degrees(self) -> Optional[float]:
        """Head tilt from the eye line, 0 = level. None without landmarks."""
        if not self.eye_r or not self.eye_l:
            return None
        (x1, y1), (x2, y2) = self.eye_r, self.eye_l
        if x1 > x2:
            (x1, y1), (x2, y2) = (x2, y2), (x1, y1)
        return math.degrees(math.atan2(y2 - y1, x2 - x1))


def largest(faces: list) -> Optional[Face]:
    return max(faces, key=lambda f: f.w * f.h) if faces else None


class FaceDetector:
    """YuNet when possible, otherwise a Haar cascade. `kind` says which."""

    def __init__(self, allow_download: bool = True, require_yunet: bool = False,
                 score_threshold: float = 0.75):
        if not _CV2:
            raise RuntimeError("OpenCV is not installed (pip install opencv-python)")
        self.kind = ""
        self.note = ""
        self._yunet = None
        self._haar = None
        self._size = None
        try:
            path = model_path(YUNET, download=allow_download)
            self._yunet = cv2.FaceDetectorYN.create(str(path), "", (320, 240),
                                                    score_threshold, 0.3, 20)
            self.kind = "yunet"
        except Exception as e:
            self.note = str(e)
            if require_yunet:
                raise RuntimeError(f"the face detector could not start ({e})")
        if self._yunet is None:
            cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            haar = cv2.CascadeClassifier(str(cascade))
            if haar.empty():
                raise RuntimeError("no face detector is available in this OpenCV install")
            self._haar = haar
            self.kind = "haar"

    def detect(self, frame) -> list:
        if frame is None:
            return []
        h, w = frame.shape[:2]
        if self._yunet is not None:
            if self._size != (w, h):
                self._yunet.setInputSize((w, h))
                self._size = (w, h)
            _, rows = self._yunet.detect(frame)
            out = []
            if rows is not None:
                for r in rows:
                    out.append(Face(float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                                    float(r[14]), (float(r[4]), float(r[5])),
                                    (float(r[6]), float(r[7])), r))
            return out
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray = cv2.equalizeHist(gray)
        min_side = max(24, int(min(w, h) * 0.12))
        boxes = self._haar.detectMultiScale(gray, 1.15, 5, minSize=(min_side, min_side))
        return [Face(float(x), float(y), float(bw), float(bh)) for (x, y, bw, bh) in boxes]


def shrink(frame, max_side: int = 480):
    """Downscale a frame for cheap detection. Returns (small, scale)."""
    h, w = frame.shape[:2]
    s = min(1.0, max_side / float(max(h, w)))
    if s >= 1.0:
        return frame, 1.0
    return cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA), s
