"""
plugins/_screen_eyes.py — what the screen agent sees before each step.

Helper module (leading underscore: the plugin loader skips it). Used by
plugins/_screen_loop.py; pure functions on PIL images so the self-test
(plugins/_screen_eyes_tests.py) can drive every part with drawn pictures.

THREE THINGS A PERSON DOES WITHOUT THINKING, AND A MODEL DOES NOT
    1. Numbered marks. A vision model asked "where is the Save button, 0-1000"
       is often a few dozen pixels off, which on a toolbar is the next button.
       Asked "which number is the Save button" it is right far more often. So
       before each look the things that can probably be pressed get a small
       numbered tag on the picture: every enabled control the accessibility
       tree reports, plus — for programs that report little — rectangles found
       in the picture itself (edges and contrast, OpenCV). The model answers
       with a number; the loop clicks the control (by its tree element when it
       has one) or the middle of the box. Rectangles inside photos are left
       out (a photo is full of edges and none of them is a button), and so are
       big panels that merely contain other boxes.

    2. Waiting until the screen stops changing. core/screenview.wait_stable
       compares accessibility readings; a page that reports nothing looks
       "settled" while it is still drawing. `settle` compares the pixels
       themselves on a coarse grid until nothing has changed for a short quiet
       time — and while it waits it notices cells that change on almost every
       sample: a spinner, a progress bar, a video, a blinking caret. Those
       are "moving", are ignored for settling (or a video would hold the wait
       to its limit every time) and are reported to the model as areas that
       are loading or animated.

    3. Did anything change where I acted? A click on a button that does
       nothing at all is worth knowing at once. `spot_change` measures the
       difference in a small square around the point that was acted on,
       separately from the whole-screen measure the loop already makes.

Nothing here touches the screen, the mouse or Qt. OpenCV and numpy are
imported only inside the shape finder; without them there are no shape
marks, and everything else still works.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

# ── tuning (measured on 1440-wide screenshots, the size the loop sends) ──────
GRID_COLS, GRID_ROWS = 64, 36      # change grid; same size the loop always used
CELL_CHANGED = 18                  # grey levels a grid cell must move to count
MAX_MARKS = 60                     # more numbers than this is noise, not help
MAX_MARK_SHARE = 0.25              # a "control" covering a quarter of the screen is a pane
MIN_SHAPE_PX = (12, 10)            # smaller than this: an icon fragment or a letter
MAX_SHAPE_SHARE = (0.5, 0.25)      # wider / taller share than this: a panel
PANEL_CHILDREN = 3                 # a box holding this many others is a panel
PHOTO_TONES = 40                   # distinct colours in a cell that make it a photo
PHOTO_CELL_PX = 24
SETTLE_MAX_S = 4.0
SETTLE_QUIET_S = 0.5
SETTLE_POLL_S = 0.15
MOVING_SHARE = 0.7                 # changed on at least this share of samples...
MOVING_MIN_SAMPLES = 5             # ...out of at least this many
MOVING_MAX_SHARE = 0.25            # more of the screen than this is a page loading, not an animation
SPOT_HALF = 40                     # half-side of the square around an acted-on point (picture px)
SPOT_PIXEL = 24                    # per-pixel difference that counts as changed
MAX_REGIONS = 4

TAG_COLOUR = (255, 196, 0)
TAG_TEXT = (16, 16, 16)


# ── marks ────────────────────────────────────────────────────────────────────
@dataclass
class Mark:
    n: int
    box: tuple[int, int, int, int]       # picture pixels: x0, y0, x1, y1
    element: Any = None                  # screenview.Element, or None for a shape
    kind: str = "shape"                  # "control" | "shape"

    def center(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0

    def center_norm(self, size: tuple[int, int]) -> tuple[float, float]:
        """Centre in the loop's 0-1000 picture units."""
        cx, cy = self.center()
        return cx / max(1, size[0]) * 1000.0, cy / max(1, size[1]) * 1000.0

    def describe(self) -> str:
        el = self.element
        if el is None:
            return f"{self.n}: box"
        name = (getattr(el, "label", "") or "")[:40]
        return f"{self.n}: {getattr(el, 'role', '?')}" + (f" {name!r}" if name else "")


