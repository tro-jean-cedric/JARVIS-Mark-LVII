"""
plugins/_holo_desk_core.py — the panels of the (reduced) Holo Desk and how
they are drawn onto a camera picture.

Helper module (leading underscore: the plugin loader skips it).

The desk is plain data (Desk / Panel) so its behaviour is unit-tested without a
camera or a window. render() paints the panels onto a BGR frame with Pillow
(it handles any language's letters, which cv2.putText does not) and returns the
frame; the plugin encodes it to JPEG and hands it to the HUD.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional

KINDS = ("clock", "system", "text")


@dataclass
class Panel:
    kind: str
    title: str = ""
    text: str = ""
    x: float = 0.06            # top-left corner, 0..1 of the picture
    y: float = 0.08
    w: float = 0.36
    h: float = 0.30
    pid: int = 0

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def body(self) -> str:
        """What the panel shows right now."""
        if self.kind == "clock":
            return time.strftime("%H:%M:%S\n%A %d %B %Y")
        if self.kind == "system":
            try:
                import psutil
                mem = psutil.virtual_memory()
                lines = [f"CPU  {psutil.cpu_percent(interval=None):.0f}%",
                         f"RAM  {mem.percent:.0f}%  ({mem.used / 2**30:.1f} GB)"]
                try:
                    bat = psutil.sensors_battery()
                    if bat is not None:
                        lines.append(f"BAT  {bat.percent:.0f}%"
                                     f"{' charging' if bat.power_plugged else ''}")
                except Exception:
                    pass
                return "\n".join(lines)
            except Exception:
                return "System readings are not available."
        return self.text or ""

    def heading(self) -> str:
        return self.title or {"clock": "Clock", "system": "System"}.get(self.kind, "Note")


@dataclass
class Desk:
    panels: list = field(default_factory=list)
    hidden: list = field(default_factory=list)      # put away by 'clear'
    active: Optional[int] = None                    # pid of the panel in focus
    _next: int = 1

    # ── panels ───────────────────────────────────────────────────────────────
    def add(self, kind: str, title: str = "", text: str = "") -> Panel:
        if kind not in KINDS:
            raise ValueError(f"unknown panel; use one of: {', '.join(KINDS)}")
        if kind in ("clock", "system"):
            for p in self.panels:            # one clock / system panel is plenty
                if p.kind == kind:
                    self.active = p.pid
                    return p
        p = Panel(kind, title, text, pid=self._next)
        self._next += 1
        p.x, p.y = self._free_spot(p)
        self.panels.append(p)
        self.active = p.pid
        return p

    def _free_spot(self, p: Panel) -> tuple:
        spots = [(0.04, 0.06), (0.60, 0.06), (0.04, 0.56), (0.60, 0.56), (0.32, 0.32)]
        for sx, sy in spots:
            if not any(abs(q.x - sx) < 0.1 and abs(q.y - sy) < 0.1 for q in self.panels):
                return sx, sy
        n = len(self.panels)
        return 0.05 + 0.03 * (n % 8), 0.05 + 0.03 * (n % 8)

    def get(self, pid: Optional[int]) -> Optional[Panel]:
        return next((p for p in self.panels if p.pid == pid), None)

    def current(self) -> Optional[Panel]:
        return self.get(self.active) or (self.panels[-1] if self.panels else None)

    def remove(self, pid: int) -> bool:
        before = len(self.panels)
        self.panels = [p for p in self.panels if p.pid != pid]
        if self.active == pid:
            self.active = self.panels[-1].pid if self.panels else None
        return len(self.panels) < before

    def cycle(self, step: int) -> Optional[Panel]:
        """Focus the next (+1) or previous (-1) panel."""
        if not self.panels:
            return None
        ids = [p.pid for p in self.panels]
        i = ids.index(self.active) if self.active in ids else 0
        self.active = ids[(i + step) % len(ids)]
        return self.current()

    def at(self, x: float, y: float) -> Optional[Panel]:
        """The top-most panel under a point (later panels are drawn on top)."""
        for p in reversed(self.panels):
            if p.contains(x, y):
                return p
        return None

    def move(self, pid: int, dx: float, dy: float) -> None:
        p = self.get(pid)
        if p is None:
            return
        p.x = min(max(p.x + dx, -p.w * 0.5), 1 - p.w * 0.5)
        p.y = min(max(p.y + dy, 0.0), 1 - p.h * 0.3)

    def resize(self, pid: int, factor: float) -> None:
        p = self.get(pid)
        if p is None:
            return
        cx, cy = p.x + p.w / 2, p.y + p.h / 2
        p.w = min(0.95, max(0.15, p.w * factor))
        p.h = min(0.95, max(0.12, p.h * factor))
        p.x, p.y = cx - p.w / 2, cy - p.h / 2

    def center(self, pid: int) -> None:
        p = self.get(pid)
        if p is not None:
            p.x, p.y = (1 - p.w) / 2, (1 - p.h) / 2

    def clear(self) -> int:
        n = len(self.panels)
        if n:
            self.hidden, self.panels, self.active = self.panels, [], None
        return n

    def restore(self) -> int:
        n = len(self.hidden)
        if n:
            self.panels, self.hidden = self.panels + self.hidden, []
            self.active = self.panels[-1].pid
        return n

    # ── persistence ──────────────────────────────────────────────────────────
    def to_list(self) -> list:
        return [asdict(p) for p in self.panels]

    def load_list(self, items: list) -> None:
        self.panels, self.hidden = [], []
        for d in items or []:
            try:
                p = Panel(**{k: d[k] for k in ("kind", "title", "text", "x", "y", "w", "h")
                             if k in d})
            except Exception:
                continue
            if p.kind not in KINDS:
                continue
            p.pid = self._next
            self._next += 1
            self.panels.append(p)
        self.active = self.panels[-1].pid if self.panels else None

    def describe(self) -> str:
        if not self.panels:
            return "no panels"
        return ", ".join(f"{p.heading()} ({p.kind})" for p in self.panels)


# ── Drawing ──────────────────────────────────────────────────────────────────

_fonts: dict = {}


def _font(px: int):
    from PIL import ImageFont
    if px not in _fonts:
        try:
            _fonts[px] = ImageFont.load_default(size=px)
        except TypeError:          # Pillow < 10.1 has one fixed bitmap font
            _fonts[px] = ImageFont.load_default()
    return _fonts[px]


def _wrap(draw, text: str, font, width: int) -> list:
    out = []
    for para in (text or "").splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) <= width or not line:
                line = trial
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def render(frame, desk: Desk, cursor: Optional[tuple] = None, hint: str = ""):
    """Paint the desk over a BGR frame (already mirrored). Returns a new frame."""
    import numpy as np
    from PIL import Image, ImageDraw
    rgb = Image.fromarray(frame[:, :, ::-1].copy()).convert("RGBA")
    W, H = rgb.size
    layer = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    head_px = max(12, H // 26)
    body_px = max(11, H // 32)
    for p in desk.panels:
        x0, y0 = int(p.x * W), int(p.y * H)
        x1, y1 = int((p.x + p.w) * W), int((p.y + p.h) * H)
        focus = p.pid == desk.active
        d.rounded_rectangle((x0, y0, x1, y1), radius=10, fill=(8, 24, 40, 170),
                            outline=(0, 220, 255, 255) if focus else (0, 150, 190, 200),
                            width=3 if focus else 1)
        d.text((x0 + 10, y0 + 6), p.heading()[:40], font=_font(head_px), fill=(0, 230, 255, 255))
        d.line((x0 + 8, y0 + head_px + 12, x1 - 8, y0 + head_px + 12), fill=(0, 150, 190, 200))
        ty = y0 + head_px + 18
        bf = _font(body_px)
        for line in _wrap(d, p.body(), bf, max(20, x1 - x0 - 20)):
            if ty + body_px > y1 - 4:
                break
            d.text((x0 + 10, ty), line, font=bf, fill=(225, 240, 250, 255))
            ty += body_px + 4
    if cursor is not None:
        cx, cy = int(cursor[0] * W), int(cursor[1] * H)
        d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=(255, 200, 0, 255), width=3)
    if hint:
        d.text((12, H - body_px - 12), hint[:90], font=_font(body_px), fill=(255, 255, 255, 220))
    out = Image.alpha_composite(rgb, layer).convert("RGB")
    return np.asarray(out)[:, :, ::-1].copy()
