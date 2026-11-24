"""
Screen & webcam capture for JARVIS vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.
"""
from __future__ import annotations

import io
import subprocess
import json
import sys
from pathlib import Path

import numpy as np

from core.lazyimport import LazyModule, installed

# OpenCV is 224 ms and is only ever touched by the webcam path — see
# core/lazyimport.py. Screen capture, which is what this action does on almost
# every call, never reaches it. numpy above stays eager: main.py already
# imports it for audio, so it is in memory before this file is read.
_CV2 = installed("cv2")

cv2 = LazyModule("cv2")

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


_ASKED_SCREEN_ACCESS = False


def _launcher_name() -> str:
    """The program macOS holds responsible for this process — the one the
    user must switch on in Privacy & Security. Walks the parent processes for
    the first .app bundle (Terminal, iTerm, PyCharm...); else the outermost
    non-shell program. The model otherwise told the user to enable "the JARVIS
    app", which does not exist in that list."""
    try:
        import os
        import psutil
        proc, outer = psutil.Process(os.getpid()), ""
        while proc is not None and proc.pid > 1:
            exe = ""
            try:
                exe = proc.exe()
            except Exception:
                pass
            if ".app/" in exe:
                return exe.split(".app/")[0].rsplit("/", 1)[-1]
            base = exe.rsplit("/", 1)[-1]
            if base and base not in ("zsh", "bash", "sh", "fish", "login") and not base.startswith("python"):
                outer = exe
            proc = proc.parent()
        if outer:
            return outer.replace(os.path.expanduser("~"), "~")
    except Exception:
        pass
    return "Terminal (or the app you started JARVIS from)"


def _open_screen_recording_settings() -> None:
    for url in ("x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"):
        try:
            if subprocess.run(["open", url], capture_output=True, timeout=5).returncode == 0:
                return
        except Exception:
            pass


def _capture_without_own_windows_mac() -> bytes | None:
    """The main display as it looks WITHOUT the assistant's own windows.

    The HUD floats over whatever the user asked about — measured: a 980×728
    window in the middle of a full-screen Telegram — so a plain screenshot was
    mostly the assistant's own interface and it answered "I only see my own
    window". Quartz composites the listed windows only, so everything else on
    screen is captured as if the HUD were not there. None = use mss."""
    try:
        import Quartz
        import AppKit
        from core import screenview
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        ids = [int(w["kCGWindowNumber"]) for w in infos
               if not screenview.is_own_pid(int(w.get("kCGWindowOwnerPID", 0) or 0))]
        if not ids:
            return None
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        img = Quartz.CGWindowListCreateImageFromArray(bounds, ids, Quartz.kCGWindowImageDefault)
        if img is None or Quartz.CGImageGetWidth(img) == 0:
            return None
        rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(img)
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        return bytes(data) if data is not None else None
    except Exception as e:
        print(f"[Vision] ⚠️  capture without own windows failed ({e}) — plain screenshot")
        return None


def _capture_screen() -> tuple[bytes, str]:

    if _get_os() == "mac":
        # Without Screen Recording permission macOS still hands back a
        # "screenshot" — the wallpaper, the menu bar and our own windows, every
        # other app blanked. The assistant then honestly reported it could see
        # only its own interface, with an open Telegram right there. Say why
        # instead of sending that picture.
        try:
            import Quartz
            if not Quartz.CGPreflightScreenCaptureAccess():
                global _ASKED_SCREEN_ACCESS
                if not _ASKED_SCREEN_ACCESS:
                    _ASKED_SCREEN_ACCESS = True
                    Quartz.CGRequestScreenCaptureAccess()   # macOS shows its prompt once
                    _open_screen_recording_settings()
                who = _launcher_name()
                raise RuntimeError(
                    "macOS has not given Screen Recording permission, so a screenshot shows only "
                    "the wallpaper and JARVIS's own window - no other app. The System Settings page "
                    "'Privacy & Security > Screen & System Audio Recording' has been opened: the user "
                    f"must switch on '{who}' there (there is NO entry called JARVIS; if '{who}' is "
                    "missing, add it with +), then restart JARVIS. This is NOT the Accessibility "
                    "list. To judge a web page without the screen, use web_search mode='read'.")
        except RuntimeError:
            raise
        except Exception:
            pass
        png = _capture_without_own_windows_mac()
        if png:
            return _compress(png, "PNG")

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
