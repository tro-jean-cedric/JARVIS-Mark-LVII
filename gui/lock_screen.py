"""gui/lock_screen.py — Face Lock's lock screen: the whole computer covered
until the owner's face is seen, or the unlock PIN is typed.

Qt main thread only. Shown and hidden by MainWindow (`_lock_screen_sig`),
which the face_lock plugin reaches through `JarvisUI.show_lock_screen(...)`
when plugins/_face_lock_core.py locks or unlocks.

WHAT IT DOES
  * a frameless, always-on-top cover on EVERY screen; the main one shows the
    live camera with a box round each face the lock is looking at (green: the
    owner, red: anyone else), the scan status and the time
  * it keeps itself on top (re-raised every 1.5 s) and swallows keys, so the
    desktop behind it cannot be used by whoever sits down
  * "Unlock another way": the unlock PIN, checked by _face_lock_core.try_pin
    (constant-time compare, five tries then a 30-second wait — the same count
    the voice tool uses)
  * it goes away by itself the moment the watcher recognises the owner

It is only ever shown when a PIN is set (face_lock._cover_wanted): with no way
in except the camera, a broken webcam would lock the owner out of their own
computer. It is not the operating system's lock and does not claim to be.
"""
from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from .style import chamfer, hud_font, with_alpha
from .theme import C

_COLOURS = {"owner": C.GREEN, "unknown": C.RED, "scanning": C.PRI}


