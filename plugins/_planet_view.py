"""
plugins/_planet_view.py — still maps for planet_watch, drawn with PIL.

Helper module (leading underscore: the loader skips it).

A map is a Web-Mercator window around a centre at a zoom level. The base is
OpenStreetMap's standard tiles when they can be fetched, and a plain dark
lat/lon grid when they cannot, so a map is always produced.

OSM tile usage policy, kept here deliberately:
  * a User-Agent that names this application;
  * every tile cached on disk (7 days) and in memory, so the same view never
    downloads twice;
  * at most two downloads at a time and a hard cap on tiles per map;
  * the attribution "© OpenStreetMap contributors" on every map.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

from plugins._planet_live import USER_AGENT

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MAX_TILES = 30
CACHE_DAYS = 7
_mem: dict[tuple[int, int, int], object] = {}
_mem_lock = threading.Lock()

BG = (6, 14, 22)
GRID = (30, 70, 90)
TEXT = (220, 240, 250)
ACCENT = (0, 212, 255)


def _cache_dir() -> Path:
    try:
        from plugins import _announce
        d = _announce.data_dir() / "planet_tiles"
    except Exception:
        d = Path(tempfile.gettempdir()) / "jarvis_planet_tiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ── Projection ────────────────────────────────────────────────────────────────
def world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    lat = max(-85.0511, min(85.0511, lat))
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def zoom_for_radius(lat: float, radius_km: float, half_px: int = 330) -> int:
    mpp = radius_km * 1000.0 / max(50, half_px)
    z = math.log2(156543.03 * max(0.05, math.cos(math.radians(lat))) / max(1e-6, mpp))
    return int(max(1, min(13, math.floor(z))))


# ── Tiles ─────────────────────────────────────────────────────────────────────
def _tile(z: int, x: int, y: int):
    from PIL import Image
    n = 2 ** z
    x %= n
    if not 0 <= y < n:
        return None
    key = (z, x, y)
    with _mem_lock:
        if key in _mem:
            return _mem[key]
    path = _cache_dir() / str(z) / str(x) / f"{y}.png"
    img = None
    try:
        if path.exists() and time.time() - path.stat().st_mtime < CACHE_DAYS * 86400:
            img = Image.open(path).convert("RGB")
    except Exception:
        img = None
    if img is None:
        import requests
        r = requests.get(TILE_URL.format(z=z, x=x, y=y), timeout=8,
                         headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        except Exception:
            pass
    with _mem_lock:
        if len(_mem) > 200:
            _mem.clear()
        _mem[key] = img
    return img


def _base(cx: float, cy: float, z: int, w: int, h: int, use_tiles: bool):
    """The background picture and whether real tiles made it."""
    from PIL import Image, ImageEnhance
    left, top = cx - w / 2, cy - h / 2
    tx0, ty0 = int(math.floor(left / TILE)), int(math.floor(top / TILE))
    tx1, ty1 = int(math.floor((left + w - 1) / TILE)), int(math.floor((top + h - 1) / TILE))
    tiles = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    canvas = Image.new("RGB", (w, h), BG)
    if use_tiles and len(tiles) <= MAX_TILES:
        got = 0
        with ThreadPoolExecutor(2) as ex:        # two connections at most
            futs = {ex.submit(_tile, z, tx, ty): (tx, ty) for tx, ty in tiles}
            for fut, (tx, ty) in futs.items():
                try:
                    img = fut.result(timeout=20)
                except Exception as e:
                    print(f"[PlanetWatch] tile {z}/{tx}/{ty} failed: {e}")
                    img = None
                if img is not None:
                    canvas.paste(img, (int(tx * TILE - left), int(ty * TILE - top)))
                    got += 1
        if got:
            # Darken so the HUD's markers stand out on the light OSM style.
            canvas = ImageEnhance.Brightness(canvas).enhance(0.62)
            return canvas, True
    return canvas, False


def _graticule(draw, cx, cy, z, w, h, strong: bool):
    span_deg = 360.0 * w / (TILE * 2 ** z)
    step = next((s for s in (60, 30, 15, 10, 5, 2, 1, 0.5, 0.25, 0.1) if span_deg / s >= 4), 0.1)
    left, top = cx - w / 2, cy - h / 2
    col = GRID if strong else (80, 110, 130)
    lon = -180.0
    while lon <= 180.0 + 1e-9:
        x, _ = world_px(0, lon, z)
        n = TILE * 2 ** z
        for k in (-1, 0, 1):
            xx = x + k * n - left
            if 0 <= xx < w:
                draw.line([(xx, 0), (xx, h)], fill=col, width=1)
        lon += step
    lat = -80.0
    while lat <= 80.0 + 1e-9:
        _, y = world_px(lat, 0, z)
        yy = y - top
        if 0 <= yy < h:
            draw.line([(0, yy), (w, yy)], fill=col, width=1)
        lat += step


# ── Public ────────────────────────────────────────────────────────────────────
def render(*, center: tuple[float, float], zoom: int, title: str,
           points: Iterable[dict] = (), legend: Iterable[tuple[str, tuple]] = (),
           home: Optional[tuple[float, float]] = None, heat: Iterable[tuple] = (),
           size: tuple[int, int] = (1024, 720), use_tiles: bool = True,
           out_name: str = "planet_watch_map.png") -> str:
    """Draw a map and return the PNG path.

    points: {'lat','lon','color':(r,g,b),'r':px radius,'label':str,'shape':'dot'|'plane'|'diamond',
             'track': degrees for planes}
    heat:   (lat, lon, value 0-100) cells drawn as translucent green (aurora).
    """
    from PIL import Image, ImageDraw

    w, h = size
    z = int(max(0, min(15, zoom)))
    n = TILE * 2 ** z
    if z <= 2:
        # The whole world fits: keep the window inside it vertically.
        h = min(h, n)
        w = min(w, n) if z < 2 else w
    cx, cy = world_px(center[0], center[1], z)
    cy = max(h / 2, min(n - h / 2, cy)) if n >= h else n / 2
    canvas, tiled = _base(cx, cy, z, w, h, use_tiles)
    draw = ImageDraw.Draw(canvas, "RGBA")
    if not tiled:
        _graticule(draw, cx, cy, z, w, h, True)
    left, top = cx - w / 2, cy - h / 2

    def to_xy(lat, lon):
        x, y = world_px(lat, lon, z)
        # nearest copy of the world, so a point across the date line still shows
        x = min((x + k * n for k in (-1, 0, 1)), key=lambda v: abs(v - cx))
        return x - left, y - top

    # aurora probability cells
    cell = max(2, int(n / 360))
    for lat, lon, val in heat:
        x, y = to_xy(lat, lon if lon <= 180 else lon - 360)
        if -cell <= x <= w + cell and -4 * cell <= y <= h + 4 * cell:
            a = int(min(170, 40 + val * 2.2))
            # one-degree cells grow taller towards the poles on this projection
            cy_half = cell / max(0.08, math.cos(math.radians(lat)))
            draw.rectangle([x - cell, y - cy_half, x + cell, y + cy_half], fill=(40, 255, 120, a))

    small = _font(13)
    for p in points:
        try:
            x, y = to_xy(float(p["lat"]), float(p["lon"]))
        except Exception:
            continue
        if not (-20 <= x <= w + 20 and -20 <= y <= h + 20):
            continue
        r = float(p.get("r", 5))
        col = tuple(p.get("color", ACCENT))
        shape = p.get("shape", "dot")
        if shape == "plane":
            ang = math.radians(float(p.get("track") or 0))
            tip = (x + math.sin(ang) * r * 1.6, y - math.cos(ang) * r * 1.6)
            lw = (x + math.sin(ang + 2.5) * r, y - math.cos(ang + 2.5) * r)
            rw = (x + math.sin(ang - 2.5) * r, y - math.cos(ang - 2.5) * r)
            draw.polygon([tip, lw, rw], fill=col + (235,), outline=(0, 0, 0, 255))
        elif shape == "diamond":
            draw.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)],
                         fill=col + (210,), outline=(0, 0, 0, 255))
        else:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=col + (150,), outline=col + (255,), width=2)
        if p.get("label"):
            draw.text((x + r + 3, y - 7), str(p["label"]), fill=TEXT, font=small,
                      stroke_width=2, stroke_fill=(0, 0, 0))

    if home:
        x, y = to_xy(*home)
        draw.line([(x - 9, y), (x + 9, y)], fill=(255, 255, 255, 255), width=3)
        draw.line([(x, y - 9), (x, y + 9)], fill=(255, 255, 255, 255), width=3)
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(255, 60, 60, 255))

    # title + legend
    big = _font(20)
    draw.rectangle([0, 0, w, 34], fill=(0, 0, 0, 150))
    draw.text((12, 7), title[:90], fill=ACCENT, font=big)
    ly = 44
    for label, col in legend:
        draw.rectangle([12, ly + 3, 24, ly + 15], fill=tuple(col) + (255,))
        draw.text((30, ly), label, fill=TEXT, font=small, stroke_width=2, stroke_fill=(0, 0, 0))
        ly += 20
    credit = "© OpenStreetMap contributors" if tiled else "grid only (map tiles unavailable)"
    tw = draw.textlength(credit, font=small)
    draw.rectangle([w - tw - 14, h - 22, w, h], fill=(0, 0, 0, 160))
    draw.text((w - tw - 7, h - 19), credit, fill=TEXT, font=small)

    stem, _dot, ext = out_name.rpartition(".")
    # A fresh name per map: two maps drawn at once must not overwrite each other
    # before the HUD has loaded the first one.
    path = os.path.join(tempfile.gettempdir(), f"{stem or out_name}_{int(time.time() * 1000) % 10**8}.{ext or 'png'}")
    _prune(stem or out_name)
    canvas.save(path, "PNG")
    return path


def _prune(stem: str, keep: int = 3) -> None:
    """Old copies of a map are removed, so the temp folder does not fill up."""
    try:
        d = Path(tempfile.gettempdir())
        old = sorted(d.glob(f"{stem}_*.png"), key=lambda q: q.stat().st_mtime, reverse=True)
        for q in old[keep:]:
            q.unlink(missing_ok=True)
    except Exception:
        pass


# ── Marker styles shared by the plugin ───────────────────────────────────────
def quake_color(age_s: float) -> tuple[int, int, int]:
    if age_s < 3 * 3600:
        return (255, 40, 40)
    if age_s < 86400:
        return (255, 140, 0)
    return (255, 220, 60)


def quake_radius(mag: float, z: int) -> float:
    return max(3.0, 2.2 * (mag - 1.5)) * (1.0 if z <= 4 else 1.3)


ALERT_COLOR = {"red": (255, 40, 40), "orange": (255, 150, 0), "green": (60, 220, 90),
               "": (120, 180, 255)}
