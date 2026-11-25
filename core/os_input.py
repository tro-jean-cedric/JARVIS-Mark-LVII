"""
core/os_input.py — the real mouse and keyboard, driven continuously.

WHY THIS IS NOT core/screenact.py
    screenact is built for an agent: one deliberate click on a named element,
    with travel time, hover settling, self-checks and a written report after
    each action. Every one of those is right for "click Send" and wrong for a
    hand in the air, which produces thirty cursor positions a second and needs
    each of them on screen within a frame. A 160 ms glide per move would make
    the cursor trail the hand by seconds.

    So this module is the low layer Air Control needs: raw moves, presses,
    pixel-smooth scrolling, keys with explicit modifiers, plus the few
    desktop-level shortcuts a gesture maps to (Spaces, Mission Control, close
    window). Text still goes through `screenact.type_text`, because that is
    where the layout-independent Unicode typing and the password-field refusal
    already live, and a second copy would drift.

ONE BACKEND PER PLATFORM, CHOSEN LAZILY
    macOS     Quartz CGEvents. Moves are posted as events, NEVER warped with
              CGWarpMouseCursorPosition: a warp moves the picture of the cursor
              but sends no mouseMoved, so hover states, tooltips and drags
              behind it do not see it. While a button is held a move is posted
              as a Dragged event, which is what makes a drag a drag.
    Windows   user32 through ctypes: SetCursorPos, SendInput, keybd_event.
    X11       pyautogui (Xlib/XTest underneath). Wayland refuses synthetic
              input from programs, and available() says so with screenact's
              sentence rather than pretending.

    Nothing native is imported until the first event. A session that never
    opens Air Control never pays for Quartz, ctypes or pyautogui here.

THE CLICK STATE ON macOS
    A double click on a Mac is not two clicks close together: it is a second
    mouseDown whose kCGMouseEventClickState field says 2. Apps read the field,
    not the timing. So a small tracker (`_ClickTracker`) numbers each press the
    way the system would: the same button again within 0.5 s and 4 pt of the
    last release counts up. `click(count=2)` straight after a single click
    sends only the second press, numbered 2 — the first is already on screen,
    and sending 1 and 2 again would make it a triple click.

pyautogui IS SHARED
    It is one module object with process-wide PAUSE and FAILSAFE. This module
    wants PAUSE 0 (thirty moves a second cannot each sleep 50 ms) and FAILSAFE
    off (a cursor driven by a hand legitimately reaches the corner of the
    screen). `_pg()` sets both right before each call and puts the previous
    values back afterwards, so computer_control and screenact keep theirs.

NOTHING RAISES
    Every public function catches and returns None, False, a Status
    (ok, why) or an outcome line. A gesture that cannot be delivered must never
    take down the GUI thread or the camera loop that asked for it.

THE PUMP
    `InputPump` owns the thread that talks to the OS. The GUI enqueues and
    returns: the latest cursor position wins (a backlog of stale moves is
    worse than a dropped one), everything else runs in order after it, and
    slow work (subprocesses, accessibility queries, network) has its own lane
    so a `wmctrl` call cannot hold up the cursor.
"""
from __future__ import annotations

import contextlib
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.lazyimport import LazyModule, installed

Status = tuple[bool, str]

# Read through this name everywhere so tests can pretend to be another OS.
_PLAT = sys.platform

_HAS_PYAUTOGUI = installed("pyautogui")
pyautogui = LazyModule("pyautogui")
_pg_lock = threading.RLock()

# One lock around every event this module posts: the pump thread, the
# dictation worker and the slow lane may all reach the backend at once, and
# the held-button / click-count state must change in one piece.
_io_lock = threading.RLock()

DOUBLE_S = 0.5          # the system's double-click window, near enough
DOUBLE_PT = 4.0         # ...and how far the pointer may drift within it
MAX_KEY_REPEAT = 500


def _log(msg: str) -> None:
    print(f"[os_input] {msg}")


@contextlib.contextmanager
def _pg():
    """pyautogui with PAUSE 0 and FAILSAFE off for this call only."""
    with _pg_lock:
        m = pyautogui._resolve()
        old = (getattr(m, "PAUSE", 0.1), getattr(m, "FAILSAFE", True))
        m.PAUSE = 0.0
        m.FAILSAFE = False
        try:
            yield m
        finally:
            m.PAUSE, m.FAILSAFE = old


# ─────────────────────────────────────────────────────────────────────────────
#  Pure helpers (tested directly)
# ─────────────────────────────────────────────────────────────────────────────
class _ClickTracker:
    """Numbers presses the way the OS does, for kCGMouseEventClickState."""

    def __init__(self, window: float = DOUBLE_S, slop: float = DOUBLE_PT,
                 most: int = 3):
        self.window, self.slop, self.most = window, slop, most
        self._last: Optional[tuple] = None      # (which, x, y, t_up, state)
        self._down_state: dict[str, int] = {}

    def _next(self, which: str, x: float, y: float, t: float) -> int:
        last = self._last
        if (last is not None and last[0] == which and 0 <= t - last[3] <= self.window
                and math.hypot(x - last[1], y - last[2]) <= self.slop):
            return min(self.most, last[4] + 1)
        return 1

    def down(self, which: str, x: float, y: float, t: float) -> int:
        state = self._next(which, x, y, t)
        self._down_state[which] = state
        return state

    def up(self, which: str, x: float, y: float, t: float) -> int:
        state = self._down_state.pop(which, 1)
        self._last = (which, x, y, t, state)
        return state

    def note(self, which: str, x: float, y: float, t: float, state: int) -> None:
        """Record a whole click sent in one go (down and up at state)."""
        self._down_state.pop(which, None)
        self._last = (which, x, y, t, int(state))

    def first_of(self, which: str, x: float, y: float, t: float, count: int) -> int:
        """The first state a click(count) still has to send: `count` itself
        when the presses before it are already on screen, else 1."""
        if count > 1 and self._next(which, x, y, t) == count:
            return count
        return 1


class _Accum:
    """Turns fractional amounts into whole steps, keeping the remainder."""

    def __init__(self, step: float = 1.0):
        self.step = float(step) if step else 1.0
        self.rest = 0.0

    def add(self, amount: float) -> int:
        self.rest += float(amount or 0.0)
        n = int(self.rest / self.step)          # truncates toward zero
        self.rest -= n * self.step
        return n

    def reset(self) -> None:
        self.rest = 0.0


# macOS virtual key codes are key POSITIONS on an ANSI keyboard.
_MAC_CODES = {
    "enter": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53,
    "delete": 117, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "f11": 103,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
    "k": 40, "n": 45, "m": 46,
    "bracketleft": 33, "bracketright": 30, "minus": 27, "equal": 24,
    "comma": 43, "period": 47, "slash": 44, "semicolon": 41, "quote": 39,
    "backslash": 42, "grave": 50,
}
_MAC_MOD_CODES = {"cmd": 55, "shift": 56, "alt": 58, "ctrl": 59}

