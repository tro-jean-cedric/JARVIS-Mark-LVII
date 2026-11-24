"""gui/subtitles.py — the live-subtitle bar: a small translucent strip that floats
above every window, near the bottom of the screen.

Used by plugins/live_subtitles.py through JarvisUI.show_subtitle() /
hide_subtitles() — the plugin runs on a worker thread and never touches this
widget; MainWindow's signals bring each line here on the Qt thread.

It is its own top-level window (not a child of the HUD) because the point is to
read it over a video or a call in another application, with the HUD minimised.
It never takes focus, can be dragged anywhere, and its ✕ closes it — the plugin
notices through JarvisUI.subtitles_visible() and stops listening.

Colours are painted from the palette at paint time, so a re-theme applies on the
next line; fonts come from gui/style.py.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from gui.style import hud_font, px_font
from gui.theme import C

_MAX_LINES = 3


class SubtitleBar(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Subtitles")
        self._text = ""
        self._hint = ""
        self._drag: QPoint | None = None
        self._moved = False                 # once dragged, stay where the user put it
        self.closed_by_user = False
        # Mirrors isVisible() as a plain attribute, written only on the Qt
        # thread, so a worker thread can read it without asking a widget.
        self.shown = False
        self._font = px_font(24, QFont.Weight.DemiBold)
        self._hint_font = px_font(13)

    # -- public, Qt thread only ------------------------------------------------
    def set_line(self, text: str, hint: str = "") -> None:
        self._text = str(text or "").strip()
        if hint:
            self._hint = str(hint)
        self.closed_by_user = False
        self._relayout()
        if not self.isVisible():
            self.show()
        self.shown = True
        self.update()

    # -- layout ------------------------------------------------------------------
    def _screen_rect(self):
        scr = self.screen() if self._moved else None
        scr = scr or QApplication.primaryScreen()
        return scr.availableGeometry() if scr else None

    def _relayout(self) -> None:
        geo = self._screen_rect()
        if geo is None:
            return
        width = int(min(1100, max(480, geo.width() * 0.62)))
        fm = QFontMetrics(self._font)
        text = self._text or "…"
        flags = int(Qt.TextFlag.TextWordWrap)
        rect = fm.boundingRect(0, 0, width - 72, 2000, flags, text)
        line_h = fm.lineSpacing()
        lines = min(_MAX_LINES, max(1, round(rect.height() / max(1, line_h))))
        height = 26 + lines * line_h + 22
        if self._moved:
            self.resize(width, height)
            return
        x = geo.x() + (geo.width() - width) // 2
        y = geo.y() + geo.height() - height - int(geo.height() * 0.08)
        self.setGeometry(x, y, width, height)

    # -- painting ----------------------------------------------------------------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = QColor(C.BG)
        bg.setAlpha(205)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.fillPath(path, bg)
        pen = QPen(QColor(C.BORDER_B))
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.drawPath(path)
        p.setPen(QPen(QColor(C.PRI), 3))
        p.drawLine(int(r.left()) + 14, int(r.top()) + 1, int(r.left()) + 70, int(r.top()) + 1)

        # The hint (language, or "listening…") in the top-left corner.
        p.setFont(self._hint_font)
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(QRectF(r.left() + 16, r.top() + 4, r.width() - 60, 18),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self._hint.upper())
        # The close mark.
        p.setFont(hud_font(11))
        p.setPen(QColor(C.TEXT_MED))
        p.drawText(self._close_rect(), int(Qt.AlignmentFlag.AlignCenter), "✕")

        p.setFont(self._font)
        p.setPen(QColor(C.WHITE))
        p.drawText(r.adjusted(36, 22, -36, -10),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
                       | Qt.TextFlag.TextWordWrap),
                   self._text or "…")
        p.end()

    def hideEvent(self, e) -> None:
        self.shown = False
        super().hideEvent(e)

    def _close_rect(self) -> QRectF:
        return QRectF(self.width() - 34, 4, 26, 22)

    # -- mouse: drag anywhere, ✕ closes -------------------------------------------
    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        if self._close_rect().contains(pos):
            self.closed_by_user = True
            self.hide()
            return
        self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._moved = True

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None
