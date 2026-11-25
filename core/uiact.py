"""
core/uiact.py — work a program's controls through its accessibility
interface, without the mouse and without bringing its window forward.

WHY, NEXT TO core/screenact.py
    screenact presses a control by moving the pointer onto it and clicking,
    and types by sending keys to whatever has the keyboard. That needs the
    window in front, visible and not covered, and it takes the pointer away
    from the user while it happens. But the same accessibility interface that
    core/screenview.py reads controls from can also ACT on them: "invoke this
    button", "set this switch on", "put this text in this field", "set this
    slider to 40", "pick this entry from the list". Asked that way, a program
    in the background, behind other windows or on another desktop does the
    thing and nothing on screen moves. It is also exact: there is no aim to
    miss.

    Not every program supports it — games, programs that draw their own
    widgets and many web pages publish little or nothing — so every caller
    keeps its pointer path as the fallback. Asking first costs one tree read.

WHAT IT GUARANTEES
    - Nothing is ever typed into a field the platform marks as secret
      (password, PIN): the user types those.
    - A disabled control is reported as disabled, never "pressed".
    - Every change is read back. `toggle`, `set_text`, `set_number` and
      `choose` report "done" only when the control afterwards holds what was
      asked; "unverified" when the program does not say; "failed" when it
      says something else. `press` reports "done" only when something in the
      program's controls changed afterwards.

    Results are (status, message) with status "done" | "unverified" |
    "failed"; callers turn them into core.outcome lines. Nothing raises.

BACKENDS
    macOS    AX through pyobjc (ApplicationServices): AXPress, settable
             AXValue, AXShowMenu / AXCancel for pop-up lists. Needs the
             Accessibility permission, like the rest of the screen work.
    Windows  UI Automation through pywinauto: Invoke, Toggle, Value,
             RangeValue, SelectionItem, ExpandCollapse patterns, and the
             legacy default action as the last resort.
    Linux    AT-SPI through pyatspi: actions, EditableText, Value. Works on
             X11 and on Wayland (it does not need the pointer).
    The per-node readers are core/screenview.py's, so a control's role and
    name here are exactly what the screen agent sees.
"""
from __future__ import annotations

import platform
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_OS = platform.system()

SCAN_BUDGET_S = 1.5
MAX_CONTROLS = 900
MAX_DEPTH = 28
ITEM_ROLES = ("listitem", "menuitem", "treeitem", "radio", "tab", "cell")
TEXT_ROLES = ("edit", "combobox")
TOGGLE_ROLES = ("checkbox", "radio", "button", "menuitem", "listitem", "treeitem")

DONE, UNVERIFIED, FAILED = "done", "unverified", "failed"


# ── one control ──────────────────────────────────────────────────────────────
@dataclass
class Control:
    cid: int
    role: str
    name: str
    value: str = ""
    enabled: bool = True
    protected: bool = False
    checked: Optional[bool] = None       # None: the control has no on/off state
    depth: int = 0
    rect: tuple = (0, 0, 0, 0)            # left, top, width, height (mouse units)
    pid: int = 0                          # the program it belongs to
    node: Any = field(default=None, repr=False, compare=False)

    @property
    def label(self) -> str:
        return self.name or self.value

    def line(self) -> str:
        """One line for a model or a list read to the user. A secret field's
        contents are never part of it."""
        bits = [f"#{self.cid}", self.role, repr(self.name[:60]) if self.name else "''"]
        if self.value and self.value != self.name and not self.protected:
            bits.append(f"value={self.value[:40]!r}")
        if self.checked is not None:
            bits.append("on" if self.checked else "off")
        if not self.enabled:
            bits.append("disabled")
        if self.protected:
            bits.append("secret")
        return " ".join(bits)


def _from_element(el: Any, node: Any, cid: int, checked: Optional[bool],
                  role: str = "") -> Control:
    return Control(cid=cid, role=role or el.role, name=el.name or "", value=el.value or "",
                   enabled=bool(el.enabled), protected=bool(getattr(el, "protected", False)),
                   checked=checked, depth=getattr(el, "depth", 0),
                   rect=tuple(getattr(el, "rect", (0, 0, 0, 0))), node=node)


def _number(v: Any) -> Optional[float]:
    """A number from what a platform hands back: some report "40" as text."""
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_BULLETS = set("•●∙·*◦⦁")