def _overlap(a, b) -> float:
    """Share of the SMALLER box that the two boxes have in common."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / float(max(1, smaller))


def _contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3] and outer != inner)


def control_boxes(elements: Iterable[Any], monitor: dict,
                  size: tuple[int, int]) -> list[tuple[tuple[int, int, int, int], Any]]:
    """(picture box, element) for every enabled, pressable control on this
    picture. Element rects are in mouse units; the picture is `size` pixels
    covering `monitor`."""
    mw, mh = float(monitor.get("width") or 0), float(monitor.get("height") or 0)
    if mw <= 0 or mh <= 0:
        return []
    sx, sy = size[0] / mw, size[1] / mh
    out, seen = [], set()
    for el in elements:
        try:
            # Secret fields are marked too: clicking into one is fine, and
            # core/screenact refuses to type into it whatever the model says.
            if not el.interactable:
                continue
            l, t, w, h = el.rect
        except Exception:
            continue
        if w <= 0 or h <= 0 or w * h > MAX_MARK_SHARE * mw * mh:
            continue
        x0 = int((l - monitor["left"]) * sx)
        y0 = int((t - monitor["top"]) * sy)
        x1, y1 = int(x0 + w * sx), int(y0 + h * sy)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if not (0 <= cx < size[0] and 0 <= cy < size[1]):
            continue                            # on another monitor or scrolled away
        box = (max(0, x0), max(0, y0), min(size[0], x1), min(size[1], y1))
        if box in seen:
            continue
        seen.add(box)
        out.append((box, el))
    return out


def photo_cells(image) -> set[tuple[int, int]]:
    """(col, row) cells of PHOTO_CELL_PX pixels that look like a photo or a
    video frame: many distinct colours. Buttons, text and flat panels have a
    handful."""
    try:
        img = image.convert("RGB")
        cells = set()
        cols = max(1, img.width // PHOTO_CELL_PX)
        rows = max(1, img.height // PHOTO_CELL_PX)
        # Coarse colour buckets (4 bits per channel) so JPEG noise on a flat
        # button does not read as many colours.
        small = img.point(lambda v: v & 0xF0)
        for r in range(rows):
            for c in range(cols):
                patch = small.crop((c * PHOTO_CELL_PX, r * PHOTO_CELL_PX,
                                    (c + 1) * PHOTO_CELL_PX, (r + 1) * PHOTO_CELL_PX))
                colours = patch.getcolors(maxcolors=PHOTO_TONES + 1)
                if colours is None:             # more than PHOTO_TONES colours
                    cells.add((c, r))
        return cells
    except Exception:
        return set()


def _in_photo(box, photos: set) -> bool:
    if not photos:
        return False
    c0, r0 = box[0] // PHOTO_CELL_PX, box[1] // PHOTO_CELL_PX
    c1, r1 = max(c0, (box[2] - 1) // PHOTO_CELL_PX), max(r0, (box[3] - 1) // PHOTO_CELL_PX)
    total = hits = 0
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            total += 1
            hits += (c, r) in photos
    return total > 0 and hits / total >= 0.5


def shape_boxes(image, photos: Optional[set] = None) -> list[tuple[int, int, int, int]]:
    """Rectangles in the picture that look like buttons, fields or tiles.
    Empty when OpenCV is not installed or the picture cannot be read."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    try:
        rgb = np.asarray(image.convert("RGB"))
        grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(cv2.GaussianBlur(grey, (3, 3), 0), 40, 120)
        contrast = cv2.adaptiveThreshold(grey, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                         cv2.THRESH_BINARY_INV, 15, 8)
        mask = cv2.bitwise_or(edges, contrast)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)))
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    except Exception as e:
        print(f"[ScreenEyes] shapes not found: {e}")
        return []
    h_img, w_img = grey.shape[:2]
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w < MIN_SHAPE_PX[0] or h < MIN_SHAPE_PX[1]:
            continue
        if w > MAX_SHAPE_SHARE[0] * w_img or h > MAX_SHAPE_SHARE[1] * h_img:
            continue
        if w / float(h) > 25:                   # a rule line, not a control
            continue
        boxes.append((x, y, x + w, y + h))
    photos = photos if photos is not None else set()
    boxes = [b for b in boxes if not _in_photo(b, photos)]
    # Near-duplicates (the outline and the fill of one button): keep the larger.
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    kept: list[tuple[int, int, int, int]] = []
    for b in boxes:
        if any(_overlap(b, k) > 0.8 and _similar_size(b, k) for k in kept):
            continue
        kept.append(b)
    # Panels: a box that holds several other boxes is a container.
    return [b for b in kept if sum(_contains(b, o) for o in kept) < PANEL_CHILDREN]


