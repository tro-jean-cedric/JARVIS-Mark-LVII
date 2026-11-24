"""
plugins/_visual_core.py — draw a "board" of blocks into a PNG for the HUD.

Helper module (leading underscore: the plugin loader skips it). Used by
visual_panel (boards the model describes) and live_panels (system and weather
boards refreshed on a timer).

WHY PIL AND NOT QPAINTER
    Plugins run on worker threads, and AGENTS.md forbids touching Qt there —
    a QPainter on a QImage is technically allowed off the GUI thread, but the
    fonts it needs are not, and one mistake there is a crash. PIL is plain,
    thread-safe Python. The finished PNG is handed to player.show_image(),
    which does the Qt side on the Qt thread.

COLOURS AND TYPE
    Colours are read from gui/theme.py's palette `C` at render time, so a board
    drawn after the user re-themes the HUD matches the new colour. Fonts follow
    gui/style.py's family preference (Bahnschrift / Segoe UI on Windows, the
    system sans elsewhere), resolved to font FILES because PIL needs a path.

THE BOARD SPEC
    {"title": str, "subtitle": str, "footer": str, "blocks": [block, ...]}
    A block is {"type", "title"?, "span"? ("full" | "half"), ...} with:
      text      {text}                         callout {text}
      stats     {items:[{label, value, detail?}]}
      list      {items:[{label, detail?, value?, image?}]}
      table     {columns:[...], rows:["a | b | c", ...]}
      bars      {items:[{label, value}], unit?}
      line      {points:[numbers], labels?:[...], unit?}
      pie       {items:[{label, value}]}
      progress  {items:[{label, value, max?}]}
      checklist {items:[{label, done?}]}
      countdown {seconds | until 'HH:MM', text?}
      image     {url | path, caption?}         gallery {items:[{image, label?}]}
      rings     {items:[{label, value (0-100), detail?}]}   (gauges, live panels)
    Values may carry units ("21°C", "$1,299"); charts read the number out.

    render(spec) never raises: a block that cannot be drawn is drawn as a note
    saying so, and the rest of the board is still shown.
"""
from __future__ import annotations

import io
import math
import os
import platform
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1280                 # board width; the HUD scales the picture to fit
MARGIN = 30
GAP = 18
PAD = 16
MAX_BLOCKS = 8
MAX_H = 2400

_lock = threading.Lock()


# ── Palette ──────────────────────────────────────────────────────────────────

def palette() -> dict:
    """The HUD's live colours, with fixed fallbacks when gui is not importable."""
    base = {"BG": "#00060a", "PANEL": "#010d14", "PANEL2": "#010f18", "BORDER": "#0d3347",
            "BORDER_B": "#1a5c7a", "PRI": "#00d4ff", "PRI_DIM": "#007a99", "PRI_GHO": "#001f2e",
            "ACC": "#ff6b00", "ACC2": "#ffcc00", "GREEN": "#00ff88", "RED": "#ff3355",
            "TEXT": "#8ffcff", "TEXT_DIM": "#3a8a9a", "TEXT_MED": "#5ab8cc", "WHITE": "#d8f8ff",
            "BAR_BG": "#011520"}
    try:
        from gui.theme import C
        for k in list(base):
            v = getattr(C, k, None)
            if isinstance(v, str) and v.startswith("#") and len(v) == 7:
                base[k] = v
    except Exception:
        pass
    return base


def _rgb(hex_: str, a: int | None = None):
    h = hex_.lstrip("#")
    c = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return c + (a,) if a is not None else c


def _mix(a: str, b: str, t: float) -> tuple:
    ra, rb = _rgb(a), _rgb(b)
    return tuple(int(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))


def series(p: dict) -> list[str]:
    return [p["PRI"], p["ACC"], p["GREEN"], p["ACC2"], p["RED"], p["TEXT_MED"], p["PRI_DIM"]]