# CGEventFlags, hard-coded so the pure spec needs no Quartz import.
_F_CMD, _F_CTRL, _F_ALT, _F_SHIFT = 0x100000, 0x40000, 0x80000, 0x20000
_F_FN, _F_NUMPAD = 0x800000, 0x200000
_MAC_MOD_FLAGS = {"cmd": _F_CMD, "ctrl": _F_CTRL, "alt": _F_ALT, "shift": _F_SHIFT}
# Real arrow keys carry Fn and NumericPad; without them some apps (and the
# Spaces shortcut) ignore a synthetic arrow. Navigation keys carry Fn.
_MAC_ARROWS = {"left", "right", "up", "down"}
_MAC_FN_KEYS = {"home", "end", "pageup", "pagedown", "delete", "f11"}

_KEY_ALIASES = {
    "return": "enter", "escape": "esc", "back": "backspace", "bksp": "backspace",
    "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "spacebar": "space",
    "[": "bracketleft", "]": "bracketright", "-": "minus", "=": "equal",
    ",": "comma", ".": "period", "/": "slash", ";": "semicolon", "'": "quote",
    "\\": "backslash", "`": "grave", " ": "space",
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
}
_MODS = ("cmd", "ctrl", "alt", "shift")


def _key_name(name: str) -> str:
    key = str(name or "").strip().lower()
    if key == " ":
        return "space"
    return _KEY_ALIASES.get(key, key)


def _mod_names(mods) -> Optional[tuple]:
    out: list[str] = []
    for m in mods or ():
        m = str(m or "").strip().lower()
        m = {"command": "cmd", "control": "ctrl", "option": "alt", "opt": "alt",
             "win": "cmd", "super": "cmd", "meta": "cmd"}.get(m, m)
        if m not in _MODS:
            return None
        if m not in out:
            out.append(m)
    return tuple(out)


def _mac_key_spec(name: str, mods=()) -> Optional[tuple[int, int]]:
    """(virtual key code, CGEventFlags) for a key and its modifiers, or None
    for a name or modifier this table does not know."""
    key = _key_name(name)
    code = _MAC_CODES.get(key)
    ms = _mod_names(mods)
    if code is None or ms is None:
        return None
    flags = 0
    for m in ms:
        flags |= _MAC_MOD_FLAGS[m]
    if key in _MAC_ARROWS:
        flags |= _F_FN | _F_NUMPAD
    elif key in _MAC_FN_KEYS:
        flags |= _F_FN
    return code, flags


