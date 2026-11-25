"""gui/holo_desk.py — the Holo Desk: the camera picture fills the screen and
glass panels float in it, moved by the user's hands.

Qt main thread only. Built and driven by MainWindow (`_holo_sig`), which the
voice tool reaches through `JarvisUI.holo_desk(cmd, **kw)`; nothing outside
gui/ creates it.

WHAT RUNS WHERE
  worker thread  camera frames from plugins/_camera.py (the ONE shared webcam)
                 and hand landmarks from plugins/_hands_onnx.py; it hands both
                 to the Qt thread through `_frame_sig` and never touches a widget
  Qt thread      a 60 fps timer: hands -> pointers (HandFilter) -> gestures
                 (Input) -> desk physics (Desk.step) -> repaint
  more threads   panel content (plugins/_holo_desk_content.py) and video decoding

The desk itself (plugins/_holo_desk_core.Desk) is shared with the voice tool,
which adds and removes panels from its own thread; both sides take its lock.

MODES
  camera   the mirrored camera picture behind the panels; Esc closes
  overlay  a transparent window over the user's own programs; the mouse goes
           to those programs ("the mouse is yours"), the hands still work
"""
from __future__ import annotations

import math
import os
import struct
import tempfile
import threading
import time
import webbrowser
import wave
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPolygonF)
from PyQt6.QtWidgets import QWidget

from .style import chamfer, hud_font, mono_font, px_font, with_alpha
from .theme import C

FPS = 60
_GLYPH = {"clock": "◷", "system": "▤", "weather": "☁", "news": "◈", "notes": "✎",
          "routines": "↻", "model": "◬", "guide": "?", "image": "▣", "video": "▶", "text": "≡"}
_BASE_DIR = Path(__file__).resolve().parent.parent


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _settings() -> dict:
    """The Holo Desk's settings (plugins/holo_desk.py PLUGIN_SETTINGS)."""
    out = {"sensitivity": "normal", "sounds": True, "skeleton": True, "remember": True}
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config("holo_desk") or {}
        s = str(cfg.get("pinch_sensitivity") or "normal").lower()
        out["sensitivity"] = s if s in ("gentle", "normal", "firm") else "normal"
        for key, name in (("sounds", "sounds"), ("skeleton", "show_hands"),
                          ("remember", "remember_layout")):
            if name in cfg:
                out[key] = _truthy(cfg[name])
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Small sounds, made once
# ─────────────────────────────────────────────────────────────────────────────
class _Sounds:
    """Short synthesised clicks and whooshes through QSoundEffect, so they
    never touch the sounddevice streams the voice uses. Silently absent when
    QtMultimedia is."""

    SPEC = {"grab": (1500, 0.035, 0.25), "drop": (900, 0.05, 0.2), "press": (2000, 0.03, 0.25),
            "added": (1200, 0.09, 0.22), "throw": (0, 0.22, 0.25), "closed": (600, 0.12, 0.2),
            "clap": (0, 0.30, 0.3), "restore": (1000, 0.25, 0.22), "ring": (1700, 0.12, 0.18)}

    def __init__(self):
        self._fx = {}
        try:
            from PyQt6.QtMultimedia import QSoundEffect
        except Exception:
            return
        folder = Path(tempfile.gettempdir()) / "jarvis_holo_sounds"
        folder.mkdir(exist_ok=True)
        for name, (freq, dur, vol) in self.SPEC.items():
            path = folder / f"{name}.wav"
            if not path.exists():
                self._write(path, freq, dur)
            fx = QSoundEffect()
            fx.setSource(QUrl.fromLocalFile(str(path)))
            fx.setVolume(vol)
            self._fx[name] = fx

    @staticmethod
    def _write(path: Path, freq: float, dur: float) -> None:
        import random
        rate = 22050
        n = int(rate * dur)
        rnd = random.Random(7)
        frames = bytearray()
        for i in range(n):
            t = i / rate
            env = math.sin(math.pi * i / n) ** 2
            if freq:                                   # a soft tone that falls a little
                v = math.sin(2 * math.pi * freq * t * (1 - 0.3 * i / n))
            else:                                      # a whoosh: filtered noise
                v = rnd.uniform(-1, 1) * (0.4 + 0.6 * math.sin(math.pi * i / n))
            frames += struct.pack("<h", int(v * env * 12000))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))

    def play(self, name: str) -> None:
        fx = self._fx.get(name)
        if fx is not None:
            fx.play()


# ─────────────────────────────────────────────────────────────────────────────
#  3D model: a wireframe from an .obj
# ─────────────────────────────────────────────────────────────────────────────
def load_obj(path: str, max_edges: int = 4000) -> tuple:
    """(vertices normalised to a unit box, edges, triangles). Polygons are
    fanned into triangles; a big model keeps every n-th edge and triangle so
    it still draws at 60 fps."""
    verts, edges, tris = [], set(), []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                parts = line.split()
                verts.append(tuple(float(v) for v in parts[1:4]))
            elif line.startswith("f "):
                idx = [int(tok.split("/")[0]) for tok in line.split()[1:]]
                idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
                for a, b in zip(idx, idx[1:] + idx[:1]):
                    edges.add((min(a, b), max(a, b)))
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not verts:
        raise ValueError("the file holds no vertices")
    xs, ys, zs = zip(*verts)
    cx, cy, cz = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2
    s = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
    verts = [((x - cx) / s, (y - cy) / s, (z - cz) / s) for x, y, z in verts]
    edges = sorted(edges)
    if len(edges) > max_edges:
        step = len(edges) / max_edges
        edges = [edges[int(i * step)] for i in range(max_edges)]
    max_tris = max_edges
    if len(tris) > max_tris:
        step = len(tris) / max_tris
        tris = [tris[int(i * step)] for i in range(max_tris)]
    return verts, edges, tris