def looks_secret(c: Control) -> bool:
    """The platform's own flag, or a value shown only as bullets — what a
    password box shows when a toolkit forgot to flag it."""
    v = (c.value or "").strip()
    return c.protected or (len(v) >= 3 and set(v) <= _BULLETS)


# ── backends ─────────────────────────────────────────────────────────────────
class Backend:
    """What one platform must provide. Every method may raise; the public
    functions below catch it. Errors are returned as a sentence, success as
    None."""

    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "working inside programs without the mouse is not possible on this system"

    def scan(self, pid: int, budget_s: float) -> list[Control]:
        return []

    def refresh(self, c: Control) -> Control:
        """The same control read again (value, state)."""
        return c

    def press(self, c: Control) -> Optional[str]:
        return "not supported"

    def set_checked(self, c: Control, on: bool) -> Optional[str]:
        return self.press(c)

    def set_text(self, c: Control, text: str) -> Optional[str]:
        return "not supported"

    def text_of(self, c: Control) -> Optional[str]:
        return self.refresh(c).value

    def range_of(self, c: Control) -> Optional[tuple[float, float, float]]:
        return None

    def set_number(self, c: Control, number: float) -> Optional[str]:
        return "not supported"

    def open_list(self, c: Control) -> list[Control]:
        return []

    def appeared(self, c: Control, before: set) -> list[Control]:
        """Entries that appeared in the program after a list was opened. For
        toolkits that show the list as a separate pop-up window rather than
        as children of the control."""
        if not c.pid:
            return []
        out = []
        for k in self.scan(c.pid, SCAN_BUDGET_S):
            if k.role in ITEM_ROLES and k.label and _key(k) not in before:
                out.append(k)
        return out

    def items_now(self, c: Control) -> set:
        if not c.pid:
            return set()
        return {_key(k) for k in self.scan(c.pid, SCAN_BUDGET_S) if k.role in ITEM_ROLES}

    def pick(self, item: Control) -> Optional[str]:
        return self.press(item)

    def close_list(self, c: Control) -> None:
        return None

    def is_open(self, c: Control) -> Optional[bool]:
        """Whether a drop-down's list is showing; None when it does not say."""
        return None


def _key(c: Control) -> tuple:
    return (c.role, c.name, c.value, c.rect)