def _normalise_combo(combo: str, platform: Optional[str] = None
                     ) -> Optional[tuple[str, tuple]]:
    """"ctrl+w" -> ("w", ("cmd",)) on macOS and ("w", ("ctrl",)) elsewhere.

    screenact.normalise_key's rule: "ctrl" and "cmd" both mean the platform's
    shortcut modifier, so one combo works everywhere. "macctrl" is the
    physical Control key on a Mac; "win"/"super" is the Windows/Super key off
    the Mac (this module's "cmd" mod there). None when the combo is empty or
    names an unknown modifier.
    """
    plat = platform or _PLAT
    mac = plat == "darwin"
    parts = [p.strip().lower() for p in str(combo or "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    *mods_in, last = parts
    mods: list[str] = []
    for m in mods_in:
        if m in ("ctrl", "control", "ctl", "cmd", "command", "meta", "apple"):
            mapped = "cmd" if mac else "ctrl"
        elif m == "macctrl":
            mapped = "ctrl"
        elif m in ("win", "super", "winleft", "windows", "start"):
            mapped = "cmd"
        elif m in ("alt", "option", "opt"):
            mapped = "alt"
        elif m == "shift":
            mapped = "shift"
        else:
            return None
        if mapped not in mods:
            mods.append(mapped)
    return _key_name(last), tuple(mods)


# ─────────────────────────────────────────────────────────────────────────────
#  Backends
# ─────────────────────────────────────────────────────────────────────────────
class _Backend:
    """Shared state and the parts every platform does the same way.

    A platform overrides the primitives: cursor, _move, _button, _wheel,
    _key_tap, _mod (press/release one modifier), key_down. Tests subclass and
    override the primitives to record what would have been posted.
    """

    name = "base"

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.mods_down: set[str] = set()
        self.clicks = _ClickTracker()
        self._last_pos = (0.0, 0.0)

    # primitives -----------------------------------------------------------
    def cursor(self) -> Optional[tuple[float, float]]:
        return None

    def _move(self, x: float, y: float, drag: str) -> None:
        raise NotImplementedError

    def _button(self, which: str, down: bool, x: float, y: float, state: int) -> None:
        raise NotImplementedError

    def _wheel(self, dx_px: float, dy_px: float) -> None:
        raise NotImplementedError

    def _key_tap(self, name: str, mods: tuple) -> Status:
        raise NotImplementedError

    def _mod(self, mod: str, down: bool) -> None:
        pass

    def key_down(self, name: str) -> Optional[bool]:
        return None

    def check(self) -> Status:
        return True, ""

    # shared ---------------------------------------------------------------
    def _pos(self) -> tuple[float, float]:
        try:
            p = self.cursor()
        except Exception:
            p = None
        if p is not None:
            self._last_pos = (float(p[0]), float(p[1]))
        return self._last_pos

    def move(self, x: float, y: float) -> None:
        drag = "left" if "left" in self.held else ("right" if "right" in self.held else "")
        self._move(float(x), float(y), drag)
        self._last_pos = (float(x), float(y))

    def button(self, down: bool, which: str = "left") -> None:
        which = "right" if str(which).lower().startswith("r") else "left"
        x, y = self._pos()
        t = time.monotonic()
        if down:
            if which in self.held:
                return
            state = self.clicks.down(which, x, y, t)
            self.held.add(which)
        else:
            if which not in self.held:
                return
            state = self.clicks.up(which, x, y, t)
            self.held.discard(which)
        self._button(which, bool(down), x, y, state)

    def click(self, which: str = "left", count: int = 1) -> None:
        which = "right" if str(which).lower().startswith("r") else "left"
        count = max(1, min(3, int(count or 1)))
        if which in self.held:
            self.button(False, which)
        x, y = self._pos()
        t = time.monotonic()
        first = self.clicks.first_of(which, x, y, t, count)
        for state in range(first, count + 1):
            self._button(which, True, x, y, state)
            self._button(which, False, x, y, state)
        self.clicks.note(which, x, y, t, count)

    def scroll(self, dx_px: float, dy_px: float) -> None:
        self._wheel(float(dx_px or 0.0), float(dy_px or 0.0))

    def key_event(self, name: str, mods=()) -> Status:
        ms = _mod_names(mods)
        if ms is None:
            return False, f"unknown modifier in {mods!r}"
        return self._key_tap(_key_name(name), ms)

    def release_all(self) -> None:
        for which in list(self.held):
            try:
                self.button(False, which)
            except Exception as e:
                _log(f"release {which}: {e}")
            self.held.discard(which)
        for m in list(self.mods_down):
            try:
                self._mod(m, False)
            except Exception as e:
                _log(f"release {m}: {e}")
            self.mods_down.discard(m)


class _MacBackend(_Backend):
    name = "quartz"

    def __init__(self) -> None:
        super().__init__()
        self._Q: Any = None
        self._src: Any = None
        self._ax = _Accum(1.0)
        self._ay = _Accum(1.0)

    def _q(self):
        if self._Q is None:
            import Quartz
            self._Q = Quartz
            try:
                self._src = Quartz.CGEventSourceCreate(
                    Quartz.kCGEventSourceStateHIDSystemState)
            except Exception:
                self._src = None
        return self._Q

    def check(self) -> Status:
        try:
            self._q()
            return True, ""
        except Exception as e:
            return False, f"Quartz (pyobjc) is not available: {e}"

    def cursor(self):
        Q = self._q()
        p = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return float(p.x), float(p.y)

    def _post(self, ev) -> None:
        self._q().CGEventPost(self._Q.kCGHIDEventTap, ev)

    def _mouse_event(self, etype, x: float, y: float, button, state: int = 0) -> None:
        Q = self._q()
        ev = Q.CGEventCreateMouseEvent(self._src, etype, (x, y), button)
        if state:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, int(state))
        self._post(ev)

    def _move(self, x, y, drag):
        Q = self._q()
        if drag == "left":
            etype, btn = Q.kCGEventLeftMouseDragged, Q.kCGMouseButtonLeft
        elif drag == "right":
            etype, btn = Q.kCGEventRightMouseDragged, Q.kCGMouseButtonRight
        else:
            etype, btn = Q.kCGEventMouseMoved, Q.kCGMouseButtonLeft
        self._mouse_event(etype, x, y, btn, 0)

    def _button(self, which, down, x, y, state):
        Q = self._q()
        if which == "right":
            etype = Q.kCGEventRightMouseDown if down else Q.kCGEventRightMouseUp
            btn = Q.kCGMouseButtonRight
        else:
            etype = Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp
            btn = Q.kCGMouseButtonLeft
        self._mouse_event(etype, x, y, btn, max(1, int(state)))

    def _wheel(self, dx_px, dy_px):
        dy = self._ay.add(dy_px)
        dx = self._ax.add(dx_px)
        if not dx and not dy:
            return
        Q = self._q()
        unit = Q.kCGScrollEventUnitPixel
        ev = None
        make2 = getattr(Q, "CGEventCreateScrollWheelEvent2", None)
        if make2 is not None:
            try:
                ev = make2(self._src, unit, 2, dy, dx, 0)
            except Exception:
                ev = None
        if ev is None:
            ev = Q.CGEventCreateScrollWheelEvent(self._src, unit, 2, dy, dx)
        try:
            Q.CGEventSetIntegerValueField(ev, Q.kCGScrollWheelEventIsContinuous, 1)
        except Exception:
            pass
        self._post(ev)

    def _key(self, code: int, down: bool, flags: int) -> None:
        Q = self._q()
        ev = Q.CGEventCreateKeyboardEvent(self._src, code, down)
        Q.CGEventSetFlags(ev, int(flags))      # explicit, even 0
        self._post(ev)

    def _mod(self, mod, down):
        flags = 0
        for m in self.mods_down:
            flags |= _MAC_MOD_FLAGS[m]
        if down:
            flags |= _MAC_MOD_FLAGS[mod]
            self.mods_down.add(mod)
        else:
            flags &= ~_MAC_MOD_FLAGS[mod]
            self.mods_down.discard(mod)
        self._key(_MAC_MOD_CODES[mod], down, flags)

    def _key_tap(self, name, mods):
        spec = _mac_key_spec(name, mods)
        if spec is None:
            return False, f"no key called {name!r}"
        code, flags = spec
        pressed: list[str] = []
        try:
            for m in mods:
                if m not in self.mods_down:
                    self._mod(m, True)
                    pressed.append(m)
            self._key(code, True, flags)
            self._key(code, False, flags)
        finally:
            for m in reversed(pressed):
                try:
                    self._mod(m, False)
                except Exception:
                    pass
        return True, ""

    def key_down(self, name):
        code = _MAC_CODES.get(_key_name(name))
        if code is None:
            return None
        Q = self._q()
        return bool(Q.CGEventSourceKeyState(
            Q.kCGEventSourceStateCombinedSessionState, code))


_WIN_VK = {
    "enter": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "esc": 0x1B, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "f11": 0x7A,
    "bracketleft": 0xDB, "bracketright": 0xDD, "minus": 0xBD, "equal": 0xBB,
    "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "semicolon": 0xBA,
    "quote": 0xDE, "backslash": 0xDC, "grave": 0xC0,
    "cmd": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
}
_WIN_EXTENDED = {0x2E, 0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x5B}


def _win_vk(name: str) -> Optional[int]:
    key = _key_name(name)
    if key in _WIN_VK:
        return _WIN_VK[key]
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        return ord(key.upper())
    return None


class _WinBackend(_Backend):
    name = "sendinput"

    def __init__(self) -> None:
        super().__init__()
        self._u32: Any = None
        self._types: Any = None
        self._wx = _Accum(1.0)
        self._wy = _Accum(1.0)

    def _user32(self):
        if self._u32 is None:
            import ctypes
            from ctypes import wintypes

            ulong_ptr = ctypes.c_size_t

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                            ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ulong_ptr)]

            class _UNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

            class INPUT(ctypes.Structure):
                _anonymous_ = ("u",)
                _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

            u32 = ctypes.WinDLL("user32", use_last_error=True)
            u32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
            u32.SendInput.restype = wintypes.UINT
            self._types = (ctypes, wintypes, MOUSEINPUT, INPUT)
            self._u32 = u32
        return self._u32

    def check(self) -> Status:
        try:
            self._user32()
            return True, ""
        except Exception as e:
            return False, f"user32 is not available: {e}"

    def cursor(self):
        u32 = self._user32()
        ctypes, wintypes = self._types[0], self._types[1]
        pt = wintypes.POINT()
        if not u32.GetCursorPos(ctypes.byref(pt)):
            return None
        return float(pt.x), float(pt.y)

    def _send_mouse(self, flags: int, data: int = 0) -> None:
        u32 = self._user32()
        ctypes, _w, MOUSEINPUT, INPUT = self._types
        rec = INPUT(type=0)
        rec.mi = MOUSEINPUT(dx=0, dy=0, mouseData=int(data) & 0xFFFFFFFF,
                            dwFlags=flags, time=0, dwExtraInfo=0)
        arr = (INPUT * 1)(rec)
        u32.SendInput(1, arr, ctypes.sizeof(INPUT))

    def _move(self, x, y, drag):
        self._user32().SetCursorPos(int(round(x)), int(round(y)))

    def _button(self, which, down, x, y, state):
        if which == "right":
            flag = 0x0008 if down else 0x0010
        else:
            flag = 0x0002 if down else 0x0004
        self._send_mouse(flag)

    def _wheel(self, dx_px, dy_px):
        # WHEEL: positive = rolled away from the user = content above, the
        # raw convention. HWHEEL: positive scrolls RIGHT, so dx is negated.
        dy = self._wy.add(dy_px * 1.2)
        dx = self._wx.add(-dx_px * 1.2)
        if dy:
            self._send_mouse(0x0800, dy)
        if dx:
            self._send_mouse(0x1000, dx)

    def _kb(self, vk: int, down: bool) -> None:
        flags = (0 if down else 0x0002) | (0x0001 if vk in _WIN_EXTENDED else 0)
        self._user32().keybd_event(vk, 0, flags, 0)

    def _mod(self, mod, down):
        self._kb(_WIN_VK[mod], down)
        (self.mods_down.add if down else self.mods_down.discard)(mod)

    def _key_tap(self, name, mods):
        vk = _win_vk(name)
        if vk is None:
            return False, f"no key called {name!r}"
        pressed: list[str] = []
        try:
            for m in mods:
                if m not in self.mods_down:
                    self._mod(m, True)
                    pressed.append(m)
            self._kb(vk, True)
            self._kb(vk, False)
        finally:
            for m in reversed(pressed):
                try:
                    self._mod(m, False)
                except Exception:
                    pass
        return True, ""

    def key_down(self, name):
        vk = _win_vk(name)
        if vk is None:
            return None
        return bool(self._user32().GetAsyncKeyState(vk) & 0x8000)


