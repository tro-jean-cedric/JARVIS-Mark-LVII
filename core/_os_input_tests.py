"""
Self-test for the continuous mouse and keyboard layer (core/os_input.py).

    python -m core._os_input_tests

Standard library only. No real event is ever posted: every backend here is a
recording stand-in, and the platform is pretended where a branch needs it.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import os_input  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def wait_for(pred, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class Recorder:
    """A backend that only writes down what it was asked to do."""

    def __init__(self):
        self.log: list = []
        self.lock = threading.Lock()
        self.pos = (5.0, 5.0)

    def move(self, x, y):
        with self.lock:
            self.log.append(("move", x, y))
            self.pos = (x, y)

    def cursor(self):
        return self.pos

    def release_all(self):
        with self.lock:
            self.log.append(("release_all",))

    def mark(self, tag):
        with self.lock:
            self.log.append(tag)


# ─────────────────────────────────────────────────────────────────────────────
def test_pump() -> None:
    rec = Recorder()
    pump = os_input.InputPump(backend=rec, cursor_hz=50)
    gate = threading.Event()
    entered = threading.Event()

    def block():
        entered.set()
        gate.wait(2.0)

    pump.post(block)
    check("pump: the blocking job started", entered.wait(1.0))
    pump.move(1, 1)
    pump.move(2, 2)
    pump.move(3, 3)
    pump.post(rec.mark, "A")
    pump.post(rec.mark, "B")
    gate.set()
    check("pump: the batch ran", wait_for(lambda: "B" in rec.log))
    seq = [e for e in rec.log if e != ("release_all",)]
    moves = [e for e in seq if isinstance(e, tuple) and e[0] == "move"]
    check("pump: moves coalesce, latest wins", moves == [("move", 3.0, 3.0)], str(moves))
    check("pump: the move runs before the batch, batch in FIFO order",
          seq[-3:] == [("move", 3.0, 3.0), "A", "B"], str(seq))

    # a move queued BEFORE a job runs before it, one queued after runs after
    # (the drag press must land at the pinch point, not the drifted hand)
    gate2 = threading.Event()
    entered2 = threading.Event()
    pump.post(lambda: (entered2.set(), gate2.wait(2.0)))
    entered2.wait(1.0)
    n0 = len(rec.log)
    pump.move(10, 10)
    pump.post(rec.mark, "press")
    pump.move(20, 20)
    gate2.set()
    wait_for(lambda: ("move", 20.0, 20.0) in rec.log)
    tail = [e for e in rec.log[n0:] if e != ("release_all",)]
    check("pump: move, job, move keep their order",
          tail == [("move", 10.0, 10.0), "press", ("move", 20.0, 20.0)], str(tail))

    pump.move(20.2, 20.2)
    pump.post(rec.mark, "C")
    wait_for(lambda: "C" in rec.log)
    moves = [e for e in rec.log if isinstance(e, tuple) and e[0] == "move"]
    check("pump: a move under 0.5 px is skipped", len(moves) == 3, str(moves))

    check("pump: cursor sampled", wait_for(lambda: pump.cursor_sample() is not None))
    check("pump: the sample follows the cursor",
          wait_for(lambda: (pump.cursor_sample() or (0,))[0] == 20.0))
    s = pump.cursor_sample()
    check("pump: sample is (x, y, t)", s is not None and len(s) == 3
          and abs(s[2] - time.monotonic()) < 1.0)

    got = []
    done_evt = threading.Event()
    pump.slow(lambda a, b: a * b, 6, 7, done=lambda r: (got.append(r), done_evt.set()))
    check("pump: slow lane runs and calls done", done_evt.wait(1.0) and got == [42])
    names = {t.name for t in threading.enumerate()}
    check("pump: both lanes are named threads",
          {"air-input", "air-input-slow"} <= names, str(names))

    pump.post(lambda: 1 / 0)
    check("pump: an exception lands in last_error",
          wait_for(lambda: "ZeroDivisionError" in pump.last_error), pump.last_error)
    pump.post(rec.mark, "after-error")
    check("pump: keeps running after an exception", wait_for(lambda: "after-error" in rec.log))

    pump.close()
    pump.close()
    check("pump: close releases everything exactly once",
          rec.log.count(("release_all",)) == 1, str(rec.log))
    pump.move(50, 50)
    pump.post(rec.mark, "late")
    time.sleep(0.05)
    check("pump: nothing runs after close", "late" not in rec.log)


def test_key_spec() -> None:
    fnnum = os_input._F_FN | os_input._F_NUMPAD
    code, flags = os_input._mac_key_spec("left", ())
    check("mac: left arrow is 123", code == 123)
    check("mac: arrows carry fn|numpad", flags & fnnum == fnnum, hex(flags))
    code, flags = os_input._mac_key_spec("right", ("ctrl",))
    check("mac: ctrl+right keeps ctrl and fn|numpad",
          flags & os_input._F_CTRL and flags & fnnum == fnnum)
    check("mac: cmd+w = (13, cmd)", os_input._mac_key_spec("w", ("cmd",)) == (13, 0x100000))
    check("mac: plain enter has flags 0", os_input._mac_key_spec("enter", ()) == (36, 0))
    check("mac: '[' alias", os_input._mac_key_spec("[", ("cmd",)) == (33, 0x100000))
    check("mac: unknown key -> None", os_input._mac_key_spec("foo", ()) is None)
    check("mac: unknown modifier -> None", os_input._mac_key_spec("a", ("hyper",)) is None)

    class FakeMac(os_input._MacBackend):
        def __init__(self):
            super().__init__()
            self.keys = []

        def _key(self, code, down, flags):
            self.keys.append((code, down, flags))

        def cursor(self):
            return (0.0, 0.0)

    fake = FakeMac()
    os_input._set_backend(fake)
    try:
        ok, why = os_input.key("foo")
        check("key: unknown name is Status False", ok is False and "foo" in why, why)
        ok, _ = os_input.key("w", ("cmd",))
        check("key: cmd+w delivered", ok)
        check("key: cmd pressed, w down/up with the flag, cmd released",
              fake.keys == [(55, True, 0x100000), (13, True, 0x100000),
                            (13, False, 0x100000), (55, False, 0)], str(fake.keys))
        check("key: modifiers not left held", not fake.mods_down)
        fake.keys.clear()
        ok, _ = os_input.key("backspace", (), 3)
        check("key: count repeats", ok and [k for k in fake.keys if k[1]] == [(51, True, 0)] * 3)
        ok, why = os_input.key("a", ("hyper",))
        check("key: unknown modifier refused", ok is False)
    finally:
        os_input._set_backend(None)


def test_hotkey_normalise() -> None:
    n = os_input._normalise_combo
    check("hotkey: ctrl is cmd on darwin", n("ctrl+w", "darwin") == ("w", ("cmd",)))
    check("hotkey: cmd is cmd on darwin", n("cmd+w", "darwin") == ("w", ("cmd",)))
    check("hotkey: ctrl stays ctrl on linux", n("ctrl+w", "linux") == ("w", ("ctrl",)))
    check("hotkey: cmd means ctrl on windows", n("cmd+w", "win32") == ("w", ("ctrl",)))
    check("hotkey: win is the Win key off the Mac", n("win+d", "win32") == ("d", ("cmd",)))
    check("hotkey: macctrl is physical ctrl on the Mac",
          n("macctrl+left", "darwin") == ("left", ("ctrl",)))
    check("hotkey: case and spaces", n(" Ctrl + Shift + T ", "linux") == ("t", ("ctrl", "shift")))
    check("hotkey: unknown modifier -> None", n("hyper+x", "linux") is None)
    check("hotkey: empty -> None", n("", "linux") is None)


def test_coords() -> None:
    mon = {"left": 100, "top": 50, "width": 1000, "height": 500}
    x, y = os_input.to_mouse(0.5, 0.5, mon)
    check("coords: centre", (x, y) == (600.0, 300.0))
    check("coords: round trip", os_input.from_mouse(x, y, mon) == (0.5, 0.5))
    check("coords: clamp 1 px inside top-left", os_input.to_mouse(0, 0, mon) == (101.0, 51.0))
    check("coords: clamp inside bottom-right",
          os_input.to_mouse(1, 1, mon) == (1098.0, 548.0))
    check("coords: far outside is clamped", os_input.to_mouse(-3, 9, mon) == (101.0, 548.0))
    check("coords: from_mouse is not clamped", os_input.from_mouse(0, 50, mon) == (-0.1, 0.0))

    mons = [{"left": 0, "top": 0, "width": 2880, "height": 1800},
            {"left": 2880, "top": 0, "width": 1920, "height": 1080}]
    with patched(os_input, monitors=lambda: [dict(m) for m in mons], _PLAT="win32"):
        m = os_input.monitor_for_qt((2881, 1, 1920, 1080), 1.0)
        check("qt: origin match within 2 px", m["left"] == 2880)
        m = os_input.monitor_for_qt((1440, 0, 960, 540), 2.0)
        check("qt: dpr-scaled origin match", m["left"] == 2880 and m["width"] == 1920)
        m = os_input.monitor_for_qt((5000, 0, 800, 600), 1.5)
        check("qt: no match on Windows -> scaled geometry",
              m == {"left": 7500, "top": 0, "width": 1200, "height": 900}, str(m))
        check("monitor_at: inside", os_input.monitor_at(3000, 10)["left"] == 2880)
        check("monitor_at: outside -> nearest", os_input.monitor_at(9000, 10)["left"] == 2880)
    with patched(os_input, monitors=lambda: [], _PLAT="darwin"):
        m = os_input.monitor_for_qt((10, 20, 800, 600), 2.0)
        check("qt: no match on macOS -> the points themselves",
              m == {"left": 10, "top": 20, "width": 800, "height": 600}, str(m))


def test_x11_scroll() -> None:
    a = os_input._Accum(40)
    check("accum: 30 px is no notch", a.add(30) == 0)
    check("accum: 60 px is one notch", a.add(30) == 1)
    check("accum: remainder kept", abs(a.rest - 20) < 1e-9)
    check("accum: negative crosses back", a.add(-70) == -1 and abs(a.rest + 10) < 1e-9)

    calls = []
    fake_pg = SimpleNamespace(scroll=lambda n: calls.append(("v", n)),
                              hscroll=lambda n: calls.append(("h", n)))

    @contextlib.contextmanager
    def pg():
        yield fake_pg

    with patched(os_input, _pg=pg):
        b = os_input._X11Backend()
        for _ in range(3):
            b.scroll(0, 25)
        check("x11: 75 px up -> one notch up", calls == [("v", 1)], str(calls))
        b.scroll(0, 5)
        check("x11: +5 px completes the second notch", calls == [("v", 1), ("v", 1)])
        b.scroll(45, 0)
        check("x11: dx > 0 (reveal left) is a negative hscroll", calls[-1] == ("h", -1))


def test_pg_restores() -> None:
    fake = SimpleNamespace(PAUSE=0.05, FAILSAFE=True)
    lazy = SimpleNamespace(_resolve=lambda: fake)
    with patched(os_input, pyautogui=lazy):
        with os_input._pg() as m:
            inside = (m.PAUSE, m.FAILSAFE)
        check("pg: PAUSE 0 and FAILSAFE off inside", inside == (0.0, False))
        check("pg: previous values restored", (fake.PAUSE, fake.FAILSAFE) == (0.05, True))


def test_click_tracker() -> None:
    c = os_input._ClickTracker()
    s1 = c.down("left", 10, 10, 0.0)
    c.up("left", 10, 10, 0.05)
    s2 = c.down("left", 12, 11, 0.3)
    c.up("left", 12, 11, 0.35)
    s3 = c.down("left", 12, 11, 0.95)
    c.up("left", 12, 11, 1.0)
    check("clicks: 1, then 2 within 0.5 s / 4 pt, then 1 after 0.6 s",
          (s1, s2, s3) == (1, 2, 1), str((s1, s2, s3)))
    s4 = c.down("left", 30, 11, 1.1)
    c.up("left", 30, 11, 1.1)
    check("clicks: too far away starts over", s4 == 1)
    s5 = c.down("right", 30, 11, 1.2)
    c.up("right", 30, 11, 1.2)
    check("clicks: another button starts over", s5 == 1)
    c.note("left", 0, 0, 2.0, 1)
    check("clicks: a double right after a click sends only state 2",
          c.first_of("left", 1, 1, 2.2, 2) == 2)
    check("clicks: a double out of the blue sends 1 and 2",
          c.first_of("left", 1, 1, 3.0, 2) == 1)


class FakeQ(SimpleNamespace):
    pass


def _fake_quartz():
    return FakeQ(
        kCGEventMouseMoved="moved", kCGEventLeftMouseDragged="ldrag",
        kCGEventRightMouseDragged="rdrag", kCGEventLeftMouseDown="ldown",
        kCGEventLeftMouseUp="lup", kCGEventRightMouseDown="rdown",
        kCGEventRightMouseUp="rup", kCGMouseButtonLeft=0, kCGMouseButtonRight=1)


class FakeMacMouse(os_input._MacBackend):
    def __init__(self):
        super().__init__()
        self.events = []
        self._Q = _fake_quartz()
        self.pos = (100.0, 100.0)

    def _q(self):
        return self._Q

    def cursor(self):
        return self.pos

    def _mouse_event(self, etype, x, y, button, state=0):
        self.events.append((etype, state))
        if etype in ("moved", "ldrag", "rdrag"):
            self.pos = (x, y)


def test_drag_and_states() -> None:
    b = FakeMacMouse()
    b.move(110, 100)
    b.button(True, "left")
    b.move(150, 120)
    b.move(160, 130)
    b.button(False, "left")
    b.move(170, 130)
    kinds = [e[0] for e in b.events]
    check("mac: a held button turns moves into drags",
          kinds == ["moved", "ldown", "ldrag", "ldrag", "lup", "moved"], str(kinds))
    b.events.clear()
    b.button(True, "right")
    b.move(180, 130)
    b.button(False, "right")
    check("mac: right drag", [e[0] for e in b.events] == ["rdown", "rdrag", "rup"])

    b = FakeMacMouse()
    b.click("left", 1)
    b.click("left", 2)
    check("mac: click then double sends states 1,1 then only 2,2",
          b.events == [("ldown", 1), ("lup", 1), ("ldown", 2), ("lup", 2)], str(b.events))
    b = FakeMacMouse()
    b.click("left", 2)
    check("mac: a cold double click sends 1,1,2,2",
          [s for _k, s in b.events] == [1, 1, 2, 2], str(b.events))
    b = FakeMacMouse()
    b.button(True, "left")
    b.release_all()
    check("mac: release_all lets go of a held button",
          [e[0] for e in b.events] == ["ldown", "lup"] and not b.held)


def test_permission_off_mac() -> None:
    with patched(os_input, _PLAT="linux"):
        check("permissions: all None off macOS",
              os_input.permission_state() == {"accessibility": None, "post_events": None})
        os_input.request_permission()
        check("permissions: request is a no-op off macOS", True)


class Boom:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise RuntimeError(f"boom in {name}")
        return fail


def test_nothing_raises() -> None:
    from core import screenact
    os_input._set_backend(Boom())
    try:
        with patched(os_input, _PLAT="win32"), \
                patched(screenact, type_text=Boom().type_text):
            results = {}
            for name, call in [
                ("cursor", lambda: os_input.cursor()),
                ("move", lambda: os_input.move(1, 2)),
                ("button", lambda: os_input.button(True)),
                ("click", lambda: os_input.click("left", 2)),
                ("scroll", lambda: os_input.scroll(3, 4)),
                ("release_all", lambda: os_input.release_all()),
                ("key", lambda: os_input.key("a")),
                ("hotkey", lambda: os_input.hotkey("ctrl+w")),
                ("type_text", lambda: os_input.type_text("hi")),
                ("key_down", lambda: os_input.key_down("esc")),
                ("nav", lambda: os_input.nav("back")),
                ("space", lambda: os_input.space("left")),
                ("overview", lambda: os_input.overview()),
                ("app_windows", lambda: os_input.app_windows()),
                ("show_desktop", lambda: os_input.show_desktop()),
                ("launcher", lambda: os_input.launcher()),
                ("close_window", lambda: os_input.close_window()),
                ("self_check", lambda: os_input.self_check()),
                ("available", lambda: os_input.available()),
                ("focused_text_field", lambda: os_input.focused_text_field()),
                ("focused_field_value", lambda: os_input.focused_field_value()),
                ("to_mouse", lambda: os_input.to_mouse(0.5, 0.5, {})),
                ("from_mouse", lambda: os_input.from_mouse(1, 1, {})),
                ("monitor_for_qt", lambda: os_input.monitor_for_qt(None, 1)),
            ]:
                try:
                    results[name] = call()
                except Exception as e:      # the failure this test exists for
                    results[name] = e
            raised = [k for k, v in results.items() if isinstance(v, BaseException)]
            check("robust: no public function raises with a broken backend", not raised,
                  str(raised))
            check("robust: Status functions report False",
                  all(results[k][0] is False for k in
                      ("key", "hotkey", "type_text", "nav", "space", "overview",
                       "app_windows", "show_desktop", "launcher", "close_window",
                       "self_check")),
                  str({k: results[k] for k in ("key", "launcher", "self_check")}))
            check("robust: cursor None, key_down None",
                  results["cursor"] is None and results["key_down"] is None)
            pump = os_input.InputPump(cursor_hz=50)
            pump.move(10, 10)
            pump.post(os_input.click, "left", 1)
            time.sleep(0.05)
            pump.close()
            check("robust: a pump over a broken backend closes cleanly", True)
    finally:
        os_input._set_backend(None)


class Follow(os_input._Backend):
    def __init__(self, frozen=False):
        super().__init__()
        self.pos = (500.0, 400.0)
        self.frozen = frozen

    def cursor(self):
        return self.pos

    def _move(self, x, y, drag):
        if not self.frozen:
            self.pos = (x, y)


def test_self_check() -> None:
    mons = [{"left": 0, "top": 0, "width": 1440, "height": 900}]
    with patched(os_input, monitors=lambda: mons):
        b = Follow()
        os_input._set_backend(b)
        try:
            ok, why = os_input.self_check()
            check("self_check: ok when the cursor follows", ok, why)
            check("self_check: the cursor is put back", b.pos == (500.0, 400.0))
            os_input._set_backend(Follow(frozen=True))
            ok, why = os_input.self_check()
            check("self_check: not ok when the cursor is frozen", not ok and why, why)
        finally:
            os_input._set_backend(None)


def test_field_from() -> None:
    other_pid = 2 ** 22 + 12345          # no such process: not ours
    text = {"AXRole": "AXTextField", "pid": other_pid, "AXPosition": (10, 20),
            "AXSize": (200, 30), "AXBoundsForRange": (15, 22, 1, 18), "app": "Notes"}
    f = os_input._field_from(text.get)
    check("field: a text field", f is not None and f.role == "AXTextField"
          and f.rect == (10.0, 20.0, 200.0, 30.0) and f.caret == (15.0, 22.0, 1.0, 18.0)
          and f.secure is False and f.pid == other_pid and f.app == "Notes", str(f))
    sec = dict(text, AXSubrole="AXSecureTextField")
    f = os_input._field_from(sec.get)
    check("field: secure by subrole", f is not None and f.secure is True)
    sec2 = dict(text, AXRoleDescription="secure text field")
    f = os_input._field_from(sec2.get)
    check("field: secure by role description (Qt)", f is not None and f.secure is True)
    own = dict(text, pid=os.getpid())
    check("field: our own field -> None", os_input._field_from(own.get) is None)
    btn = {"AXRole": "AXButton", "pid": other_pid, "AXPosition": (0, 0), "AXSize": (5, 5)}
    check("field: a button -> None", os_input._field_from(btn.get) is None)
    web = {"AXRole": "AXGroup", "AXSelectedTextRange": True, "settable:AXValue": True,
           "pid": other_pid, "AXBoundsForRange": (0, 0, 0, 0)}
    f = os_input._field_from(web.get)
    check("field: editable by selection + settable value", f is not None)
    check("field: a zero caret rect is None", f is not None and f.caret is None)
    ro = dict(web, **{"settable:AXValue": False})
    check("field: read-only selectable text -> None", os_input._field_from(ro.get) is None)


def test_screenview_ignored() -> None:
    from core import screenview
    check("screenview: IGNORED_WINDOW_NUMBERS is a set",
          isinstance(getattr(screenview, "IGNORED_WINDOW_NUMBERS", None), set))


def main_() -> int:
    test_pump()
    test_key_spec()
    test_hotkey_normalise()
    test_coords()
    test_x11_scroll()
    test_pg_restores()
    test_click_tracker()
    test_drag_and_states()
    test_permission_off_mac()
    test_nothing_raises()
    test_self_check()
    test_field_from()
    test_screenview_ignored()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