class _MacAX(Backend):
    name = "macOS accessibility"

    def __init__(self):
        import ApplicationServices as AS
        self.AS = AS

    def available(self):
        if not self.AS.AXIsProcessTrusted():
            return False, ("macOS has not given this app the Accessibility permission (System "
                           "Settings > Privacy & Security > Accessibility)")
        return True, ""

    def _attr(self, node, name):
        from core import screenview
        return screenview._ax_attr(self.AS, node, name)

    def _checked(self, node, role) -> Optional[bool]:
        if role not in ("checkbox", "radio"):
            return None
        v = self._attr(node, "AXValue")
        return bool(v) if isinstance(v, (int, float, bool)) else None

    def _control(self, node, cid, depth) -> Optional[Control]:
        from core import screenview
        el, _title = screenview._element_from_ax(self.AS, node, cid, depth)
        if el is None:
            return None
        # A pop-up or menu button IS a drop-down (Qt's QComboBox shows up as
        # AXMenuButton, Cocoa's as AXPopUpButton); its title is the current entry.
        raw = str(self._attr(node, "AXRole") or "")
        role = "combobox" if raw in ("AXPopUpButton", "AXMenuButton") else ""
        return _from_element(el, node, cid, self._checked(node, el.role), role)

    def scan(self, pid, budget_s):
        from core import screenview
        app = self.AS.AXUIElementCreateApplication(pid)
        deadline = time.monotonic() + budget_s
        out: list[Control] = []
        frontier = [(app, 0)]
        while frontier and len(out) < MAX_CONTROLS and time.monotonic() < deadline:
            node, depth = frontier.pop(0)
            c = self._control(node, len(out), depth)
            if c is not None:
                c.pid = pid
                out.append(c)
            if depth < MAX_DEPTH:
                frontier.extend((k, depth + 1) for k in screenview._ax_children(self.AS, node))
        return out

    def refresh(self, c):
        fresh = self._control(c.node, c.cid, c.depth)
        if fresh is not None:
            fresh.pid = c.pid
        return fresh or c

    def _act(self, node, action) -> Optional[str]:
        err = self.AS.AXUIElementPerformAction(node, action)
        return None if err == 0 else f"the program refused the {action} request (code {err})"

    def press(self, c):
        return self._act(c.node, "AXPress")

    def _settable(self, node, attr) -> bool:
        try:
            err, ok = self.AS.AXUIElementIsAttributeSettable(node, attr, None)
            return err == 0 and bool(ok)
        except Exception:
            return False

    def set_text(self, c, text):
        if not self._settable(c.node, "AXValue"):
            return "this field does not accept text this way"
        err = self.AS.AXUIElementSetAttributeValue(c.node, "AXValue", text)
        return None if err == 0 else f"the field refused the text (code {err})"

    def text_of(self, c):
        v = self._attr(c.node, "AXValue")
        return str(v) if isinstance(v, (str, int, float)) else None

    def range_of(self, c):
        lo, hi, now = (_number(self._attr(c.node, k))
                       for k in ("AXMinValue", "AXMaxValue", "AXValue"))
        if now is None:
            return None
        return (lo if lo is not None else float("-inf"),
                hi if hi is not None else float("inf"), now)

    def set_number(self, c, number):
        if not self._settable(c.node, "AXValue"):
            return "this control does not accept a number this way"
        err = self.AS.AXUIElementSetAttributeValue(c.node, "AXValue", number)
        return None if err == 0 else f"the control refused the number (code {err})"

    def open_list(self, c):
        from core import screenview
        before = self.items_now(c)
        # Opening is a toggle on some toolkits: never "open" one that is open.
        if self._attr(c.node, "AXExpanded") is not True:
            if self._act(c.node, "AXShowMenu") is not None:
                self._act(c.node, "AXPress")
        # The entries are published a moment after the list opens, and later
        # on a second opening than on the first (measured with Qt: empty at
        # 0.3 s the second time) - so look a few times before giving up.
        items: list[Control] = []
        deadline = time.monotonic() + 1.2
        while not items and time.monotonic() < deadline:
            time.sleep(0.15)
            frontier = [(c.node, 0)]
            while frontier and len(items) < 200:
                node, depth = frontier.pop(0)
                if depth > 0:
                    k = self._control(node, len(items), depth)
                    if k is not None and k.role in ITEM_ROLES and k.label:
                        items.append(k)
                if depth < 6:
                    frontier.extend((n, depth + 1) for n in screenview._ax_children(self.AS, node))
        if not items:
            items = self.appeared(c, before)
        # Remember the pop-up itself (the entries' parent): it is what takes
        # the cancel that closes it again — the button ignores one (measured
        # with Qt: the menu stayed open and the next open shut it instead).
        self._popup = self._attr(items[0].node, "AXParent") if items else None
        return items

    def close_list(self, c):
        """Close it and check. Qt's pop-up answers AXCancel with success and
        stays open (measured), so the button's AXExpanded decides, and a
        second AXShowMenu - a toggle there - is the way that works."""
        popup = getattr(self, "_popup", None)
        self._popup = None
        for node, action in ((popup, "AXCancel"), (c.node, "AXCancel"),
                             (c.node, "AXShowMenu"), (c.node, "AXPress")):
            if self._attr(c.node, "AXExpanded") is not True:
                return
            if node is not None:
                self._act(node, action)
                time.sleep(0.2)

    def is_open(self, c) -> Optional[bool]:
        v = self._attr(c.node, "AXExpanded")
        return v if isinstance(v, bool) else None