_X11_KEYS = {"cmd": "winleft", "bracketleft": "[", "bracketright": "]",
             "minus": "-", "equal": "=", "comma": ",", "period": ".",
             "slash": "/", "semicolon": ";", "quote": "'", "backslash": "\\",
             "grave": "`"}


class _X11Backend(_Backend):
    name = "xtest"
    NOTCH_PX = 40.0

    def __init__(self) -> None:
        super().__init__()
        self._nx = _Accum(self.NOTCH_PX)
        self._ny = _Accum(self.NOTCH_PX)

    def check(self) -> Status:
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui is not installed. Run: pip install pyautogui"
        try:
            with _pg():
                pass
            return True, ""
        except Exception as e:
            return False, f"pyautogui could not start: {e}"

    def cursor(self):
        with _pg() as m:
            p = m.position()
        return float(p[0]), float(p[1])

    def _move(self, x, y, drag):
        with _pg() as m:
            m.moveTo(int(round(x)), int(round(y)))

    def _button(self, which, down, x, y, state):
        with _pg() as m:
            (m.mouseDown if down else m.mouseUp)(button=which)

    def click(self, which="left", count=1):
        which = "right" if str(which).lower().startswith("r") else "left"
        count = max(1, min(3, int(count or 1)))
        if which in self.held:
            self.button(False, which)
        x, y = self._pos()
        t = time.monotonic()
        first = self.clicks.first_of(which, x, y, t, count)
        with _pg() as m:
            m.click(clicks=count - first + 1, interval=0, button=which)
        self.clicks.note(which, x, y, t, count)

    def _wheel(self, dx_px, dy_px):
        ny = self._ny.add(dy_px)
        nx = self._nx.add(-dx_px)          # hscroll positive = right
        if not nx and not ny:
            return
        with _pg() as m:
            if ny:
                m.scroll(ny)
            if nx:
                m.hscroll(nx)

    def _mod(self, mod, down):
        with _pg() as m:
            (m.keyDown if down else m.keyUp)(_X11_KEYS.get(mod, mod))
        (self.mods_down.add if down else self.mods_down.discard)(mod)

    def _key_tap(self, name, mods):
        key = _X11_KEYS.get(name, name)
        with _pg() as m:
            if key not in getattr(m, "KEYBOARD_KEYS", (key,)):
                return False, f"no key called {name!r}"
            m.hotkey(*[_X11_KEYS.get(x, x) for x in mods], key)
        return True, ""


_backend: Optional[_Backend] = None
_backend_lock = threading.Lock()


def _set_backend(backend) -> None:
    """Replace the platform backend (tests). None goes back to the default."""
    global _backend
    with _backend_lock:
        _backend = backend


def _get_backend():
    global _backend
    with _backend_lock:
        if _backend is None:
            if _PLAT == "darwin":
                _backend = _MacBackend()
            elif _PLAT.startswith("win"):
                _backend = _WinBackend()
            else:
                _backend = _X11Backend()
        return _backend


# ─────────────────────────────────────────────────────────────────────────────
#  Availability and permissions
# ─────────────────────────────────────────────────────────────────────────────
def available() -> Status:
    """(True, "") when synthetic input can reach the screen from here."""
    try:
        from core import screenact
        ok, why = screenact.available()
        if not ok:
            return False, why
    except Exception as e:
        return False, f"the input layer could not load: {e}"
    try:
        return _get_backend().check()
    except Exception as e:
        return False, str(e)


def permission_state() -> dict:
    """{"accessibility": bool|None, "post_events": bool|None}; None off macOS
    or when the system will not say."""
    out: dict = {"accessibility": None, "post_events": None}
    if _PLAT != "darwin":
        return out
    try:
        import ApplicationServices as AS
        out["accessibility"] = bool(AS.AXIsProcessTrusted())
    except Exception:
        pass
    try:
        import Quartz
        fn = getattr(Quartz, "CGPreflightPostEventAccess", None)
        if fn is not None:
            out["post_events"] = bool(fn())
    except Exception:
        pass
    return out


def request_permission() -> None:
    """Ask macOS to show its permission prompts (once each; macOS decides)."""
    if _PLAT != "darwin":
        return
    try:
        state = permission_state()
        if state.get("accessibility") is False:
            import ApplicationServices as AS
            AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
        if state.get("post_events") is False:
            import Quartz
            fn = getattr(Quartz, "CGRequestPostEventAccess", None)
            if fn is not None:
                fn()
    except Exception as e:
        _log(f"permission request failed: {e}")