def _similar_size(a, b) -> bool:
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return min(aa, ab) / float(max(1, max(aa, ab))) > 0.6


def find_marks(image, monitor: dict, elements: Iterable[Any] = (), *,
               shapes: bool = True, limit: int = MAX_MARKS) -> list[Mark]:
    """Numbered marks for one picture: controls first, then shapes that no
    control already covers, numbered in reading order (top to bottom, then
    left to right). Never raises."""
    try:
        size = image.size
        found: list[Mark] = [Mark(0, box, el, "control")
                             for box, el in control_boxes(elements, monitor, size)]
        if shapes and len(found) < limit:
            for box in shape_boxes(image, photo_cells(image)):
                if any(_overlap(box, m.box) > 0.5 for m in found):
                    continue
                found.append(Mark(0, box, None, "shape"))
        controls = [m for m in found if m.kind == "control"]
        others = [m for m in found if m.kind != "control"]
        # Controls win a tight budget; among shapes, button-sized ones first.
        others.sort(key=lambda m: abs((m.box[3] - m.box[1]) - 28))
        chosen = (controls + others)[:limit]
        band = max(8, size[1] // 60)
        chosen.sort(key=lambda m: (m.box[1] // band, m.box[0]))
        for i, m in enumerate(chosen, 1):
            m.n = i
        return chosen
    except Exception as e:
        print(f"[ScreenEyes] marks skipped: {e}")
        return []


def draw_marks(image, marks: list[Mark]):
    """A copy of `image` with each mark outlined and its number in a tag at
    its top-left corner. The original is left clean for crops and checks."""
    from PIL import ImageDraw, ImageFont
    out = image.convert("RGB").copy()
    if not marks:
        return out
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default(size=13)
    except Exception:
        font = ImageFont.load_default()
    for m in marks:
        x0, y0, x1, y1 = m.box
        d.rectangle([x0, y0, x1, y1], outline=TAG_COLOUR, width=1)
        label = str(m.n)
        try:
            l, t, r, b = d.textbbox((0, 0), label, font=font)
            tw, th = r - l, b - t
        except Exception:
            tw, th = 7 * len(label), 11
        tx, ty = x0, max(0, y0 - th - 4)
        d.rectangle([tx, ty, tx + tw + 4, ty + th + 4], fill=TAG_COLOUR)
        d.text((tx + 2, ty + 1), label, fill=TAG_TEXT, font=font)
    return out


def by_number(marks: list[Mark], n: Any) -> Optional[Mark]:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return next((m for m in marks if m.n == n), None)


# ── change on a coarse grid ──────────────────────────────────────────────────
def tiny(image) -> list[int]:
    """The picture as GRID_COLS x GRID_ROWS grey levels."""
    return list(image.convert("L").resize((GRID_COLS, GRID_ROWS)).getdata())


def changed_cells(a: list[int], b: list[int], ignore: Iterable[int] = ()) -> set[int]:
    skip = set(ignore)
    return {i for i, (p, q) in enumerate(zip(a, b))
            if abs(p - q) > CELL_CHANGED and i not in skip}


def regions(cells: Iterable[int], limit: int = MAX_REGIONS
            ) -> list[tuple[int, int, int, int]]:
    """Connected groups of grid cells as boxes in 0-1000 units, biggest first."""
    left = set(cells)
    found = []
    while left:
        start = left.pop()
        stack, group = [start], [start]
        while stack:
            i = stack.pop()
            c, r = i % GRID_COLS, i // GRID_COLS
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                cc, rr = c + dc, r + dr
                j = rr * GRID_COLS + cc
                if 0 <= cc < GRID_COLS and 0 <= rr < GRID_ROWS and j in left:
                    left.discard(j)
                    stack.append(j)
                    group.append(j)
        cs = [i % GRID_COLS for i in group]
        rs = [i // GRID_COLS for i in group]
        found.append((len(group), (int(min(cs) * 1000 / GRID_COLS), int(min(rs) * 1000 / GRID_ROWS),
                                   int((max(cs) + 1) * 1000 / GRID_COLS),
                                   int((max(rs) + 1) * 1000 / GRID_ROWS))))
    found.sort(key=lambda t: -t[0])
    return [box for _n, box in found[:limit]]


@dataclass
class Settled:
    seconds: float = 0.0
    still: bool = True                         # False: the wait ran out
    moving: set = field(default_factory=set)   # grid cells that kept changing
    samples: int = 0

    def moving_regions(self) -> list[tuple[int, int, int, int]]:
        return regions(self.moving)

    def describe(self) -> str:
        """One line for the model, or "" when there is nothing to say."""
        parts = []
        boxes = self.moving_regions()
        if boxes:
            where = "; ".join(f"x={b[0]}-{b[2]}, y={b[1]}-{b[3]}" for b in boxes)
            parts.append("STILL MOVING (a spinner, progress bar, video or animation - if the "
                         f"task waits on it, use \"wait\"): {where}")
        if not self.still:
            parts.append(f"The screen was still changing after {self.seconds:.1f} s; it may "
                         "not have finished loading.")
        return "\n".join(parts)


def settle(grab: Callable[[], Any], *, max_s: float = SETTLE_MAX_S,
           quiet_s: float = SETTLE_QUIET_S, poll_s: float = SETTLE_POLL_S,
           stop: Any = None, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> tuple[Any, Settled]:
    """Grab pictures until nothing but the always-moving cells has changed
    for `quiet_s`, or `max_s` has passed. Returns (last picture, Settled).

    `grab()` returns a PIL picture (the loop paints its own window grey
    first, so the assistant's animations never hold the wait). `clock` and
    `sleep` are injectable for the self-test. Never raises past `grab`."""
    start = clock()
    image = grab()
    prev = tiny(image)
    counts: dict[int, int] = {}
    samples = 0
    quiet_since = start
    moving: set[int] = set()
    while True:
        if stop is not None and stop.is_set():
            return image, Settled(clock() - start, False, moving, samples)
        if clock() - start >= max_s:
            return image, Settled(clock() - start, False, moving, samples)
        sleep(poll_s)
        image = grab()
        cur = tiny(image)
        changed = changed_cells(prev, cur)
        prev = cur
        samples += 1
        for i in changed:
            counts[i] = counts.get(i, 0) + 1
        if samples >= MOVING_MIN_SAMPLES:
            moving = {i for i, n in counts.items() if n >= MOVING_SHARE * samples}
            if len(moving) > MOVING_MAX_SHARE * GRID_COLS * GRID_ROWS:
                moving = set()                  # the whole page is loading
        if changed - moving:
            quiet_since = clock()
        elif clock() - quiet_since >= quiet_s:
            return image, Settled(clock() - start, True, moving, samples)


def spot_change(before, after, x: float, y: float, half: int = SPOT_HALF) -> float:
    """Share of pixels that changed in a square around (x, y), picture pixels.
    0.0 when the pictures cannot be compared."""
    try:
        from PIL import ImageChops
        if before.size != after.size:
            return 0.0
        box = (max(0, int(x - half)), max(0, int(y - half)),
               min(before.width, int(x + half)), min(before.height, int(y + half)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return 0.0
        diff = ImageChops.difference(before.convert("L").crop(box), after.convert("L").crop(box))
        hist = diff.histogram()
        changed = sum(hist[SPOT_PIXEL:])
        return changed / float(max(1, sum(hist)))
    except Exception:
        return 0.0