class _WinUIA(Backend):
    name = "Windows UI Automation"

    def available(self):
        try:
            import pywinauto.uia_element_info  # noqa: F401
            from pywinauto.controls.uiawrapper import UIAWrapper  # noqa: F401
        except Exception as e:
            return False, f"UI Automation is not available here ({e}); pip install pywinauto"
        return True, ""

    @staticmethod
    def _wrap(node):
        from pywinauto.controls.uiawrapper import UIAWrapper
        return UIAWrapper(node)

    def _checked(self, node, role) -> Optional[bool]:
        w = self._wrap(node)
        try:
            return w.get_toggle_state() == 1
        except Exception:
            pass
        if role in ("radio", "listitem", "tab", "treeitem"):
            try:
                return bool(w.iface_selection_item.CurrentIsSelected)
            except Exception:
                pass
        return None

    def _control(self, node, cid, depth) -> Optional[Control]:
        from core import screenview
        el = screenview._element_from_uia(node, cid, depth, 1.0)
        if el is None:
            return None
        checked = self._checked(node, el.role) if el.role in TOGGLE_ROLES + ("tab",) else None
        return _from_element(el, node, cid, checked)

    def scan(self, pid, budget_s):
        from pywinauto import Desktop
        deadline = time.monotonic() + budget_s
        out: list[Control] = []
        frontier = [(w.element_info, 0) for w in Desktop(backend="uia").windows(process=pid)]
        while frontier and len(out) < MAX_CONTROLS and time.monotonic() < deadline:
            node, depth = frontier.pop(0)
            c = self._control(node, len(out), depth)
            if c is not None:
                c.pid = pid
                out.append(c)
            if depth < MAX_DEPTH:
                try:
                    frontier.extend((k, depth + 1) for k in node.children())
                except Exception:
                    pass
        return out

    def refresh(self, c):
        return self._control(c.node, c.cid, c.depth) or c

    def press(self, c):
        w = self._wrap(c.node)
        for attempt in (lambda: w.iface_invoke.Invoke(),
                        lambda: w.iface_toggle.Toggle(),
                        lambda: w.iface_selection_item.Select(),
                        lambda: (w.iface_expand_collapse.Collapse()
                                 if w.iface_expand_collapse.CurrentExpandCollapseState == 1
                                 else w.iface_expand_collapse.Expand()),
                        lambda: w.iface_legacy_iaccessible.DoDefaultAction()):
            try:
                attempt()
                return None
            except Exception:
                continue
        return "this control can only be pressed by clicking, not without the mouse"

    def set_checked(self, c, on):
        w = self._wrap(c.node)
        try:
            w.iface_toggle
        except Exception:
            if on:
                try:
                    w.iface_selection_item.Select()
                    return None
                except Exception:
                    pass
            return self.press(c)
        for _ in range(3):              # toggles may have a third, mixed state
            if (self._checked(c.node, c.role) is True) == on:
                return None
            w.iface_toggle.Toggle()
        return None

    def set_text(self, c, text):
        w = self._wrap(c.node)
        try:
            iface = w.iface_value
        except Exception:
            return "this field does not accept text this way"
        if iface.CurrentIsReadOnly:
            return "this field is read-only"
        iface.SetValue(text)
        return None

    def text_of(self, c):
        try:
            return str(self._wrap(c.node).iface_value.CurrentValue)
        except Exception:
            return None

    def range_of(self, c):
        try:
            r = self._wrap(c.node).iface_range_value
            return float(r.CurrentMinimum), float(r.CurrentMaximum), float(r.CurrentValue)
        except Exception:
            return None

    def set_number(self, c, number):
        try:
            r = self._wrap(c.node).iface_range_value
        except Exception:
            try:
                self._wrap(c.node).iface_value.SetValue(_number_text(number))
                return None
            except Exception:
                return "this control does not accept a number this way"
        if r.CurrentIsReadOnly:
            return "this control is read-only"
        r.SetValue(float(number))
        return None

    def open_list(self, c):
        w = self._wrap(c.node)
        try:
            w.iface_expand_collapse.Expand()
            time.sleep(0.25)
        except Exception:
            pass
        items, frontier = [], [(c.node, 0)]
        while frontier and len(items) < 200:
            node, depth = frontier.pop(0)
            if depth > 0:
                k = self._control(node, len(items), depth)
                if k is not None and k.role in ITEM_ROLES and k.label:
                    items.append(k)
            if depth < 6:
                try:
                    frontier.extend((n, depth + 1) for n in node.children())
                except Exception:
                    pass
        return items

    def pick(self, item):
        w = self._wrap(item.node)
        try:
            w.iface_selection_item.Select()
            return None
        except Exception:
            return self.press(item)

    def close_list(self, c):
        try:
            self._wrap(c.node).iface_expand_collapse.Collapse()
        except Exception:
            pass