def self_check() -> Status:
    """Move the cursor by one point, read it back, put it back.

    An unpermitted macOS (and Wayland) accepts the event and discards it, so
    the only honest test of "can we drive the cursor" is to look.
    """
    try:
        p = cursor()
        if p is None:
            return False, "the cursor position cannot be read here"
        dx = 1.0
        mon = monitor_at(p[0], p[1])
        if mon.get("width") and p[0] + 2 >= mon["left"] + mon["width"]:
            dx = -1.0
        target = (p[0] + dx, p[1])
        move(*target)
        time.sleep(0.03)
        q = cursor()
        move(*p)
        if q is None:
            return False, "the cursor position cannot be read here"
        if abs(q[0] - target[0]) <= 0.6 and abs(q[1] - target[1]) <= 0.6:
            return True, ""
        why = "the system ignored a synthetic mouse move"
        if _PLAT == "darwin":
            why += ("; allow this app in System Settings > Privacy & Security "
                    "> Accessibility")
        return False, why
    except Exception as e:
        return False, f"the self check failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
#  Monitors and coordinates
# ─────────────────────────────────────────────────────────────────────────────
_FALLBACK_MON = {"left": 0, "top": 0, "width": 1440, "height": 900}


def _native_monitors() -> list[dict]:
    """Monitors without mss: Quartz display bounds (points) on macOS, the
    primary screen's size elsewhere."""
    try:
        if _PLAT == "darwin":
            import Quartz
            err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
            if err != 0:
                return []
            main = Quartz.CGMainDisplayID()
            out = []
            for d in sorted(list(ids)[:n], key=lambda d: d != main):
                r = Quartz.CGDisplayBounds(d)
                out.append({"left": int(r.origin.x), "top": int(r.origin.y),
                            "width": int(r.size.width), "height": int(r.size.height)})
            return [m for m in out if m["width"] > 0 and m["height"] > 0]
        if _HAS_PYAUTOGUI:
            with _pg() as m:
                w, h = m.size()
            if w and h:
                return [{"left": 0, "top": 0, "width": int(w), "height": int(h)}]
    except Exception:
        pass
    return []


def monitors() -> list[dict]:
    """The physical monitors, in mouse space, primary first."""
    try:
        from core import screenview
        ms = screenview.monitors()
        out = [dict(m) for m in (ms[1:] or ms[:1])
               if m.get("width", 0) > 0 and m.get("height", 0) > 0]
        return out or _native_monitors()
    except Exception:
        return _native_monitors()


def _contains(m: dict, x: float, y: float) -> bool:
    return (m["left"] <= x < m["left"] + m["width"]
            and m["top"] <= y < m["top"] + m["height"])


def monitor_at(x: float, y: float) -> dict:
    """The monitor holding (x, y), else the nearest one."""
    try:
        ms = monitors()
        if not ms:
            return dict(_FALLBACK_MON)
        for m in ms:
            if _contains(m, x, y):
                return m

        def dist(m):
            cx = min(max(x, m["left"]), m["left"] + m["width"])
            cy = min(max(y, m["top"]), m["top"] + m["height"])
            return math.hypot(x - cx, y - cy)
        return min(ms, key=dist)
    except Exception:
        return dict(_FALLBACK_MON)


def monitor_for_qt(geo: tuple, dpr: float) -> dict:
    """The monitor a QScreen is, in mouse space.

    Qt reports a screen's geometry in its own logical units: points on macOS
    (the same as mouse space) and device-independent pixels on Windows, where
    mouse space is physical. So first an origin match as-is, then one scaled
    by the device pixel ratio, and with no match the geometry itself (scaled
    off the Mac).
    """
    try:
        gx, gy, gw, gh = (float(v) for v in geo)
        d = float(dpr or 1.0) or 1.0
        ms = monitors()
        for m in ms:
            if abs(m["left"] - gx) <= 2 and abs(m["top"] - gy) <= 2:
                return m
        for m in ms:
            if abs(m["left"] - gx * d) <= 2 and abs(m["top"] - gy * d) <= 2:
                return m
        if _PLAT == "darwin":
            d = 1.0
        return {"left": int(round(gx * d)), "top": int(round(gy * d)),
                "width": int(round(gw * d)), "height": int(round(gh * d))}
    except Exception:
        return dict(_FALLBACK_MON)


def to_mouse(u: float, v: float, mon: dict) -> tuple[float, float]:
    """Screen units on `mon` to mouse space, kept one pixel inside its edges
    (a cursor parked on the very edge can trigger hot corners and the Dock)."""
    try:
        left, top = float(mon["left"]), float(mon["top"])
        w, h = float(mon["width"]), float(mon["height"])
        x = left + float(u) * w
        y = top + float(v) * h
        x = min(max(x, left + 1), left + w - 2)
        y = min(max(y, top + 1), top + h - 2)
        return x, y
    except Exception:
        return 0.0, 0.0


def from_mouse(x: float, y: float, mon: dict) -> tuple[float, float]:
    try:
        w, h = float(mon["width"]), float(mon["height"])
        u = (float(x) - float(mon["left"])) / w if w else 0.5
        v = (float(y) - float(mon["top"])) / h if h else 0.5
        return u, v
    except Exception:
        return 0.5, 0.5


# ─────────────────────────────────────────────────────────────────────────────
#  Mouse
# ─────────────────────────────────────────────────────────────────────────────
def cursor() -> Optional[tuple[float, float]]:
    try:
        p = _get_backend().cursor()
        return None if p is None else (float(p[0]), float(p[1]))
    except Exception:
        return None


def move(x, y) -> None:
    """Move the cursor; a drag event instead while a button is held."""
    try:
        with _io_lock:
            _get_backend().move(float(x), float(y))
    except Exception as e:
        _log(f"move: {e}")


def button(down: bool, which: str = "left") -> None:
    try:
        with _io_lock:
            _get_backend().button(bool(down), which)
    except Exception as e:
        _log(f"button: {e}")


def click(which: str = "left", count: int = 1) -> None:
    try:
        with _io_lock:
            _get_backend().click(which, count)
    except Exception as e:
        _log(f"click: {e}")


def scroll(dx_px: float, dy_px: float) -> None:
    """Smooth pixel scroll. dy_px > 0 reveals content ABOVE (wheel rolled
    up); dx_px > 0 reveals content to the LEFT. Fractions are carried over."""
    try:
        with _io_lock:
            _get_backend().scroll(float(dx_px or 0.0), float(dy_px or 0.0))
    except Exception as e:
        _log(f"scroll: {e}")