# ─────────────────────────────────────────────────────────────────────────────
#  Video panels: frames decoded on their own thread
# ─────────────────────────────────────────────────────────────────────────────
class _Video:
    def __init__(self, source: str):
        self.source = source
        self.frame: QImage | None = None
        self.error = ""
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="holo-video").start()

    def _run(self) -> None:
        try:
            import cv2
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                self.error = "the video could not be opened"
                return
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            delay = 1.0 / max(5.0, min(60.0, fps))
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)          # loop it
                    ok, frame = cap.read()
                    if not ok:
                        break
                h, w = frame.shape[:2]
                if w > 960:
                    frame = cv2.resize(frame, (960, int(h * 960 / w)))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                hh, ww = rgb.shape[:2]
                self.frame = QImage(rgb.data, ww, hh, ww * 3, QImage.Format.Format_RGB888).copy()
                self.stop.wait(delay)
            cap.release()
        except Exception as e:
            self.error = f"the video stopped: {e}"


# ─────────────────────────────────────────────────────────────────────────────
#  The window
# ─────────────────────────────────────────────────────────────────────────────
class HoloDeskWindow(QWidget):
    _frame_sig = pyqtSignal(object, object, float)     # QImage | None, hands, time
    _status_sig = pyqtSignal(str)

    def __init__(self, desk, mode: str = "camera", on_close=None, log=None):
        super().__init__(None)
        from plugins import _holo_desk_core as core
        self.core = core
        self.desk = desk
        self.inp = core.Input(desk)
        self.settings = _settings()
        self.filter = core.HandFilter(self.settings["sensitivity"])
        self.on_close = on_close
        self.log = log or (lambda s: None)
        self.mode = "camera"
        self.frame: QImage | None = None
        self.hands: list = []
        self.hands_at = 0.0
        self.mouse = None                              # Pointer or None
        self.pointers: list = []
        self.effects: list = []                        # (kind, x, y, t0, colour)
        self.status = "Starting the camera..."
        self.hint_until = time.monotonic() + 9
        self.hint = "Raise your hand - pinch a panel to move it, pinch a button to open one"
        self.camera_error = ""
        self._images: dict = {}                        # pid -> (arg, QImage | None)
        self._videos: dict = {}                        # pid -> _Video
        self._models: dict = {}                        # pid -> (arg, verts, edges) | error
        self._stop = threading.Event()
        self._last = time.monotonic()
        self._closing = False
        self.sounds = _Sounds() if self.settings["sounds"] else None

        self.setWindowTitle("Holo Desk")
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._frame_sig.connect(self._on_frame)
        self._status_sig.connect(self._on_status)
        self.set_mode(mode)

        # Panel content loads off the Qt thread, for new panels and for the
        # ones already on the desk, and stays fresh while the desk is open.
        from plugins import _holo_desk_content as content
        self.content = content
        desk.on_added = lambda panel: content.load(panel, desk)
        for panel in desk.live():
            content.load(panel, desk)
        threading.Thread(target=content.refresher, args=(desk, self._stop), daemon=True,
                         name="holo-refresh").start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / FPS))
        threading.Thread(target=self._lock_loop, daemon=True, name="holo-lock").start()
        self._worker = threading.Thread(target=self._camera_loop, daemon=True, name="holo-desk")
        self._worker.start()
        core.STATE.update(open=True, mode=self.mode, camera="", since=time.time())

    # ── modes ────────────────────────────────────────────────────────────────
    def set_mode(self, mode: str) -> None:
        mode = "overlay" if mode == "overlay" else "camera"
        self.mode = mode
        geo = self.geometry() if self.isVisible() else None
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool)
        if mode == "overlay":
            flags |= Qt.WindowType.WindowTransparentForInput
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, mode == "overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, mode == "overlay")
        if was_visible:
            if geo is not None:
                self.setGeometry(geo)
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
        self.core.STATE["mode"] = mode
        self.update()

    def open_on(self, screen) -> None:
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    # ── camera + hands, on the worker thread ─────────────────────────────────
    def _camera_loop(self) -> None:
        from plugins import _camera
        name = "holo_desk"
        tracker = None
        try:
            if not _camera.subscribe(name):
                self._status_sig.emit("camera:OpenCV is not installed")
                return
            if _camera.wait_frame(8.0) is None:
                self._status_sig.emit("camera:" + (_camera.last_error()
                                                   or "no camera shows a live picture"))
            try:
                self._status_sig.emit("Loading the hand models (the first time downloads ~8 MB)...")
                from plugins import _hands_onnx
                tracker = _hands_onnx.Tracker()
                self._status_sig.emit("")
            except Exception as e:
                self._status_sig.emit(f"hands:the hand models could not be loaded ({e})")
            import cv2
            last = None
            while not self._stop.wait(1 / 40):
                frame = _camera.latest(1.0)
                if frame is None or frame is last:
                    continue
                last = frame
                hands = []
                if tracker is not None:
                    h, w = frame.shape[:2]
                    small = frame if w <= 640 else cv2.resize(frame, (640, int(h * 640 / w)))
                    try:
                        hands = tracker.process(small)
                    except Exception as e:
                        print(f"[HoloDesk] hand tracking error: {e}")
                img = None
                if self.mode == "camera":
                    h, w = frame.shape[:2]
                    if w > 1280:
                        frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
                    rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                    hh, ww = rgb.shape[:2]
                    img = QImage(rgb.data, ww, hh, ww * 3, QImage.Format.Format_RGB888).copy()
                if self._stop.is_set():
                    break
                self._frame_sig.emit(img, hands, time.monotonic())
        except RuntimeError:
            pass                       # the window was closed while a frame was in flight
        except Exception as e:
            print(f"[HoloDesk] camera loop stopped: {e}")
        finally:
            try:
                _camera.unsubscribe(name)
            except Exception:
                pass

    def _lock_loop(self) -> None:
        """Close the desk when the computer is locked: a full-screen camera
        picture must not stay up behind the lock screen. Polled here, off the
        Qt thread, because on Linux the check runs loginctl."""
        from core.screen_lock import is_locked
        while not self._stop.wait(2.0):
            if is_locked():
                try:
                    self._status_sig.emit("locked")
                except RuntimeError:
                    pass
                return

    def _on_status(self, text: str) -> None:
        if text == "locked":
            print("[HoloDesk] the computer was locked: closing")
            self.log("Holo Desk closed: the computer was locked.")
            self.request_close()
            return
        if text.startswith("camera:"):
            self.camera_error = text[7:]
            self.core.STATE["camera"] = self.camera_error
            self.status = f"No camera: {self.camera_error}. The mouse works meanwhile."
        elif text.startswith("hands:"):
            self.status = text[6:] + " - the mouse still works."
        else:
            self.status = text
        self.update()

    def _on_frame(self, img, hands, t) -> None:
        if img is not None:
            self.frame = img
            if self.camera_error:
                self.camera_error = ""
                self.core.STATE["camera"] = ""
                self.status = ""
        self.hands, self.hands_at = hands, t

    # ── mapping the camera picture onto the screen ───────────────────────────
    def _cover(self) -> tuple:
        """(scale, offset x, offset y, picture w, h) for a picture covering the window."""
        W, H = max(1, self.width()), max(1, self.height())
        fw, fh = (self.frame.width(), self.frame.height()) if self.frame is not None else (16, 9)
        s = max(W / fw, H / fh)
        return s, (W - fw * s) / 2, (H - fh * s) / 2, fw, fh

    def _to_screen(self, x: float, y: float) -> tuple:
        """A hand's place in the picture -> 0..1 of the screen.

        Camera mode: exactly where the hand is in the picture behind, so the
        cursor sits on the fingers. Overlay mode shows no picture, so there
        nothing has to line up and the map is stretched round the centre:
        the corners are reachable without leaving the camera's view."""
        s, ox, oy, fw, fh = self._cover()
        W, H = max(1, self.width()), max(1, self.height())
        sx, sy = (x * fw * s + ox) / W, (y * fh * s + oy) / H
        reach = 1.0 if self.mode == "camera" else 1.2
        return 0.5 + (sx - 0.5) * reach, 0.5 + (sy - 0.5) * reach

    # ── the frame tick (Qt thread) ───────────────────────────────────────────
    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last = min(0.1, now - self._last), now
        hands = self.hands if now - self.hands_at < 0.4 else []
        pointers = self.filter.update(hands, self._to_screen, now)
        if self.mouse is not None:
            pointers.append(self.mouse)
        self.pointers = pointers
        events = self.inp.update(pointers, now) + self.desk.step(dt)
        for ev in events:
            self._event(ev, now)
        self.effects = [e for e in self.effects if now - e[3] < 0.7]
        self.update()

    def _event(self, ev, now: float) -> None:
        kind = ev[0]
        if self.sounds is not None:
            self.sounds.play(kind)
        if kind in ("tap", "ring"):
            self.effects.append(("ripple", ev[1], ev[2], now, C.PRI))
        elif kind in ("closed", "throw", "grab", "added", "focus"):
            p = next((q for q in self.desk.panels if q.pid == ev[1]), None)
            if p is not None:
                col = {"closed": C.RED, "throw": C.ACC2}.get(kind, C.PRI)
                self.effects.append(("ripple", p.x + p.w / 2, p.y + p.h / 2, now, col))
        elif kind in ("clap", "restore"):
            self.effects.append(("flash", 0.5, 0.5, now, C.PRI if kind == "restore" else C.ACC))
        if kind == "row":
            self._press_row(ev[1], ev[2])
        if kind == "closed":
            self._forget(ev[1])

    def _press_row(self, pid: int, row: int) -> None:
        p = self.desk.get(pid)
        if p is None or row < 0 or row >= len(p.rows):
            return
        text, link = p.rows[row][0], (p.rows[row][1] if len(p.rows[row]) > 1 else "")
        if link.startswith("note:"):
            path = link[5:]
            panel = self.desk.add("text", title=text, text="Opening the note...")
            threading.Thread(target=lambda: setattr(panel, "text", self.content.read_note(path)),
                             daemon=True).start()
            return
        if link:
            try:
                webbrowser.open(link)
                self.log(f"Holo Desk: opened {link}")
            except Exception as e:
                self.log(f"Holo Desk: could not open {link}: {e}")
        else:
            # A row without a link (a note, a routine): show it whole in a text panel.
            self.desk.add("text", title=p.heading(), text=text)

    def _forget(self, pid: int) -> None:
        v = self._videos.pop(pid, None)
        if v is not None:
            v.stop.set()
        self._images.pop(pid, None)
        self._models.pop(pid, None)

    # ── mouse and keys (they drive the same Input as the hands) ─────────────
    def _mouse_at(self, e, down: bool) -> None:
        pos = e.position()
        self.mouse = self.core.Pointer("mouse", pos.x() / max(1, self.width()),
                                       pos.y() / max(1, self.height()), down)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._mouse_at(e, True)

    def mouseMoveEvent(self, e):
        self._mouse_at(e, bool(e.buttons() & Qt.MouseButton.LeftButton))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._mouse_at(e, False)

    def leaveEvent(self, e):
        self.mouse = None

    def wheelEvent(self, e):
        pos = e.position()
        x, y = pos.x() / max(1, self.width()), pos.y() / max(1, self.height())
        p = self.desk.at(x, y)
        if p is None:
            return
        steps = e.angleDelta().y() / 120.0
        if p.kind in self.core.LIST_KINDS and not p.on_header(x, y) and p.rows:
            self.desk.scroll(p.pid, -steps * 2)
        else:
            self.desk.resize(p.pid, 1.08 ** steps)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.request_close()
        else:
            super().keyPressEvent(e)

    def request_close(self) -> None:
        if callable(self.on_close):
            self.on_close()
        else:
            self.shutdown()

    def _teardown(self) -> None:
        """Stop everything once; keep the layout for next time first, so it is
        saved before anyone is told the desk is closed."""
        self._closing = True
        self._stop.set()
        self._timer.stop()
        for v in self._videos.values():
            v.stop.set()
        self._videos.clear()
        with self.desk.lock:
            for p in self.desk.panels:
                p.held = False
        if self.settings.get("remember"):
            self.core.save_last(self.desk)
        self.core.STATE.update(open=False, camera="")

    def shutdown(self) -> None:
        if self._closing:
            return
        self._teardown()
        self.close()
        self.deleteLater()

    def closeEvent(self, e):
        if not self._closing:
            self._teardown()
            if callable(self.on_close):
                self.on_close(already_closed=True)
        super().closeEvent(e)

    # ─────────────────────────────────────────────────────────────────────────
    #  Painting
    # ─────────────────────────────────────────────────────────────────────────
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        W, H = self.width(), self.height()
        now = time.monotonic()
        if self.mode == "camera":
            p.fillRect(self.rect(), QColor(C.BG))
            if self.frame is not None:
                s, ox, oy, fw, fh = self._cover()
                p.drawImage(QRectF(ox, oy, fw * s, fh * s), self.frame)
            # The picture is tinted towards the theme so the panels read on it.
            p.fillRect(self.rect(), with_alpha(C.BG, 0.38))
            grad = QLinearGradient(0, 0, 0, H)
            grad.setColorAt(0, with_alpha(C.PRI_GHO, 0.55))
            grad.setColorAt(0.25, with_alpha(C.PRI_GHO, 0.0))
            grad.setColorAt(0.8, with_alpha(C.BG, 0.0))
            grad.setColorAt(1, with_alpha(C.BG, 0.7))
            p.fillRect(self.rect(), grad)
            pen = QPen(with_alpha(C.PRI, 0.035))
            p.setPen(pen)
            for y in range(0, H, 4):
                p.drawLine(0, y, W, y)
        else:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        with self.desk.lock:
            panels = list(self.desk.panels)
            focused = self.desk.focused
        for panel in panels:
            self._paint_panel(p, panel, now, panel.pid == focused)
        self._paint_dock(p)
        self._paint_ring(p)
        self._paint_effects(p, now)
        self._paint_cursors(p)
        self._paint_status(p, now)
        p.end()

    def _rect(self, panel) -> QRectF:
        W, H = self.width(), self.height()
        return QRectF(panel.x * W, panel.y * H, panel.w * W, panel.h * H)

    def _paint_panel(self, p: QPainter, panel, now: float, focused: bool) -> None:
        r = self._rect(panel)
        alpha = 1.0
        scale = 1.0
        if panel.gone:
            k = min(1.0, (now - panel.gone) / self.desk.VANISH)
            alpha, scale = 1 - k, 1 - 0.15 * k
        elif now - panel.born < 0.3:
            k = (now - panel.born) / 0.3
            alpha, scale = k, 0.85 + 0.15 * k
        if alpha <= 0.01:
            return
        if scale != 1.0:
            c = r.center()
            r = QRectF(c.x() - r.width() * scale / 2, c.y() - r.height() * scale / 2,
                       r.width() * scale, r.height() * scale)
        p.save()
        p.setOpacity(alpha)
        H = self.height()
        cut = min(18.0, r.width() * 0.08)
        shape = chamfer(r, cut, ("tl", "br"))
        if panel.held:                                 # lifted: a shadow under it
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha("#000000", 0.45))
            p.drawPolygon(chamfer(r.translated(10, 14), cut, ("tl", "br")))
        p.setPen(Qt.PenStyle.NoPen)
        glass = QLinearGradient(r.topLeft(), r.bottomLeft())
        glass.setColorAt(0, with_alpha(C.PANEL2, 0.90))
        glass.setColorAt(1, with_alpha(C.PANEL, 0.78))
        p.setBrush(glass)
        p.drawPolygon(shape)
        edge = C.PRI if (focused or panel.held) else C.BORDER_B
        for width, a in ((6, 0.10), (3, 0.22), (1.3, 0.95)):
            p.setPen(QPen(with_alpha(edge, a), width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(shape)
        # Title bar
        hh = self.core.HEADER * H
        head = QRectF(r.left(), r.top(), r.width(), hh)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(C.PRI_GHO, 0.9))
        p.drawPolygon(chamfer(head, min(cut, hh), ("tl",)))
        p.setPen(QColor(C.PRI))
        p.setFont(hud_font(max(8.0, hh * 0.30), QFont.Weight.Bold))
        title = f"{_GLYPH.get(panel.kind, '')}  {panel.heading().upper()}"
        fm = QFontMetricsF(p.font())
        title = fm.elidedText(title, Qt.TextElideMode.ElideRight, r.width() - hh - cut - 16)
        p.drawText(QRectF(r.left() + cut + 4, r.top(), r.width() - hh - cut, hh),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
        p.setPen(QColor(C.TEXT_DIM))
        p.setFont(hud_font(max(8.0, hh * 0.34)))
        p.drawText(QRectF(r.right() - hh, r.top(), hh, hh), Qt.AlignmentFlag.AlignCenter, "✕")
        body = QRectF(r.left() + 12, r.top() + hh + 6, r.width() - 24, r.height() - hh - 14)
        p.setClipRect(body.adjusted(-2, -2, 2, 2))
        try:
            self._paint_body(p, panel, body, now)
        except Exception as e:
            p.setPen(QColor(C.RED))
            p.setFont(hud_font(9))
            p.drawText(body, Qt.TextFlag.TextWordWrap, f"Could not draw this panel: {e}")
        p.restore()

    # ── panel bodies ─────────────────────────────────────────────────────────
    def _text(self, p, rect, text, size, colour=None, align=None, bold=False,
              fit: bool = False) -> None:
        """Text in a rect, word-wrapped. With fit, the size shrinks until the
        widest line fits on one line — for big figures (a clock, a temperature)
        that must not wrap or be cut."""
        weight = QFont.Weight.Bold if bold else None
        font = hud_font(size, weight)
        if fit and text:
            widest = max(text.split("\n"), key=len)
            while size > 7:
                font = hud_font(size, weight)
                if QFontMetricsF(font).horizontalAdvance(widest) <= rect.width() - 2:
                    break
                size *= 0.92
        p.setPen(QColor(colour or C.TEXT))
        p.setFont(font)
        p.drawText(rect, int(align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop))
                   | int(Qt.TextFlag.TextWordWrap), text)

    def _paint_body(self, p: QPainter, panel, b: QRectF, now: float) -> None:
        k = panel.kind
        unit = max(7.0, b.height() / 14)
        if panel.status and k not in ("clock", "system") and not panel.rows and not panel.text:
            self._text(p, b, panel.status, unit * 0.9, C.TEXT_DIM)
            return
        if k == "clock":
            self._text(p, QRectF(b.left(), b.top(), b.width(), b.height() * 0.62),
                       time.strftime("%H:%M:%S"), b.height() * 0.30,
                       C.WHITE, Qt.AlignmentFlag.AlignCenter, bold=True, fit=True)
            self._text(p, QRectF(b.left(), b.top() + b.height() * 0.62, b.width(), b.height() * 0.38),
                       time.strftime("%A %d %B %Y"), unit * 0.9, C.TEXT_MED,
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            return
        if k == "system":
            self._paint_system(p, b, unit)
            return
        if k in self.core.LIST_KINDS and panel.rows:
            self._paint_rows(p, panel, b, unit)
            return
        if k == "image":
            self._paint_image(p, panel, b, unit)
            return
        if k == "video":
            self._paint_video(p, panel, b, unit)
            return
        if k == "model":
            self._paint_model(p, panel, b, unit)
            return
        if k == "weather" and panel.text:
            lines = panel.text.split("\n")
            self._text(p, QRectF(b.left(), b.top(), b.width(), b.height() * 0.42), lines[0],
                       b.height() * 0.16, C.WHITE, bold=True, fit=True)
            self._text(p, QRectF(b.left(), b.top() + b.height() * 0.42, b.width(), b.height() * 0.58),
                       "\n".join(lines[1:]), unit * 0.85, C.TEXT_MED)
            return
        self._text(p, b, panel.body() or "(empty)", unit * 0.9)

    def _paint_system(self, p, b, unit) -> None:
        lines = self.core.system_lines()
        n = max(1, len(lines))
        row = b.height() / n
        for i, line in enumerate(lines):
            y = b.top() + i * row
            label = line.split()[0] if line.split() else ""
            pct = None
            for tok in line.replace("%", " % ").split():
                try:
                    pct = float(tok)
                    break
                except ValueError:
                    continue
            self._text(p, QRectF(b.left(), y, b.width(), row * 0.5), line, unit * 0.8, C.TEXT_MED)
            if pct is not None and label in ("CPU", "RAM", "BAT", "DISK"):
                bar = QRectF(b.left(), y + row * 0.55, b.width(), max(4.0, row * 0.18))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(C.BAR_BG, 0.9))
                p.drawRect(bar)
                col = C.RED if (pct > 85 and label != "BAT") else C.PRI
                p.setBrush(QColor(col))
                p.drawRect(QRectF(bar.left(), bar.top(), bar.width() * min(1.0, pct / 100), bar.height()))

    def _paint_rows(self, p, panel, b, unit) -> None:
        H = self.height()
        row_h = self.core.Input.ROW_H * H
        first = int(panel.scroll)
        frac = panel.scroll - first
        y = b.top() - frac * row_h
        p.setFont(hud_font(max(7.0, row_h * 0.30)))
        fm = QFontMetricsF(p.font())
        hover_rows = set()
        for hx, hy, what in self.inp.hover.values():
            if what == f"panel:{panel.pid}":
                hover_rows.add(self.inp.row_at(panel, hy))
        for i in range(first, len(panel.rows)):
            if y > b.bottom():
                break
            text = panel.rows[i][0]
            link = panel.rows[i][1] if len(panel.rows[i]) > 1 else ""
            rr = QRectF(b.left(), y, b.width(), row_h)
            if i in hover_rows:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(C.PRI, 0.14))
                p.drawRect(rr)
            p.setPen(QColor(C.PRI if link else C.TEXT))
            p.drawText(rr.adjusted(4, 0, -4, 0), int(Qt.AlignmentFlag.AlignVCenter),
                       fm.elidedText(("›  " if link else "·  ") + text, Qt.TextElideMode.ElideRight,
                                     rr.width() - 8))
            p.setPen(QPen(with_alpha(C.BORDER, 0.6), 1))
            p.drawLine(QPointF(rr.left(), rr.bottom()), QPointF(rr.right(), rr.bottom()))
            y += row_h
        if len(panel.rows) * row_h > b.height():          # a scroll bar
            frac_seen = b.height() / (len(panel.rows) * row_h)
            pos = panel.scroll / max(1, len(panel.rows))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(C.PRI, 0.5))
            p.drawRect(QRectF(b.right() - 3, b.top() + pos * b.height(), 3,
                              max(12.0, frac_seen * b.height())))

    def _paint_image(self, p, panel, b, unit) -> None:
        cached = self._images.get(panel.pid)
        if cached is None or cached[0] != panel.arg:
            img = QImage(panel.arg) if panel.arg and os.path.isfile(panel.arg) else QImage()
            cached = (panel.arg, None if img.isNull() else img)
            self._images[panel.pid] = cached
        img = cached[1]
        if img is None:
            self._text(p, b, "This picture could not be opened.", unit * 0.9, C.TEXT_DIM)
            return
        self._fit(p, img, b)

    def _fit(self, p, img: QImage, b: QRectF) -> None:
        s = min(b.width() / img.width(), b.height() / img.height())
        w, h = img.width() * s, img.height() * s
        p.drawImage(QRectF(b.center().x() - w / 2, b.center().y() - h / 2, w, h), img)

    def _paint_video(self, p, panel, b, unit) -> None:
        if panel.status:
            self._text(p, b, panel.status, unit * 0.9, C.TEXT_DIM)
            return
        v = self._videos.get(panel.pid)
        if v is None or v.source != panel.arg:
            if v is not None:
                v.stop.set()
            v = self._videos[panel.pid] = _Video(panel.arg)
        if v.error:
            self._text(p, b, v.error, unit * 0.9, C.RED)
        elif v.frame is not None:
            self._fit(p, v.frame, b)
        else:
            self._text(p, b, "Loading the video...", unit * 0.9, C.TEXT_DIM)

    def _paint_model(self, p, panel, b, unit) -> None:
        arg = panel.arg or str(_BASE_DIR / "core" / "face_model.obj")
        cached = self._models.get(panel.pid)
        if cached is None or cached[0] != arg:
            try:
                verts, edges, tris = load_obj(arg)
                cached = (arg, verts, edges, tris, "")
            except Exception as e:
                cached = (arg, [], [], [], f"The model could not be opened: {e}")
            self._models[panel.pid] = cached
        _, verts, edges, tris, err = cached
        if err:
            self._text(p, b, err, unit * 0.9, C.RED)
            return
        if not panel.held:
            panel.yaw += 0.006                         # turns slowly by itself
        cy_, sy_ = math.cos(panel.yaw), math.sin(panel.yaw)
        cp, sp = math.cos(panel.pitch), math.sin(panel.pitch)
        size = min(b.width(), b.height()) * 0.9
        cx, cy = b.center().x(), b.center().y()
        pts, rot = [], []
        for x, y, z in verts:
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            y1 = y * cp - z1 * sp
            z2 = y * sp + z1 * cp
            persp = 1.0 / (1.6 - z2)
            pts.append((cx + x1 * size * persp, cy - y1 * size * persp))
            rot.append((x1, y1, z2))
        # Shaded surface: triangles far to near (painter's algorithm), lit from
        # the upper left. Lit on whichever side faces the light, so a model
        # wound either way shades the same.
        lx, ly, lz = 0.37, 0.46, 0.81
        order = []
        for t in tris:
            a, b2, c = rot[t[0]], rot[t[1]], rot[t[2]]
            ux, uy, uz = b2[0] - a[0], b2[1] - a[1], b2[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            if nz < 0:
                nx, ny, nz = -nx, -ny, -nz
            shade = max(0.0, (nx * lx + ny * ly + nz * lz) / ln)
            order.append((a[2] + b2[2] + c[2], t, shade))
        order.sort(key=lambda o: o[0])
        if order:
            base = QColor(C.PRI)
            brushes = {}
            for _z, t, shade in order:
                level = int(shade * 15)
                br = brushes.get(level)
                if br is None:
                    k = 0.12 + 0.78 * level / 15
                    col = QColor(int(base.red() * k), int(base.green() * k),
                                 int(base.blue() * k), 235)
                    br = brushes[level] = (col, QPen(col, 0.7))
                p.setBrush(br[0])
                p.setPen(br[1])
                p.drawPolygon(QPolygonF([QPointF(*pts[t[0]]), QPointF(*pts[t[1]]),
                                         QPointF(*pts[t[2]])]))
        near = []
        for a, c in edges:
            if rot[a][2] + rot[c][2] > 0:                  # front edges: the hologram's glint
                near.append(QPointF(*pts[a]))
                near.append(QPointF(*pts[c]))
        p.setPen(QPen(with_alpha(C.WHITE, 0.18 if order else 0.85), 0.8))
        p.drawLines(near)

    # ── the dock, the ring, cursors, effects, status ─────────────────────────
    def _paint_dock(self, p: QPainter) -> None:
        W, H = self.width(), self.height()
        r = self.core.DOCK_R * H
        hovered = {what[5:] for _x, _y, what in self.inp.hover.values() if what.startswith("dock:")}
        items = self.inp.dock_items()
        x0 = items[0][1] * W - r * 1.6
        x1 = items[-1][1] * W + r * 1.6
        bar = QRectF(x0, self.core.DOCK_Y * H - r * 1.35, x1 - x0, r * 2.7)
        p.setPen(QPen(with_alpha(C.BORDER_B, 0.8), 1))
        p.setBrush(with_alpha(C.PANEL, 0.62))
        p.drawPolygon(chamfer(bar, r * 0.6, ("tl", "tr", "bl", "br")))
        for kind, x, y in items:
            c = QPointF(x * W, y * H)
            on = kind in hovered
            p.setPen(QPen(QColor(C.PRI if on else C.BORDER_B), 2 if on else 1.2))
            p.setBrush(with_alpha(C.PRI if on else C.PRI_GHO, 0.35 if on else 0.75))
            p.drawEllipse(c, r, r)
            p.setPen(QColor(C.WHITE if on else C.PRI))
            p.setFont(px_font(r * 0.95))
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter,
                       _GLYPH.get(kind, "?"))
            if on:
                self._label(p, c.x(), c.y() - r * 1.9, self.core.LABELS.get(kind, kind))

    def _label(self, p, x, y, text) -> None:
        p.setFont(hud_font(9, QFont.Weight.Bold))
        fm = QFontMetricsF(p.font())
        w = fm.horizontalAdvance(text) + 14
        rect = QRectF(x - w / 2, y - 11, w, 22)
        p.setPen(QPen(QColor(C.PRI), 1))
        p.setBrush(with_alpha(C.BG, 0.85))
        p.drawRect(rect)
        p.setPen(QColor(C.TEXT))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_ring(self, p: QPainter) -> None:
        ring = self.inp.ring
        if not ring:
            return
        W, H = self.width(), self.height()
        c = QPointF(ring["x"] * W, ring["y"] * H)
        rad = self.inp.RING_R * H
        p.setPen(QPen(with_alpha(C.PRI, 0.5), 1.5, Qt.PenStyle.DashLine))
        p.setBrush(with_alpha(C.BG, 0.35))
        p.drawEllipse(c, rad * 1.25, rad * 1.25)
        for kind, x, y in ring["items"]:
            q = QPointF(x * W, y * H)
            on = ring.get("hover") == kind
            br = rad * (0.36 if on else 0.30)
            p.setPen(QPen(QColor(C.PRI if on else C.BORDER_B), 2 if on else 1.2))
            p.setBrush(with_alpha(C.PRI if on else C.PRI_GHO, 0.45 if on else 0.85))
            p.drawEllipse(q, br, br)
            p.setPen(QColor(C.WHITE if on else C.PRI))
            p.setFont(px_font(br * 0.95))
            p.drawText(QRectF(q.x() - br, q.y() - br, 2 * br, 2 * br), Qt.AlignmentFlag.AlignCenter,
                       _GLYPH.get(kind, "?"))
            if on:
                self._label(p, q.x(), q.y() - br - 16, self.core.LABELS.get(kind, kind))

    _BONES = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10),
              (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18),
              (18, 19), (19, 20), (0, 17))

    def _paint_cursors(self, p: QPainter) -> None:
        W, H = self.width(), self.height()
        now = time.monotonic()
        # Holding still on empty space: an arc fills until the ring opens.
        for x, y, frac in self.inp.hold_progress(now):
            rect = QRectF(x * W - 28, y * H - 28, 56, 56)
            p.setPen(QPen(with_alpha(C.PRI, 0.25), 4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(rect)
            p.setPen(QPen(QColor(C.PRI), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, 90 * 16, int(-360 * 16 * frac))
        for ptr in self.pointers:
            if ptr.pid == "mouse":
                continue
            if self.settings.get("skeleton") and ptr.pts:
                lines = [QPointF(ptr.pts[a][0] * W, ptr.pts[a][1] * H) for bone in self._BONES
                         for a in bone]
                p.setPen(QPen(with_alpha(C.PRI, 0.32), 2))
                p.drawLines(lines)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(C.WHITE, 0.45))
                for i in (4, 8, 12, 16, 20):
                    p.drawEllipse(QPointF(ptr.pts[i][0] * W, ptr.pts[i][1] * H), 3, 3)
            c = QPointF(ptr.x * W, ptr.y * H)
            r = 16.0
            col = QColor(C.ACC2 if ptr.down else C.PRI)
            p.setPen(QPen(with_alpha(col, 0.35), 6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, r, r)
            # The inner ring closes in as the fingers do; full and filled = pinched.
            inner = r * (1.0 - 0.45 * max(0.0, min(1.0, ptr.pinch)))
            p.setPen(QPen(col, 2))
            p.setBrush(with_alpha(col, 0.55) if ptr.down else Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, inner, inner)
            if ptr.pointing:
                p.setPen(QPen(col, 1.5))
                p.drawLine(QPointF(c.x() - r * 1.6, c.y()), QPointF(c.x() + r * 1.6, c.y()))
                p.drawLine(QPointF(c.x(), c.y() - r * 1.6), QPointF(c.x(), c.y() + r * 1.6))

    def _paint_effects(self, p: QPainter, now: float) -> None:
        W, H = self.width(), self.height()
        for kind, x, y, t0, col in self.effects:
            k = (now - t0) / 0.7
            if kind == "ripple":
                rad = 20 + 120 * k
                p.setPen(QPen(with_alpha(col, 0.8 * (1 - k)), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x * W, y * H), rad, rad)
            elif kind == "flash":
                p.fillRect(self.rect(), with_alpha(col, 0.18 * (1 - k)))

    def _paint_status(self, p: QPainter, now: float) -> None:
        W, H = self.width(), self.height()
        mode = "CAMERA" if self.mode == "camera" else "OVERLAY"
        hands = len([q for q in self.pointers if q.pid != "mouse"])
        head = f"◈  HOLO DESK  ·  {mode}  ·  {hands} HAND{'S' if hands != 1 else ''}"
        p.setFont(hud_font(10, QFont.Weight.Bold))
        p.setPen(QColor(C.PRI))
        p.drawText(QRectF(24, 14, W * 0.6, 26), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   head)
        line = self.status or (self.hint if now < self.hint_until else "")
        if self.mode == "camera" and not self.status and now >= self.hint_until:
            line = "Esc closes the desk  ·  ? shows every gesture"
        if line:
            p.setFont(hud_font(9))
            p.setPen(QColor(C.ACC2 if self.status else C.TEXT_MED))
            p.drawText(QRectF(24, 40, W * 0.8, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       line)
        if not self.desk.live() and not self.desk.hidden:
            p.setFont(hud_font(12))
            p.setPen(with_alpha(C.TEXT, 0.75))
            p.drawText(QRectF(0, H * 0.42, W, 40), Qt.AlignmentFlag.AlignCenter,
                       "Pinch a button below, or pinch and hold on empty space, to bring out a panel")
        elif self.desk.hidden and not self.desk.live():
            p.setFont(hud_font(12))
            p.setPen(with_alpha(C.TEXT, 0.75))
            p.drawText(QRectF(0, H * 0.42, W, 40), Qt.AlignmentFlag.AlignCenter,
                       "Everything is put away - pull your open hands apart to bring it back")