# ── Fonts ────────────────────────────────────────────────────────────────────
# The same order of preference as gui/style.py's DISPLAY_FAMILIES / MONO_FAMILIES,
# as file paths per platform.
_SANS = {
    "Windows": ["bahnschrift.ttf", "segoeui.ttf", "arial.ttf"],
    "Darwin": ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc",
               "/System/Library/Fonts/Helvetica.ttc",
               "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"],
    "Linux": ["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
              "/usr/share/fonts/noto/NotoSans-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
}
_SANS_BOLD = {
    "Windows": ["bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"],
    "Darwin": ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc",
               "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
    "Linux": ["/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
              "/usr/share/fonts/noto/NotoSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}
_MONO = {
    "Windows": ["CascadiaMono.ttf", "consola.ttf", "cour.ttf"],
    "Darwin": ["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc",
               "/System/Library/Fonts/Courier.ttc"],
    "Linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/TTF/DejaVuSansMono.ttf"],
}
_font_cache: dict = {}


def _font_file(table: dict) -> str | None:
    sysname = platform.system()
    for cand in table.get(sysname, []) + table.get("Linux", []):
        path = cand
        if sysname == "Windows" and not os.path.isabs(cand):
            path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", cand)
        if os.path.exists(path):
            return path
    return None


def font(size: int, bold: bool = False, mono: bool = False):
    key = (int(size), bold, mono)
    f = _font_cache.get(key)
    if f is not None:
        return f
    path = _font_file(_MONO if mono else (_SANS_BOLD if bold else _SANS))
    try:
        f = ImageFont.truetype(path, int(size)) if path else ImageFont.load_default(size=int(size))
    except Exception:
        try:
            f = ImageFont.load_default(size=int(size))
        except Exception:
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _tw(f, text: str) -> float:
    try:
        return f.getlength(text)
    except Exception:
        return len(text) * getattr(f, "size", 12) * 0.6


def wrap(text: str, f, width: float, max_lines: int = 30) -> list[str]:
    out: list[str] = []
    for para in str(text or "").split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        line = ""
        for w in words:
            cand = (line + " " + w).strip()
            if _tw(f, cand) <= width:
                line = cand
                continue
            if line:
                out.append(line)
            while _tw(f, w) > width and len(w) > 1:        # one word wider than the box
                cut = max(1, int(len(w) * width / max(1.0, _tw(f, w))) - 1)
                out.append(w[:cut])
                w = w[cut:]
            line = w
        if line:
            out.append(line)
        if len(out) >= max_lines:
            break
    if len(out) > max_lines:
        out = out[:max_lines]
    return out


def ellipsize(text: str, f, width: float) -> str:
    text = " ".join(str(text or "").split())
    if _tw(f, text) <= width:
        return text
    while text and _tw(f, text + "…") > width:
        text = text[:-1]
    return text + "…"


_NUM_RE = re.compile(r"-?\d[\d,  ]*(?:\.\d+)?|-?\.\d+")


def num(value) -> float | None:
    """The number in '21°C', '$1,299', '45 %', '1.2k' — or None."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip()
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "")
    # "1,299" (thousands) vs "3,5" (decimal comma)
    if "," in raw and "." not in raw:
        parts = raw.split(",")
        raw = raw.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        v = float(raw)
    except ValueError:
        return None
    # "1.2k" — but not "5 km" or "3 kg".
    rest = s[m.end():].lower()
    if rest.startswith("k") and not rest[1:2].isalpha():
        v *= 1_000
    return v


def _fmt(v: float) -> str:
    if abs(v) >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.1f}"


_PREFIX_UNITS = ("$", "€", "£", "¥", "₺", "₹", "₽", "₩", "CHF", "R$")


def with_unit(v: float, unit: str) -> str:
    """'$1,299' for a currency, '21°C' / '45 %' otherwise."""
    unit = str(unit or "").strip()
    if not unit:
        return _fmt(v)
    if unit in _PREFIX_UNITS:
        return ("-" if v < 0 else "") + unit + _fmt(abs(v))
    return _fmt(v) + ("" if unit in ("%", "°", "°C", "°F") else " ") + unit


# ── Pictures from the web ────────────────────────────────────────────────────

def fetch_image(src: str, max_side: int = 900) -> Image.Image | None:
    """A picture from a URL or a local path, or None. Never raises."""
    if not src:
        return None
    try:
        if re.match(r"^https?://", str(src), re.I):
            import requests
            r = requests.get(src, timeout=8, headers={"User-Agent": "Mozilla/5.0 JARVIS"})
            r.raise_for_status()
            if len(r.content) > 15_000_000:
                return None
            img = Image.open(io.BytesIO(r.content))
        else:
            p = Path(str(src)).expanduser()
            if not p.is_file():
                return None
            img = Image.open(p)
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        return img
    except Exception as e:
        print(f"[visual] picture not loaded ({str(src)[:80]}): {e}")
        return None


def search_images(query: str, n: int = 4) -> list[str]:
    """Image URLs for a query through DuckDuckGo (ddgs). [] when unavailable."""
    if not query:
        return []
    try:
        try:
            from ddgs import DDGS
        except Exception:
            from duckduckgo_search import DDGS          # older package name
        with DDGS() as d:
            res = d.images(query, max_results=max(n * 2, 6), safesearch="moderate")
        urls = [r.get("image") or r.get("thumbnail") for r in (res or [])]
        return [u for u in urls if u][: n * 2]
    except Exception as e:
        print(f"[visual] image search failed for {query!r}: {e}")
        return []


# ── Measuring and drawing blocks ─────────────────────────────────────────────

def _items(block: dict) -> list[dict]:
    items = block.get("items") or []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            out.append(it)
        elif isinstance(it, (str, int, float)):
            out.append({"label": str(it)})
    return out


def _rows(block: dict) -> list[list[str]]:
    rows = block.get("rows") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, (list, tuple)):
            out.append([str(c) for c in r])
        else:
            out.append([c.strip() for c in str(r).split("|")])
    return out


def _default_span(block: dict) -> str:
    span = str(block.get("span") or "").lower()
    if span in ("full", "half"):
        return span
    t = block.get("type")
    if t in ("table", "line", "gallery", "image"):
        return "full"
    if t == "text" and len(str(block.get("text") or "")) > 260:
        return "full"
    if t in ("stats", "rings") and len(_items(block)) > 3:
        return "full"
    if t == "list" and any(it.get("image") for it in _items(block)):
        return "full"
    return "half"


def _title_h(block: dict) -> int:
    return 36 if block.get("title") else 0


def measure(block: dict, width: int) -> int:
    """Height in pixels this block needs at `width`."""
    t = block.get("type")
    inner = width - 2 * PAD
    h = _title_h(block) + 2 * PAD
    try:
        if t in ("text", "callout"):
            f = font(24 if t == "callout" else 21, bold=(t == "callout"))
            lines = wrap(str(block.get("text") or ""), f, inner - (18 if t == "callout" else 0), 16)
            return h + max(1, len(lines)) * int(f.size * 1.4)
        if t in ("stats", "rings"):
            n = max(1, min(len(_items(block)), 8))
            per_row = max(1, min(n, inner // 170))
            rows = math.ceil(n / per_row)
            return h + rows * (150 if t == "rings" else 116) + (rows - 1) * 10
        if t == "list":
            items = _items(block)[:12]
            has_img = any(it.get("image") for it in items)
            row = 76 if has_img else (54 if any(it.get("detail") for it in items) else 38)
            return h + max(1, len(items)) * row
        if t == "table":
            return h + 42 + max(1, min(len(_rows(block)), 16)) * 36
        if t == "bars":
            return h + max(1, min(len(_items(block)), 14)) * 36
        if t in ("line", "pie"):
            return h + 250
        if t == "progress":
            return h + max(1, min(len(_items(block)), 10)) * 50
        if t == "checklist":
            return h + max(1, min(len(_items(block)), 14)) * 38
        if t == "countdown":
            return h + 140
        if t == "image":
            return h + 380 + (30 if block.get("caption") else 0)
        if t == "gallery":
            return h + 250
    except Exception:
        pass
    return h + 40


class _Ctx:
    def __init__(self, img: Image.Image, p: dict):
        self.img = img
        self.d = ImageDraw.Draw(img, "RGBA")
        self.p = p


def _panel(c: _Ctx, x: int, y: int, w: int, h: int, title: str = "") -> int:
    p = c.p
    c.d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=_rgb(p["PANEL"]),
                          outline=_rgb(p["BORDER"]), width=2)
    c.d.line([x + 12, y, x + 70, y], fill=_rgb(p["PRI"]), width=3)        # HUD accent tick
    if title:
        c.d.text((x + PAD, y + PAD - 2), ellipsize(title.upper(), font(17, bold=True), w - 2 * PAD),
                 font=font(17, bold=True), fill=_rgb(p["PRI"]))
        return y + PAD + 36
    return y + PAD


def _draw_text(c, b, x, y, w, h):
    p = c.p
    callout = b.get("type") == "callout"
    f = font(24 if callout else 21, bold=callout)
    off = 18 if callout else 0
    if callout:
        c.d.rectangle([x, y, x + 5, y + h - PAD - 4], fill=_rgb(p["ACC"]))
    lh = int(f.size * 1.4)
    for i, line in enumerate(wrap(str(b.get("text") or ""), f, w - off, 16)):
        c.d.text((x + off, y + i * lh), line, font=f, fill=_rgb(p["WHITE"] if callout else p["TEXT"]))


def _draw_stats(c, b, x, y, w, h):
    p = c.p
    items = _items(b)[:8]
    if not items:
        return
    per_row = max(1, min(len(items), w // 170))
    tw = (w - (per_row - 1) * 10) // per_row
    for i, it in enumerate(items):
        r, col = divmod(i, per_row)
        tx, ty = x + col * (tw + 10), y + r * 126
        c.d.rounded_rectangle([tx, ty, tx + tw, ty + 116], radius=8, fill=_rgb(p["PANEL2"]),
                              outline=_rgb(p["BORDER_B"]), width=1)
        fv = font(40, bold=True)
        val = ellipsize(str(it.get("value", "")), fv, tw - 24)
        c.d.text((tx + 12, ty + 10), val, font=fv, fill=_rgb(series(p)[i % 3] if i < 3 else p["WHITE"]))
        c.d.text((tx + 12, ty + 62), ellipsize(str(it.get("label", "")).upper(), font(15, bold=True),
                                               tw - 24), font=font(15, bold=True), fill=_rgb(p["TEXT_MED"]))
        if it.get("detail"):
            c.d.text((tx + 12, ty + 86), ellipsize(str(it["detail"]), font(15), tw - 24),
                     font=font(15), fill=_rgb(p["TEXT_DIM"]))


def _draw_rings(c, b, x, y, w, h):
    p = c.p
    items = _items(b)[:8]
    if not items:
        return
    per_row = max(1, min(len(items), w // 170))
    tw = (w - (per_row - 1) * 10) // per_row
    for i, it in enumerate(items):
        r, col = divmod(i, per_row)
        cx, cy = x + col * (tw + 10) + tw // 2, y + r * 160 + 62
        rad = 54
        v = num(it.get("value"))
        frac = max(0.0, min(1.0, (v or 0) / 100.0))
        colr = p["GREEN"] if frac < 0.6 else (p["ACC2"] if frac < 0.85 else p["RED"])
        box = [cx - rad, cy - rad, cx + rad, cy + rad]
        c.d.ellipse(box, outline=_rgb(p["BAR_BG"]), width=12)
        if frac > 0:
            c.d.arc(box, start=-90, end=-90 + 360 * frac, fill=_rgb(colr), width=12)
        txt = f"{v:.0f}%" if v is not None else "—"
        fv = font(28, bold=True)
        c.d.text((cx - _tw(fv, txt) / 2, cy - 18), txt, font=fv, fill=_rgb(p["WHITE"]))
        lab = ellipsize(str(it.get("label", "")).upper(), font(15, bold=True), tw - 10)
        c.d.text((cx - _tw(font(15, bold=True), lab) / 2, cy + rad + 8), lab,
                 font=font(15, bold=True), fill=_rgb(p["TEXT_MED"]))
        if it.get("detail"):
            det = ellipsize(str(it["detail"]), font(14), tw - 10)
            c.d.text((cx - _tw(font(14), det) / 2, cy + rad + 28), det, font=font(14),
                     fill=_rgb(p["TEXT_DIM"]))


def _draw_list(c, b, x, y, w, h):
    p = c.p
    items = _items(b)[:12]
    has_img = any(it.get("image") for it in items)
    row = 76 if has_img else (54 if any(it.get("detail") for it in items) else 38)
    for i, it in enumerate(items):
        ry = y + i * row
        tx = x
        if has_img:
            pic = fetch_image(str(it.get("image") or ""), 200)
            if pic is not None:
                pic = _cover(pic, 64, 64)
                c.img.paste(pic, (x, ry + 2))
            else:
                c.d.rectangle([x, ry + 2, x + 64, ry + 66], outline=_rgb(p["BORDER"]))
            tx = x + 78
        else:
            c.d.ellipse([x, ry + 11, x + 9, ry + 20], fill=_rgb(p["PRI"]))
            tx = x + 22
        val = str(it.get("value") or "")
        fv = font(20, bold=True)
        vw = _tw(fv, val) + 12 if val else 0
        c.d.text((tx, ry + 2), ellipsize(str(it.get("label", "")), font(20), w - (tx - x) - vw),
                 font=font(20), fill=_rgb(p["WHITE"]))
        if val:
            c.d.text((x + w - vw + 12, ry + 2), val, font=fv, fill=_rgb(p["ACC2"]))
        if it.get("detail"):
            c.d.text((tx, ry + 28), ellipsize(str(it["detail"]), font(16), w - (tx - x)),
                     font=font(16), fill=_rgb(p["TEXT_DIM"]))


def _draw_table(c, b, x, y, w, h):
    p = c.p
    cols = [str(cc) for cc in (b.get("columns") or [])]
    rows = _rows(b)[:16]
    ncol = max([len(cols)] + [len(r) for r in rows] + [1])
    cols += [""] * (ncol - len(cols))
    fh, fc = font(16, bold=True), font(18)
    # Column widths from content, within limits.
    want = []
    for ci in range(ncol):
        cells = [cols[ci]] + [r[ci] if ci < len(r) else "" for r in rows]
        want.append(min(420, max(70, max(_tw(fc, s) for s in cells) + 24)))
    scale = w / sum(want)
    widths = [int(v * scale) for v in want]
    c.d.rectangle([x, y, x + w, y + 36], fill=_rgb(p["PRI_GHO"]))
    cx = x
    for ci, name in enumerate(cols):
        c.d.text((cx + 10, y + 8), ellipsize(name.upper(), fh, widths[ci] - 16), font=fh,
                 fill=_rgb(p["PRI"]))
        cx += widths[ci]
    for ri, r in enumerate(rows):
        ry = y + 42 + ri * 36
        if ri % 2:
            c.d.rectangle([x, ry - 3, x + w, ry + 33], fill=_rgb(p["PANEL2"]))
        cx = x
        for ci in range(ncol):
            cell = r[ci] if ci < len(r) else ""
            c.d.text((cx + 10, ry + 4), ellipsize(cell, fc, widths[ci] - 16), font=fc,
                     fill=_rgb(p["WHITE"] if ci == 0 else p["TEXT"]))
            cx += widths[ci]
    if len(_rows(b)) > 16:
        c.d.text((x, y + h - 22), f"+{len(_rows(b)) - 16} more rows", font=font(14),
                 fill=_rgb(p["TEXT_DIM"]))


def _draw_bars(c, b, x, y, w, h):
    p = c.p
    items = _items(b)[:14]
    vals = [num(it.get("value")) or 0.0 for it in items]
    unit = str(b.get("unit") or "")
    top = max([abs(v) for v in vals] + [1e-9])
    if unit.strip() == "%":
        top = max(top, 100.0)          # percentages are drawn against the whole
    lw = min(w * 0.36, max([_tw(font(18), str(it.get("label", ""))) for it in items] + [40]) + 14)
    for i, (it, v) in enumerate(zip(items, vals)):
        ry = y + i * 36
        c.d.text((x, ry + 4), ellipsize(str(it.get("label", "")), font(18), lw - 10), font=font(18),
                 fill=_rgb(p["TEXT"]))
        bw = (w - lw - 110) * abs(v) / top
        c.d.rounded_rectangle([x + lw, ry + 6, x + lw + w - lw - 110, ry + 26], radius=4,
                              fill=_rgb(p["BAR_BG"]))
        if bw > 1:
            c.d.rounded_rectangle([x + lw, ry + 6, x + lw + bw, ry + 26], radius=4,
                                  fill=_rgb(p["ACC"] if v == max(vals) and len(vals) > 1 else p["PRI"]))
        raw = it.get("value")
        label = with_unit(v, unit) if isinstance(raw, (int, float)) or str(raw).strip() == _fmt(v).replace(",", "") \
            or re.fullmatch(r"-?[\d.,]+", str(raw).strip() or "x") else str(raw)
        c.d.text((x + w - 100, ry + 4), ellipsize(label, font(18, bold=True), 100), font=font(18, bold=True),
                 fill=_rgb(p["WHITE"]))


def _draw_line(c, b, x, y, w, h):
    p = c.p
    pts = [num(v) for v in (b.get("points") or [])]
    pts = [v for v in pts if v is not None][:400]
    labels = [str(s) for s in (b.get("labels") or [])]
    unit = str(b.get("unit") or "")
    if len(pts) < 2:
        c.d.text((x, y), "Not enough points to draw a line.", font=font(18), fill=_rgb(p["TEXT_DIM"]))
        return
    lo, hi = min(pts), max(pts)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    pad_l, pad_b = 64, 30
    gx, gy, gw, gh = x + pad_l, y + 6, w - pad_l - 6, 250 - 6 - pad_b - 10
    for k in range(5):
        yy = gy + gh * k / 4
        c.d.line([gx, yy, gx + gw, yy], fill=_rgb(p["BORDER"]), width=1)
        val = hi - (hi - lo) * k / 4
        c.d.text((x, yy - 9), ellipsize(with_unit(val, unit), font(14), pad_l - 6), font=font(14),
                 fill=_rgb(p["TEXT_DIM"]))
    xy = [(gx + gw * i / (len(pts) - 1), gy + gh * (1 - (v - lo) / (hi - lo))) for i, v in enumerate(pts)]
    poly = xy + [(gx + gw, gy + gh), (gx, gy + gh)]
    c.d.polygon(poly, fill=_rgb(p["PRI"], 40))
    c.d.line(xy, fill=_rgb(p["PRI"]), width=3, joint="curve")
    c.d.ellipse([xy[-1][0] - 5, xy[-1][1] - 5, xy[-1][0] + 5, xy[-1][1] + 5], fill=_rgb(p["ACC2"]))
    if labels:
        n = len(labels)
        step = max(1, math.ceil(n / 8))
        for i in range(0, n, step):
            lx = gx + gw * i / max(1, n - 1)
            t = ellipsize(labels[i], font(13), gw / 8)
            c.d.text((lx - _tw(font(13), t) / 2, gy + gh + 8), t, font=font(13), fill=_rgb(p["TEXT_DIM"]))


def _draw_pie(c, b, x, y, w, h):
    p = c.p
    items = _items(b)[:8]
    vals = [max(0.0, num(it.get("value")) or 0.0) for it in items]
    total = sum(vals)
    if total <= 0:
        c.d.text((x, y), "No values to draw.", font=font(18), fill=_rgb(p["TEXT_DIM"]))
        return
    d = 220
    box = [x + 6, y + 6, x + 6 + d, y + 6 + d]
    start = -90.0
    cols = series(p)
    for i, v in enumerate(vals):
        ext = 360 * v / total
        if ext > 0.3:
            c.d.pieslice(box, start, start + ext, fill=_rgb(cols[i % len(cols)]),
                         outline=_rgb(p["PANEL"]), width=2)
        start += ext
    c.d.ellipse([x + 6 + d * 0.3, y + 6 + d * 0.3, x + 6 + d * 0.7, y + 6 + d * 0.7], fill=_rgb(p["PANEL"]))
    lx = x + d + 30
    for i, (it, v) in enumerate(zip(items, vals)):
        ly = y + 10 + i * 30
        c.d.rectangle([lx, ly + 4, lx + 16, ly + 20], fill=_rgb(cols[i % len(cols)]))
        txt = f"{it.get('label', '')}  {100 * v / total:.0f}%"
        c.d.text((lx + 26, ly), ellipsize(txt, font(18), w - (lx - x) - 30), font=font(18),
                 fill=_rgb(p["TEXT"]))


def _draw_progress(c, b, x, y, w, h):
    p = c.p
    for i, it in enumerate(_items(b)[:10]):
        ry = y + i * 50
        v = num(it.get("value")) or 0.0
        mx = num(it.get("max")) or 100.0
        frac = max(0.0, min(1.0, v / mx if mx else 0))
        right = str(it.get("detail") or (f"{_fmt(v)} / {_fmt(mx)}" if it.get("max") else f"{frac * 100:.0f}%"))
        c.d.text((x, ry), ellipsize(str(it.get("label", "")), font(18), w - 160), font=font(18),
                 fill=_rgb(p["TEXT"]))
        c.d.text((x + w - _tw(font(17, bold=True), right), ry), right, font=font(17, bold=True),
                 fill=_rgb(p["WHITE"]))
        c.d.rounded_rectangle([x, ry + 26, x + w, ry + 40], radius=6, fill=_rgb(p["BAR_BG"]))
        colr = p["GREEN"] if frac < 0.75 else (p["ACC2"] if frac < 0.9 else p["RED"])
        if b.get("neutral"):
            colr = p["PRI"]
        if frac > 0:
            c.d.rounded_rectangle([x, ry + 26, x + max(12, w * frac), ry + 40], radius=6, fill=_rgb(colr))


def _draw_checklist(c, b, x, y, w, h):
    p = c.p
    for i, it in enumerate(_items(b)[:14]):
        ry = y + i * 38
        done = bool(it.get("done"))
        c.d.rounded_rectangle([x, ry + 4, x + 24, ry + 28], radius=4,
                              outline=_rgb(p["GREEN"] if done else p["PRI"]), width=2)
        if done:
            c.d.line([(x + 5, ry + 16), (x + 11, ry + 23), (x + 20, ry + 9)], fill=_rgb(p["GREEN"]), width=3)
        f = font(20)
        txt = ellipsize(str(it.get("label", "")), f, w - 40)
        c.d.text((x + 36, ry + 3), txt, font=f, fill=_rgb(p["TEXT_DIM"] if done else p["WHITE"]))
        if done:
            c.d.line([x + 36, ry + 17, x + 36 + _tw(f, txt), ry + 17], fill=_rgb(p["TEXT_DIM"]), width=2)


def countdown_left(b: dict, now: float | None = None) -> float | None:
    """Seconds left on a countdown block (it may be negative), or None."""
    now = time.time() if now is None else now
    if b.get("_end"):
        return float(b["_end"]) - now
    return None


def prepare_countdown(b: dict, now: float | None = None) -> None:
    """Fix a countdown's end time once, so re-rendering keeps counting down."""
    if b.get("_end"):
        return
    now = time.time() if now is None else now
    secs = num(b.get("seconds"))
    if secs is not None and secs > 0:
        b["_end"] = now + secs
        return
    until = str(b.get("until") or "").strip()
    m = re.match(r"^(\d{1,2})[:.h](\d{2})$", until)
    if m:
        t = datetime.now().replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)), second=0,
                                   microsecond=0)
        end = t.timestamp()
        if end <= now:
            end += 86400
        b["_end"] = end