def release_all() -> None:
    """Let go of every button and modifier this module is holding."""
    try:
        with _io_lock:
            _get_backend().release_all()
    except Exception as e:
        _log(f"release_all: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Keyboard
# ─────────────────────────────────────────────────────────────────────────────
def key(name: str, mods: tuple = (), count: int = 1) -> Status:
    """Press a key `count` times with modifiers held. The caller decides
    whether the focused app is the right target (see own_app_focused)."""
    try:
        n = max(1, min(MAX_KEY_REPEAT, int(count or 1)))
        if _mod_names(mods) is None:
            return False, f"unknown modifier in {tuple(mods)!r}"
        b = _get_backend()
        for i in range(n):
            with _io_lock:
                ok, why = b.key_event(name, tuple(mods or ()))
            if not ok:
                return False, why
            if n > 1 and i % 25 == 24:
                time.sleep(0.005)   # let a slow app drain its queue
        return True, ""
    except Exception as e:
        return False, f"the key could not be sent: {e}"


def hotkey(combo: str) -> Status:
    """"ctrl+w" style; ctrl means Cmd on macOS (screenact's rule)."""
    try:
        spec = _normalise_combo(combo)
        if spec is None:
            return False, f"not a key combination: {combo!r}"
        return key(spec[0], spec[1])
    except Exception as e:
        return False, str(e)


def type_text(text: str) -> Status:
    """Unicode text at the caret, through screenact (layout independent,
    clipboard restored, refuses when the assistant has the keyboard)."""
    try:
        from core import screenact
        act = screenact.type_text(str(text or ""))
        return bool(act.ok), (act.detail if act.ok else act.error) or ""
    except Exception as e:
        return False, f"the text could not be typed: {e}"


def key_down(name: str) -> Optional[bool]:
    """Is this physical key down right now? None when the OS cannot tell."""
    try:
        return _get_backend().key_down(name)
    except Exception:
        return None


def own_app_focused() -> bool:
    """Does the assistant's own window have the keyboard?"""
    try:
        from core import screenview
        pid = screenview.foreground_pid()
        return bool(pid) and bool(screenview.is_own_pid(pid))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Desktop shortcuts
# ─────────────────────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: float = 4.0) -> tuple[bool, str, str]:
    """(ok, stdout, why). stderr is logged, never thrown away."""
    exe = shutil.which(cmd[0])
    if not exe:
        return False, "", f"{cmd[0]} is not installed"
    try:
        r = subprocess.run([exe, *cmd[1:]], capture_output=True, timeout=timeout)
    except Exception as e:
        return False, "", f"{cmd[0]} failed: {e}"
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if err:
        _log(f"{' '.join(cmd)}: {err}")
    if r.returncode != 0:
        return False, out, err or f"{cmd[0]} exited with {r.returncode}"
    return True, out, ""


def nav(what: str) -> Status:
    """Browser-style Back / Forward."""
    try:
        fwd = str(what or "").lower().startswith("f")
        if _PLAT == "darwin":
            return key("bracketright" if fwd else "bracketleft", ("cmd",))
        return key("right" if fwd else "left", ("alt",))
    except Exception as e:
        return False, str(e)


def space(direction: str) -> Status:
    """Switch to the desktop (Space) on the left or the right."""
    try:
        right = str(direction or "").lower().startswith("r")
        arrow = "right" if right else "left"
        if _PLAT == "darwin":
            return key(arrow, ("ctrl",))
        if _PLAT.startswith("win"):
            return key(arrow, ("cmd", "ctrl"))
        ok, out, why = _run(["wmctrl", "-d"])
        if ok:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            cur = next((i for i, ln in enumerate(lines)
                        if len(ln.split()) > 1 and ln.split()[1] == "*"), None)
            if cur is not None and lines:
                target = cur + (1 if right else -1)
                if not 0 <= target < len(lines):
                    return False, "there is no desktop further that way"
                ok2, _o, why2 = _run(["wmctrl", "-s", str(target)])
                if ok2:
                    return True, ""
                why = why2
        ok, _o, why3 = _run(["xdotool", "key", f"ctrl+alt+{arrow.capitalize()}"])
        return (True, "") if ok else (False, why3 or why)
    except Exception as e:
        return False, str(e)


def overview() -> Status:
    """Mission Control / Task View / the activities overview."""
    try:
        if _PLAT == "darwin":
            ok, _o, why = _run(["open", "-a", "Mission Control"])
            if ok:
                return True, ""
            return key("up", ("ctrl",))
        if _PLAT.startswith("win"):
            return key("tab", ("cmd",))
        ok, _o, why = _run(["xdotool", "key", "super"])
        return (True, "") if ok else (False, why)
    except Exception as e:
        return False, str(e)


def app_windows() -> Status:
    """The front app's windows (App Exposé on macOS)."""
    try:
        if _PLAT == "darwin":
            return key("down", ("ctrl",))
        if _PLAT.startswith("win"):
            return key("tab", ("cmd",))
        return overview()
    except Exception as e:
        return False, str(e)


def show_desktop() -> Status:
    try:
        if _PLAT == "darwin":
            return key("f11")
        if _PLAT.startswith("win"):
            return key("d", ("cmd",))
        ok, _o, why = _run(["wmctrl", "-k", "on"])
        return (True, "") if ok else (False, why)
    except Exception as e:
        return False, str(e)


def launcher() -> Status:
    """Spotlight / the Start menu / the desktop's launcher."""
    try:
        if _PLAT == "darwin":
            return key("space", ("cmd",))
        if _PLAT.startswith("win"):
            b = _get_backend()
            with _io_lock:
                b._mod("cmd", True)
                b._mod("cmd", False)
            return True, ""
        ok, _o, why = _run(["xdotool", "key", "super"])
        return (True, "") if ok else (False, why)
    except Exception as e:
        return False, str(e)


def close_window() -> Status:
    """Cmd+W / Ctrl+W: the tab or window. Never Cmd+Q. Refused while the
    assistant itself has the keyboard, so it cannot close its own window."""
    try:
        if own_app_focused():
            return False, ("the assistant's own window has the keyboard; "
                           "nothing was closed")
        return hotkey("ctrl+w")
    except Exception as e:
        return False, str(e)


def open_browser(url: str = "") -> str:
    """Open the user's real browser; a core.outcome line."""
    from core import outcome
    try:
        from actions import browser_control
        return browser_control._open_native(url or "about:blank", None)
    except Exception as e:
        first = str(e)
    try:
        import webbrowser
        if url and webbrowser.open(url):
            return outcome.unverified("browser", "the browser was asked to open",
                                      url=url)
    except Exception:
        pass
    return outcome.failed("browser", f"the browser could not be opened: {first}")


# ─────────────────────────────────────────────────────────────────────────────
#  The focused text field
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TextField:
    pid: int
    role: str
    secure: bool
    rect: tuple            # (x, y, w, h) in mouse space
    caret: Optional[tuple]  # (x, y, w, h) in mouse space, or None
    app: str = ""


_MAC_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}


def _is_own(pid: int) -> bool:
    try:
        from core import screenview
        return bool(pid) and bool(screenview.is_own_pid(int(pid)))
    except Exception:
        return False


def _secure_words() -> tuple:
    try:
        from core import screenview
        return tuple(screenview._SECURE_WORDS)
    except Exception:
        return ("secure",)


def _rect4(value) -> Optional[tuple]:
    try:
        x, y, w, h = (float(v) for v in value)
        return (x, y, w, h)
    except Exception:
        return None


