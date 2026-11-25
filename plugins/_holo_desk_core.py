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
  pinch anywhere on a card . pick it up; open the fingers to let go (on a
                             list, a sideways pull or a still hold picks it up)
  let go while moving ...... it is thrown; thrown off the screen, it closes
  pinch inside a list ...... drag up and down to scroll it
  two fingers (V) on a list  move the hand up and down to scroll it
  open hand, then a fist ... "crush": hold an open hand still over a card,
                             then close it into a fist; the card closes
  Undo chip ................ every close shows "Undo close" above the dock for
                             8 s; pinch it (or say "bring it back")
  both hands on one panel .. pull apart / together to resize it
  pull a held panel nearer . it grows (the hand gets bigger in the picture)
  clap two open hands ...... every card is hidden; spread them again to
                             show it all
  pinch and hold on space .. a ring of new panels opens round the hand
  quick pinch .............. presses buttons: the dock, a headline, a note, the
                             x; near a button the cursor snaps onto it
  point at a panel ......... it becomes "this" for "close this", "read this"
                             (hovering over one for 0.4 s does the same)
  turn a held hand ......... rotates the 3D model

WHY IT IS BUILT LIKE THIS
  Hands are noisy and a hand cannot feel a button. So targets are bigger than
  they are drawn (HEADER_HIT, CLOSE_HIT, DOCK_HIT, RING_HIT), the cursor is
  pulled onto a button it is near (MAGNET) and the lit button stays lit until
  the hand is clearly elsewhere (no flicker between two neighbours), and the
  button that is lit is the one a pinch presses. Closing must never happen by
  accident, so it needs two deliberate poses in a row (crush) or a quick tap
  on the x, and every close can be taken back for TRASH_S seconds. A throw is
  measured on the hand's motion BEFORE the last 50 ms: opening the fingers to
  let go moves the fingertips, and that used to fling a card the user only
  meant to drop.

  The tracking itself (smoothing, pinch decision, pose voting) lives in
  plugins/_gesture_core.py, shared with Air Control; HandFilter below only
  turns its HandStates into Pointers.
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

HEADER = 0.045            # title bar height, screen units (as drawn)
DOCK_Y = 0.915            # the dock's centre line
DOCK_R = 0.032            # a dock button's radius (in screen-height units)

# Hit areas: bigger than what is drawn, because a hand cursor is never as
# precise as a mouse. Drawing keeps HEADER and DOCK_R.
HEADER_HIT = 0.065        # a pinch this far below a card's top counts as its title bar
CLOSE_HIT = 0.055         # side of the square round the x glyph that closes a card
DOCK_HIT = 1.6            # a dock button answers within DOCK_R * DOCK_HIT
RING_HIT = 0.07           # a ring item answers within this (nearest wins)
MAGNET = 0.035            # a hand cursor this near a button snaps toward it
HOVER_FOCUS = 0.4         # seconds a hand rests over a card before it is "this"
TRASH_S = 8.0             # seconds the Undo chip stays after a close