class _LinuxATSPI(Backend):
    name = "AT-SPI"

    def __init__(self):
        import pyatspi
        self.at = pyatspi

    def available(self):
        return True, ""

    def _checked(self, node, role) -> Optional[bool]:
        if role not in TOGGLE_ROLES:
            return None
        try:
            states = node.getState()
            if role in ("checkbox", "radio") or states.contains(self.at.STATE_CHECKABLE):
                return bool(states.contains(self.at.STATE_CHECKED)
                            or states.contains(self.at.STATE_PRESSED))
        except Exception:
            pass
        return None

    def _control(self, node, cid, depth):
        from core import screenview
        el = screenview._element_from_atspi(self.at, node, cid, depth)
        if el is None:
            return None
        return _from_element(el, node, cid, self._checked(node, el.role))

    def scan(self, pid, budget_s):
        desktop = self.at.Registry.getDesktop(0)
        deadline = time.monotonic() + budget_s
        frontier = []
        for app in desktop:
            try:
                if app is not None and app.get_process_id() == pid:
                    frontier.extend((w, 0) for w in app if w is not None)
            except Exception:
                continue
        out: list[Control] = []
        while frontier and len(out) < MAX_CONTROLS and time.monotonic() < deadline:
            node, depth = frontier.pop(0)
            c = self._control(node, len(out), depth)
            if c is not None:
                c.pid = pid
                out.append(c)
            if depth < MAX_DEPTH:
                try:
                    frontier.extend((k, depth + 1) for k in node if k is not None)
                except Exception:
                    pass
        return out

    def refresh(self, c):
        return self._control(c.node, c.cid, c.depth) or c

    def press(self, c):
        try:
            act = c.node.queryAction()
        except NotImplementedError:
            return "this control can only be pressed by clicking, not without the mouse"
        names = [act.getName(i).lower() for i in range(act.nActions)]
        for wanted in ("click", "press", "activate", "toggle", "jump"):
            if wanted in names:
                return None if act.doAction(names.index(wanted)) else "the program refused it"
        if names:
            return None if act.doAction(0) else "the program refused it"
        return "this control can only be pressed by clicking, not without the mouse"

    def set_text(self, c, text):
        try:
            editable = c.node.queryEditableText()
        except NotImplementedError:
            return "this field does not accept text this way"
        return None if editable.setTextContents(text) else "the field refused the text"

    def text_of(self, c):
        try:
            t = c.node.queryText()
            return t.getText(0, t.characterCount)
        except Exception:
            return None

    def range_of(self, c):
        try:
            v = c.node.queryValue()
            return float(v.minimumValue), float(v.maximumValue), float(v.currentValue)
        except Exception:
            return None

    def set_number(self, c, number):
        try:
            c.node.queryValue().currentValue = float(number)
            return None
        except Exception:
            return "this control does not accept a number this way"

    def open_list(self, c):
        self.press(c)
        time.sleep(0.25)
        items, frontier = [], [(c.node, 0)]
        while frontier and len(items) < 200:
            node, depth = frontier.pop(0)
            if depth > 0:
                k = self._control(node, len(items), depth)
                if k is not None and k.role in ITEM_ROLES and k.label:
                    items.append(k)
            if depth < 6:
                try:
                    frontier.extend((n, depth + 1) for n in node if n is not None)
                except Exception:
                    pass
        return items


_backend: Optional[Backend] = None


def backend() -> Backend:
    """The backend for this system (made once). A missing library gives the
    plain Backend, whose available() says what is missing."""
    global _backend
    if _backend is None:
        try:
            if _OS == "Darwin":
                _backend = _MacAX()
            elif _OS == "Windows":
                _backend = _WinUIA()
            elif _OS == "Linux":
                _backend = _LinuxATSPI()
        except Exception as e:
            print(f"[UIAct] no backend: {e}")
        if _backend is None:
            _backend = Backend()
    return _backend


def use_backend(b: Optional[Backend]) -> None:
    """Swap the backend (the self-test's fake one); None goes back to the real one."""
    global _backend
    _backend = b


# ── reading and finding ──────────────────────────────────────────────────────
def available() -> tuple[bool, str]:
    try:
        return backend().available()
    except Exception as e:
        return False, f"the accessibility interface could not be used: {e}"