def _field_from(reader: Callable[[str], Any]) -> Optional[TextField]:
    """A TextField from an accessibility element, or None.

    `reader(attr)` returns plain Python values (None when absent):
        AXRole, AXSubrole, AXRoleDescription -> str
        AXPosition -> (x, y); AXSize -> (w, h)
        AXSelectedTextRange -> anything truthy when the element has one
        settable:AXValue -> bool
        AXBoundsForRange -> (x, y, w, h) of the selection / caret
        pid -> int; app -> str
    Pure so it can be tested with a dict.
    """
    try:
        role = str(reader("AXRole") or "")
        editable = role in _MAC_TEXT_ROLES or (
            bool(reader("AXSelectedTextRange")) and bool(reader("settable:AXValue")))
        if not editable:
            return None
        pid = int(reader("pid") or 0)
        if _is_own(pid):
            return None
        subrole = str(reader("AXSubrole") or "").lower()
        desc = str(reader("AXRoleDescription") or "").lower()
        secure = ("secure" in subrole or any(w in desc for w in _secure_words())
                  or bool(reader("protected")))
        pos, size = reader("AXPosition"), reader("AXSize")
        rect = (0.0, 0.0, 0.0, 0.0)
        if pos is not None and size is not None:
            rect = (float(pos[0]), float(pos[1]), float(size[0]), float(size[1]))
        caret = _rect4(reader("AXBoundsForRange"))
        if caret is not None and (caret[3] <= 0 or caret == (0.0, 0.0, 0.0, 0.0)):
            caret = None
        return TextField(pid=pid, role=role, secure=bool(secure), rect=rect,
                         caret=caret, app=str(reader("app") or ""))
    except Exception:
        return None


def _mac_focused_element():
    import ApplicationServices as AS
    sysw = AS.AXUIElementCreateSystemWide()
    try:
        AS.AXUIElementSetMessagingTimeout(sysw, 0.25)
    except Exception:
        pass
    err, el = AS.AXUIElementCopyAttributeValue(sysw, "AXFocusedUIElement", None)
    if err != 0 or el is None:
        return AS, None
    try:
        AS.AXUIElementSetMessagingTimeout(el, 0.25)
    except Exception:
        pass
    return AS, el


def _mac_reader(AS, el) -> Callable[[str], Any]:
    cache: dict = {}

    def raw(attr):
        try:
            err, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
            return v if err == 0 else None
        except Exception:
            return None

    def read(attr):
        if attr in cache:
            return cache[attr]
        val: Any = None
        try:
            if attr == "pid":
                err, pid = AS.AXUIElementGetPid(el, None)
                val = int(pid) if err == 0 else 0
            elif attr == "settable:AXValue":
                err, ok = AS.AXUIElementIsAttributeSettable(el, "AXValue", None)
                val = err == 0 and bool(ok)
            elif attr == "AXPosition":
                v = raw(attr)
                if v is not None:
                    ok, p = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
                    val = (p.x, p.y) if ok else None
            elif attr == "AXSize":
                v = raw(attr)
                if v is not None:
                    ok, s = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
                    val = (s.width, s.height) if ok else None
            elif attr == "AXBoundsForRange":
                rng = raw("AXSelectedTextRange")
                if rng is not None:
                    err, v = AS.AXUIElementCopyParameterizedAttributeValue(
                        el, "AXBoundsForRange", rng, None)
                    if err == 0 and v is not None:
                        ok, r = AS.AXValueGetValue(v, AS.kAXValueCGRectType, None)
                        if ok:
                            val = (r.origin.x, r.origin.y, r.size.width, r.size.height)
            elif attr == "app":
                from AppKit import NSRunningApplication
                ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                    int(read("pid") or 0))
                val = str(ra.localizedName() or "") if ra is not None else ""
            elif attr == "protected":
                val = None
            else:
                val = raw(attr)
                if attr in ("AXRole", "AXSubrole", "AXRoleDescription") and val is not None:
                    val = str(val)
        except Exception:
            val = None
        cache[attr] = val
        return val

    return read


def _win_focused() -> Optional[TextField]:
    import comtypes
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    from pywinauto.uia_defines import IUIA
    el = IUIA().iuia.GetFocusedElement()
    if el is None:
        return None
    ct = int(el.CurrentControlType)
    if ct not in (50004, 50030, 50003):
        return None
    if ct == 50030:
        # A Document is also a whole web page; only an editable one counts.
        try:
            if el.GetCurrentPropertyValue(30046) is not False:   # ValueIsReadOnly
                return None
        except Exception:
            return None
    pid = int(el.CurrentProcessId or 0)
    if _is_own(pid):
        return None
    r = el.CurrentBoundingRectangle
    rect = (float(r.left), float(r.top), float(r.right - r.left), float(r.bottom - r.top))
    caret = None
    try:
        import ctypes
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        u32 = ctypes.WinDLL("user32")
        if u32.GetGUIThreadInfo(0, ctypes.byref(gti)) and gti.hwndCaret:
            pt = wintypes.POINT(gti.rcCaret.left, gti.rcCaret.top)
            u32.ClientToScreen(gti.hwndCaret, ctypes.byref(pt))
            h = gti.rcCaret.bottom - gti.rcCaret.top
            if h > 0:
                caret = (float(pt.x), float(pt.y),
                         float(max(1, gti.rcCaret.right - gti.rcCaret.left)), float(h))
    except Exception:
        caret = None
    return TextField(pid=pid, role={50004: "edit", 50030: "document",
                                    50003: "combobox"}[ct],
                     secure=bool(el.CurrentIsPassword), rect=rect, caret=caret,
                     app=str(getattr(el, "CurrentFrameworkId", "") or ""))


def _linux_focused() -> Optional[TextField]:
    if not installed("pyatspi"):
        return None
    import pyatspi
    desktop = pyatspi.Registry.getDesktop(0)
    seen = 0
    for app in desktop:
        if app is None:
            continue
        for win in app:
            try:
                if not win.getState().contains(pyatspi.STATE_ACTIVE):
                    continue
            except Exception:
                continue
            frontier = [win]
            while frontier and seen < 600:
                node = frontier.pop(0)
                seen += 1
                try:
                    st = node.getState()
                    if st.contains(pyatspi.STATE_FOCUSED) and st.contains(pyatspi.STATE_EDITABLE):
                        pid = int(app.get_process_id())
                        if _is_own(pid):
                            return None
                        ext = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                        return TextField(
                            pid=pid, role=str(node.getRoleName()),
                            secure=node.getRole() == pyatspi.ROLE_PASSWORD_TEXT,
                            rect=(float(ext.x), float(ext.y), float(ext.width),
                                  float(ext.height)),
                            caret=None, app=str(app.name or ""))
                    frontier.extend(list(node))
                except Exception:
                    continue
    return None