class LockScreen(QWidget):
    _frame_sig = pyqtSignal(object)

    def __init__(self, primary: bool, owner: str = "", on_unlocked=None):
        super().__init__(None)
        from plugins import _face_lock_core as fl
        self.fl = fl
        self.primary = primary
        self.owner = owner
        self.on_unlocked = on_unlocked
        self.frame: QImage | None = None
        self.message = ""
        self.message_ok = False
        self._stop = threading.Event()
        self.setWindowTitle("Locked")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Window)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.other = QPushButton("Unlock another way", self)
        self.other.setCursor(Qt.CursorShape.PointingHandCursor)
        self.other.setFont(hud_font(10))
        self.other.setStyleSheet(
            f"QPushButton {{ color: {C.TEXT_MED}; background: transparent; border: none; "
            f"text-decoration: underline; }} QPushButton:hover {{ color: {C.PRI}; }}")
        self.other.clicked.connect(self._open_key)
        self.pin = QLineEdit(self)
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin.setPlaceholderText("Unlock PIN")
        self.pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pin.setFont(hud_font(13))
        self.pin.setStyleSheet(
            f"QLineEdit {{ background: {C.DARK}; color: {C.WHITE}; border: 1px solid {C.BORDER_B}; "
            f"border-radius: 3px; padding: 6px; }} QLineEdit:focus {{ border-color: {C.PRI}; }}")
        self.pin.returnPressed.connect(self._try_key)
        self.go = QPushButton("UNLOCK", self)
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.setFont(hud_font(10, QFont.Weight.Bold))
        self.go.setStyleSheet(
            f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; border: 1px solid {C.PRI}; "
            f"border-radius: 3px; padding: 0 14px; }} QPushButton:hover {{ color: {C.WHITE}; }}")
        self.go.clicked.connect(self._try_key)
        self.note = QLabel(self)
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setFont(hud_font(9))
        self.note.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        for w in (self.pin, self.go, self.note):
            w.hide()
        if not primary:
            self.other.hide()

        self._frame_sig.connect(self._on_frame)
        self._top = QTimer(self)
        self._top.timeout.connect(self._keep_on_top)
        self._top.start(1500)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh)
        self._tick.start(250)
        if primary:
            threading.Thread(target=self._camera_loop, daemon=True, name="lock-camera").start()

    # ── showing ──────────────────────────────────────────────────────────────
    def cover(self, screen) -> None:
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        if self.primary:
            self.activateWindow()
            self.setFocus()

    def _keep_on_top(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        if self.primary and not self.isActiveWindow():
            self.activateWindow()

    def shutdown(self) -> None:
        self._stop.set()
        self._top.stop()
        self._tick.stop()
        self.hide()
        self.deleteLater()

    # ── the camera, on a worker thread ───────────────────────────────────────
    def _camera_loop(self) -> None:
        from plugins import _camera
        name = "face_lock_screen"
        try:
            if not _camera.subscribe(name):
                return
            import cv2
            last = None
            while not self._stop.wait(1 / 15):
                frame = _camera.latest(1.0)
                if frame is None or frame is last:
                    continue
                last = frame
                h, w = frame.shape[:2]
                if w > 960:
                    frame = cv2.resize(frame, (960, int(h * 960 / w)))
                rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                hh, ww = rgb.shape[:2]
                img = QImage(rgb.data, ww, hh, ww * 3, QImage.Format.Format_RGB888).copy()
                if self._stop.is_set():
                    break
                self._frame_sig.emit(img)
        except RuntimeError:
            pass                                    # closed while a frame was in flight
        except Exception as e:
            print(f"[LockScreen] camera stopped: {e}")
        finally:
            try:
                _camera.unsubscribe(name)
            except Exception:
                pass

    def _on_frame(self, img) -> None:
        self.frame = img
        self.update()

    # ── the other way in ─────────────────────────────────────────────────────
    def _open_key(self) -> None:
        self.other.hide()
        for w in (self.pin, self.go, self.note):
            w.show()
        self.note.setText("" if self.fl.pin_set() else
                          "No unlock PIN is set - it is in Face lock's settings.")
        self._place()
        self.pin.setFocus()

    def _try_key(self) -> None:
        ok, msg = self.fl.try_pin(self.pin.text())
        self.pin.clear()
        self.note.setText(msg)
        self.note.setStyleSheet(f"color: {C.GREEN if ok else C.RED}; background: transparent;")
        if ok and callable(self.on_unlocked):
            self.on_unlocked()

    def _refresh(self) -> None:
        wait = self.fl.pin_wait()
        locked_out = wait > 0
        self.pin.setEnabled(not locked_out)
        self.go.setEnabled(not locked_out)
        if locked_out and self.pin.isVisible():
            self.note.setText(f"Too many wrong tries - wait {int(wait) + 1} s.")
        self.update()

    def keyPressEvent(self, e):
        # Nothing reaches the desktop. A digit opens the PIN box and types into it.
        if not self.primary:
            return
        if e.key() == Qt.Key.Key_Escape:
            for w in (self.pin, self.go, self.note):
                w.hide()
            self.other.show()
            return
        if e.text() and e.text().isprintable() and not self.pin.isVisible():
            self._open_key()
            self.pin.setText(e.text())

    def closeEvent(self, e):
        # Only shutdown() may close it (when the lock is lifted); Alt+F4 / Cmd+W do not.
        if self._stop.is_set():
            super().closeEvent(e)
        else:
            e.ignore()

    # ── layout and painting ──────────────────────────────────────────────────
    def _cam_rect(self) -> QRectF:
        W, H = self.width(), self.height()
        h = H * 0.46
        w = h * 4 / 3
        return QRectF((W - w) / 2, H * 0.24, w, h)

    def _place(self) -> None:
        r = self._cam_rect()
        y = r.bottom() + 70
        self.other.adjustSize()
        self.other.move(int(self.width() / 2 - self.other.width() / 2), int(y))
        self.pin.setGeometry(int(self.width() / 2 - 170), int(y), 220, 40)
        self.go.setGeometry(int(self.width() / 2 + 60), int(y), 112, 40)
        self.note.setGeometry(0, int(y + 48), self.width(), 24)

    def resizeEvent(self, e):
        self._place()
        super().resizeEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0, QColor(C.PANEL))
        grad.setColorAt(1, QColor(C.BG))
        p.fillRect(self.rect(), grad)
        p.setPen(QPen(with_alpha(C.PRI, 0.04), 1))
        for y in range(0, H, 4):
            p.drawLine(0, y, W, y)

        # Title
        p.setPen(QColor(C.PRI))
        f = hud_font(28 if self.primary else 22, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 10)
        p.setFont(f)
        p.drawText(QRectF(0, H * 0.08, W, 60), Qt.AlignmentFlag.AlignCenter, "◈  LOCKED")
        p.setPen(QColor(C.TEXT_DIM))
        p.setFont(hud_font(14))
        p.drawText(QRectF(W - 260, 24, 230, 30),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   time.strftime("%H:%M"))
        if not self.primary:
            return
        who = f"{self.owner}, look" if self.owner else "Look"
        p.setPen(QColor(C.TEXT_MED))
        p.setFont(hud_font(12))
        p.drawText(QRectF(0, H * 0.08 + 56, W, 30), Qt.AlignmentFlag.AlignCenter,
                   f"{who} at the camera to unlock")

        # The camera, with the faces the lock sees
        r = self._cam_rect()
        shape = chamfer(r, 18, ("tl", "br"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C.DARK))
        p.drawPolygon(shape)
        if self.frame is not None:
            img = self.frame
            s = max(r.width() / img.width(), r.height() / img.height())
            w, h = img.width() * s, img.height() * s
            p.save()
            p.setClipRect(r)
            p.drawImage(QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h), img)
            view = self.fl.last_view()
            if time.time() - float(view.get("at") or 0) < 1.5:
                for x, y, bw, bh, label in view.get("faces") or []:
                    col = QColor(_COLOURS.get(label, C.PRI))
                    fx = r.center().x() - w / 2 + (1 - x - bw) * w      # mirrored
                    fy = r.center().y() - h / 2 + y * h
                    p.setPen(QPen(col, 2.5))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRect(QRectF(fx, fy, bw * w, bh * h))
            p.restore()
        else:
            p.setPen(QColor(C.TEXT_DIM))
            p.setFont(hud_font(11))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Waiting for the camera...")
        for width, a in ((6, 0.12), (1.5, 0.9)):
            p.setPen(QPen(with_alpha(C.PRI, a), width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(shape)

        # What the watcher is seeing, in words
        view = self.fl.last_view()
        caption = view.get("caption") if time.time() - float(view.get("at") or 0) < 1.5 else ""
        p.setPen(QColor(C.TEXT))
        p.setFont(hud_font(11))
        p.drawText(QRectF(0, r.bottom() + 16, W, 28), Qt.AlignmentFlag.AlignCenter,
                   caption or "Looking for you...")
        p.end()


class LockScreens:
    """One cover per screen; the primary one has the camera and the PIN."""

    def __init__(self):
        self.screens: list[LockScreen] = []

    @property
    def shown(self) -> bool:
        return bool(self.screens)

    def show(self, primary_screen, all_screens, owner: str = "") -> None:
        if self.screens:
            for s in self.screens:
                s._keep_on_top()
            return
        others = [s for s in all_screens if s is not primary_screen]
        main = LockScreen(True, owner, on_unlocked=self.hide)
        main.cover(primary_screen)
        self.screens = [main]
        for scr in others:
            cover = LockScreen(False)
            cover.cover(scr)
            self.screens.append(cover)
        main.raise_()
        main.activateWindow()

    def hide(self) -> None:
        screens, self.screens = self.screens, []
        for s in screens:
            s.shutdown()
