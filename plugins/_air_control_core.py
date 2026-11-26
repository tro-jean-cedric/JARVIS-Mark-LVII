"""
plugins/_air_control_core.py — Air Control: the user's hand drives the real
computer (cursor, clicks, drags, scrolling, desktops, typing).

Helper module (leading underscore: the plugin loader skips it). Pure logic: no
Qt, no camera, no OS calls. Three parties use it:

    gui/air_control.py     the see-through overlay. Every tick it feeds
                           AirEngine.update() with the HandStates from
                           plugins/_gesture_core.HandFilter (profile "air",
                           picture coords) and hands the returned ACTIONS to a
                           Dispatcher, which queues them on core/os_input's
                           InputPump. It draws AirEngine.view().
    plugins/air_control.py the voice tool. It reads STATE, which the window
                           keeps up to date.
    plugins/_air_control_tests.py  drives the engine with stand-in hands and a
                           fake clock (python -m plugins._air_control_tests).

WHY THE ENGINE RETURNS ACTIONS INSTEAD OF MOVING THE MOUSE ITSELF
    Everything here is a decision about a person's hand, and every decision has
    to be testable without a camera, a screen or a mouse. The engine says
    ("click", "left", 1); the Dispatcher turns that into a queued OS call. The
    split also keeps OS calls off the Qt thread: the pump runs them on its own
    threads, so a slow accessibility query never freezes the overlay.

THE GESTURES (the voice tool's 'help' and the overlay's hint describe these)
    relaxed hand ............. moves the cursor, like a trackpad in the air
    quick pinch .............. click; two quick pinches = double click
    pinch and move ........... drag
    thumb on middle finger ... right click
    pinch held still 0.9 s ... action menu (right click, keyboard, back, forward,
                               browser, launcher, Mission Control, exit)
    two fingers (V) .......... scroll, with momentum when you let go fast
    flat open hand ........... pause the cursor to reposition the hand (clutch)
    open-hand swipe .......... left/right = next/previous desktop, up = Mission
                               Control, down = app windows / show desktop
    open palm, then a fist ... close the tab or window (Cmd+W / Ctrl+W): the
                               palm opens in place and stays still 0.6 s (an
                               open hand that moved - a clutch - does not
                               count), then the fist is held 0.8 s while a
                               ring counts down; opening the hand cancels
    both hands open, still ... exit (so do Esc and "stop air control")

WHY SO MANY REASONS NOT TO CLICK (the "void" press)
    A false click on the real desktop can send an email, close a document or
    drop a file somewhere. Every place where the cursor JUMPS (a hand found
    again, the end of a clutch, the end of a physical-mouse pause) is followed
    by 0.3 s in which a pinch is ignored, and so is a pinch made while the hand
    moves fast, by a hand that just appeared, or right after the pose changed.
    A missed click costs one more pinch; a wrong one can cost real work.

WHY CLICKS ARE RETRO-DATED
    Closing the thumb and index drags the knuckles a little. The press is
    therefore sent at the cursor position recorded when the fingers STARTED to
    close (HandState.t_aim), looked up in a 0.5 s cursor history, not where the
    cursor drifted to by the time the pinch was recognised.

Coordinates: "picture" = HandState space (0..1 of the camera frame, mirrored
x); "screen units" (u, v) = 0..1 of the overlay's monitor. Nothing here
raises: update() and the Dispatcher catch their own errors.
"""
from __future__ import annotations

import math
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

# Written by the overlay window (Qt thread), read by the voice tool.
STATE = {"open": False, "since": 0.0, "status": "", "error": "", "input_ok": False,
         "keyboard": False}

# ── Tunables (seconds, screen units, picture units) ──────────────────────────
BOX = {"slow": 0.60, "normal": 0.45, "fast": 0.33}   # mapping box width, picture units
GUARD = 0.3                 # no click this long after the cursor jumped
MIN_AGE = 6                 # a hand younger than this (camera frames) cannot click
FAST_PRESS = 0.6            # x box width per second: faster than this, a press is void
POSE_SETTLE = 0.1           # a press this soon after a pose change is void
CLICK_MOVE = 0.025          # pending -> drag beyond this
# A held pinch: still (within DRAG_MOVE) -> the menu at HOLD_TIME; moved past
# DRAG_MOVE, or moving steadily faster than DRAG_SPEED -> a drag. Measured on a
# real hand (Nov 2026): a still hold drifts 0.004-0.02 of the picture in its
# first 0.9 s (1-4.5% of the box); the old 1.2% made the menu a lottery and
# turned most holds into drags on nothing ("the sound, then nothing").
DRAG_MOVE = 0.045           # box units
DRAG_SPEED = 0.05           # picture units / s: faster than a holding hand drifts
DRAG_SPEED_AFTER = 0.2      # s: a quick tap with a wobble stays a click
HOLD_STILL = 0.012          # the menu needs the pinch this still...
HOLD_TIME = 0.9             # ...for this long
HOLD_ARC = 0.3              # the menu's arc starts filling here
DOUBLE_T = 0.40
DOUBLE_D = 0.03
DRAG_IDLE = 8.0             # a drag without movement is released after this
DRAG_IDLE_D = 0.005
MID_TAP = 0.35              # right click: released within this
LOST_PAUSE = 0.25           # no hand for this long: paused
LOST_RELEASE = 0.35         # ...and a held button is released after this
MOUSE_QUIET = 0.12          # no move command for this long before a mouse check
MOUSE_DIFF = 0.02           # real cursor this far from what we sent = the user's mouse
MOUSE_PAUSE = 1.5
V_ENTER = 0.15              # V held this long before scrolling starts
SCROLL_GAIN = 2500.0        # px per screen unit
AXIS_TRAVEL = 0.03
INERTIA_MIN = 0.3           # screen units / s when leaving V
INERTIA_TAU = 0.35
INERTIA_STOP = 20.0         # px/s
EXIT_SPEED = 0.3
EXIT_TIME = 1.0
# The close gesture shares the open palm with the clutch, and Cmd+W cannot be
# undone: the palm must open in place and stay still CRUSH_ARM s (a clutch that
# moved cannot arm), then the fist is held CRUSH_CONFIRM s under a visible
# countdown that opening the hand cancels.
CRUSH_ARM = 0.6
CRUSH_CONFIRM = 0.8
CRUSH_RESET_SPEED = 0.5
# A swipe must cover its distance at this AVERAGE speed (picture units / s):
# an open-hand clutch reposition has a flick-like peak but not its average.
SWIPE_MIN_AVG = 1.5
CRUSH_IN_AIR = False        # see _new_crush
MENU_R = 0.11
MENU_HIT = 0.07
MENU_CENTRE = 0.45          # x MENU_R: letting go this near the middle clicks
DICTATE_HOLD = 0.35
REPEAT_AFTER = 0.5
REPEAT_RATE = 8.0
FIST_STOP = 0.3
TOAST_S = 1.2
HINT_S = 10.0
HISTORY_S = 0.5
MOVE_EPS = 0.0003
CLAMP = (0.0005, 0.9995)

HINT = "Any hand moves · pinch clicks · two fingers scroll · fist pauses · fist flick switches desktop"
OWN_FOCUS = "JARVIS has the keyboard - click into your app first"
SECURE_NOTE = "\U0001F512 Password field: type this one on your keyboard"

MENU_ITEMS = ("right_click", "keyboard", "back", "forward", "browser", "launcher",
              "overview", "close_window", "exit")


def _overview_label() -> str:
    if sys.platform == "darwin":
        return "Mission Control"
    return "Task View" if sys.platform.startswith("win") else "Overview"


def _menu_labels() -> dict:
    return {"right_click": ("Right click", "▣"), "keyboard": ("Keyboard", "⌨"),
            "back": ("Back", "←"), "forward": ("Forward", "→"),
            "browser": ("Browser", "◎"), "launcher": ("Launcher", "▦"),
            "overview": (_overview_label(), "⧉"), "close_window": ("Close window", "⊠"),
            "exit": ("Exit", "✕")}


# ── Settings ─────────────────────────────────────────────────────────────────
@dataclass
class AirSettings:
    sensitivity: str = "normal"
    hand: str = "any"
    pointer_speed: str = "normal"
    precision: bool = True
    scroll_natural: bool = True
    scroll_speed: float = 1.0
    space_swipes: bool = True
    type_bar: bool = True
    keyboard_layout: str = "qwerty"
    show_hands: bool = True
    sounds: bool = True
    learn_words: bool = True      # air-keyboard words feed the suggestions (on disk)


def _flag(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "y"):
            return True
        if s in ("0", "false", "no", "off", "n", ""):
            return False
    return default


def _choice(v, options: tuple, default: str) -> str:
    s = str(v if v is not None else "").strip().lower()
    return s if s in options else default