def focused_text_field() -> Optional[TextField]:
    """The text field that has the keyboard, in another app, or None."""
    try:
        if _PLAT == "darwin":
            AS, el = _mac_focused_element()
            if el is None:
                return None
            return _field_from(_mac_reader(AS, el))
        if _PLAT.startswith("win"):
            return _win_focused()
        return _linux_focused()
    except Exception:
        return None


def focused_field_value() -> Optional[str]:
    """The focused field's text, for checking that typing arrived. None for a
    password field, the assistant's own field, or when it cannot be read."""
    try:
        if _PLAT == "darwin":
            AS, el = _mac_focused_element()
            if el is None:
                return None
            read = _mac_reader(AS, el)
            f = _field_from(read)
            if f is None or f.secure:
                return None
            v = read("AXValue")
            return v if isinstance(v, str) else (str(v) if v is not None else None)
        if _PLAT.startswith("win"):
            f = _win_focused()
            if f is None or f.secure:
                return None
            from pywinauto.uia_defines import IUIA
            el = IUIA().iuia.GetFocusedElement()
            v = el.GetCurrentPropertyValue(30045)       # ValueValue
            return v if isinstance(v, str) else None
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  The pump
# ─────────────────────────────────────────────────────────────────────────────
class InputPump:
    """One thread that talks to the OS, fed without ever blocking the GUI.

    Lane 1 ('air-input'): posted jobs and cursor moves in the order they
    came. Moves in a row coalesce (only the latest runs), but a move queued
    before a job always runs before it: a press posted after "move to the
    pinch point" must land at the pinch point, not wherever the hand has
    drifted to since. It also samples the real cursor
    `cursor_hz` times a second, so the GUI can notice the physical mouse.
    Lane 2 ('air-input-slow'): subprocesses, accessibility, network.
    Exceptions are kept in `last_error` and never leave the threads.
    """

    MIN_MOVE_PX = 0.5

    def __init__(self, backend=None, cursor_hz: float = 30.0):
        self._backend = backend
        self._period = 1.0 / max(1.0, float(cursor_hz or 30.0))
        self._cv = threading.Condition()
        self._move: Optional[tuple[float, float]] = None
        self._sent: Optional[tuple[float, float]] = None
        self._batch: deque = deque()
        self._slow: "queue.Queue" = queue.Queue()
        self._sample: Optional[tuple[float, float, float]] = None
        self._closed = False
        self.last_error = ""
        self._t1 = threading.Thread(target=self._loop, name="air-input", daemon=True)
        self._t2 = threading.Thread(target=self._slow_loop, name="air-input-slow",
                                    daemon=True)
        self._t1.start()
        self._t2.start()

    # what the backend means here: an explicit one, else the module (which
    # follows _set_backend)
    def _do_move(self, x: float, y: float) -> None:
        if self._backend is not None:
            self._backend.move(x, y)
        else:
            move(x, y)

    def _do_cursor(self):
        if self._backend is not None:
            return self._backend.cursor()
        return cursor()

    def _do_release(self) -> None:
        if self._backend is not None:
            self._backend.release_all()
        else:
            release_all()

    def _err(self, where: str, e: BaseException) -> None:
        self.last_error = f"{where}: {type(e).__name__}: {e}"

    # producers --------------------------------------------------------------
    def move(self, x: float, y: float) -> None:
        try:
            with self._cv:
                if self._closed:
                    return
                self._move = (float(x), float(y))
                self._cv.notify()
        except Exception as e:
            self._err("move", e)

    def post(self, fn, *args) -> None:
        try:
            with self._cv:
                if self._closed:
                    return
                if self._move is not None:
                    # keep FIFO order: the move queued before this job runs first
                    self._batch.append((None, self._move))
                    self._move = None
                self._batch.append((fn, args))
                self._cv.notify()
        except Exception as e:
            self._err("post", e)

    def slow(self, fn, *args, done=None) -> None:
        if self._closed:
            return
        self._slow.put((fn, args, done))

    def cursor_sample(self) -> Optional[tuple[float, float, float]]:
        return self._sample

    # lanes ----------------------------------------------------------------
    def _apply_move(self, mv) -> None:
        last = self._sent
        if last is None or math.hypot(mv[0] - last[0], mv[1] - last[1]) >= self.MIN_MOVE_PX:
            try:
                self._do_move(*mv)
                self._sent = mv
            except Exception as e:
                self._err("move", e)

    def _loop(self) -> None:
        next_sample = 0.0
        while True:
            with self._cv:
                if not self._closed and self._move is None and not self._batch:
                    wait = next_sample - time.monotonic()
                    self._cv.wait(timeout=min(self._period, max(0.0, wait)) or 0.001)
                if self._closed:
                    return
                mv, self._move = self._move, None
                jobs = list(self._batch)
                self._batch.clear()
            # jobs first: any move queued before a job is already in the batch
            # (see post), so `mv` is the move queued after the last job
            for fn, args in jobs:
                if fn is None:
                    self._apply_move(args)
                    continue
                try:
                    fn(*args)
                except Exception as e:
                    self._err(getattr(fn, "__name__", "job"), e)
            if mv is not None:
                self._apply_move(mv)
            now = time.monotonic()
            if now >= next_sample:
                next_sample = now + self._period
                try:
                    p = self._do_cursor()
                    self._sample = None if p is None else (float(p[0]), float(p[1]), now)
                except Exception as e:
                    self._err("cursor", e)

    def _slow_loop(self) -> None:
        while not self._closed:
            try:
                item = self._slow.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None or self._closed:
                return
            fn, args, done = item
            try:
                result = fn(*args)
            except Exception as e:
                self._err(getattr(fn, "__name__", "slow job"), e)
                result = None
            if done is not None:
                try:
                    done(result)
                except Exception as e:
                    self._err("done", e)

    def close(self) -> None:
        """Stop both lanes, then let go of everything held. Idempotent."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._move = None
            self._batch.clear()
            self._cv.notify_all()
        try:
            self._slow.put_nowait(None)
        except Exception:
            pass
        if threading.current_thread() is not self._t1:
            self._t1.join(timeout=0.3)
        try:
            self._do_release()
        except Exception as e:
            self._err("release_all", e)


# ─────────────────────────────────────────────────────────────────────────────
#  Self test: reads, moves the cursor by one point and back, never clicks
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("available:       ", available())
    print("permission_state:", permission_state())
    print("self_check:      ", self_check())
    ms = monitors()
    print("monitors:        ", ms)
    if ms:
        m = ms[0]
        x, y = to_mouse(0.25, 0.75, m)
        print("to_mouse(0.25, 0.75):", (x, y), "-> from_mouse:", from_mouse(x, y, m))
    print("cursor:          ", cursor())
    print("own_app_focused: ", own_app_focused())
    print("esc down:        ", key_down("esc"))
    print("focused field:   ", focused_text_field())