def controls(pid: int, budget_s: float = SCAN_BUDGET_S) -> tuple[list[Control], str]:
    """(controls of every window of process `pid`, "") or ([], why). The
    windows are read where they are; nothing is brought forward."""
    ok, why = available()
    if not ok:
        return [], why
    if not pid:
        return [], "no program was named"
    try:
        found = backend().scan(int(pid), budget_s)
    except Exception as e:
        return [], f"the program's controls could not be read: {e}"
    if not found:
        return [], "this program shows nothing to the accessibility interface"
    return found, ""


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[\s_\-:…\.]+", " ", text.casefold()).strip()


def find(items: list[Control], words: str, roles: tuple = (),
         actionable: bool = True) -> list[Control]:
    """Controls whose name (or value) matches `words`, best first: the exact
    name, then names that start with it, then names that contain it as a
    whole word, then as any part. Shallower controls first within a rank."""
    want = fold(words)
    if not want:
        return []
    from core import screenview
    pool = [c for c in items
            if (not roles or c.role in roles)
            and (not actionable or c.role in screenview.INTERACTABLE or c.checked is not None)]
    ranked = []
    for c in pool:
        for text in (c.name, "" if looks_secret(c) else c.value):
            f = fold(text)
            if not f:
                continue
            if f == want:
                rank = 0
            elif f.startswith(want):
                rank = 1
            elif re.search(rf"(?<!\w){re.escape(want)}(?!\w)", f):
                rank = 2
            elif want in f:
                rank = 3
            else:
                continue
            ranked.append((rank, c.depth, c.cid, c))
            break
    ranked.sort(key=lambda t: t[:3])
    return [t[3] for t in ranked]


def _digest(items: list[Control]) -> list[tuple]:
    return [(c.role, c.name, "" if c.protected else c.value, c.checked, c.enabled)
            for c in items]


def same_text(a: Optional[str], b: Optional[str]) -> bool:
    """Equal once spacing and line endings are ignored: programs reflow."""
    norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip()
    return norm(a) == norm(b)