GUIDE = [
    "Carry a card: pinch anywhere on it, then open your fingers to drop it",
    "Release while still moving to fling it; flung off the screen, it is dismissed",
    "Close a card: hold an open hand still over it, then make a fist",
    "Closed one by mistake? Pinch 'Undo close' above the dock, or say 'bring it back'",
    "In a list, pinch and drag up or down to scroll, or hold up two fingers and move",
    "Resize: pinch the same card with both hands and spread or close them",
    "Bring a held card closer to the camera to enlarge it",
    "Hide everything: bring two open hands together quickly; spread them again to show it all",
    "New card: keep a pinch still on empty space until a circle of choices appears",
    "Tap with a short pinch to press buttons, headlines and notes; near a button the cursor snaps to it",
    "Aim your index finger at a card (or rest your hand on it), then say 'read this', 'close this' or 'make this bigger'",
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
        """Hit test only (HEADER_HIT is taller than the drawn bar)."""
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + HEADER_HIT

    def close_center(self) -> tuple:
        """Where the x glyph is drawn."""
        return self.x + self.w - HEADER / 2, self.y + HEADER / 2

    def on_close(self, px: float, py: float) -> bool:
        """A square of CLOSE_HIT round the x glyph (it may reach a little past
        the card's edge: a hand aims at the glyph, not at the card)."""
        cx, cy = self.close_center()
        return abs(px - cx) <= CLOSE_HIT / 2 and abs(py - cy) <= CLOSE_HIT / 2

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
    TRASH_S = TRASH_S         # the Undo chip's life after a close
    TRASH_KEEP = 5            # closed cards kept for reopen(), this session only

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.lock = threading.RLock()
        self.panels: list[Panel] = []
        self.hidden: list[Panel] = []        # put away by clear / clap
        self.trash: list[tuple] = []         # [(Panel, closed at)], oldest first
        self._closes = 0                     # counts every close, for Input's Undo chip
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

    def close(self, pid: int, trash: bool = True) -> bool:
        """Fade a card out. With `trash`, it can come back with reopen()."""
        with self.lock:
            p = self.get(pid)
            if p is None:
                return False
            p.gone = self._clock()
            p.held = False
            if trash:
                self._to_trash(p, p.gone)
            if self.focused == pid:
                alive = [q for q in self.live() if q.pid != pid]
                self.focused = alive[-1].pid if alive else None
            return True

    def _to_trash(self, p: Panel, now: float) -> None:
        self.trash = [e for e in self.trash if e[0] is not p] + [(p, now)]
        del self.trash[:-self.TRASH_KEEP]
        self._closes += 1

    def reopen(self, pid: Optional[int] = None) -> Optional[Panel]:
        """Bring back the newest closed card (or the closed card `pid`).
        Returns it, or None when there is nothing to bring back."""
        with self.lock:
            entry = None
            for e in reversed(self.trash):
                if pid is None or e[0].pid == pid:
                    entry = e
                    break
            if entry is None:
                return None
            p = entry[0]
            self.trash.remove(entry)
            p.gone, p.born, p.held = 0.0, self._clock(), False
            p.vx = p.vy = 0.0
            self._keep_on_screen(p)
            # A card thrown off the screen comes back whole, not half outside.
            p.x = min(max(p.x, 0.0), max(0.0, 1.0 - p.w))
            p.y = min(max(p.y, 0.0), max(0.0, 1.0 - p.h))
            if p in self.panels:
                self._raise(p)
            else:
                self.panels.append(p)
            self.focused = p.pid
            return p

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
                    self._to_trash(p, now)
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
    pose: str = ""              # voted pose (plugins/_gesture_core.POSES); "" for the mouse
    state: str = ""             # the index pinch: open | armed | pressed; "" for the mouse
    vx: float = 0.0             # filtered velocity, screen units / s
    vy: float = 0.0
    conf: float = 1.0           # the tracker's confidence in this hand
    lost: bool = False          # held through a tracking dropout (no hand this frame)


@dataclass
class _Grip:
    """What one pointer is doing since its pinch began."""
    mode: str                   # move | scroll | decide | resize | hold | ring | dock | tap | chip
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
    # ~0.4 s at the window's 60 Hz tick: enough for the throw window below.
    trail: deque = field(default_factory=lambda: deque(maxlen=24))


class Input:
    """Turns pointers into desk changes. Feed `update(pointers, t)` once per
    frame; it returns events for sounds and effects:
        ("grab", pid) ("drop", pid) ("throw", pid) ("tap", x, y) ("press", kind)
        ("ring", x, y) ("added", pid) ("scroll", pid) ("resize", pid)
        ("clap", n) ("restore", n) ("focus", pid) ("closed", pid) ("row", pid, i)
        ("armed", pid) ("crushed", pid) ("reopened", pid)
    (pid = a panel's id.) For drawing: hover, ring, cursor_at(), crush_marks(),
    undo_chip(), hold_progress(), v_scroll.
    """

    TAP_TIME = 0.35
    TAP_MOVE = 0.02
    HOLD_TIME = 0.7           # pinch-and-hold on empty space opens the ring
    RING_R = 0.11
    FLING = 1.1               # screen units / s: faster than this on release is a throw
    ROW_H = 0.032             # one list row, screen units (the window draws to match)
    # A throw is measured on [release - THROW_SPAN, release - THROW_SKIP]: the
    # last 50 ms are the fingers opening, which moves the cursor but is not a throw.
    THROW_SPAN = 0.20
    THROW_SKIP = 0.05
    # A pinch on a list's body is a scroll or a carry; the first motion decides.
    DECIDE_MOVE = 0.015
    DECIDE_HOLD = 0.35        # ... or holding still this long picks the card up
    V_DWELL = 0.15            # two fingers this long over a list before it scrolls
    CRUSH_MARGIN = 0.03       # how far outside the card a crushing hand may drift
    MAGNET_PULL = 0.6         # the drawn cursor moves this share of the way to a lit button
    MAGNET_KEEP = 1.4         # a lit button stays lit until MAGNET * this
    CHIP = (0.5 - 0.08, DOCK_Y - 0.10, 0.16, 0.05)    # the Undo chip: x, y, w, h

    def __init__(self, desk: Desk):
        self.desk = desk
        self.grips: dict[str, _Grip] = {}
        self.ring: Optional[dict] = None     # {"x", "y", "items", "hover", "owner"}
        self.hover: dict[str, tuple] = {}    # pointer id -> (x, y, what)
        self.v_scroll: dict[str, int] = {}   # pointer id -> panel scrolled with two fingers
        self._palms: deque = deque(maxlen=30)   # (t, distance) while two open palms
        self._cleared_at = 0.0
        self._pos: dict[str, tuple] = {}     # pointer id -> (x, y) this frame
        self._lit: dict[str, tuple] = {}     # pointer id -> (what, cx, cy): the magnet's target
        self._dwell: dict[str, list] = {}    # pointer id -> [panel, since, done]: hover focus
        self._v: dict[str, dict] = {}        # pointer id -> {"y0", "scroll0"} while V-scrolling
        self._v_wait: dict[str, tuple] = {}  # pointer id -> (panel, since): V before it counts
        self._crush: dict[str, dict] = {}    # pointer id -> crush detector and its card
        self._crush_missing = False
        self._chip_at = 0.0                  # when the last close happened (0 = no chip)
        self._closes_seen = getattr(desk, "_closes", 0)

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def dock_items() -> list:
        """(kind, x, y) of each dock button; '?' is the guide."""
        n = len(DOCK)
        span = 0.075
        x0 = 0.5 - span * (n - 1) / 2
        return [(k, x0 + i * span, DOCK_Y) for i, k in enumerate(DOCK)]

    def dock_at(self, x: float, y: float) -> str:
        best, bd = "", DOCK_R * DOCK_HIT
        for k, dx, dy in self.dock_items():
            d = math.hypot((x - dx) * 1.7, y - dy)
            if d < bd:
                best, bd = k, d
        return best

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
        return best if bd < RING_HIT else ""

    def _close_at(self, x: float, y: float) -> Optional[Panel]:
        """The card whose x the point is on (its hit square may reach just past
        the card's edge), unless another card covers that spot."""
        with self.desk.lock:
            for q in reversed(self.desk.panels):
                if q.gone:
                    continue
                if q.on_close(x, y):
                    return q
                if q.contains(x, y):
                    return None
        return None

    def undo_chip(self, t: float) -> Optional[dict]:
        """The 'Undo close' chip above the dock, for TRASH_S after a close."""
        if not self._chip_at or not self.desk.trash:
            return None
        left = min(TRASH_S, TRASH_S - (t - self._chip_at))
        if left <= 0:
            return None
        x, y, w, h = self.CHIP
        return {"x": x, "y": y, "w": w, "h": h, "label": "Undo close", "left": left}

    def _on_chip(self, x: float, y: float, t: float) -> bool:
        chip = self.undo_chip(t)
        if chip is None:
            return False
        m = 0.01
        return (chip["x"] - m <= x <= chip["x"] + chip["w"] + m
                and chip["y"] - m <= y <= chip["y"] + chip["h"] + m)

    def _targets(self, t: float) -> list:
        """Everything a hand cursor snaps to: (what, cx, cy, x-scale)."""
        out = [("dock:" + k, dx, dy, 1.7) for k, dx, dy in self.dock_items()]
        for panel in self.desk.live():
            cx, cy = panel.close_center()
            if self.desk.at(cx, cy) is panel:          # not under another card
                out.append((f"close:{panel.pid}", cx, cy, 1.0))
        if self.ring:
            out += [("ring:" + k, rx, ry, 1.7) for k, rx, ry in self.ring["items"]]
        chip = self.undo_chip(t)
        if chip is not None:
            out.append(("chip", chip["x"] + chip["w"] / 2, chip["y"] + chip["h"] / 2, 1.0))
        return out

    def cursor_at(self, pointer_id: str) -> Optional[tuple]:
        """Where to DRAW a pointer's cursor: pulled toward the button it lit."""
        pos = self._pos.get(pointer_id)
        if pos is None:
            return None
        lit = self._lit.get(pointer_id)
        if not lit:
            return pos
        k = self.MAGNET_PULL
        return pos[0] + k * (lit[1] - pos[0]), pos[1] + k * (lit[2] - pos[1])

    def crush_marks(self, t: float) -> list:
        """Crushes under way, for drawing: the armed card's red edge, the
        closing arc."""
        out = []
        for pid, st in self._crush.items():
            if st["phase"] == "armed":
                window = getattr(st["det"], "WINDOW", 0.8) or 0.8
                prog = min(1.0, max(0.0, (t - st["t_armed"]) / window))
            elif st["phase"] == "closing":
                prog = min(1.0, max(0.0, st["progress"]))
            else:
                continue
            out.append({"pointer": pid, "panel": st["panel"], "phase": st["phase"],
                        "progress": prog})
        return out

    # ── the frame ────────────────────────────────────────────────────────────
    def update(self, pointers: list, t: float) -> list:
        events: list = []
        seen = set()
        for p in pointers:
            seen.add(p.pid)
            self._pos[p.pid] = (p.x, p.y)
            g = self.grips.get(p.pid)
            if p.down and g is None:
                self._press(p, t, events)
                self._lit.pop(p.pid, None)
            elif p.down and g is not None:
                self._drag(p, g, t, events)
            elif not p.down and g is not None:
                self._release(p, g, t, events)
            if not p.down:
                self._magnet(p, t)
                self._hover(p, t, events)
            if not getattr(p, "lost", False):
                self._two_fingers(p, t, events)
                self._crush_step(p, t, events)
        for gone in [k for k in self.grips if k not in seen]:
            g = self.grips[gone]
            self._release(None, g, t, events, lost=gone)
        for table in (self.hover, self._pos, self._lit, self._dwell, self.v_scroll, self._v,
                      self._v_wait, self._crush):
            for k in [k for k in table if k not in seen]:
                del table[k]
        self._two_hands(pointers, t, events)
        self._clap(pointers, t, events)
        if self.ring and self.ring.get("owner") not in self.grips:
            self.ring = None
        # Every close (x, crush, a throw off the screen, a voice command) shows
        # the Undo chip. Desk.step's closes come after this call, so they are
        # picked up on the next frame.
        closes = getattr(self.desk, "_closes", 0)
        if closes != self._closes_seen:
            self._closes_seen = closes
            self._chip_at = t
        return events

    def _magnet(self, p: Pointer, t: float) -> None:
        """Light the button a hand cursor is near; keep it lit until the hand
        is clearly away (MAGNET_KEEP), so two neighbours never flicker."""
        if not p.size:                       # the mouse is precise enough as it is
            self._lit.pop(p.pid, None)
            return
        targets = self._targets(t)

        def dist(tg):
            return math.hypot((p.x - tg[1]) * tg[3], p.y - tg[2])

        cur = self._lit.get(p.pid)
        if cur:
            same = next((tg for tg in targets if tg[0] == cur[0]), None)
            if same is not None and dist(same) <= MAGNET * self.MAGNET_KEEP:
                self._lit[p.pid] = same[:3]
                return
        best = min(targets, key=dist, default=None)
        if best is not None and dist(best) <= MAGNET:
            self._lit[p.pid] = best[:3]
        else:
            self._lit.pop(p.pid, None)

    def _focus(self, panel: Panel, events: list) -> None:
        if self.desk.focused != panel.pid:
            self.desk.focused = panel.pid
            events.append(("focus", panel.pid))

    def _hover(self, p: Pointer, t: float, events: list) -> None:
        lit = self._lit.get(p.pid)
        panel = self.desk.at(p.x, p.y)
        dock = "" if lit else self.dock_at(p.x, p.y)
        if lit:
            what = lit[0]
        elif self._on_chip(p.x, p.y, t):
            what = "chip"
        elif dock:
            what = "dock:" + dock
        elif panel is not None:
            what = f"panel:{panel.pid}"
        else:
            what = ""
        if panel is not None and not dock:
            if p.pointing:
                self._focus(panel, events)
            elif p.size:
                # Resting a hand over a card makes it "this" too.
                d = self._dwell.get(p.pid)
                if d is None or d[0] != panel.pid:
                    self._dwell[p.pid] = [panel.pid, t, False]
                elif not d[2] and t - d[1] >= HOVER_FOCUS:
                    d[2] = True
                    self._focus(panel, events)
        else:
            self._dwell.pop(p.pid, None)
        self.hover[p.pid] = (p.x, p.y, what)

    def _press(self, p: Pointer, t: float, events: list) -> None:
        desk = self.desk
        lit = self._lit.get(p.pid)
        what = lit[0] if lit else ""
        # The target lit on the last open frame, unless the pinch landed
        # clearly elsewhere (the hand jumped, or it is the ring's: only the
        # ring's owner picks from it).
        if what.startswith("ring:") or (lit and math.hypot(
                (p.x - lit[1]) * (1.7 if what.startswith("dock:") else 1.0),
                p.y - lit[2]) > MAGNET * self.MAGNET_KEEP):
            what = ""
        if what == "chip" or (not what and self._on_chip(p.x, p.y, t)):
            self.grips[p.pid] = _Grip("chip", t, p.x, p.y)
            return
        if what.startswith("dock:"):
            kind = what[5:]
        else:
            kind = "" if what else self.dock_at(p.x, p.y)
        if kind:
            self.grips[p.pid] = _Grip("dock", t, p.x, p.y, item=kind)
            return
        panel = desk.at(p.x, p.y)
        # A second hand on a panel the first already holds: resize with both.
        if panel is not None:
            holder = next((g for k, g in self.grips.items()
                           if g.panel == panel.pid
                           and g.mode in ("move", "scroll", "decide", "tap")), None)
            if holder is not None:
                self.grips[p.pid] = _Grip("resize", t, p.x, p.y, panel=panel.pid, w0=panel.w)
                holder.mode = "resize"
                # A holder that was a tap on the x is a resize grip now, not a
                # pending close: it must neither close the card on release nor
                # turn into a carry when it moves.
                holder.item = ""
                return
        shut = None
        if what.startswith("close:"):
            try:
                shut = desk.get(int(what[6:]))
            except ValueError:
                shut = None
        if shut is None:
            shut = self._close_at(p.x, p.y)
        if shut is not None:
            with desk.lock:
                desk._raise(shut)
                desk.focused = shut.pid
                shut.vx = shut.vy = 0.0
            self.grips[p.pid] = _Grip("tap", t, p.x, p.y, panel=shut.pid, item="close",
                                      size0=p.size, angle0=p.angle)
            return
        if panel is not None:
            with desk.lock:
                desk._raise(panel)
                desk.focused = panel.pid
                panel.vx = panel.vy = 0.0
            if panel.kind in LIST_KINDS and not panel.on_header(p.x, p.y) and panel.rows:
                # Scroll or carry? The first motion (or holding still) decides.
                g = _Grip("decide", t, p.x, p.y, panel=panel.pid, scroll0=panel.scroll)
                g.trail.append((t, p.x, p.y))
                self.grips[p.pid] = g
                return
            g = _Grip("move", t, p.x, p.y, panel=panel.pid)
            g.trail.append((t, p.x, p.y))
            self.grips[p.pid] = g
            self._start_move(p, g, panel, events)
            return
        self.grips[p.pid] = _Grip("hold", t, p.x, p.y)

    def _start_move(self, p: Pointer, g: _Grip, panel: Panel, events: list) -> None:
        """The pointer now carries the panel, held where the pinch began."""
        with self.desk.lock:
            panel.held = True
        g.mode, g.item = "move", ""
        g.off = (g.x0 - panel.x, g.y0 - panel.y)
        g.size0, g.w0, g.angle0, g.yaw0 = p.size, panel.w, p.angle, panel.yaw
        events.append(("grab", panel.pid))

    def _drag(self, p: Pointer, g: _Grip, t: float, events: list) -> None:
        desk = self.desk
        g.moved = max(g.moved, math.hypot(p.x - g.x0, p.y - g.y0))
        g.trail.append((t, p.x, p.y))
        if g.mode == "chip":
            return
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
        if g.item == "close":
            # A pinch on the x that then moves off carries the card instead.
            if g.moved > self.TAP_MOVE * 1.5:
                self._start_move(p, g, panel, events)
            else:
                return
        if g.mode == "decide":
            dx, dy = p.x - g.x0, p.y - g.y0
            # The decision is total: once the hand has moved, mostly vertical
            # scrolls and anything else carries (a diagonal pull used to stay
            # undecided for the whole gesture and do nothing).
            if math.hypot(dx, dy) >= self.DECIDE_MOVE:
                if abs(dy) >= abs(dx):
                    g.mode = "scroll"
                else:
                    self._start_move(p, g, panel, events)
            elif t - g.t0 >= self.DECIDE_HOLD:
                self._start_move(p, g, panel, events)
            if g.mode == "decide":
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
        if g.mode == "chip":
            if p is not None and quick:
                panel = desk.reopen()
                self._chip_at = 0.0
                if panel is not None:
                    events.append(("reopened", panel.pid))
            return
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
        if g.mode in ("scroll", "decide"):
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

    @classmethod
    def _velocity(cls, trail) -> tuple:
        """Release speed, leaving out the last THROW_SKIP s (the fingers
        opening). A grip too short for that window falls back to the last
        0.15 s."""
        if not trail:
            return 0.0, 0.0
        end = trail[-1][0]
        eps = 1e-6
        pts = [q for q in trail
               if end - cls.THROW_SPAN - eps <= q[0] <= end - cls.THROW_SKIP + eps]
        if len(pts) < 2:
            pts = [q for q in trail if end - q[0] <= 0.15]
        if len(pts) < 2:
            return 0.0, 0.0
        dt = pts[-1][0] - pts[0][0]
        if dt <= 1e-3:
            return 0.0, 0.0
        return (pts[-1][1] - pts[0][1]) / dt, (pts[-1][2] - pts[0][2]) / dt

    # ── poses beyond the pinch ───────────────────────────────────────────────
    def _two_fingers(self, p: Pointer, t: float, events: list) -> None:
        """Two fingers (V) held over a list's body scroll it with the hand."""
        if not p.size or p.down or getattr(p, "pose", "") != "v":
            self._v_wait.pop(p.pid, None)
            self.v_scroll.pop(p.pid, None)
            self._v.pop(p.pid, None)
            return
        desk = self.desk
        if p.pid in self.v_scroll:
            panel = desk.get(self.v_scroll[p.pid])
            st = self._v.get(p.pid)
            if panel is None or st is None:
                self.v_scroll.pop(p.pid, None)
                self._v.pop(p.pid, None)
                return
            with desk.lock:
                panel.scroll = min(max(0.0, st["scroll0"] + (st["y0"] - p.y) / self.ROW_H),
                                   max(0, len(panel.rows) - 1))
            return
        panel = desk.at(p.x, p.y)
        if (panel is None or panel.kind not in LIST_KINDS or not panel.rows
                or panel.on_header(p.x, p.y)):
            self._v_wait.pop(p.pid, None)
            return
        wait = self._v_wait.get(p.pid)
        if wait is None or wait[0] != panel.pid:
            self._v_wait[p.pid] = (panel.pid, t)
            return
        if t - wait[1] < self.V_DWELL:
            return
        self.v_scroll[p.pid] = panel.pid
        self._v[p.pid] = {"y0": p.y, "scroll0": panel.scroll}
        events.append(("scroll", panel.pid))

    def _new_crush(self):
        try:
            from plugins import _gesture_core
            return _gesture_core.CrushDetector(confirm=0.25)
        except Exception as e:
            if not self._crush_missing:
                self._crush_missing = True
                print(f"[HoloDesk] the close gesture is off: {e}")
            return None

    def _crush_reset(self, st: dict) -> None:
        try:
            st["det"].reset()
        except Exception:
            pass
        st.update(phase="idle", panel=None, progress=0.0)

    def _crush_step(self, p: Pointer, t: float, events: list) -> None:
        """Open hand held still over a card, then a fist: the card closes."""
        st = self._crush.get(p.pid)
        if not p.size or p.down or not getattr(p, "pose", ""):
            if st is not None and st["phase"] != "idle":
                self._crush_reset(st)
            return
        if st is None:
            det = self._new_crush()
            if det is None:
                return
            st = self._crush[p.pid] = {"det": det, "panel": None, "phase": "idle",
                                       "progress": 0.0, "t_armed": 0.0}
        try:
            phase, prog = st["det"].update(p.pose, p.x, p.y,
                                           math.hypot(getattr(p, "vx", 0.0),
                                                      getattr(p, "vy", 0.0)), t)
        except Exception as e:
            print(f"[HoloDesk] close gesture: {e}")
            self._crush_reset(st)
            return
        desk = self.desk
        if phase in ("armed", "closing"):
            if st["panel"] is None:
                target = desk.at(p.x, p.y)
                if target is None:
                    self._crush_reset(st)
                    return
                st.update(panel=target.pid, t_armed=t)
                events.append(("armed", target.pid))
            panel = desk.get(st["panel"])
            m = self.CRUSH_MARGIN
            if panel is None or not (panel.x - m <= p.x <= panel.x + panel.w + m
                                     and panel.y - m <= p.y <= panel.y + panel.h + m):
                self._crush_reset(st)
                return
            st.update(phase=phase, progress=prog)
            return
        if phase == "fired":
            pid = st["panel"]
            st.update(phase="idle", panel=None, progress=0.0)
            if pid is not None and desk.close(pid):
                events.append(("crushed", pid))
                events.append(("closed", pid))
            return
        st.update(phase="idle", panel=None, progress=0.0)      # idle or cancelled

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

    A thin wrapper: the work (stable slots, One Euro smoothing once per camera
    frame, the knuckle-anchored cursor, the debounced pinch, pose voting)
    is plugins/_gesture_core.HandFilter with the "desk" profile, shared with
    Air Control so both modes feel the same. This class keeps the old
    signature the window calls and turns each HandState into a Pointer."""

    def __init__(self, sensitivity: str = "normal"):
        self.states: list = []             # the HandStates behind the last result
        self._inner = None
        self._failed = False
        self.PINCH_ON, self.PINCH_OFF = 0.30, 0.45
        try:
            from plugins import _gesture_core as gc
            sens = str(sensitivity or "normal").strip().lower()
            if sens not in getattr(gc, "SENSITIVITY", {}):
                sens = "normal"
            self._inner = gc.HandFilter(sens, profile="desk", max_hands=2)
            self.PINCH_ON, self.PINCH_OFF = self._inner.on, self._inner.off
        except Exception as e:
            print(f"[HoloDesk] hand tracking filter unavailable: {e}")

    def update(self, hands: list, to_screen: Callable[[float, float], tuple],
               t: float) -> list:
        """Call once per camera frame with its capture time `t` (a repeat with
        the same t returns the same pointers)."""
        if self._inner is None:
            return []
        try:
            states = list(self._inner.update(hands, t, to_screen) or [])
        except Exception as e:
            if not self._failed:
                self._failed = True
                print(f"[HoloDesk] hand filter failed: {e}")
            return []
        self.states = states
        return [self._pointer(s) for s in states]

    @staticmethod
    def _pointer(s) -> Pointer:
        g = lambda k, d: getattr(s, k, d)          # noqa: E731 - duck-typed HandState
        down = bool(g("down", False))
        pose = str(g("pose", "") or "")
        return Pointer(str(g("slot", "h0")), float(g("x", 0.0)), float(g("y", 0.0)), down,
                       size=max(float(g("size", 0.0) or 0.0), 1e-3),
                       angle=float(g("angle", 0.0) or 0.0),
                       open_palm=pose == "open" and not down,
                       pointing=pose == "point" and not down,
                       pinch=float(g("closeness", 0.0) or 0.0),
                       pts=g("pts", None), pose=pose, state=str(g("pinch", "") or ""),
                       vx=float(g("vx", 0.0) or 0.0), vy=float(g("vy", 0.0) or 0.0),
                       conf=float(g("conf", 1.0) if g("conf", 1.0) is not None else 1.0),
                       lost=bool(g("lost", False)))


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
