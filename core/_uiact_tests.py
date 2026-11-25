"""
Self-test for working programs without the mouse (core/uiact.py) and the two
callers that use it (actions/app_control.py, plugins/screen_agent.py).

    python -m core._uiact_tests            # fake backend: no screen, no permission
    python -m core._uiact_tests --live     # also drive a real test window (macOS,
                                           # native AppKit; needs the
                                           # Accessibility permission)

The fake backend behaves like a program whose controls can lie or refuse:
a toggle that does not move, a field that changes the text, a slider that
snaps, a list that ignores the pick. Every verdict uiact gives is checked
against what the fake program really holds.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import uiact  # noqa: E402
from core.uiact import Control  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeProgram(uiact.Backend):
    """A program with a handful of controls, and switches for misbehaving."""
    name = "fake"

    def __init__(self):
        self.reset()

    def reset(self):
        self.c = [
            Control(0, "button", "Play"),
            Control(1, "button", "Play next"),
            Control(2, "checkbox", "Wi-Fi", checked=False),
            Control(3, "slider", "Volume", value="10"),
            Control(4, "edit", "Search"),
            Control(5, "edit", "Password", protected=True),
            Control(6, "edit", "PIN", value="••••"),
            Control(7, "button", "Delete", enabled=False),
            Control(8, "combobox", "Quality", value="Auto"),
            Control(9, "text", "Status", value="Stopped"),
            Control(10, "button", "Lecture"),            # "play" in French
        ]
        for c in self.c:
            c.pid = 42
        self.stuck_toggle = self.mangle_text = self.ignore_pick = False
        self.snap = 0
        self.typed: list[str] = []

    def available(self):
        return True, ""

    def scan(self, pid, budget_s):
        return [Control(**{k: getattr(c, k) for k in c.__dataclass_fields__}) for c in self.c]

    def refresh(self, c):
        now = self.scan(0, 0)
        return now[c.cid] if c.cid < len(now) else c

    def press(self, c):
        if c.name == "Play":
            self.c[9].value = "Playing"
        return None

    def set_checked(self, c, on):
        if not self.stuck_toggle:
            self.c[c.cid].checked = on
        return None

    def set_text(self, c, text):
        self.typed.append(text)
        self.c[c.cid].value = text.upper() if self.mangle_text else text
        return None

    def text_of(self, c):
        return self.c[c.cid].value

    def range_of(self, c):
        return 0.0, 100.0, float(self.c[c.cid].value)

    def set_number(self, c, number):
        self.c[c.cid].value = str(number + self.snap)
        return None

    def open_list(self, c):
        return [Control(20 + i, "listitem", n) for i, n in enumerate(["Auto", "720p", "1080p"])]

    def pick(self, item):
        if not self.ignore_pick:
            self.c[8].value = item.name
        return None


def test_find(fake: FakeProgram) -> None:
    items = fake.scan(42, 1)
    f = lambda w, **k: [c.name for c in uiact.find(items, w, **k)]
    check("find: exact name before longer ones", f("play")[:2] == ["Play", "Play next"])
    check("find: case, accents and spacing folded", f("wi fi") == ["Wi-Fi"] and f("WIFI") == [])
    check("find: role filter", f("play", roles=("checkbox",)) == [])
    check("find: plain text is not actionable", f("status") == []
          and f("status", actionable=False) == ["Status"])
    check("find: a secret field's value is never matched", f("••••") == [])


def test_actions(fake: FakeProgram) -> None:
    items = fake.scan(42, 1)
    by = {c.name: c for c in items}

    st, msg = uiact.press(by["Play"], pid=42, settle_s=0)
    check("press: done when the program changed", st == "done", msg)
    st, msg = uiact.press(by["Lecture"], pid=42, settle_s=0)
    check("press: unverified when nothing readable changed", st == "unverified", msg)
    st, msg = uiact.press(by["Delete"], pid=42)
    check("press: a disabled control is refused", st == "failed" and "disabled" in msg)

    st, msg = uiact.toggle(by["Wi-Fi"], True)
    check("toggle on: done, really on", st == "done" and fake.c[2].checked is True, msg)
    st, msg = uiact.toggle(by["Wi-Fi"], True)
    check("toggle on again: already on", st == "done" and "already" in msg)
    st, msg = uiact.toggle(by["Wi-Fi"])
    check("toggle with no wish flips it", st == "done" and fake.c[2].checked is False, msg)
    fake.stuck_toggle = True
    st, msg = uiact.toggle(by["Wi-Fi"], True)
    check("toggle that does not move: failed, says so", st == "failed" and "still off" in msg, msg)
    st, msg = uiact.toggle(by["Play"], True)
    check("toggle on a plain button: no on/off state", st == "failed" and "on/off" in msg)

    st, msg = uiact.set_text(by["Search"], "jazz")
    check("set text: done, read back", st == "done" and fake.c[4].value == "jazz", msg)
    st, msg = uiact.set_text(by["Search"], " piano", append=True)
    check("append text", st == "done" and fake.c[4].value == "jazz piano", msg)
    fake.mangle_text = True
    st, msg = uiact.set_text(by["Search"], "abc")
    check("a field that changes the text: failed", st == "failed", msg)
    typed = len(fake.typed)
    st, msg = uiact.set_text(by["Password"], "hunter2")
    check("password field (flagged): refused, nothing sent", st == "failed"
          and "password" in msg and len(fake.typed) == typed)
    st, msg = uiact.set_text(by["PIN"], "1234")
    check("secret field shown as bullets (not flagged): refused", st == "failed"
          and len(fake.typed) == typed)

    st, msg = uiact.set_number(by["Volume"], 40)
    check("set number: done", st == "done" and fake.c[3].value == "40", msg)
    fake.snap = 0.5
    st, msg = uiact.set_number(by["Volume"], 60)
    check("a slider that snaps within 1 % counts", st == "done", msg)
    fake.snap = 7
    st, msg = uiact.set_number(by["Volume"], 20)
    check("a slider that lands elsewhere: failed", st == "failed" and "not 20" in msg, msg)
    st, msg = uiact.set_number(by["Volume"], 400)
    check("out of range: refused before anything is sent", st == "failed" and "outside" in msg)

    names, why = uiact.options(by["Quality"])
    check("options", names == ["Auto", "720p", "1080p"], f"{names} {why}")
    st, msg = uiact.choose(by["Quality"], "1080P")
    check("choose: done, the control shows it", st == "done" and fake.c[8].value == "1080p", msg)
    st, msg = uiact.choose(by["Quality"], "4k")
    check("choose an entry that is not there: lists what is", st == "failed"
          and "720p" in msg, msg)
    fake.ignore_pick = True
    st, msg = uiact.choose(fake.refresh(by["Quality"]), "720p")
    check("a list that ignores the pick: failed, 'without the mouse'", st == "failed"
          and "without the mouse" in msg, msg)


def test_app_control(fake: FakeProgram) -> None:
    import actions.app_control as ac
    fake.reset()
    saved = ac._target_pid, ac._is_risky, ac._model_pick
    ac._target_pid = lambda app: (42, "Fake Player")
    ac._is_risky = lambda el, view: (False, "")
    ac._model_pick = lambda app, words, listing: 10 if words == "the play button" else None
    try:
        r = ac.app_control({"action": "press", "app": "fake", "control": "Play"})
        check("app_control press: done, without the mouse", r.startswith("status=done")
              and "without the mouse" in r, r)
        r = ac.app_control({"action": "press", "app": "fake", "control": "the play button"})
        check("app_control: the model picks the meant control (other language)",
              "Lecture" in r, r)
        r = ac.app_control({"action": "toggle", "app": "fake", "control": "wifi", "text": "on"})
        check("app_control toggle: no literal match, no model pick -> honest failure",
              r.startswith("status=failed"), r)
        r = ac.app_control({"action": "toggle", "app": "fake", "control": "Wi-Fi", "text": "on"})
        check("app_control toggle on", r.startswith("status=done") and fake.c[2].checked, r)
        r = ac.app_control({"action": "set", "app": "fake", "control": "volume", "text": "35,0"})
        check("app_control set (decimal comma)", r.startswith("status=done")
              and fake.c[3].value == "35.0", r)
        r = ac.app_control({"action": "choose", "app": "fake", "control": "Quality", "text": "720p"})
        check("app_control choose", r.startswith("status=done") and fake.c[8].value == "720p", r)
        r = ac.app_control({"action": "options", "app": "fake", "control": "Quality"})
        check("app_control options are wrapped as untrusted", "1080p" in r and "untrusted" in r.lower(), r)
        r = ac.app_control({"action": "type", "app": "fake", "control": "Password", "text": "x"})
        check("app_control type into a password field: refused", r.startswith("status=failed")
              and "password" in r, r)
        r = ac.app_control({"action": "set", "app": "fake", "control": "volume"})
        check("app_control set without a number asks", r.startswith("status=ask"), r)
        fake.ignore_pick = True
        r = ac.app_control({"action": "choose", "app": "fake", "control": "Quality", "text": "Auto"})
        check("app_control: 'not without the mouse' -> points to the screen agent",
              r.startswith("status=failed") and "screen_agent" in r, r)

        ac._is_risky = lambda el, view: (True, "Press Play")
        import core.confirm as confirm
        asked = []
        saved_req = confirm.request
        confirm.request = lambda key, title, detail, run: (asked.append((title, run)),
                                                           "[CONFIRMATION_PENDING]")[1]
        try:
            before = fake.c[9].value = "Stopped"
            r = ac.app_control({"action": "press", "app": "fake", "control": "Play"})
            check("app_control: a risky press waits for the user's confirm",
                  r == "[CONFIRMATION_PENDING]" and fake.c[9].value == before and asked)
            done = asked[0][1]()
            check("…and happens once confirmed", done.startswith("status=done")
                  and fake.c[9].value == "Playing", done)
        finally:
            confirm.request = saved_req
    finally:
        ac._target_pid, ac._is_risky, ac._model_pick = saved


def test_screen_agent_quick(fake: FakeProgram) -> None:
    import actions.app_control as ac
    import plugins.screen_agent as sa
    fake.reset()
    saved = ac._target_pid, ac._is_risky
    ac._target_pid = lambda app: (42, "Fake Player")
    ac._is_risky = lambda el, view: (False, "")
    try:
        r = sa._without_mouse({"quick": True, "app": "fake", "control": "Wi-Fi", "do": "toggle",
                               "text": "on"})
        check("screen_agent quick: done at once without the loop",
              r is not None and r.startswith("status=done") and fake.c[2].checked)
        r = sa._without_mouse({"quick": True, "app": "fake", "control": "Search", "do": "type",
                               "text": "news"})
        check("screen_agent quick type replaces the text", r is not None
              and fake.c[4].value == "news")
        fake.ignore_pick = True
        r = sa._without_mouse({"quick": True, "app": "fake", "control": "Quality", "do": "choose",
                               "text": "720p"})
        check("screen_agent quick: left to the look-and-click loop when it cannot", r is None)
        r = sa._without_mouse({"quick": True, "app": "fake", "control": "Play", "do": "drag"})
        check("screen_agent quick: an unknown kind of step goes to the loop", r is None)
    finally:
        ac._target_pid, ac._is_risky = saved


def test_unavailable() -> None:
    uiact.use_backend(uiact.Backend())
    try:
        check("no backend: available() says why", uiact.available()[0] is False)
        items, why = uiact.controls(42)
        check("no backend: no controls, a reason", items == [] and why)
        import actions.app_control as ac
        check("no backend: app_control falls back (None)",
              ac.without_mouse("press", "x", "Play") is None)
    finally:
        uiact.use_backend(None)


def live() -> None:
    """The real test windows, driven through the real backend."""
    if sys.platform != "darwin":
        print("SKIP  live: the test windows are macOS-only for now")
        return
    ok, why = uiact.available()
    if not ok:
        print(f"SKIP  live: {why}")
        return
    import tempfile
    here = Path(__file__).resolve().parent
    for fixture in ("_uiact_fixture_mac.py",):
        with tempfile.TemporaryDirectory() as tmp:
            print(f"--- live: {fixture}")
            r = subprocess.run([sys.executable, str(here / "_uiact_live.py"), str(ROOT),
                                str(here / fixture), str(Path(tmp) / "state.json")],
                               capture_output=True, text=True, timeout=240)
            out = [l for l in r.stdout.splitlines() if l.startswith(("PASS", "FAIL", "INFO"))]
            print("\n".join(out))
            ok = r.returncode == 0 and "ALL PASSED" in r.stdout
            if not ok:
                print(r.stderr[-3000:])
            check(f"live: {fixture}", ok)


def main() -> int:
    fake = FakeProgram()
    uiact.use_backend(fake)
    try:
        test_find(fake)
        test_actions(fake)
        test_app_control(fake)
        test_screen_agent_quick(fake)
    finally:
        uiact.use_backend(None)
    test_unavailable()
    if "--live" in sys.argv:
        live()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