def _draw_countdown(c, b, x, y, w, h):
    p = c.p
    prepare_countdown(b)
    left = countdown_left(b)
    if left is None:
        txt = "--:--"
    else:
        s = max(0, int(round(left)))
        hh, rem = divmod(s, 3600)
        mm, ss = divmod(rem, 60)
        txt = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
    f = font(84, bold=True, mono=True)
    colr = p["RED"] if left is not None and left <= 0 else (p["ACC2"] if left is not None and left < 60 else p["PRI"])
    c.d.text((x + (w - _tw(f, txt)) / 2, y), txt, font=f, fill=_rgb(colr))
    if b.get("text"):
        t = ellipsize(str(b["text"]), font(18), w)
        c.d.text((x + (w - _tw(font(18), t)) / 2, y + 104), t, font=font(18), fill=_rgb(p["TEXT_MED"]))


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and crop to fill exactly w x h."""
    iw, ih = img.size
    s = max(w / iw, h / ih)
    img = img.resize((max(1, int(iw * s)), max(1, int(ih * s))))
    iw, ih = img.size
    l, t = (iw - w) // 2, (ih - h) // 2
    return img.crop((l, t, l + w, t + h))


def _contain(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((w, h))
    return img


def _draw_image(c, b, x, y, w, h):
    p = c.p
    src = b.get("url") or b.get("path") or b.get("image")
    if not src and b.get("query"):
        urls = search_images(str(b["query"]), 3)
        src = next((u for u in urls if fetch_image(u, 64) is not None), "")
    pic = fetch_image(str(src or ""), 1200)
    box_h = 380
    if pic is None:
        c.d.rectangle([x, y, x + w, y + box_h], outline=_rgb(p["BORDER"]))
        c.d.text((x + 16, y + 16), "The picture could not be loaded.", font=font(18), fill=_rgb(p["TEXT_DIM"]))
    else:
        pic = _contain(pic, w, box_h)
        c.img.paste(pic, (x + (w - pic.size[0]) // 2, y + (box_h - pic.size[1]) // 2))
    if b.get("caption"):
        cap = ellipsize(str(b["caption"]), font(17), w)
        c.d.text((x + (w - _tw(font(17), cap)) / 2, y + box_h + 6), cap, font=font(17), fill=_rgb(p["TEXT_MED"]))


def _draw_gallery(c, b, x, y, w, h):
    p = c.p
    items = _items(b)
    srcs = [(str(it.get("image") or it.get("url") or ""), str(it.get("label") or "")) for it in items]
    if not srcs and b.get("query"):
        srcs = [(u, "") for u in search_images(str(b["query"]), 6)]
    pics = []
    for src, label in srcs:
        img = fetch_image(src, 600)
        if img is not None:
            pics.append((img, label))
        if len(pics) >= 4:
            break
    if not pics:
        c.d.text((x, y), "No pictures could be loaded.", font=font(18), fill=_rgb(p["TEXT_DIM"]))
        return
    n = len(pics)
    tw = (w - (n - 1) * 12) // n
    th = 210
    for i, (img, label) in enumerate(pics):
        tx = x + i * (tw + 12)
        c.img.paste(_cover(img, tw, th), (tx, y))
        c.d.rectangle([tx, y, tx + tw, y + th], outline=_rgb(p["BORDER_B"]), width=1)
        if label:
            c.d.rectangle([tx, y + th - 30, tx + tw, y + th], fill=_rgb(p["BG"], 200))
            c.d.text((tx + 8, y + th - 26), ellipsize(label, font(16), tw - 16), font=font(16),
                     fill=_rgb(p["WHITE"]))


_DRAW = {
    "text": _draw_text, "callout": _draw_text, "stats": _draw_stats, "rings": _draw_rings,
    "list": _draw_list, "table": _draw_table, "bars": _draw_bars, "line": _draw_line,
    "pie": _draw_pie, "progress": _draw_progress, "checklist": _draw_checklist,
    "countdown": _draw_countdown, "image": _draw_image, "gallery": _draw_gallery,
}
TYPES = tuple(_DRAW)


def render(spec: dict) -> Image.Image:
    """The whole board as a PIL image. Never raises."""
    p = palette()
    title = str(spec.get("title") or "")
    subtitle = str(spec.get("subtitle") or "")
    footer = str(spec.get("footer") or "")
    blocks = [b for b in (spec.get("blocks") or []) if isinstance(b, dict)][:MAX_BLOCKS]

    inner = W - 2 * MARGIN
    half = (inner - GAP) // 2
    # Lay blocks out in rows: a "full" block takes a row, two "half" blocks share one.
    layout, row, y = [], [], MARGIN + (64 if title else 0) + (26 if subtitle else 0)

    def flush(row_items, y0):
        hgt = max(measure(b, wdt) for b, wdt in row_items)
        xx = MARGIN
        for b, wdt in row_items:
            layout.append((b, xx, y0, wdt, hgt))
            xx += wdt + GAP
        return y0 + hgt + GAP

    def widen(row_items):
        # A half block left alone on its row takes the whole width, except the
        # kinds that look odd stretched (a few tiles, a single clock).
        if len(row_items) == 1 and row_items[0][0].get("type") not in ("stats", "rings", "countdown", "pie"):
            return [(row_items[0][0], inner)]
        return row_items

    for b in blocks:
        if _default_span(b) == "full":
            if row:
                y = flush(widen(row), y)
                row = []
            y = flush([(b, inner)], y)
        else:
            row.append((b, half))
            if len(row) == 2:
                y = flush(row, y)
                row = []
    if row:
        y = flush(widen(row), y)
    total_h = min(MAX_H, max(420, y - GAP + MARGIN + (30 if footer else 0)))

    img = Image.new("RGB", (W, total_h), _rgb(p["BG"]))
    c = _Ctx(img, p)
    # Faint HUD grid behind everything.
    for gx in range(0, W, 40):
        c.d.line([gx, 0, gx, total_h], fill=_mix(p["BG"], p["PRI_GHO"], 0.6), width=1)
    for gy in range(0, total_h, 40):
        c.d.line([0, gy, W, gy], fill=_mix(p["BG"], p["PRI_GHO"], 0.6), width=1)

    if title:
        ft = font(34, bold=True)
        c.d.text((MARGIN, MARGIN - 4), ellipsize(title, ft, inner - 200), font=ft, fill=_rgb(p["WHITE"]))
        stamp = datetime.now().strftime("%H:%M")
        c.d.text((W - MARGIN - _tw(font(16, mono=True), stamp), MARGIN + 6), stamp,
                 font=font(16, mono=True), fill=_rgb(p["TEXT_DIM"]))
        c.d.line([MARGIN, MARGIN + 48, MARGIN + 160, MARGIN + 48], fill=_rgb(p["PRI"]), width=3)
        c.d.line([MARGIN + 170, MARGIN + 48, W - MARGIN, MARGIN + 48], fill=_rgb(p["BORDER"]), width=1)
    if subtitle:
        c.d.text((MARGIN, MARGIN + 56), ellipsize(subtitle, font(17), inner), font=font(17),
                 fill=_rgb(p["TEXT_MED"]))

    for b, bx, by, bw, bh in layout:
        if by + 40 > total_h:
            break
        try:
            inner_y = _panel(c, bx, by, bw, bh, str(b.get("title") or ""))
            fn = _DRAW.get(str(b.get("type") or ""))
            if fn is None:
                c.d.text((bx + PAD, inner_y), f"(unknown block type '{b.get('type')}')", font=font(16),
                         fill=_rgb(p["TEXT_DIM"]))
                continue
            fn(c, b, bx + PAD, inner_y, bw - 2 * PAD, bh - (inner_y - by) - PAD)
        except Exception as e:
            print(f"[visual] block {b.get('type')} not drawn: {e}")
            try:
                c.d.text((bx + PAD, by + PAD + 30), f"(this part could not be drawn: {str(e)[:60]})",
                         font=font(15), fill=_rgb(p["RED"]))
            except Exception:
                pass

    if footer:
        c.d.text((MARGIN, total_h - MARGIN - 4), ellipsize(footer, font(15), inner), font=font(15),
                 fill=_rgb(p["TEXT_DIM"]))
    return img


# ── Files ────────────────────────────────────────────────────────────────────

def out_dir() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parent.parent
    d = base / "memory" / "plugin_data" / "boards"
    d.mkdir(parents=True, exist_ok=True)
    return d


_counter = [0]


def render_to_file(spec: dict, stem: str = "board") -> Path:
    """Render and save as PNG under memory/plugin_data/boards/.

    The name changes on every render (two alternating files per stem), so the
    HUD never shows a cached older picture of the same path."""
    img = render(spec)
    with _lock:
        _counter[0] += 1
        n = _counter[0] % 2
    path = out_dir() / f"{stem}_{n}.png"
    img.save(path, "PNG", optimize=False)
    return path


def as_text(spec: dict) -> str:
    """The same board as plain text, for the content panel fallback and the model."""
    out = []
    if spec.get("subtitle"):
        out.append(str(spec["subtitle"]))
    for b in (spec.get("blocks") or [])[:MAX_BLOCKS]:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if b.get("title"):
            out.append(f"\n{str(b['title']).upper()}")
        if t in ("text", "callout"):
            out.append(str(b.get("text") or ""))
        elif t == "table":
            cols = b.get("columns") or []
            if cols:
                out.append(" | ".join(str(c) for c in cols))
            out.extend(" | ".join(r) for r in _rows(b)[:20])
        elif t == "line":
            pts = [v for v in (num(x) for x in (b.get("points") or [])) if v is not None]
            if pts:
                u = b.get("unit") or ""
                out.append(f"{len(pts)} points, from {with_unit(pts[0], u)} to {with_unit(pts[-1], u)} "
                           f"(min {with_unit(min(pts), u)}, max {with_unit(max(pts), u)})")
        elif t == "countdown":
            prepare_countdown(b)
            left = countdown_left(b)
            if left is not None:
                out.append(f"Countdown: {int(max(0, left)) // 60} min {int(max(0, left)) % 60} s left"
                           + (f" - {b['text']}" if b.get("text") else ""))
        elif t in ("image", "gallery"):
            out.append(f"(picture{'s' if t == 'gallery' else ''}: {b.get('query') or b.get('caption') or ''})")
        else:
            for it in _items(b)[:14]:
                mark = ""
                if t == "checklist":
                    mark = "[x] " if it.get("done") else "[ ] "
                bits = [str(it.get("label", ""))]
                if it.get("value") not in (None, ""):
                    val = it["value"]
                    if t == "bars" and isinstance(val, (int, float)):
                        val = with_unit(float(val), b.get("unit") or "")
                    bits.append(str(val) + (f" / {it['max']}" if it.get("max") else ""))
                if it.get("detail"):
                    bits.append(f"({it['detail']})")
                out.append("- " + mark + " ".join(bits))
    if spec.get("footer"):
        out.append(f"\n{spec['footer']}")
    return "\n".join(out).strip()


# ── Putting a board on the HUD ───────────────────────────────────────────────

def show(player, spec: dict, stem: str = "board", title: str = "") -> tuple[bool, str]:
    """Render `spec` and put it where the face is. -> (shown, path or reason).

    Falls back to the text panel under the head when there is no picture
    surface (an older ui.py) or the picture could not be drawn."""
    try:
        path = render_to_file(spec, stem)
    except Exception as e:
        reason = f"the board could not be drawn: {e}"
        _text_fallback(player, spec, title)
        return False, reason
    fn = getattr(player, "show_image", None) if player is not None else None
    if not callable(fn):
        _text_fallback(player, spec, title)
        return False, "the screen has no picture surface here, so it is shown as text"
    try:
        fn(str(path), title or str(spec.get("title") or ""))
        return True, str(path)
    except Exception as e:
        _text_fallback(player, spec, title)
        return False, f"the screen could not show it right now: {e}"


def _text_fallback(player, spec: dict, title: str = "") -> None:
    if player is None:
        return
    try:
        player.show_content(title or str(spec.get("title") or "Board"), as_text(spec))
    except Exception:
        pass


def is_showing(player, stem: str) -> bool | None:
    """Is a board saved under `stem` the picture on the HUD right now?
    None when the HUD cannot say (no player, or an older ui.py)."""
    fn = getattr(player, "video_state", None) if player is not None else None
    if not callable(fn):
        return None
    try:
        st = fn() or {}
    except Exception:
        return None
    if st.get("kind") != "image":
        return False
    src = str(st.get("source") or "")
    if not src:
        return None
    return Path(src).name.startswith(stem + "_") and Path(src).parent == out_dir()


def close(player, stem: str) -> bool:
    """Close the HUD picture if it is ours. -> whether something was closed."""
    showing = is_showing(player, stem)
    if showing is False:
        return False
    try:
        player.stop_video()
        return True
    except Exception:
        return False