def _number_text(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _usable(c: Control) -> Optional[tuple[str, str]]:
    if not c.enabled:
        return FAILED, f"'{c.label[:40]}' is disabled right now"
    return None


# ── acting ───────────────────────────────────────────────────────────────────
def press(c: Control, pid: int = 0, settle_s: float = 0.4) -> tuple[str, str]:
    """Press without the mouse. With `pid`, the program's controls are read
    again afterwards: "done" when anything in them changed."""
    blocked = _usable(c)
    if blocked:
        return blocked
    b = backend()
    try:
        before = _digest(b.scan(pid, SCAN_BUDGET_S)) if pid else None
        err = b.press(c)
    except Exception as e:
        err = str(e)
    if err:
        return FAILED, err
    if before is None:
        return UNVERIFIED, f"'{c.label[:40]}' was pressed"
    time.sleep(settle_s)
    try:
        after = _digest(b.scan(pid, SCAN_BUDGET_S))
    except Exception:
        after = before
    if after != before:
        return DONE, f"pressed '{c.label[:40]}'"
    return UNVERIFIED, f"'{c.label[:40]}' was pressed, but nothing in the program changed that could be read"


def toggle(c: Control, on: Optional[bool] = None) -> tuple[str, str]:
    """Switch on (True), off (False) or over (None), and read it back."""
    blocked = _usable(c)
    if blocked:
        return blocked
    b = backend()
    try:
        state = b.refresh(c).checked
        if state is None and c.checked is None:
            return FAILED, f"'{c.label[:40]}' has no on/off state"
        if on is not None and state is on:
            return DONE, f"'{c.label[:40]}' was already {'on' if on else 'off'}"
        target = (not state) if on is None else on
        err = b.set_checked(c, target)
        if err:
            return FAILED, err
        time.sleep(0.15)
        now = b.refresh(c).checked
    except Exception as e:
        return FAILED, f"it could not be switched: {e}"
    if now is None:
        return UNVERIFIED, f"'{c.label[:40]}' was switched, but it does not report its state"
    if now is target:
        return DONE, f"'{c.label[:40]}' is now {'on' if now else 'off'}"
    return FAILED, f"'{c.label[:40]}' is still {'on' if now else 'off'}"


def set_text(c: Control, text: str, append: bool = False) -> tuple[str, str]:
    """Put `text` in a field (after what it holds, with `append`), read back."""
    blocked = _usable(c)
    if blocked:
        return blocked
    if looks_secret(c) or looks_secret(backend().refresh(c)):
        return FAILED, "this is a secret field (a password or a PIN); only the user may fill it in"
    b = backend()
    try:
        wanted = text
        if append:
            current = b.text_of(c) or ""
            wanted = current + text
        err = b.set_text(c, wanted)
        if err:
            return FAILED, err
        time.sleep(0.1)
        now = b.text_of(c)
    except Exception as e:
        return FAILED, f"the text could not be set: {e}"
    if now is None:
        return UNVERIFIED, "the text was given to the field, but the field does not say what it holds"
    if same_text(now, wanted):
        return DONE, f"'{c.label[:40]}' now holds the text"
    return FAILED, "the field now holds something else than what was asked"


def set_number(c: Control, number: float) -> tuple[str, str]:
    """Set a slider, spin box or other range control, read back."""
    blocked = _usable(c)
    if blocked:
        return blocked
    b = backend()
    try:
        rng = b.range_of(c)
        if rng is not None and not (rng[0] <= number <= rng[1]):
            return FAILED, (f"'{c.label[:40]}' goes from {_number_text(rng[0])} to "
                            f"{_number_text(rng[1])}; {_number_text(number)} is outside that")
        err = b.set_number(c, number)
        if err:
            return FAILED, err
        time.sleep(0.1)
        after = b.range_of(c)
    except Exception as e:
        return FAILED, f"the number could not be set: {e}"
    if after is None:
        return UNVERIFIED, f"{_number_text(number)} was given to '{c.label[:40]}', but it does not say its value"
    # Sliders snap to their own steps; within one percent of the range is it.
    span = after[1] - after[0]
    tolerance = max(1e-6, span * 0.01) if span < float("inf") else 1e-6
    if abs(after[2] - number) <= tolerance:
        return DONE, f"'{c.label[:40]}' is now {_number_text(after[2])}"
    return FAILED, f"'{c.label[:40]}' is {_number_text(after[2])}, not {_number_text(number)}"


def options(c: Control) -> tuple[list[str], str]:
    """The entries of a drop-down or list, opened and closed again."""
    blocked = _usable(c)
    if blocked:
        return [], blocked[1]
    b = backend()
    try:
        items = b.open_list(c)
        b.close_list(c)
    except Exception as e:
        return [], f"its entries could not be read: {e}"
    names = []
    for it in items:
        if it.label and it.label not in names:
            names.append(it.label)
    return names, "" if names else "it lists no entries this way"


def choose(c: Control, item: str) -> tuple[str, str]:
    """Pick `item` from a drop-down or list and read back what it shows."""
    blocked = _usable(c)
    if blocked:
        return blocked
    b = backend()
    try:
        items = b.open_list(c)
        if not items:
            b.close_list(c)
            return FAILED, "the list's entries cannot be read without the mouse"
        hits = find(items, item, actionable=False)
        if not hits:
            b.close_list(c)
            shown = ", ".join(i.label for i in items[:12])
            return FAILED, (f"there is no entry '{item}'" + (f"; it offers: {shown}" if shown else ""))
        pick = hits[0]
        err = b.pick(pick)
        if err:
            b.close_list(c)
            return FAILED, err
        time.sleep(0.3)
        now = b.refresh(c)
        picked = b.refresh(pick)
        still_open = b.is_open(c)
        if still_open:
            b.close_list(c)
    except Exception as e:
        return FAILED, f"the entry could not be chosen: {e}"
    shown = fold(now.value or now.name)
    if fold(pick.label) and fold(pick.label) in shown:
        return DONE, f"'{c.label[:40]}' now shows '{pick.label[:40]}'"
    if picked.checked is True:
        return DONE, f"'{pick.label[:40]}' is chosen"
    if still_open or (shown and shown == fold(c.value or c.name)):
        # Qt's pop-up list on macOS ignores every accessibility way of
        # picking an entry (measured): say so, so the caller uses the pointer.
        return FAILED, (f"the program ignored the choice of '{pick.label[:40]}' made without "
                        "the mouse")
    return UNVERIFIED, f"'{pick.label[:40]}' was picked, but the control does not show it"