def settings_from(cfg: dict) -> AirSettings:
    """AirSettings from a stored namespace dict; tolerant of strings and junk."""
    c = cfg if isinstance(cfg, dict) else {}
    try:
        speed = float(str(c.get("scroll_speed", 1.0)).strip() or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if not math.isfinite(speed):
        speed = 1.0
    return AirSettings(
        sensitivity=_choice(c.get("pinch_sensitivity", c.get("sensitivity")),
                            ("gentle", "normal", "firm"), "normal"),
        hand=_choice(c.get("hand"), ("any", "right", "left"), "any"),
        pointer_speed=_choice(c.get("pointer_speed"), tuple(BOX), "normal"),
        precision=_flag(c.get("precision"), True),
        scroll_natural=_flag(c.get("scroll_natural"), True),
        scroll_speed=min(2.0, max(0.5, speed)),
        space_swipes=_flag(c.get("space_swipes"), True),
        type_bar=_flag(c.get("type_bar"), True),
        keyboard_layout=_choice(c.get("keyboard_layout"), ("qwerty", "azerty", "qwertz"),
                                "qwerty"),
        show_hands=_flag(c.get("show_hands"), True),
        sounds=_flag(c.get("sounds"), True),
        learn_words=_flag(c.get("learn_words"), True),
    )


def load_settings() -> AirSettings:
    """The user's Air Control settings (namespace 'air_control'). Never raises."""
    try:
        from memory.config_manager import get_plugin_config
        return settings_from(get_plugin_config("air_control"))
    except Exception:
        return AirSettings()


# ── Data handed between the window and the engine ────────────────────────────
@dataclass
class FieldInfo:
    rect: tuple                       # (u, v, w, h) of the focused text field
    caret: Optional[tuple] = None     # (u, v, w, h) of the caret, when the app reports it
    secure: bool = False
    role: str = ""
    pid: int = 0


@dataclass
class AirView:
    status: str = "no_hand"           # active | no_hand | paused_mouse | blocked | exiting
    cursor: Optional[tuple] = None
    hand: Optional[tuple] = None
    ring: str = "lost"
    pinch: float = 0.0
    progress: float = 0.0
    scroll_axis: str = ""
    menu: Optional[dict] = None
    bar: Optional[dict] = None
    keyboard: Optional[dict] = None
    exit_progress: float = 0.0
    hint: str = ""
    toast: Optional[tuple] = None
    hands_seen: int = 0
    hand_pts: Optional[list] = None


def _g(obj, name, default=None):
    """Duck-typed attribute read: HandStates may be SimpleNamespace stand-ins."""
    try:
        v = getattr(obj, name, default)
    except Exception:
        return default
    return default if v is None else v


def _num(obj, name, default=0.0) -> float:
    try:
        v = float(_g(obj, name, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _inside(r: tuple, u: float, v: float, grow: float = 1.0) -> bool:
    x, y, w, h = r
    cx, cy = x + w / 2, y + h / 2
    return abs(u - cx) <= w * grow / 2 and abs(v - cy) <= h * grow / 2


@dataclass
class _Grip:
    kind: str                  # pending | drag | menu | ui | void | spent
    t0: float
    u0: float
    v0: float
    ax: float = 0.0            # anchor at press, picture coords
    ay: float = 0.0
    target: str = ""           # ui target id
    moved: float = 0.0         # largest distance of the mapped anchor from (u0, v0)
    hold_fired: bool = False
    repeat_next: float = 0.0
    ref: tuple = (0.0, 0.0)    # drag: last point it moved from
    ref_t: float = 0.0


# ── Air keyboard layouts ─────────────────────────────────────────────────────
_LETTERS = {"qwerty": ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
            "azerty": ("azertyuiop", "qsdfghjklm", "wxcvbn"),
            "qwertz": ("qwertzuiop", "asdfghjkl", "yxcvbnm")}
_SPECIAL = {"shift": "⇧", "backspace": "⌫", "123": "123", "abc": "ABC",
            "space": "space", "enter": "⏎", "hide": "⌨↓"}
KEY_W = 0.085
KEY_GAP = 0.006
KB_TOP, KB_BOTTOM = 0.62, 0.98
KB_VGAP = 0.008
SUGGEST_V = 0.58
SUGGEST_W, SUGGEST_H, SUGGEST_GAP = 0.16, 0.04, 0.01
# The keyboard moves to the TOP of the screen when the field it types into
# is in the lower half (chat inputs, terminals): it must never cover the text
# being typed. Its echo strip, on the side nearest the field, shows what the
# keyboard typed so far, so a typo is seen before Enter sends it.
KB_TOP_UP, KB_BOTTOM_UP = 0.10, 0.46
SUGGEST_V_UP = 0.055
ECHO_W, ECHO_H, ECHO_MAX = 0.5, 0.036, 48


def keyboard_rows(layout: str, layer: str) -> list:
    """Rows of (id, label, width in keys)."""
    if layer == "123":
        return [[(c, c, 1.0) for c in "1234567890"],
                [(c, c, 1.0) for c in '-/:;()€&@"'],
                [("shift", _SPECIAL["shift"], 1.5)] + [(c, c, 1.0) for c in ".,?!'"]
                + [("backspace", _SPECIAL["backspace"], 1.5)],
                [("abc", _SPECIAL["abc"], 1.5), ("space", _SPECIAL["space"], 5.0),
                 ("enter", _SPECIAL["enter"], 1.5), ("hide", _SPECIAL["hide"], 1.0)]]
    r1, r2, r3 = _LETTERS.get(layout, _LETTERS["qwerty"])
    return [[(c, c, 1.0) for c in r1],
            [(c, c, 1.0) for c in r2],
            [("shift", _SPECIAL["shift"], 1.5)] + [(c, c, 1.0) for c in r3]
            + [("backspace", _SPECIAL["backspace"], 1.5)],
            [("123", _SPECIAL["123"], 1.5), (",", ",", 1.0), ("space", _SPECIAL["space"], 4.0),
             (".", ".", 1.0), ("enter", _SPECIAL["enter"], 1.5), ("hide", _SPECIAL["hide"], 1.0)]]


def keyboard_keys(layout: str, layer: str, top: float = KB_TOP,
                  bottom: float = KB_BOTTOM) -> list:
    """[(id, label, x, y, w, h)] in screen units, rows centred, v in [top, bottom]
    (by default [0.62, 0.98]; [0.10, 0.46] when the keyboard sits on top)."""
    rows = keyboard_rows(layout, layer)
    rh = (bottom - top - KB_VGAP * (len(rows) - 1)) / len(rows)
    keys = []
    for i, row in enumerate(rows):
        width = sum(m for _, _, m in row) * KEY_W + KEY_GAP * (len(row) - 1)
        x = 0.5 - width / 2
        y = top + i * (rh + KB_VGAP)
        for kid, label, m in row:
            w = m * KEY_W + KEY_GAP * (m - 1 if m > 1 else 0)
            keys.append((kid, label, x, y, w, rh))
            x += w + KEY_GAP
    return keys


# ── The engine ───────────────────────────────────────────────────────────────
class AirEngine:
    """Turns HandStates into OS actions. Call update() on every GUI tick."""

    WORD_STORE = True          # tests set False so nothing is written to disk

    def __init__(self, settings: AirSettings, screen_aspect: float,
                 frame_aspect: float = 16 / 9, clock: Callable[[], float] = time.monotonic):
        self.settings = settings if isinstance(settings, AirSettings) else AirSettings()
        self.clock = clock
        self.screen_aspect = float(screen_aspect) if screen_aspect and screen_aspect > 0 else 16 / 10
        self.frame_aspect = float(frame_aspect) if frame_aspect and frame_aspect > 0 else 16 / 9
        self.bw = BOX.get(self.settings.pointer_speed, BOX["normal"])
        self.bh = self.bw / self.screen_aspect * self.frame_aspect
        self._swipe = self._new_swipe()
        self._crush = self._new_crush()
        self._words = None
        self._events: list = []
        self._queued: list = []
        self._input_ok = True
        self._exited = False
        self._t0: Optional[float] = None
        self._now = 0.0
        # cursor and mapping
        self._seen = False
        self._cursor = (0.5, 0.5)
        self._bx = 0.0
        self._by = 0.0
        self._off = (0.0, 0.0)
        self._prev_m: Optional[tuple] = None
        self._history: deque = deque()
        self._last_sent: Optional[tuple] = None
        self._commanded: deque = deque(maxlen=2)
        self._last_move_cmd_t = -1e9
        self._transition_until = 0.0
        # hands
        self._slot = ""
        self._prim = None
        self._states: list = []
        self._need_acquire = True
        self._last_real_t: Optional[float] = None
        self._last_sample_t: Optional[float] = None
        self._lost_handled = False
        self._np_pose = ""
        self._pose_changed_t = -1e9
        self._seen_t_press = 0.0
        self._mid_down = False
        self._mode = "move"
        self._frozen = False
        self._closeness = 0.0
        self._pinch = "open"
        # gestures
        self._grip: Optional[_Grip] = None
        self._mid: Optional[_Grip] = None
        self._button: Optional[str] = None
        self._last_click: Optional[tuple] = None      # (t, u, v)
        self._menu: Optional[dict] = None
        self._v_t0: Optional[float] = None
        self._v_prev: Optional[tuple] = None
        self._axis = ""
        self._travel = (0.0, 0.0)
        self._pend_px = (0.0, 0.0)
        self._acc_px = (0.0, 0.0)
        self._v_vel = (0.0, 0.0)
        self._inertia: Optional[list] = None          # [vx_px, vy_px, t_last]
        self._queued_px: Optional[tuple] = None       # unlocked scroll left over at V's end
        self._crush_phase = ("idle", 0.0)
        self._fist_t0: Optional[float] = None
        self._fist_fired = False
        self._exit_t0: Optional[float] = None
        self._exit_p = 0.0
        self._paused_until = 0.0
        self._last_rc: Optional[tuple] = None
        self._toast: Optional[tuple] = None
        # text entry
        self._field: Optional[FieldInfo] = None
        self._field_key = None
        self._cand_key = None
        self._pos_n = 0
        self._neg_n = 0
        self._dismissed = None
        self._dictating = False
        self._level = 0.0
        self._preview = ""
        self._bar_hover = ""
        self._kb: Optional[dict] = None               # {"layer", "shift", "manual", "keys"}
        self._kb_hover = ""
        self._kb_pressed: Optional[tuple] = None      # (id, t)
        self._prefix: Optional[str] = ""
        self._sugg: list = []
        self._echo = ""                               # what the air keyboard typed
        self._typed_t = -1e9                          # when we last sent a key or text
        self._cand_secure = False                     # the latest field polled was secure

    # ── detectors (plugins/_gesture_core, written alongside; optional) ──────
    @staticmethod
    def _new_swipe():
        try:
            from plugins._gesture_core import SwipeDetector
            # Fist flicks only. Seen live (Nov 2026): a real resting hand has
            # all four fingers out with 10-25 deg of spread, right on the
            # open/relaxed line, so open-hand swipes fired on fast cursor
            # moves. The fist is the clutch (the cursor is frozen), so a fist
            # flick never drags the pointer with it.
            return SwipeDetector(min_avg=SWIPE_MIN_AVG, poses=("fist",))
        except Exception as e:
            print(f"[air] swipes off: {e}")
            return None

    @staticmethod
    def _new_crush():
        # Off in Air Control: the fist is now the clutch, and "open palm, then
        # fist" is exactly how a user grabs to reposition - it would close
        # windows by accident. "Close window" lives in the pinch-hold menu.
        # (The Holo Desk keeps the crush to close a card.)
        if not CRUSH_IN_AIR:
            return None
        try:
            from plugins._gesture_core import CrushDetector
            return CrushDetector(confirm=CRUSH_CONFIRM, arm_time=CRUSH_ARM, fresh=True)
        except Exception as e:
            print(f"[air] crush off: {e}")
            return None

    def _swipe_update(self, x, y, pose, t):
        d = self._swipe
        if d is None:
            return None
        try:
            return d.update(x, y, pose, t)
        except Exception:
            return None

    def _swipe_reset(self):
        try:
            if self._swipe is not None:
                self._swipe.reset()
        except Exception:
            pass

    def _crush_update(self, pose, x, y, speed, t):
        d = self._crush
        if d is None:
            return ("idle", 0.0)
        try:
            ph, pr = d.update(pose, x, y, speed, t)
            return (str(ph), float(pr or 0.0))
        except Exception:
            return ("idle", 0.0)

    def _crush_reset(self):
        self._crush_phase = ("idle", 0.0)
        try:
            if self._crush is not None:
                self._crush.reset()
        except Exception:
            pass

    @property
    def words(self):
        if self._words is None:
            try:
                from plugins import _air_words
                self._words = _air_words.WordPredictor(
                    _air_words.lang_for(self.settings.keyboard_layout), store=self.WORD_STORE)
            except Exception as e:
                print(f"[air] word suggestions off: {e}")
                self._words = False
        return self._words or None

    @words.setter
    def words(self, value):
        self._words = value

    # ── public API ───────────────────────────────────────────────────────────
    def set_input_ok(self, ok: bool) -> None:
        ok = bool(ok)
        if not ok and self._input_ok:
            self._queued.extend(self._release_held())
        if ok and not self._input_ok:
            self._need_acquire = True     # nothing was sent meanwhile: start from the real cursor
        self._input_ok = ok

    def set_frame_aspect(self, a: float) -> None:
        try:
            a = float(a)
        except (TypeError, ValueError):
            return
        if a > 0 and abs(a - self.frame_aspect) > 1e-6:
            self.frame_aspect = a
            self.bh = self.bw / self.screen_aspect * self.frame_aspect
            if self._prim is not None:
                self._reanchor(self._prim)

    def take_events(self) -> list:
        ev, self._events = self._events, []
        return ev

    def request_exit(self, why: str) -> list:
        if self._exited:
            return []
        self._grip = None
        self._mid = None
        self._button = None
        self._menu = None
        self._inertia = None
        self._exited = True
        self._exit_p = 1.0
        self._events.append(("exit", why))
        return [("release_all",), ("exit", why)]

    def update(self, hands: list, t: float, real_cursor: Optional[tuple] = None) -> list:
        try:
            return self._update(hands, float(t), real_cursor)
        except Exception:
            traceback.print_exc()
            return self._release_held()

    # ── the tick ─────────────────────────────────────────────────────────────
    def _update(self, hands, t, real_cursor) -> list:
        acts: list = []
        self._now = t
        if self._t0 is None:
            self._t0 = t
        if self._exited:
            return acts
        if self._queued:
            acts.extend(self._queued)
            self._queued = []
        rc = None
        if real_cursor is not None:
            try:
                rc = (_clamp(float(real_cursor[0]), *CLAMP), _clamp(float(real_cursor[1]), *CLAMP))
            except (TypeError, ValueError, IndexError):
                rc = None
        states = [h for h in (hands or []) if h is not None]
        self._states = states
        prim = self._pick(states)
        self._prim = prim

        if self._exit_gesture(states, t, acts):
            return acts

        absent = prim is None or bool(_g(prim, "lost", False))
        if absent:
            self._absent(t, acts)
            self._tick_inertia(t, acts)
            return acts

        if not self._need_acquire and self._mouse_check(rc, t, acts):
            return acts

        if self._need_acquire or _g(prim, "slot", "") != self._slot:
            self._acquire(prim, t, rc, acts)
        st = _num(prim, "t", t)
        if self._last_sample_t is None or st != self._last_sample_t:
            dt = 0.0 if self._last_sample_t is None else max(0.0, st - self._last_sample_t)
            self._sample(prim, t, st, dt, acts)
            self._last_sample_t = st
            self._last_real_t = t
        self._timers(prim, t, acts)
        return acts

    def _pick(self, states):
        cands = [s for s in states if _num(s, "age", 0) >= 3]
        want = self.settings.hand
        if want in ("left", "right"):
            cands = [s for s in cands if str(_g(s, "side", "")).lower() == want]
        if not cands:
            return None
        for s in cands:
            if _g(s, "slot", "") == self._slot and self._slot:
                return s
        return max(cands, key=lambda s: _num(s, "age", 0))

    # ── output gate ──────────────────────────────────────────────────────────
    def _out(self, acts: list, a: tuple) -> None:
        kind = a[0]
        if kind in ("release_all", "exit") or (kind == "button" and not a[1]):
            acts.append(a)
            return
        if not self._input_ok or self._exit_p > 0:
            return
        acts.append(a)
        if kind in ("type", "key"):
            self._typed_t = getattr(self, "_now", 0.0) or 0.0
        if kind == "move":
            self._last_sent = (a[1], a[2])
            self._commanded.append((a[1], a[2]))
            self._last_move_cmd_t = self._now

    def _event(self, *e) -> None:
        self._events.append(tuple(e))

    def _toast_(self, text: str) -> None:
        self._toast = (text, self._now)

    def _release_held(self) -> list:
        out = []
        if self._button:
            out.append(("button", False, self._button))
            out.append(("release_all",))
            self._button = None
        if self._grip is not None and self._grip.kind == "drag":
            self._grip.kind = "spent"
        return out

    # ── exit by both open hands ──────────────────────────────────────────────
    def _exit_gesture(self, states, t, acts) -> bool:
        opens = [s for s in states
                 if _num(s, "age", 0) >= 3 and not _g(s, "lost", False)
                 and _g(s, "pose", "") == "open"
                 and math.hypot(_num(s, "vx"), _num(s, "vy")) < EXIT_SPEED]
        if len(opens) < 2:
            self._exit_t0 = None
            self._exit_p = 0.0
            return False
        if self._exit_t0 is None:
            self._exit_t0 = t
        self._exit_p = _clamp((t - self._exit_t0) / EXIT_TIME)
        if self._exit_p >= 1.0:
            acts.extend(self.request_exit("gesture"))
        return True

    # ── hand lost / found ────────────────────────────────────────────────────
    def _absent(self, t, acts) -> None:
        since = self._last_real_t if self._last_real_t is not None else self._t0
        gone = t - since
        if gone >= LOST_PAUSE and not self._lost_handled:
            self._lost_handled = True
            self._need_acquire = True
            self._menu = None
            self._mid = None
            self._v_t0 = None
            self._v_prev = None
            self._axis = ""
            self._inertia = None
            self._fist_t0 = None
            if self._grip is not None and self._grip.kind != "drag":
                self._grip = None
            self._swipe_reset()
            self._crush_reset()
        if gone >= LOST_RELEASE and self._button:
            u, v = self._cursor
            acts.append(("button", False, self._button))
            acts.append(("release_all",))
            self._button = None
            self._grip = None
            self._event("drag_end", u, v)

    def _acquire(self, s, t, rc, acts) -> None:
        if self._button and self._grip is not None and self._grip.kind == "drag" \
                and _g(s, "slot", "") != self._slot and self._slot:
            # A different hand took over in the middle of a drag: let go first.
            acts.append(("button", False, self._button))
            self._button = None
            self._grip = None
        if self._grip is not None and self._grip.kind != "drag":
            self._grip = None
        if rc is not None:
            self._cursor = rc
        elif not self._seen:
            self._cursor = (0.5, 0.5)
        self._seen = True
        self._slot = str(_g(s, "slot", ""))
        self._need_acquire = False
        self._lost_handled = False
        self._history.clear()
        self._last_sent = self._cursor
        self._commanded = deque([self._cursor], maxlen=2)
        self._reanchor(s)
        self._transition_until = t + GUARD
        self._np_pose = {"fist": "fist", "v": "v"}.get(str(_g(s, "pose", "")), "move")
        self._pose_changed_t = _num(s, "t", t)
        if _g(s, "down", False):
            # A pinch that began before we had the hand is not ours to act on.
            self._seen_t_press = _num(s, "t_press", 0.0)
        self._mid_down = _g(s, "pinch_mid", "") == "pressed"
        self._mode = "move"
        self._frozen = False
        self._last_sample_t = None

    # ── the physical mouse ───────────────────────────────────────────────────
    def _mouse_check(self, rc, t, acts) -> bool:
        if self._paused_until:
            if t < self._paused_until:
                if rc is not None:
                    if self._last_rc is not None and \
                            math.hypot(rc[0] - self._last_rc[0], rc[1] - self._last_rc[1]) > 0.004:
                        self._paused_until = t + MOUSE_PAUSE   # still using it
                    self._cursor = rc
                    self._last_rc = rc
                return True
            # Pause over: start again from wherever the user left the cursor.
            self._paused_until = 0.0
            self._need_acquire = True
            self._slot = ""
            return False
        if rc is None or not self._seen or not self._input_ok:
            self._last_rc = rc
            return False
        cmds = list(self._commanded) or [self._cursor]
        if t - self._last_move_cmd_t > MOUSE_QUIET and \
                all(math.hypot(rc[0] - c[0], rc[1] - c[1]) > MOUSE_DIFF for c in cmds):
            acts.extend(self._release_held())
            self._grip = None
            self._mid = None
            self._menu = None
            self._inertia = None
            self._v_t0 = None
            self._v_prev = None
            self._paused_until = t + MOUSE_PAUSE
            self._cursor = rc
            self._last_rc = rc
            self._event("blocked", "mouse")
            return True
        self._last_rc = rc
        return False

    # ── mapping ──────────────────────────────────────────────────────────────
    def _reanchor(self, s) -> None:
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        cu, cv = self._cursor
        self._bx = x - cu * self.bw
        self._by = y - cv * self.bh
        self._off = (0.0, 0.0)
        self._prev_m = (cu, cv)

    def _map_raw(self, x, y) -> tuple:
        return ((x - self._bx) / self.bw, (y - self._by) / self.bh)

    def _track(self, s, dt) -> tuple:
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        mu, mv = self._map_raw(x, y)
        ou, ov = self._off
        if self.settings.precision and self._prev_m is not None and dt > 0:
            ss = math.hypot(_num(s, "vx") / self.bw, _num(s, "vy") / self.bh)
            if ss < 0.15:
                ou -= 0.5 * (mu - self._prev_m[0])
                ov -= 0.5 * (mv - self._prev_m[1])
            elif ss > 0.4:
                k = math.exp(-dt / 0.25)
                ou *= k
                ov *= k
        cu, cv = mu + ou, mv + ov
        # Rubber box: the box follows the hand at the edges, so the cursor
        # stops at the edge and comes back the moment the hand does.
        if cu < 0.0 or cu > 1.0:
            ex = cu - _clamp(cu)
            self._bx += ex * self.bw
            mu -= ex
            cu = _clamp(cu)
        if cv < 0.0 or cv > 1.0:
            ey = cv - _clamp(cv)
            self._by += ey * self.bh
            mv -= ey
            cv = _clamp(cv)
        self._off = (ou, ov)
        self._prev_m = (mu, mv)
        return (_clamp(cu, *CLAMP), _clamp(cv, *CLAMP))

    def _aim(self, ta: float) -> tuple:
        best = None
        for ht, u, v in self._history:
            if ht <= ta + 1e-6:
                best = (u, v)
            else:
                break
        if best is None:
            best = (self._history[0][1], self._history[0][2]) if self._history else self._cursor
        return best

    def _send_move(self, acts) -> None:
        u, v = self._cursor
        last = self._last_sent
        if last is None or math.hypot(u - last[0], v - last[1]) > MOVE_EPS:
            self._out(acts, ("move", u, v))

    # ── one new camera sample of the primary hand ────────────────────────────
    def _sample(self, s, t, st, dt, acts) -> None:
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        vx, vy = _num(s, "vx"), _num(s, "vy")
        speed = math.hypot(vx, vy)
        pose = str(_g(s, "pose", "none"))
        self._closeness = _num(s, "closeness")
        self._pinch = str(_g(s, "pinch", "open"))
        # Only a change of what the hand DOES counts (move / clutch / scroll):
        # open <-> relaxed <-> point flicker on a real hand and all just move.
        kind = {"fist": "fist", "v": "v"}.get(pose, "move")
        if pose not in ("pinch", "pinch_mid") and kind != self._np_pose:
            self._np_pose = kind
            self._pose_changed_t = st
        down = bool(_g(s, "down", False))
        tp = _num(s, "t_press", 0.0)
        press_edge = down and tp > 0 and tp != self._seen_t_press
        if press_edge:
            self._seen_t_press = tp
        mid_down = _g(s, "pinch_mid", "") == "pressed"
        mid_edge = mid_down and not self._mid_down
        self._mid_down = mid_down

        swipe = self._swipe_update(x, y, pose, st)
        modal = self._kb is not None or self._dictating
        # Every hand shape moves the cursor except the fist (the clutch: lift
        # the mouse) and the V (scroll). An open hand used to be the clutch;
        # live traces showed real resting hands flicker between open and
        # relaxed, freezing the cursor at random and voiding the next pinch.
        pose_mode = {"fist": "clutch", "v": "v"}.get(pose, "move")
        if self._mode == "clutch" and pose_mode != "clutch":
            # Leaving the clutch moves the cursor again: no click for a moment,
            # including a pinch made straight out of the open hand.
            self._transition_until = t + GUARD

        freeze = False
        track_quiet = False          # track internally but send no OS moves
        mode = "grip"
        g = self._grip
        if g is not None and not down:
            self._grip_release(g, s, t, acts)
            g = None
            freeze = True            # the click point stays put this frame
            mode = "after"
        elif g is not None:
            freeze, track_quiet = self._grip_hold(g, s, t, acts)
        elif press_edge:
            g = self._grip_press(s, t, st, acts)
            freeze = g is not None and g.kind == "pending"
        elif self._mid is not None:
            m = self._mid
            if not mid_down:
                self._mid_release(m, s, t, acts)
                mode = "after"
            else:
                m.moved = max(m.moved, math.hypot((x - m.ax) / self.bw, (y - m.ay) / self.bh))
            freeze = True
        elif mid_edge:
            self._mid_press(s, t, st, speed)
            freeze = True
        else:
            mode = pose_mode

        # Leaving a pose mode.
        if mode != self._mode and self._mode == "v":
            self._leave_v()
        if mode in ("clutch", "v"):
            freeze = True
        if mode != "v":
            self._v_t0 = None
            self._v_prev = None
            self._axis = ""

        # Gestures that belong to the pose.
        no_grip = self._grip is None and self._mid is None and mode != "after"
        if swipe and no_grip:
            self._on_swipe(swipe, acts)
        if no_grip and not modal:
            if speed > CRUSH_RESET_SPEED:
                if self._crush_phase[0] != "idle":
                    self._crush_reset()
                # still shown to the detector, so it knows this open hand moved
                # (a clutch) and must not arm when it stops
                self._crush_update(pose, x, y, speed, st)
            else:
                ph, pr = self._crush_update(pose, x, y, speed, st)
                if ph == "fired":
                    self._crush_reset()
                    self._out(acts, ("close_window",))
                    self._event("crush")
                    self._toast_("Close window")
                else:
                    self._crush_phase = ("idle", 0.0) if ph == "cancelled" else (ph, pr)
        elif self._crush_phase[0] != "idle" or modal:
            self._crush_reset()
        if mode == "clutch" and pose == "fist" and self._dictating:
            if self._fist_t0 is None:
                self._fist_t0 = t
        else:
            self._fist_t0 = None
            self._fist_fired = False
        if mode == "v":
            self._scroll_sample(s, st, acts)
        else:
            self._v_vel = (0.0, 0.0)

        # The cursor.
        if freeze:
            self._frozen = True
        else:
            if self._frozen:
                self._reanchor(s)
                self._frozen = False
            self._cursor = self._track(s, dt)
            if not track_quiet:
                self._send_move(acts)
        if mode != "after":
            self._mode = mode
        else:
            self._mode = "move"
        self._history.append((st, self._cursor[0], self._cursor[1]))
        while self._history and self._history[0][0] < st - HISTORY_S:
            self._history.popleft()

    # ── pinch grips ──────────────────────────────────────────────────────────
    def _void(self, s, t, st, speed) -> str:
        if t < self._transition_until:
            return "jump"
        if _num(s, "age", 0) < MIN_AGE:
            return "new hand"
        if speed > FAST_PRESS * self.bw:
            return "fast"
        if st - self._pose_changed_t < POSE_SETTLE:
            return "pose"
        return ""

    def _grip_press(self, s, t, st, acts) -> Optional[_Grip]:
        speed = math.hypot(_num(s, "vx"), _num(s, "vy"))
        u0, v0 = self._aim(_num(s, "t_aim", st))
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        self._inertia = None
        why = self._void(s, t, _num(s, "t_press", st), speed)
        if why:
            self._grip = _Grip("void", t, u0, v0, x, y)
            self._event("blocked", why)
            return self._grip
        hit = self._ui_hit(u0, v0)
        if hit is not None:
            self._grip = _Grip("ui", t, u0, v0, x, y, target=hit)
            self._ui_press(self._grip, t, acts)
            return self._grip
        self._grip = _Grip("pending", t, u0, v0, x, y)
        self._cursor = (u0, v0)
        self._reanchor(s)
        self._frozen = True
        self._event("pinch_tick", u0, v0)
        return self._grip

    def _grip_hold(self, g: _Grip, s, t, acts) -> tuple:
        """(freeze, quiet) for this frame."""
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        if g.kind == "pending":
            d = math.hypot((x - g.ax) / self.bw, (y - g.ay) / self.bh)
            g.moved = max(g.moved, d)
            speed = math.hypot(_num(s, "vx"), _num(s, "vy"))
            steady = t - g.t0 >= DRAG_SPEED_AFTER and speed > DRAG_SPEED
            if d > DRAG_MOVE or (d > HOLD_STILL and steady):
                g.kind = "drag"
                self._out(acts, ("move", g.u0, g.v0))
                self._out(acts, ("button", True, "left"))
                self._button = "left"
                g.ref, g.ref_t = (g.u0, g.v0), t
                self._event("drag_start", g.u0, g.v0)
                self._caret_moved()
                self._frozen = False      # the box is anchored at the press: no re-anchor
                return False, False
            return True, False
        if g.kind == "menu":
            return False, True
        return False, False

    def _grip_release(self, g: _Grip, s, t, acts) -> None:
        self._grip = None
        if g.kind == "pending":
            if t - g.t0 <= HOLD_TIME:
                lc = self._last_click
                count = 2 if (lc and t - lc[0] < DOUBLE_T and
                              math.hypot(g.u0 - lc[1], g.v0 - lc[2]) < DOUBLE_D) else 1
                self._out(acts, ("move", g.u0, g.v0))
                self._out(acts, ("click", "left", count))
                self._event("click", g.u0, g.v0, count)
                self._caret_moved()
                self._last_click = None if count == 2 else (t, g.u0, g.v0)
            self._cursor = (g.u0, g.v0)
            self._reanchor(s)
        elif g.kind == "drag":
            if self._button:
                self._out(acts, ("button", False, self._button))
                self._button = None
            self._event("drag_end", self._cursor[0], self._cursor[1])
            self._frozen = False
        elif g.kind == "menu":
            m = self._menu or {}
            item = m.get("hover", "")
            centre = False
            if not item and m:
                u, v = self._cursor
                centre = math.hypot((u - m["x"]) * self.screen_aspect, v - m["y"]) \
                    < MENU_R * MENU_CENTRE
            self._menu = None
            self._cursor = (g.u0, g.v0)
            self._reanchor(s)
            if item:
                self._menu_act(item, g, t, acts)
            elif centre:
                # A slow click: live traces (Nov 2026) showed pinches held
                # 1.1-1.4 s that opened the menu when a click was meant.
                # Let go in the middle = the click; away from it = cancel.
                self._out(acts, ("move", g.u0, g.v0))
                self._out(acts, ("click", "left", 1))
                self._event("click", g.u0, g.v0, 1)
                self._caret_moved()
                self._last_click = (t, g.u0, g.v0)
        elif g.kind == "ui":
            if g.target == "bar:dictate":
                self._out(acts, ("dictate", "hold_stop" if g.hold_fired else "toggle"))
        if g.kind in ("pending", "menu"):
            self._frozen = False

    def _menu_act(self, item, g: _Grip, t, acts) -> None:
        self._event("menu", item)
        if item == "right_click":
            self._out(acts, ("move", g.u0, g.v0))
            self._out(acts, ("click", "right", 1))
            self._event("right_click", g.u0, g.v0)
            self._caret_moved()
        elif item == "keyboard":
            self.open_keyboard(t)
        elif item in ("back", "forward"):
            self._out(acts, ("nav", item))
        elif item == "browser":
            self._out(acts, ("browser",))
        elif item == "launcher":
            self._out(acts, ("launcher",))
        elif item == "overview":
            self._out(acts, ("overview",))
        elif item == "close_window":
            self._out(acts, ("close_window",))
            self._toast_("Close window")
        elif item == "exit":
            acts.extend(self.request_exit("menu"))

    def _open_menu(self, g: _Grip) -> None:
        sa = self.screen_aspect
        ru = MENU_R / sa
        cx = _clamp(g.u0, ru + 0.03, 1 - ru - 0.03)
        cy = _clamp(g.v0, MENU_R + 0.04, 1 - MENU_R - 0.04)
        labels = _menu_labels()
        items = []
        for i, iid in enumerate(MENU_ITEMS):
            a = -math.pi / 2 + i * 2 * math.pi / len(MENU_ITEMS)
            label, glyph = labels[iid]
            items.append((iid, label, glyph, cx + ru * math.cos(a), cy + MENU_R * math.sin(a)))
        self._menu = {"x": cx, "y": cy, "r": MENU_R, "items": items, "hover": ""}
        g.kind = "menu"
        self._event("menu_open", cx, cy)

    def _menu_hover(self) -> None:
        m = self._menu
        if not m:
            return
        u, v = self._cursor
        best, bd = "", MENU_HIT
        for iid, _l, _g2, ix, iy in m["items"]:
            d = math.hypot((u - ix) * self.screen_aspect, v - iy)
            if d < bd:
                best, bd = iid, d
        m["hover"] = best

    def _mid_press(self, s, t, st, speed) -> None:
        u0, v0 = self._aim(st - 0.1)
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        why = self._void(s, t, st, speed)
        self._mid = _Grip("void" if why else "pending", t, u0, v0, x, y)
        if why:
            self._event("blocked", why)
        self._cursor = (u0, v0)

    def _mid_release(self, m: _Grip, s, t, acts) -> None:
        self._mid = None
        if m.kind == "pending" and t - m.t0 <= MID_TAP and m.moved < CLICK_MOVE:
            if self._ui_hit(m.u0, m.v0) is None:
                self._out(acts, ("move", m.u0, m.v0))
                self._out(acts, ("click", "right", 1))
                self._event("right_click", m.u0, m.v0)
                self._caret_moved()
        self._cursor = (m.u0, m.v0)
        self._reanchor(s)
        self._frozen = False

    # ── swipes ───────────────────────────────────────────────────────────────
    def _on_swipe(self, d: str, acts) -> None:
        self._crush_reset()
        if self._dictating and d == "left":
            self._out(acts, ("dictate", "undo"))
            self._event("swipe", d, "Undo")
            self._toast_("↶ Undo")
            return
        if self._kb is not None and d == "down":
            self.close_keyboard()
            self._event("swipe", d, "Keyboard")
            return
        if self._kb is not None or self._dictating or not self.settings.space_swipes:
            return
        if d == "left":
            act, label = ("space", "right"), "Desktop →"
        elif d == "right":
            act, label = ("space", "left"), "← Desktop"
        elif d == "up":
            act, label = ("overview",), _overview_label()
        elif d == "down":
            if sys.platform == "darwin":
                act, label = ("app_windows",), "App windows"
            else:
                act, label = ("show_desktop",), "Show desktop"
        else:
            return
        self._out(acts, act)
        self._event("swipe", d, label)
        self._toast_(label)

    # ── scrolling ────────────────────────────────────────────────────────────
    def _scroll_sample(self, s, st, acts) -> None:
        x, y = _num(s, "x", 0.5), _num(s, "y", 0.5)
        if self._v_t0 is None:
            self._v_t0 = st
            self._v_prev = None
            self._inertia = None
        if st - self._v_t0 < V_ENTER:
            return
        gain = SCROLL_GAIN * float(self.settings.scroll_speed or 1.0)
        sign = 1.0 if self.settings.scroll_natural else -1.0
        vu, vv = _num(s, "vx") / self.bw, _num(s, "vy") / self.bh
        if self._v_prev is None:
            self._v_prev = (x, y)
            self._axis = ""
            self._travel = (0.0, 0.0)
            self._pend_px = (0.0, 0.0)
            self._acc_px = (0.0, 0.0)
            self._v_vel = self._lock(sign * vu * gain, sign * vv * gain)
            return
        du = (x - self._v_prev[0]) / self.bw
        dv = (y - self._v_prev[1]) / self.bh
        self._v_prev = (x, y)
        dx, dy = sign * du * gain, sign * dv * gain
        if not self._axis:
            tu, tv = self._travel[0] + du, self._travel[1] + dv
            self._travel = (tu, tv)
            px, py = self._pend_px[0] + dx, self._pend_px[1] + dy
            if math.hypot(tu, tv) >= AXIS_TRAVEL:
                if abs(tu) > 2 * abs(tv):
                    self._axis = "h"
                elif abs(tv) > 2 * abs(tu):
                    self._axis = "v"
                else:
                    self._axis = "both"
                px, py = self._lock(px, py)
                self._pend_px = (0.0, 0.0)
                self._add_px(px, py, acts)
            else:
                self._pend_px = (px, py)
        else:
            self._add_px(*self._lock(dx, dy), acts)
        self._v_vel = self._lock(sign * vu * gain, sign * vv * gain)

    def _lock(self, dx, dy) -> tuple:
        if self._axis == "h":
            return dx, 0.0
        if self._axis == "v":
            return 0.0, dy
        return dx, dy

    def _add_px(self, dx, dy, acts) -> None:
        ax, ay = self._acc_px[0] + dx, self._acc_px[1] + dy
        ox = float(math.trunc(ax)) if abs(ax) >= 1 else 0.0
        oy = float(math.trunc(ay)) if abs(ay) >= 1 else 0.0
        if ox or oy:
            self._out(acts, ("scroll", ox, oy))
        self._acc_px = (ax - ox, ay - oy)

    def _leave_v(self) -> None:
        if self._v_prev is not None:
            if self._pend_px != (0.0, 0.0):
                self._queued_px = self._pend_px
            vx, vy = self._v_vel
            gain = SCROLL_GAIN * float(self.settings.scroll_speed or 1.0)
            if math.hypot(vx, vy) / gain > INERTIA_MIN:
                self._inertia = [vx, vy, self._now]
        self._v_t0 = None
        self._v_prev = None
        self._pend_px = (0.0, 0.0)

    def _tick_inertia(self, t, acts) -> None:
        it = self._inertia
        if not it:
            return
        dt = t - it[2]
        if dt <= 0:
            return
        k = math.exp(-dt / INERTIA_TAU)
        dx = it[0] * INERTIA_TAU * (1 - k)
        dy = it[1] * INERTIA_TAU * (1 - k)
        it[0] *= k
        it[1] *= k
        it[2] = t
        self._add_px(dx, dy, acts)
        if math.hypot(it[0], it[1]) < INERTIA_STOP:
            self._inertia = None

    # ── time-driven parts (every tick) ───────────────────────────────────────
    def _timers(self, s, t, acts) -> None:
        g = self._grip
        if g is not None:
            if g.kind == "pending" and g.moved < DRAG_MOVE and t - g.t0 >= HOLD_TIME:
                self._open_menu(g)
            elif g.kind == "drag":
                c = self._cursor
                if math.hypot(c[0] - g.ref[0], c[1] - g.ref[1]) > DRAG_IDLE_D:
                    g.ref, g.ref_t = c, t
                elif t - g.ref_t >= DRAG_IDLE:
                    if self._button:
                        self._out(acts, ("button", False, self._button))
                        self._button = None
                    self._event("drag_end", c[0], c[1])
                    g.kind = "spent"
            elif g.kind == "ui":
                if g.target == "bar:dictate" and not g.hold_fired and t - g.t0 >= DICTATE_HOLD:
                    g.hold_fired = True
                    self._out(acts, ("dictate", "hold_start"))
                elif g.target in ("bar:backspace", "key:backspace") and t >= g.repeat_next:
                    self._out(acts, ("key", "backspace", (), 1))
                    self._backspace_prefix()
                    g.repeat_next = t + 1.0 / REPEAT_RATE
        if self._menu:
            self._menu_hover()
        if self._fist_t0 is not None and self._dictating and not self._fist_fired \
                and t - self._fist_t0 >= FIST_STOP:
            self._fist_fired = True
            self._out(acts, ("dictate", "stop"))
        pend = self._queued_px
        if pend:
            self._queued_px = None
            self._add_px(pend[0], pend[1], acts)
        self._tick_inertia(t, acts)
        if self._kb is not None:
            self._update_kb_hover()
        hit = self._bar_hit(*self._cursor) if self._bar_visible() else None
        self._bar_hover = hit[4:] if hit else ""

    # ── text entry: the type bar ─────────────────────────────────────────────
    def set_field(self, field: Optional[FieldInfo], t: float) -> None:
        try:
            self._set_field(field, t)
        except Exception:
            traceback.print_exc()

    def _set_field(self, field, t) -> None:
        if field is None:
            self._neg_n += 1
            self._pos_n = 0
            self._cand_key = None
            if self._neg_n >= 3 and self._field is not None:
                self._field = None
                self._field_key = None
                self._dismissed = None
                self._prefix = ""
                self._update_sugg()
                if self._kb is not None and not self._kb.get("manual"):
                    self.close_keyboard()
            return
        self._neg_n = 0
        secure = bool(_g(field, "secure", False))
        self._cand_secure = secure
        if secure:
            # letters already pinched may be the start of a password: never
            # learned, never offered as completions
            self._prefix = ""
            self._sugg = []
            self._echo = ""
        key = (int(_num(field, "pid", 0)), str(_g(field, "role", "")))
        if key == self._cand_key:
            self._pos_n += 1
        else:
            self._cand_key = key
            self._pos_n = 1
        if self._field is not None and key == self._field_key:
            # our own typing moves the caret (right, back on ⌫, down on a
            # wrap): only a jump while we are not typing means "moved"
            if self._jumped(self._field, field, t - self._typed_t < 0.6):
                # same app and role, but another field or the caret jumped
                # (a click, the physical keyboard): the word is unknown
                self._caret_moved()
            self._field = field
        elif self._pos_n >= 2:
            if key != self._field_key:
                self._prefix = ""
                self._update_sugg()
                self._dismissed = None
            self._field = field
            self._field_key = key
        if self._field is not None and bool(_g(self._field, "secure", False)) and self._kb:
            self.close_keyboard()

    @staticmethod
    def _jumped(old, new, typing: bool = False) -> bool:
        try:
            ro, rn = tuple(_g(old, "rect", ())), tuple(_g(new, "rect", ()))
            if len(ro) >= 2 and len(rn) >= 2 and \
                    math.hypot(ro[0] - rn[0], ro[1] - rn[1]) > 0.01:
                return True
            if typing:
                return False
            co, cn = _g(old, "caret", None), _g(new, "caret", None)
            if co and cn:
                co, cn = tuple(co), tuple(cn)
                # another line, or backwards: typing only moves it right
                if abs(co[1] - cn[1]) > max(0.5 * float(co[3] or 0.0), 0.006) \
                        or cn[0] < co[0] - 0.004:
                    return True
        except Exception:
            return False
        return False

    def _bar_visible(self) -> bool:
        return (self._field is not None and bool(self.settings.type_bar)
                and self._dismissed != self._field_key)

    def _bar_buttons(self) -> list:
        if bool(_g(self._field, "secure", False)):
            return [("close", "✕", "Close")]
        if self._dictating:
            return [("stop", "✓", "Done"), ("undo", "↶", "Undo"),
                    ("backspace", "⌫", "Delete"), ("enter", "⏎", "Send"),
                    ("close", "✕", "Close")]
        return [("dictate", "\U0001F399", "Dictate"), ("keys", "⌨", "Keys"),
                ("backspace", "⌫", "Delete"), ("enter", "⏎", "Enter"),
                ("close", "✕", "Close")]

    def _bar_geom(self) -> Optional[dict]:
        if not self._bar_visible():
            return None
        f = self._field
        rect = tuple(_g(f, "rect", (0.4, 0.4, 0.2, 0.04)))
        caret = _g(f, "caret", None)
        ref = tuple(caret) if caret else rect
        secure = bool(_g(f, "secure", False))
        h = 0.06
        bw = 0.06 / self.screen_aspect * 1.1
        gap = 0.008
        btns = self._bar_buttons()
        w = len(btns) * bw + (len(btns) + 1) * gap
        if secure:
            w = max(w, 0.30)
        cx = (ref[0] + ref[2] / 2) if caret else (rect[0] + rect[2] / 2)
        # ABOVE the field (or its caret line in a tall text area): the strip
        # right under a field is where its own suggestion list drops down
        # (address bars, search boxes, "To:" fields), and a bar there ate
        # those pinches. Below only when there is no room above.
        line = ref if (caret and rect[3] > 0.08) else rect
        y = line[1] - 0.012 - h
        if y < 0.005:
            y = line[1] + line[3] + 0.012
        x = _clamp(cx - w / 2, 0.005, max(0.005, 0.995 - w))
        y = _clamp(y, 0.005, 0.995 - h)
        bh = h - 2 * gap
        out = []
        bx = x + w - len(btns) * (bw + gap) if secure else x + gap
        for bid, glyph, label in btns:
            out.append((bid, glyph, label, bx, y + gap, bw, bh))
            bx += bw + gap
        state = "secure" if secure else "dictating" if self._dictating else \
            "keys" if self._kb is not None else "idle"
        return {"x": x, "y": y, "w": w, "h": h, "state": state, "buttons": out,
                "hover": self._bar_hover, "level": self._level, "preview": self._preview,
                "note": SECURE_NOTE if secure else ""}

    def _bar_hit(self, u, v) -> Optional[str]:
        """'bar:<id>' only for a pinch squarely on a button. The bar's
        background and the gaps pass the pinch through to the app below, so
        the bar never silently eats a click meant for what it covers."""
        geo = self._bar_geom()
        if not geo:
            return None
        for bid, _gl, _lb, x, y, w, h in geo["buttons"]:
            if _inside((x, y, w, h), u, v):
                return "bar:" + bid
        return None

    # ── text entry: the air keyboard ─────────────────────────────────────────
    def open_keyboard(self, t: float) -> None:
        self._open_keyboard(t, manual=True)

    def _open_keyboard(self, t, manual: bool) -> None:
        if self._field is not None and bool(_g(self._field, "secure", False)):
            self._toast_(SECURE_NOTE)
            return
        if self._kb is None:
            side = self._kb_side_for(self._field)
            self._kb = {"layer": "abc", "shift": False, "manual": manual, "side": side,
                        "keys": self._kb_keys("abc", side)}
            self._kb_hover = ""
            self._prefix = ""
            self._echo = ""
            self._update_sugg()
        else:
            self._kb["manual"] = self._kb.get("manual") or manual

    def close_keyboard(self) -> None:
        self._kb = None
        self._kb_hover = ""

    @staticmethod
    def _kb_side_for(field) -> str:
        """'top' when the field (or its caret) is in the lower half of the
        screen, so the keyboard never covers the text it types."""
        if field is None:
            return "bottom"
        try:
            ref = _g(field, "caret", None) or _g(field, "rect", None)
            ref = tuple(ref)
            return "top" if ref[1] + ref[3] / 2 > 0.5 else "bottom"
        except Exception:
            return "bottom"

    def _kb_keys(self, layer: str, side: str) -> list:
        if side == "top":
            return keyboard_keys(self.settings.keyboard_layout, layer, KB_TOP_UP, KB_BOTTOM_UP)
        return keyboard_keys(self.settings.keyboard_layout, layer)

    def _kb_side(self) -> str:
        return str((self._kb or {}).get("side") or "bottom")

    def _echo_rect(self) -> tuple:
        """The echo strip, on the keyboard's side nearest the field."""
        if self._kb_side() == "top":
            v = KB_BOTTOM_UP + 0.012
        else:
            v = SUGGEST_V - SUGGEST_H / 2 - 0.012 - ECHO_H
        return (0.5 - ECHO_W / 2, v, ECHO_W, ECHO_H)

    def _kb_rect(self) -> Optional[tuple]:
        if self._kb is None:
            return None
        keys = self._kb["keys"]
        x0 = min(k[2] for k in keys) - 0.01
        x1 = max(k[2] + k[4] for k in keys) + 0.01
        er = self._echo_rect()
        if self._kb_side() == "top":
            y0 = SUGGEST_V_UP - SUGGEST_H / 2 - 0.01
            y1 = er[1] + er[3] + 0.01
        else:
            y0 = er[1] - 0.01
            y1 = KB_BOTTOM + 0.01
        return (x0, y0, x1 - x0, y1 - y0)

    def _sugg_rects(self) -> list:
        n = 3
        total = n * SUGGEST_W + (n - 1) * SUGGEST_GAP
        x = 0.5 - total / 2
        y = (SUGGEST_V_UP if self._kb_side() == "top" else SUGGEST_V) - SUGGEST_H / 2
        return [(x + i * (SUGGEST_W + SUGGEST_GAP), y, SUGGEST_W, SUGGEST_H) for i in range(n)]

    def _key_at(self, u, v) -> str:
        if self._kb is None:
            return ""
        for kid, _l, x, y, w, h in self._kb["keys"]:
            if _inside((x, y, w, h), u, v):
                return kid
        # In a gap: the nearest key within the hover margin.
        best, bd = "", 9.0
        for kid, _l, x, y, w, h in self._kb["keys"]:
            if _inside((x, y, w + 0.3 * KEY_W, h + 0.3 * h), u, v):
                d = math.hypot(u - (x + w / 2), v - (y + h / 2))
                if d < bd:
                    best, bd = kid, d
        return best

    def _update_kb_hover(self) -> None:
        u, v = self._cursor
        cur = self._kb_hover
        if cur:
            for kid, _l, x, y, w, h in self._kb["keys"]:
                if kid == cur:
                    m = 0.15 * KEY_W
                    if x - m <= u <= x + w + m and y - 0.15 * h <= v <= y + h + 0.15 * h:
                        return
                    break
        self._kb_hover = self._key_at(u, v)

    def _ui_hit(self, u, v) -> Optional[str]:
        """What overlay UI is at (u, v): 'bar:<id>', 'key:<id>', 'suggest:<i>', 'kb:', or None."""
        b = self._bar_hit(u, v)
        if b is not None:
            return b
        r = self._kb_rect()
        if r is not None and _inside(r, u, v):
            for i, sr in enumerate(self._sugg_rects()):
                if i < len(self._sugg) and _inside(sr, u, v):
                    return f"suggest:{i}"
            k = self._key_at(u, v)
            return f"key:{k}" if k else "kb:"
        return None

    def _ui_press(self, g: _Grip, t, acts) -> None:
        target = g.target
        kind, _, tid = target.partition(":")
        if not tid:
            return
        self._event("ui_press", tid if kind != "suggest" else target)
        if kind == "bar":
            if tid == "dictate":
                return                       # acts on release (tap) or after 0.35 s (hold)
            if tid == "backspace":
                self._out(acts, ("key", "backspace", (), 1))
                self._backspace_prefix()
                g.repeat_next = t + REPEAT_AFTER
            elif tid == "enter":
                self._out(acts, ("key", "enter", (), 1))
                self._end_word(learn=False)
                self._echo = ""
            elif tid == "keys":
                if self._kb is None:
                    self._open_keyboard(t, manual=False)
                else:
                    self.close_keyboard()
            elif tid == "close":
                if self._dictating:
                    self._out(acts, ("dictate", "stop"))
                self._dismissed = self._field_key
                if self._kb is not None and not self._kb.get("manual"):
                    self.close_keyboard()
            elif tid == "stop":
                self._out(acts, ("dictate", "stop"))
            elif tid == "undo":
                self._out(acts, ("dictate", "undo"))
            return
        if kind == "suggest":
            try:
                word = self._sugg[int(tid)]
            except (ValueError, IndexError):
                return
            if self._prefix is None:
                return                       # the caret moved: the word is unknown
            n = len(self._prefix)
            if n:
                self._out(acts, ("key", "backspace", (), n))
                self._echo = self._echo[:-n] if len(self._echo) >= n else ""
            self._out(acts, ("type", word + " "))
            self._echo_add(word + " ")
            self._learn(word)
            self._prefix = ""
            self._update_sugg()
            return
        if kind == "key" and self._kb is not None:
            self._kb_pressed = (tid, t)
            kb = self._kb
            if tid == "backspace":
                self._out(acts, ("key", "backspace", (), 1))
                self._echo = self._echo[:-1]
                self._backspace_prefix()
                g.repeat_next = t + REPEAT_AFTER
            elif tid == "space":
                self._out(acts, ("type", " "))
                self._echo_add(" ")
                self._end_word()
            elif tid == "enter":
                self._out(acts, ("key", "enter", (), 1))
                # a word ended by Enter is often a password or a command
                # (a terminal prompt that does not report itself as secure)
                self._end_word(learn=False)
                self._echo = ""
            elif tid == "shift":
                kb["shift"] = not kb["shift"]
            elif tid in ("123", "abc"):
                kb["layer"] = "123" if tid == "123" else "abc"
                kb["keys"] = self._kb_keys(kb["layer"], self._kb_side())
                self._kb_hover = ""
            elif tid == "hide":
                self.close_keyboard()
            else:
                ch = tid
                if kb["shift"] and ch.isalpha():
                    ch = ch.upper()
                    kb["shift"] = False
                self._out(acts, ("type", ch))
                self._echo_add(ch)
                if ch.isalnum() or ch == "'":
                    if self._prefix is not None:
                        self._prefix += ch
                else:
                    self._end_word()
                self._update_sugg()

    def _end_word(self, learn: bool = True) -> None:
        if learn and self._prefix and len(self._prefix) >= 2:
            self._learn(self._prefix)
        self._prefix = ""
        self._update_sugg()

    def _caret_moved(self) -> None:
        """A click, drag or jump put the caret somewhere we did not type: the
        word in progress is unknown now (None), so a suggestion can never
        backspace over text the user did not type here."""
        self._prefix = None
        self._sugg = []
        self._echo = ""

    def _echo_add(self, text: str) -> None:
        self._echo = (self._echo + text)[-ECHO_MAX:]

    def _learn_ok(self) -> bool:
        """Words are learned only from a field known NOT to be a password
        field: none of the fields polled recently was secure, and learning
        is on. Without a field (a terminal's sudo prompt reports none, or not
        as secure) nothing is learned."""
        if not bool(getattr(self.settings, "learn_words", True)):
            return False
        f = self._field
        if f is None or bool(_g(f, "secure", False)) or self._cand_secure:
            return False
        return True

    def _backspace_prefix(self) -> None:
        if self._prefix:
            self._prefix = self._prefix[:-1]
        else:
            self._prefix = None        # past the word's start: we no longer know it
        self._update_sugg()

    def _learn(self, text: str) -> None:
        if not self._learn_ok():
            return
        w = self.words
        if w is not None:
            try:
                w.learn(text)
            except Exception:
                pass

    def _update_sugg(self) -> None:
        self._sugg = []
        if not self._prefix or self._kb is None:
            return
        w = self.words
        if w is None:
            return
        try:
            self._sugg = list(w.suggest(self._prefix, 3))[:3]
        except Exception:
            self._sugg = []

    def on_dictation(self, kind: str, data: dict) -> None:
        d = data if isinstance(data, dict) else {}
        if kind == "listening":
            self._dictating = True
            self._preview = ""
        elif kind == "level":
            try:
                self._level = _clamp(float(d.get("level", 0.0)))
            except (TypeError, ValueError):
                pass
        elif kind == "typed":
            # Shown on the bar only. Dictated words are NOT learned: what is
            # spoken into an email or a form (names, addresses) must not pile
            # up on disk as vocabulary (core/dictation.py PRIVACY).
            self._preview = str(d.get("preview", "") or "")
        elif kind == "failed":
            why = str(d.get("why", "") or "dictation failed")
            self._toast_(why)
        elif kind == "stopped":
            self._dictating = False
            self._level = 0.0
            self._fist_t0 = None

    # ── what the overlay draws ───────────────────────────────────────────────
    def view(self, t: float) -> AirView:
        try:
            return self._view(t)
        except Exception:
            traceback.print_exc()
            return AirView()

    def _status(self, t) -> str:
        if self._exited:
            return "exiting"
        if not self._input_ok:
            return "blocked"
        if self._paused_until and t < self._paused_until:
            return "paused_mouse"
        prim = self._prim
        since = self._last_real_t if self._last_real_t is not None else (self._t0 or t)
        if (prim is None or _g(prim, "lost", False)) and t - since >= LOST_PAUSE:
            return "no_hand"
        if self._last_real_t is None:
            return "no_hand"
        return "active"

    def _view(self, t) -> AirView:
        status = self._status(t)
        prim = self._prim
        g = self._grip
        progress = 0.0
        ring = "track"
        if status == "no_hand":
            ring = "lost"
        elif g is not None and g.kind == "drag":
            ring = "drag"
        elif g is not None and g.kind == "menu":
            ring = "hold"
        elif g is not None and g.kind == "pending":
            held = t - g.t0
            if held >= HOLD_ARC and g.moved < DRAG_MOVE:
                ring = "hold"
                progress = _clamp((held - HOLD_ARC) / (HOLD_TIME - HOLD_ARC))
            else:
                ring = "pressed"
        elif g is not None and g.kind == "ui":
            ring = "ui"
            if g.target == "bar:dictate":
                progress = _clamp((t - g.t0) / DICTATE_HOLD)
        elif self._mid is not None:
            ring = "pressed"
        elif self._crush_phase[0] in ("armed", "closing"):
            ring = "crush"
            progress = self._crush_phase[1]
        elif self._mode == "v":
            ring = "scroll"
        elif self._mode == "clutch" and self._dictating and self._fist_t0 is not None:
            ring = "crush"
            progress = _clamp((t - self._fist_t0) / FIST_STOP)
        elif self._mode == "clutch":
            ring = "clutch"
        elif self._pinch == "armed":
            ring = "armed"
        hand = None
        if prim is not None and self._seen:
            mu, mv = self._map_raw(_num(prim, "x", 0.5), _num(prim, "y", 0.5))
            hand = (_clamp(mu), _clamp(mv))
        toast = None
        if self._toast is not None:
            age = (t - self._toast[1]) / TOAST_S
            if 0 <= age <= 1:
                toast = (self._toast[0], age)
            elif age > 1:
                self._toast = None
        kb = None
        if self._kb is not None:
            pressed = ""
            if self._kb_pressed and t - self._kb_pressed[1] < 0.18:
                pressed = self._kb_pressed[0]
            rects = self._sugg_rects()
            kb = {"keys": list(self._kb["keys"]), "hover": self._kb_hover, "pressed": pressed,
                  "suggest": [(w,) + tuple(rects[i]) for i, w in enumerate(self._sugg[:3])],
                  "layer": self._kb["layer"], "shift": bool(self._kb["shift"]),
                  "side": self._kb_side(), "echo": self._echo,
                  "echo_rect": self._echo_rect()}
        menu = None
        if self._menu:
            menu = dict(self._menu)
            menu["items"] = list(self._menu["items"])
        pts = _g(prim, "pts", None) if prim is not None else None
        return AirView(
            status=status,
            cursor=self._cursor if self._seen else None,
            hand=hand,
            ring=ring,
            pinch=1.0 if (g is not None and g.kind in ("pending", "drag", "menu")) else
            _clamp(self._closeness),
            progress=progress,
            scroll_axis=self._axis if self._mode == "v" else "",
            menu=menu,
            bar=self._bar_geom(),
            keyboard=kb,
            exit_progress=self._exit_p,
            hint=HINT if self._t0 is not None and t - self._t0 < HINT_S else "",
            toast=toast,
            hands_seen=len(self._states),
            hand_pts=list(pts) if pts else None,
        )


# ── Dispatcher: actions -> the InputPump (core/os_input) ─────────────────────
def _feedback_text(line: str) -> str:
    """A person-readable reason out of a core.outcome line."""
    text = str(line or "")
    try:
        from core import outcome
        text = outcome.unwrap(text) or text
    except Exception:
        pass
    if text.startswith("status="):
        import re
        m = re.search(r'reason=("([^"]*)"|\S+)', text)
        if m:
            return m.group(2) if m.group(2) is not None else m.group(1)
        return text.split(" -- ")[0]
    return text


class Dispatcher:
    """Runs AirEngine actions through core.os_input's InputPump.

    run() is called on the Qt thread and only enqueues: pointer work goes on
    the pump's fast lane, anything that may take a while (subprocesses,
    accessibility, dictation) on its slow lane. Keystrokes, text and Cmd+W
    check first that JARVIS itself does not have the keyboard, so a gesture can
    never type into JARVIS's own window or close it.
    """

    def __init__(self, pump, mon: dict, os_input_mod=None, dictation_mod=None,
                 on_feedback: Callable[[str], None] = lambda text: None):
        self.pump = pump
        self.mon = mon
        self._os_mod = os_input_mod
        self._dict_mod = dictation_mod
        self.on_feedback = on_feedback

    def _os(self):
        if self._os_mod is None:
            import importlib
            self._os_mod = importlib.import_module("core.os_input")
        return self._os_mod

    def _dict(self):
        if self._dict_mod is None:
            import importlib
            self._dict_mod = importlib.import_module("core.dictation")
        return self._dict_mod

    def _feedback(self, text: str) -> None:
        try:
            if text:
                self.on_feedback(text)
        except Exception:
            pass

    def _status(self, res) -> None:
        """Report a failed Status (ok, why) or a problem outcome line."""
        try:
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], bool):
                if not res[0]:
                    self._feedback(str(res[1] or "that did not work"))
                return
            if isinstance(res, str):
                from core import outcome
                if outcome.is_problem(res):
                    self._feedback(_feedback_text(res))
        except Exception:
            pass

    def _guarded(self, fn, *args) -> None:
        os_mod = self._os()

        def job():
            try:
                if os_mod.own_app_focused():
                    self._feedback(OWN_FOCUS)
                    return
                self._status(fn(*args))
            except Exception as e:
                self._feedback(f"that did not work ({e})")
        self.pump.post(job)

    def _slow(self, fn, *args) -> None:
        def call():
            try:
                return fn(*args)
            except Exception as e:
                return (False, str(e))
        self.pump.slow(call, done=self._status)

    def run(self, actions: list) -> None:
        for a in actions or []:
            try:
                self._one(a)
            except Exception as e:
                print(f"[air] action {a!r} failed: {e}")

    def _one(self, a: tuple) -> None:
        if not a:
            return
        kind = a[0]
        if kind == "exit":
            return
        if kind in ("dictate",):
            self._dictate(a[1] if len(a) > 1 else "toggle")
            return
        os_mod = self._os()
        if kind == "move":
            x, y = os_mod.to_mouse(float(a[1]), float(a[2]), self.mon)
            self.pump.move(x, y)
        elif kind == "button":
            self.pump.post(os_mod.button, bool(a[1]), a[2] if len(a) > 2 else "left")
        elif kind == "click":
            self.pump.post(os_mod.click, a[1] if len(a) > 1 else "left",
                           int(a[2]) if len(a) > 2 else 1)
        elif kind == "scroll":
            self.pump.post(os_mod.scroll, float(a[1]), float(a[2]))
        elif kind == "release_all":
            self.pump.post(os_mod.release_all)
        elif kind == "key":
            self._guarded(os_mod.key, a[1], tuple(a[2]) if len(a) > 2 else (),
                          int(a[3]) if len(a) > 3 else 1)
        elif kind == "type":
            self._guarded(os_mod.type_text, a[1])
        elif kind == "close_window":
            self._guarded(os_mod.close_window)
        elif kind == "nav":
            self._slow(os_mod.nav, a[1])
        elif kind == "space":
            self._slow(os_mod.space, a[1])
        elif kind in ("overview", "app_windows", "show_desktop", "launcher"):
            self._slow(getattr(os_mod, kind))
        elif kind == "browser":
            self._slow(os_mod.open_browser)

    def _dictate(self, op: str) -> None:
        dm = self._dict()

        def call():
            try:
                if op == "toggle":
                    return dm.stop() if dm.active() else dm.start(source="gesture")
                if op == "hold_start":
                    return dm.start(source="gesture", hold=True)
                if op in ("hold_stop", "stop"):
                    return dm.stop(commit=True)
                if op == "undo":
                    return dm.undo_last()
                if op == "send":
                    return dm.send()
                return ""
            except Exception as e:
                return f"status=failed action=dictation reason=\"{e}\""
        self.pump.slow(call, done=self._status)
