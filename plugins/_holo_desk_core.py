"""
plugins/_holo_desk_core.py — the Holo Desk's panels, their physics, and the
hands (or mouse) that move them.

Helper module (leading underscore: the plugin loader skips it). Pure logic: no
Qt, no camera, no network. The window (gui/holo_desk.py) draws a Desk and feeds
an Input with hands from plugins/_hands_onnx.py or with the mouse; the voice
tool (plugins/holo_desk.py) adds, removes and reads panels. Both sides use the
same Desk object, so every method takes the desk's lock.

Coordinates are 0..1 of the SCREEN on both axes. A panel's w and h are in
those units too; the window turns them into pixels.

THE GESTURES (what the guide panel and the 'help' action describe)
  pinch a title bar ........ pick the panel up; open the fingers to let go
  let go while moving ...... it is thrown; thrown off the screen, it closes
  pinch inside a list ...... scroll it up and down
  both hands on one panel .. pull apart / together to resize it
  pull a held panel nearer . it grows (the hand gets bigger in the picture)
  clap two open hands ...... every card is hidden; spread them again to
                             show it all
  pinch and hold on space .. a ring of new panels opens round the hand
  quick pinch .............. presses buttons: the dock, a headline, a note
  point at a panel ......... it becomes "this" for "close this", "read this"
  turn a held hand ......... rotates the 3D model
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

KINDS = ("clock", "system", "weather", "news", "notes", "routines", "model",
         "image", "video", "text", "guide")
LIST_KINDS = ("news", "notes", "routines", "guide")
SINGLE = ("clock", "system", "guide", "routines", "notes")   # one of each is plenty
# What the dock and the ring offer, in order.
DOCK = ("clock", "system", "weather", "news", "notes", "routines", "model", "guide")
RING = ("clock", "system", "weather", "news", "notes", "routines", "model")

LABELS = {"clock": "Clock", "system": "System", "weather": "Weather", "news": "News",
          "notes": "Notes", "routines": "Coming up", "model": "3D model", "image": "Picture",
          "video": "Video", "text": "Note", "guide": "How to use the desk"}

DEFAULT_SIZE = {"clock": (0.24, 0.20), "system": (0.24, 0.26), "weather": (0.26, 0.28),
                "news": (0.30, 0.46), "notes": (0.28, 0.42), "routines": (0.28, 0.36),
                "model": (0.28, 0.40), "image": (0.32, 0.38), "video": (0.36, 0.36),
                "text": (0.30, 0.34), "guide": (0.34, 0.56)}

HEADER = 0.045            # title bar height, screen units
DOCK_Y = 0.915            # the dock's centre line
DOCK_R = 0.032            # a dock button's radius (in screen-height units)

GUIDE = [
    "Carry a card: pinch its top bar, then open your fingers to drop it",
    "Release while still moving to fling it; flung off the screen, it is dismissed",
    "In a list, pinch and drag up or down to scroll",
    "Resize: pinch the same card with both hands and spread or close them",
    "Bring a held card closer to the camera to enlarge it",
    "Hide everything: bring two open hands together quickly; spread them again to show it all",
    "New card: keep a pinch still on empty space until a circle of choices appears",
    "Tap with a short pinch to press buttons, headlines and notes",
    "Aim your index finger at a card, then say 'read this', 'close this' or 'make this bigger'",
    "Holding the 3D model, twist your wrist to spin it",
    "With a mouse: drag a top bar, roll the wheel to resize, Esc closes the desk",
]


@dataclass
class Panel:
    kind: str
    title: str = ""
    text: str = ""
    arg: str = ""              # city, topic, file path or link
    x: float = 0.06            # top-left corner
    y: float = 0.08
    w: float = 0.30
    h: float = 0.30
    pid: int = 0
    # Runtime state (not saved)
    rows: list = field(default_factory=list)      # [(text, link or "")] for lists
    status: str = ""           # "loading", an error, or ""
    scroll: float = 0.0        # rows scrolled
    yaw: float = 0.6           # 3D model rotation
    pitch: float = 0.15
    vx: float = 0.0            # thrown velocity, screen units / s
    vy: float = 0.0
    held: bool = False
    born: float = 0.0          # for the appear animation
    gone: float = 0.0          # when it started to vanish (0 = alive)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def on_header(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + HEADER

    def on_close(self, px: float, py: float) -> bool:
        return self.on_header(px, py) and px >= self.x + self.w - HEADER

    def heading(self) -> str:
        if self.title:
            return self.title
        if self.kind == "weather" and self.arg:
            return f"Weather - {self.arg}"
        if self.kind == "news" and self.arg:
            return f"News - {self.arg}"
        return LABELS.get(self.kind, "Note")

    def body(self) -> str:
        """What the panel shows, as text: for 'read this' and the guide."""
        if self.kind == "clock":
            return time.strftime("%H:%M:%S\n%A %d %B %Y")
        if self.kind == "system":
            return "\n".join(system_lines())
        if self.rows:
            return "\n".join(r[0] for r in self.rows)
        if self.status and not self.text:
            return self.status
        if self.kind in ("image", "video", "model") and not self.text:
            return f"{LABELS[self.kind]}: {self.arg or 'the assistant'}"
        return self.text or ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("kind", "title", "text", "arg", "x", "y", "w", "h")}


def system_lines() -> list:
    try:
        import psutil
        mem = psutil.virtual_memory()
        lines = [f"CPU  {psutil.cpu_percent(interval=None):.0f}%",
                 f"RAM  {mem.percent:.0f}%  ({mem.used / 2**30:.1f} of {mem.total / 2**30:.0f} GB)"]
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                lines.append(f"BAT  {bat.percent:.0f}%{'  charging' if bat.power_plugged else ''}")
        except Exception:
            pass
        try:
            disk = psutil.disk_usage("/")
            lines.append(f"DISK {disk.percent:.0f}%")
        except Exception:
            pass
        return lines
    except Exception:
        return ["System readings are not available."]


# ─────────────────────────────────────────────────────────────────────────────
#  The desk
# ─────────────────────────────────────────────────────────────────────────────
class Desk:
    FRICTION = 2.6            # per second: a throw slows to ~7% in a second
    SNAP = 0.025              # a dropped panel this close to an edge sticks to it
    VANISH = 0.35             # seconds a closing panel takes to fade

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.lock = threading.RLock()
        self.panels: list[Panel] = []
        self.hidden: list[Panel] = []        # put away by clear / clap
        self.focused: Optional[int] = None   # "this"
        self.mode = "camera"                 # camera | overlay
        self._next = 1
        self._clock = clock
        self.on_added: Optional[Callable[[Panel], None]] = None   # content loader hook

    # ── panels ───────────────────────────────────────────────────────────────
    def add(self, kind: str, title: str = "", text: str = "", arg: str = "",
            at: Optional[tuple] = None) -> Panel:
        if kind not in KINDS:
            raise ValueError("unknown panel; use one of: " + ", ".join(KINDS))
        with self.lock:
            if kind in SINGLE:
                for p in self.panels:
                    if p.kind == kind:
                        self.focused = p.pid
                        self._raise(p)
                        return p
            w, h = DEFAULT_SIZE[kind]
            p = Panel(kind, title, text, arg, w=w, h=h, pid=self._next, born=self._clock())
            self._next += 1
            if at is not None:
                p.x, p.y = at[0] - w / 2, at[1] - HEADER / 2
                self._keep_on_screen(p)
            else:
                p.x, p.y = self._free_spot(p)
            self.panels.append(p)
            self.focused = p.pid
        if self.on_added is not None:
            try:
                self.on_added(p)
            except Exception:
                pass
        return p

    TOP = 0.085               # below the desk's status line
    BOTTOM = 0.86             # above the dock

    def _free_spot(self, p: Panel) -> tuple:
        """The place that overlaps the panels already there the least, scanning
        a grid from the top left; ties go to the earliest (top-left) spot."""
        others = [q for q in self.panels if q.gone == 0]
        best, best_cost = (0.02, self.TOP), None
        steps_x = max(1, int((1 - p.w - 0.04) / 0.02))
        steps_y = max(1, int((self.BOTTOM - p.h - self.TOP) / 0.02))
        for j in range(steps_y + 1):
            y = self.TOP + j * 0.02
            for i in range(steps_x + 1):
                x = 0.02 + i * 0.02
                cost = 0.0
                for q in others:
                    ox = min(x + p.w, q.x + q.w) - max(x, q.x)
                    oy = min(y + p.h, q.y + q.h) - max(y, q.y)
                    if ox > 0 and oy > 0:
                        cost += ox * oy
                if best_cost is None or cost < best_cost - 1e-9:
                    best, best_cost = (x, y), cost
                    if cost == 0:
                        return best
        return best

    def get(self, pid: Optional[int]) -> Optional[Panel]:
        with self.lock:
            return next((p for p in self.panels if p.pid == pid and p.gone == 0), None)

    def live(self) -> list:
        with self.lock:
            return [p for p in self.panels if p.gone == 0]

    def current(self) -> Optional[Panel]:
        """'this': the panel pointed at or touched last, else the newest."""
        with self.lock:
            p = self.get(self.focused)
            if p is None:
                alive = self.live()
                p = alive[-1] if alive else None
            return p

    def at(self, x: float, y: float) -> Optional[Panel]:
        with self.lock:
            for p in reversed(self.panels):
                if p.gone == 0 and p.contains(x, y):
                    return p
        return None

    def _raise(self, p: Panel) -> None:
        if p in self.panels:
            self.panels.remove(p)
            self.panels.append(p)

    def close(self, pid: int) -> bool:
        with self.lock:
            p = self.get(pid)
            if p is None:
                return False
            p.gone = self._clock()
            p.held = False
            if self.focused == pid:
                alive = [q for q in self.live() if q.pid != pid]
                self.focused = alive[-1].pid if alive else None
            return True

    def resize(self, pid: int, factor: float, center: Optional[tuple] = None) -> None:
        with self.lock:
            p = self.get(pid)
            if p is None:
                return
            cx, cy = center or (p.x + p.w / 2, p.y + p.h / 2)
            base_w, base_h = DEFAULT_SIZE.get(p.kind, (0.3, 0.3))
            nw = min(0.96, max(base_w * 0.55, p.w * factor))
            f = nw / p.w
            p.w, p.h = nw, min(0.92, max(base_h * 0.55, p.h * f))
            p.x, p.y = cx - p.w / 2, cy - p.h / 2
            self._keep_on_screen(p)

    def center(self, pid: int) -> None:
        with self.lock:
            p = self.get(pid)
            if p is not None:
                p.x, p.y = (1 - p.w) / 2, (1 - p.h) / 2 - 0.03
                p.vx = p.vy = 0.0

    def clear(self) -> int:
        """Put every panel away (clap, 'close everything'); restore() brings them back."""
        with self.lock:
            alive = self.live()
            if not alive:
                return 0
            for p in alive:
                p.held, p.vx, p.vy = False, 0.0, 0.0
            self.hidden = alive
            self.panels = [p for p in self.panels if p not in alive]
            self.focused = None
            return len(alive)

    def restore(self) -> int:
        with self.lock:
            n = len(self.hidden)
            if n:
                now = self._clock()
                for p in self.hidden:
                    p.born = now
                self.panels += self.hidden
                self.hidden = []
                self.focused = self.panels[-1].pid
            return n

    def scroll(self, pid: int, rows: float) -> None:
        with self.lock:
            p = self.get(pid)
            if p is not None:
                p.scroll = min(max(0.0, p.scroll + rows), max(0, len(p.rows) - 1))

    # ── physics ──────────────────────────────────────────────────────────────
    def _keep_on_screen(self, p: Panel) -> None:
        p.x = min(max(p.x, -p.w * 0.5), 1 - p.w * 0.5)
        p.y = min(max(p.y, 0.0), 1 - HEADER)

    def snap(self, p: Panel) -> None:
        """A panel let go near an edge (or another panel's edge) lines up with it."""
        edges_x = [0.0, 1.0] + [q.x + q.w for q in self.live() if q is not p] \
            + [q.x for q in self.live() if q is not p]
        edges_y = [0.0, 1.0] + [q.y + q.h for q in self.live() if q is not p] \
            + [q.y for q in self.live() if q is not p]
        for e in edges_x:
            if abs(p.x - e) < self.SNAP:
                p.x = e
                break
            if abs(p.x + p.w - e) < self.SNAP:
                p.x = e - p.w
                break
        for e in edges_y:
            if abs(p.y - e) < self.SNAP:
                p.y = e
                break
            if abs(p.y + p.h - e) < self.SNAP:
                p.y = e - p.h
                break

    def step(self, dt: float) -> list:
        """Advance thrown panels; returns events [("closed", pid), ...]."""
        events = []
        now = self._clock()
        with self.lock:
            for p in list(self.panels):
                if p.gone:
                    if now - p.gone > self.VANISH:
                        self.panels.remove(p)
                    continue
                if p.held or (p.vx == 0 and p.vy == 0):
                    continue
                p.x += p.vx * dt
                p.y += p.vy * dt
                decay = math.exp(-self.FRICTION * dt)
                p.vx *= decay
                p.vy *= decay
                off = (p.x > 1.0 or p.x + p.w < 0.0 or p.y > 1.0 or p.y + p.h < 0.0)
                if off:
                    p.gone = now
                    p.vx = p.vy = 0.0
                    if self.focused == p.pid:
                        self.focused = None
                    events.append(("closed", p.pid))
                elif math.hypot(p.vx, p.vy) < 0.02:
                    p.vx = p.vy = 0.0
                    self._keep_on_screen(p)
                    self.snap(p)
        return events

    # ── persistence and description ──────────────────────────────────────────
    def to_list(self) -> list:
        with self.lock:
            return [p.to_dict() for p in self.live()]

    def load_list(self, items: list) -> int:
        with self.lock:
            self.panels, self.hidden = [], []
            for d in items or []:
                if not isinstance(d, dict) or d.get("kind") not in KINDS:
                    continue
                p = Panel(**{k: d[k] for k in ("kind", "title", "text", "arg", "x", "y", "w", "h")
                             if k in d})
                p.pid, p.born = self._next, self._clock()
                self._next += 1
                self.panels.append(p)
            self.focused = self.panels[-1].pid if self.panels else None
            loaded = list(self.panels)
        for p in loaded:
            if self.on_added is not None:
                try:
                    self.on_added(p)
                except Exception:
                    pass
        return len(loaded)

    def describe(self) -> str:
        alive = self.live()
        if not alive:
            return "no panels"
        return ", ".join(f"{p.heading()} ({p.kind})" for p in alive)


# ─────────────────────────────────────────────────────────────────────────────
#  Pointers: a hand or the mouse
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Pointer:
    """One hand (or the mouse) as the desk sees it this frame."""
    pid: str                    # stable id: "h0", "h1", "mouse"
    x: float                    # screen 0..1
    y: float
    down: bool                  # pinched / button held
    size: float = 0.0           # hand size in the picture (bigger = nearer); 0 for mouse
    angle: float = 0.0          # hand rotation, radians
    open_palm: bool = False
    pointing: bool = False
    pinch: float = 0.0          # 0 = open .. 1 = pinched, for the cursor
    pts: Optional[list] = None  # the hand's 21 points on the screen, for the skeleton


@dataclass
class _Grip:
    """What one pointer is doing since its pinch began."""
    mode: str                   # move | scroll | resize | hold | ring | dock | tap
    t0: float
    x0: float
    y0: float
    panel: Optional[int] = None
    off: tuple = (0.0, 0.0)
    size0: float = 0.0
    w0: float = 0.0
    angle0: float = 0.0
    yaw0: float = 0.0
    scroll0: float = 0.0
    item: str = ""
    moved: float = 0.0
    d0: float = 0.0             # two-hand resize: starting distance
    trail: deque = field(default_factory=lambda: deque(maxlen=8))


class Input:
    """Turns pointers into desk changes. Feed `update(pointers, t)` once per
    frame; it returns events for sounds and effects:
        ("grab", pid) ("drop", pid) ("throw", pid) ("tap", x, y) ("press", kind)
        ("ring", x, y) ("added", pid) ("scroll", pid) ("resize", pid)
        ("clap", n) ("restore", n) ("focus", pid) ("closed", pid)
    """

    TAP_TIME = 0.35
    TAP_MOVE = 0.02
    HOLD_TIME = 0.7           # pinch-and-hold on empty space opens the ring
    RING_R = 0.11
    FLING = 1.1               # screen units / s: faster than this on release is a throw
    ROW_H = 0.032             # one list row, screen units (the window draws to match)

    def __init__(self, desk: Desk):
        self.desk = desk
        self.grips: dict[str, _Grip] = {}
        self.ring: Optional[dict] = None     # {"x", "y", "items", "hover", "owner"}
        self.hover: dict[str, tuple] = {}    # pointer id -> (x, y, what)
        self._palms: deque = deque(maxlen=30)   # (t, distance) while two open palms
        self._cleared_at = 0.0

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def dock_items() -> list:
        """(kind, x, y) of each dock button; '?' is the guide."""
        n = len(DOCK)
        span = 0.075
        x0 = 0.5 - span * (n - 1) / 2
        return [(k, x0 + i * span, DOCK_Y) for i, k in enumerate(DOCK)]

    def dock_at(self, x: float, y: float) -> str:
        for k, dx, dy in self.dock_items():
            if math.hypot((x - dx) * 1.7, y - dy) < DOCK_R * 1.3:
                return k
        return ""

    def ring_items(self, cx: float, cy: float) -> list:
        n = len(RING)
        out = []
        for i, k in enumerate(RING):
            a = -math.pi / 2 + 2 * math.pi * i / n
            out.append((k, cx + math.cos(a) * self.RING_R * 0.6, cy + math.sin(a) * self.RING_R))
        return out

    def _ring_hit(self, x: float, y: float) -> str:
        if not self.ring:
            return ""
        best, bd = "", 1e9
        for k, rx, ry in self.ring["items"]:
            d = math.hypot((x - rx) * 1.7, y - ry)
            if d < bd:
                best, bd = k, d
        return best if bd < 0.05 else ""

    # ── the frame ────────────────────────────────────────────────────────────
    def update(self, pointers: list, t: float) -> list:
        events: list = []
        seen = set()
        for p in pointers:
            seen.add(p.pid)
            g = self.grips.get(p.pid)
            if p.down and g is None:
                self._press(p, t, events)
            elif p.down and g is not None:
                self._drag(p, g, t, events)
            elif not p.down and g is not None:
                self._release(p, g, t, events)
            if not p.down:
                self._hover(p, events)
        for gone in [k for k in self.grips if k not in seen]:
            g = self.grips[gone]
            self._release(None, g, t, events, lost=gone)
        for k in [k for k in self.hover if k not in seen]:
            del self.hover[k]
        self._two_hands(pointers, t, events)
        self._clap(pointers, t, events)
        if self.ring and self.ring.get("owner") not in self.grips:
            self.ring = None
        return events

    def _hover(self, p: Pointer, events: list) -> None:
        what = ""
        if self.dock_at(p.x, p.y):
            what = "dock:" + self.dock_at(p.x, p.y)
        else:
            panel = self.desk.at(p.x, p.y)
            if panel is not None:
                what = f"panel:{panel.pid}"
                if p.pointing and self.desk.focused != panel.pid:
                    self.desk.focused = panel.pid
                    events.append(("focus", panel.pid))
        self.hover[p.pid] = (p.x, p.y, what)

    def _press(self, p: Pointer, t: float, events: list) -> None:
        desk = self.desk
        kind = self.dock_at(p.x, p.y)
        if kind:
            self.grips[p.pid] = _Grip("dock", t, p.x, p.y, item=kind)
            return
        panel = desk.at(p.x, p.y)
        # A second hand on a panel the first already holds: resize with both.
        if panel is not None:
            holder = next((g for k, g in self.grips.items()
                           if g.panel == panel.pid and g.mode in ("move", "scroll", "tap")), None)
            if holder is not None:
                self.grips[p.pid] = _Grip("resize", t, p.x, p.y, panel=panel.pid, w0=panel.w)
                holder.mode = "resize"
                return
        if panel is not None:
            with desk.lock:
                desk._raise(panel)
                desk.focused = panel.pid
                panel.vx = panel.vy = 0.0
            if panel.on_close(p.x, p.y):
                self.grips[p.pid] = _Grip("tap", t, p.x, p.y, panel=panel.pid, item="close")
                return
            if panel.kind in LIST_KINDS and not panel.on_header(p.x, p.y) and panel.rows:
                self.grips[p.pid] = _Grip("scroll", t, p.x, p.y, panel=panel.pid,
                                          scroll0=panel.scroll)
                return
            panel.held = True
            g = _Grip("move", t, p.x, p.y, panel=panel.pid, off=(p.x - panel.x, p.y - panel.y),
                      size0=p.size, w0=panel.w, angle0=p.angle, yaw0=panel.yaw)
            g.trail.append((t, p.x, p.y))
            self.grips[p.pid] = g
            events.append(("grab", panel.pid))
            return
        self.grips[p.pid] = _Grip("hold", t, p.x, p.y)

    def _drag(self, p: Pointer, g: _Grip, t: float, events: list) -> None:
        desk = self.desk
        g.moved = max(g.moved, math.hypot(p.x - g.x0, p.y - g.y0))
        g.trail.append((t, p.x, p.y))
        if g.mode == "hold":
            if g.moved > self.TAP_MOVE * 1.5:
                g.mode = "tap"            # moved off: just a press on empty space
            elif t - g.t0 >= self.HOLD_TIME:
                g.mode = "ring"
                self.ring = {"x": g.x0, "y": g.y0, "items": self.ring_items(g.x0, g.y0),
                             "hover": "", "owner": p.pid}
                events.append(("ring", g.x0, g.y0))
            return
        if g.mode == "ring" and self.ring:
            self.ring["hover"] = self._ring_hit(p.x, p.y)
            return
        if g.mode == "dock":
            # Pulled away from the dock: the new panel comes out with the hand.
            if g.moved > 0.04:
                panel = desk.add(g.item, at=(p.x, p.y))
                panel.held = True
                g.mode, g.panel = "move", panel.pid
                g.off = (p.x - panel.x, p.y - panel.y)
                g.size0, g.w0, g.angle0, g.yaw0 = p.size, panel.w, p.angle, panel.yaw
                events.append(("added", panel.pid))
                events.append(("grab", panel.pid))
            return
        panel = desk.get(g.panel)
        if panel is None:
            return
        if g.mode == "scroll":
            with desk.lock:
                panel.scroll = min(max(0.0, g.scroll0 + (g.y0 - p.y) / self.ROW_H),
                                   max(0, len(panel.rows) - 1))
            return
        if g.mode == "move":
            with desk.lock:
                panel.x, panel.y = p.x - g.off[0], p.y - g.off[1]
                desk._keep_on_screen(panel)
                # Pulled nearer (the hand grows in the picture): the panel grows.
                if p.size and g.size0:
                    ratio = p.size / g.size0
                    if abs(ratio - 1) > 0.12:
                        target = g.w0 * ratio ** 1.5
                        f = target / panel.w
                        if abs(f - 1) > 0.01:
                            desk.resize(panel.pid, f, center=(panel.x + panel.w / 2,
                                                              panel.y + panel.h / 2))
                            g.off = (p.x - panel.x, p.y - panel.y)
                # Turning the hand while holding the model rotates it.
                if panel.kind == "model" and p.size:
                    panel.yaw = g.yaw0 + (p.angle - g.angle0) * 1.6

    def _release(self, p: Optional[Pointer], g: _Grip, t: float, events: list,
                 lost: str = "") -> None:
        desk = self.desk
        key = p.pid if p is not None else lost
        self.grips.pop(key, None)
        quick = t - g.t0 <= self.TAP_TIME and g.moved <= self.TAP_MOVE
        if g.mode == "dock":
            if p is not None:
                panel = desk.add(g.item)
                events.append(("press", g.item))
                events.append(("added", panel.pid))
            return
        if g.mode == "ring":
            item = self.ring.get("hover") if self.ring else ""
            cx, cy = (self.ring["x"], self.ring["y"]) if self.ring else (g.x0, g.y0)
            self.ring = None
            if item:
                panel = desk.add(item, at=(cx, cy))
                events.append(("added", panel.pid))
            return
        if g.mode in ("hold", "tap") and g.panel is None:
            if quick:
                events.append(("tap", g.x0, g.y0))
            return
        panel = desk.get(g.panel)
        if panel is None:
            return
        if g.item == "close":
            if quick and desk.close(panel.pid):
                events.append(("closed", panel.pid))
            return
        if g.mode == "scroll":
            if quick:
                events.append(("tap", g.x0, g.y0))
                events.append(("row", panel.pid, self.row_at(panel, g.y0)))
            return
        if g.mode == "resize":
            with desk.lock:
                panel.held = any(o.panel == panel.pid for o in self.grips.values())
            for o in self.grips.values():
                if o.panel == panel.pid and o.mode == "resize":
                    o.mode = "move"
                    o.off = (o.trail[-1][1] - panel.x, o.trail[-1][2] - panel.y) if o.trail \
                        else o.off
                    o.w0 = panel.w
                    o.d0 = 0.0
            return
        if g.mode == "move":
            vx, vy = self._velocity(g.trail)
            with desk.lock:
                panel.held = False
                if quick:
                    events.append(("tap", g.x0, g.y0))
                    events.append(("row", panel.pid, self.row_at(panel, g.y0)))
                elif math.hypot(vx, vy) > self.FLING:
                    panel.vx, panel.vy = vx, vy
                    events.append(("throw", panel.pid))
                else:
                    desk.snap(panel)
                    events.append(("drop", panel.pid))

    def hold_progress(self, t: float) -> list:
        """(x, y, 0..1) for each pinch held on empty space, on its way to the
        ring — drawn as an arc that fills, so holding still has visible meaning."""
        return [(g.x0, g.y0, min(1.0, (t - g.t0) / self.HOLD_TIME))
                for g in self.grips.values() if g.mode == "hold" and t - g.t0 > 0.12]

    def row_at(self, panel: Panel, y: float) -> int:
        """Which list row a point is on (-1 = none)."""
        if not panel.rows:
            return -1
        i = int((y - panel.y - HEADER - 0.01) / self.ROW_H + panel.scroll)
        return i if 0 <= i < len(panel.rows) else -1

    @staticmethod
    def _velocity(trail) -> tuple:
        pts = [q for q in trail if trail and trail[-1][0] - q[0] <= 0.15]
        if len(pts) < 2:
            return 0.0, 0.0
        dt = pts[-1][0] - pts[0][0]
        if dt <= 1e-3:
            return 0.0, 0.0
        return (pts[-1][1] - pts[0][1]) / dt, (pts[-1][2] - pts[0][2]) / dt

    def _two_hands(self, pointers: list, t: float, events: list) -> None:
        """Both hands on one panel: their distance sets its size."""
        pairs = {}
        for p in pointers:
            g = self.grips.get(p.pid)
            if g is not None and g.mode == "resize" and g.panel is not None:
                pairs.setdefault(g.panel, []).append((p, g))
        for pid, both in pairs.items():
            if len(both) != 2:
                continue
            (a, ga), (b, gb) = both
            d = math.hypot(a.x - b.x, a.y - b.y)
            if not ga.d0 or not gb.d0:
                ga.d0 = gb.d0 = max(d, 0.02)
                panel = self.desk.get(pid)
                ga.w0 = gb.w0 = panel.w if panel else 0.3
                continue
            panel = self.desk.get(pid)
            if panel is None:
                continue
            target = ga.w0 * d / ga.d0
            f = target / panel.w
            if abs(f - 1) > 0.01:
                self.desk.resize(pid, f, center=((a.x + b.x) / 2, (a.y + b.y) / 2))
                events.append(("resize", pid))

    def _clap(self, pointers: list, t: float, events: list) -> None:
        """Two open palms coming together fast: put everything away. Pulled
        apart again afterwards: bring it back."""
        hands = [p for p in pointers if p.size and p.open_palm and not p.down]
        if len(hands) != 2:
            self._palms.clear()
            return
        a, b = hands
        d = math.hypot(a.x - b.x, a.y - b.y)
        self._palms.append((t, d))
        recent = [q for q in self._palms if t - q[0] <= 0.4]
        if len(recent) < 3:
            return
        widest = max(q[1] for q in recent)
        if d < 0.12 and widest - d > 0.18:
            if self.desk.live():
                n = self.desk.clear()
                self._cleared_at = t
                self._palms.clear()
                events.append(("clap", n))
            return
        if self.desk.hidden and t - self._cleared_at > 0.6:
            window = [q for q in self._palms if t - q[0] <= 1.0]
            narrowest = min(q[1] for q in window)
            if narrowest < 0.15 and d - narrowest > 0.3:
                n = self.desk.restore()
                self._palms.clear()
                events.append(("restore", n))


# ─────────────────────────────────────────────────────────────────────────────
#  From tracked hands to pointers
# ─────────────────────────────────────────────────────────────────────────────
class HandFilter:
    """Tracked hands (plugins/_hands_onnx.Hand) -> steady Pointers.

    Keeps up to two hands in stable slots (nearest to where each was), smooths
    the jitter the landmark model has from frame to frame, and decides "pinched"
    with hysteresis so a pinch on the edge does not flicker on and off."""

    # (pinch on, pinch off) as a thumb-index gap in palm sizes. "gentle"
    # counts a looser pinch; "firm" wants the fingers really together.
    SENSITIVITY = {"gentle": (0.40, 0.58), "normal": (0.33, 0.50), "firm": (0.27, 0.42)}
    # Smoothing follows speed (the idea of the 1-euro filter): a still hand is
    # smoothed hard so the cursor does not shiver, a fast one hardly at all so
    # it does not lag behind a throw.
    SMOOTH_SLOW = 0.25        # weight of the new position when still
    SMOOTH_FAST = 0.85        # ... when moving fast
    FAST = 1.5                # screen widths per second counted as "fast"
    LOST = 0.25               # seconds a vanished hand keeps its slot (and its pinch)

    def __init__(self, sensitivity: str = "normal"):
        self.slots: dict[str, dict] = {}
        self.PINCH_ON, self.PINCH_OFF = self.SENSITIVITY.get(sensitivity,
                                                             self.SENSITIVITY["normal"])

    def _alpha(self, s: dict, cx: float, cy: float, t: float) -> float:
        dt = max(1e-3, t - s.get("seen", t - 1 / 30))
        speed = math.hypot(cx - s["x"], cy - s["y"]) / dt
        k = min(1.0, speed / self.FAST)
        return self.SMOOTH_SLOW + (self.SMOOTH_FAST - self.SMOOTH_SLOW) * k

    def update(self, hands: list, to_screen: Callable[[float, float], tuple],
               t: float) -> list:
        out, taken = [], set()
        for hand in hands[:2]:
            cx, cy = to_screen(*hand.pinch_point)
            best, bd = None, 0.25
            for k, s in self.slots.items():
                d = math.hypot(s["x"] - cx, s["y"] - cy)
                if k not in taken and d < bd:
                    best, bd = k, d
            if best is None:
                best = next((k for k in ("h0", "h1") if k not in self.slots and k not in taken),
                            None) or next(k for k in ("h0", "h1") if k not in taken)
                self.slots[best] = {"x": cx, "y": cy, "down": False, "angle": hand.angle,
                                    "size": hand.size}
            taken.add(best)
            s = self.slots[best]
            a = self._alpha(s, cx, cy, t)
            s["x"] += (cx - s["x"]) * a
            s["y"] += (cy - s["y"]) * a
            s["size"] += (hand.size - s["size"]) * a
            s["angle"] += _wrap(hand.angle - s["angle"]) * a
            s["seen"] = t
            r = hand.pinch_ratio
            s["down"] = r < (self.PINCH_OFF if s["down"] else self.PINCH_ON)
            # 0 = fingers wide open, 1 = pinched: drawn by the cursor, so the
            # user sees how close a pinch is before it counts.
            closeness = min(1.0, max(0.0, (1.0 - r) / (1.0 - self.PINCH_ON)))
            pts = getattr(hand, "pts", None)
            out.append(Pointer(best, s["x"], s["y"], s["down"], size=max(s["size"], 1e-3),
                               angle=s["angle"], open_palm=hand.open_palm and not s["down"],
                               pointing=hand.pointing and not s["down"],
                               pinch=1.0 if s["down"] else closeness,
                               pts=[to_screen(*q) for q in pts] if pts else None))
        for k in list(self.slots):
            s = self.slots[k]
            if k in taken:
                continue
            if t - s.get("seen", t) > self.LOST:
                del self.slots[k]
            elif s["down"]:
                # Tracking drops a frame now and then; letting go there would
                # throw whatever the hand is carrying.
                out.append(Pointer(k, s["x"], s["y"], True, size=s["size"], angle=s["angle"]))
        return out


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


# ─────────────────────────────────────────────────────────────────────────────
#  One desk for the whole app
# ─────────────────────────────────────────────────────────────────────────────
# The voice tool and the window share it. STATE is written by the window (Qt
# thread) and read by the tool (worker thread): plain values only.
_shared: Optional[Desk] = None
_shared_lock = threading.Lock()
STATE: dict = {"open": False, "mode": "camera", "camera": "", "since": 0.0}


def shared() -> Desk:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Desk()
        return _shared


# ─────────────────────────────────────────────────────────────────────────────
#  The last layout, kept between sessions
# ─────────────────────────────────────────────────────────────────────────────
STORE = "holo_desk"          # memory/plugin_data/holo_desk.json: {"desks": {...}, "last": [...]}


def save_last(desk: Desk) -> str:
    """Keep the panels as they are now, to come back next time. '' or why not."""
    try:
        from plugins import _announce
        data = _announce.load(STORE, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data["last"] = desk.to_list() + [dict(p.to_dict(), hidden=True) for p in desk.hidden]
        _announce.save(STORE, data)
        return ""
    except Exception as e:
        print(f"[HoloDesk] layout not saved: {e}")
        return str(e)


def load_last(desk: Desk) -> int:
    """Put the last session's panels back (also the ones put away). Panel count."""
    try:
        from plugins import _announce
        data = _announce.load(STORE, {}) or {}
        items = data.get("last") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return 0
        shown = [d for d in items if isinstance(d, dict) and not d.get("hidden")]
        hidden = [d for d in items if isinstance(d, dict) and d.get("hidden")]
        n = desk.load_list(shown)
        if hidden:
            extra = Desk(clock=desk._clock)
            extra._next = desk._next + 100
            extra.load_list(hidden)
            with desk.lock:
                desk.hidden = extra.panels
                desk._next = extra._next
        return n + len(hidden)
    except Exception as e:
        print(f"[HoloDesk] the last layout could not be loaded: {e}")
        return 0
