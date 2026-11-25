"""Drive the test window (core/_uiact_fixture_mac.py) through
core/uiact.py and actions/app_control.py for real, with the window in the
BACKGROUND, and check every result against the window's own state file.
Run by `python -m core._uiact_tests --live`; by hand:
    python core/_uiact_live.py <repo> <fixture.py> <state.json>"""
import json
import subprocess
import sys
import time

repo, fixture, state_path = sys.argv[1:4]
NATIVE = fixture.endswith("_mac.py")
sys.path.insert(0, repo)

from core import screenview, uiact  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


def state():
    time.sleep(0.35 if NATIVE else 0.05)
    for _ in range(10):
        try:
            return json.load(open(state_path))
        except Exception:
            time.sleep(0.1)
    return {}


proc = subprocess.Popen([sys.executable, fixture, state_path])
try:
    time.sleep(3.0)
    subprocess.run(["osascript", "-e", 'tell application "Finder" to activate'], check=False)
    time.sleep(1.0)
    front = screenview.foreground_pid()
    check("the fixture window is in the background", front != proc.pid, f"front={front}")

    t = time.monotonic()
    items, why = uiact.controls(proc.pid)
    print(f"INFO  {len(items)} controls in {time.monotonic() - t:.2f}s via {uiact.backend().name}")
    for c in items:
        print("      ", c.line())
    check("controls read from a background window", bool(items), why)

    def one(words, roles=()):
        hits = uiact.find(items, words, roles=roles)
        return hits[0] if hits else None

    c = one("Count", roles=("button",))
    st, msg = uiact.press(c, proc.pid)
    if NATIVE:   # nothing on this window shows the count: honest "unverified"
        check("press: really pressed; says unverified since nothing visible changed",
              st == "unverified" and state().get("clicks") == 1, f"{st} {msg} {state()}")
    else:
        check("press: button pressed, program changed, read back", st == "done"
              and state().get("clicks") == 1, f"{st} {msg} {state()}")

    c = one("Dark mode")
    st, msg = uiact.toggle(c, True)
    check("toggle on", st == "done" and state().get("dark") is True, f"{st} {msg}")
    st, msg = uiact.toggle(c, True)
    check("toggle on again: already on, nothing changed", st == "done" and "already" in msg
          and state().get("dark") is True, f"{st} {msg}")
    st, msg = uiact.toggle(c, False)
    check("toggle off", st == "done" and state().get("dark") is False, f"{st} {msg}")

    c = one("Volume")
    st, msg = uiact.set_number(c, 40)
    check("set number 40", st == "done" and state().get("volume") == 40, f"{st} {msg} {state()}")
    st, msg = uiact.set_number(c, 400)
    check("set number out of range refused", st == "failed" and "outside" in msg
          and state().get("volume") == 40, f"{st} {msg}")

    c = one("Note")
    st, msg = uiact.set_text(c, "hello there")
    check("set text", st == "done" and state().get("note") == "hello there", f"{st} {msg} {state()}")
    st, msg = uiact.set_text(c, " friend", append=True)
    check("append text", st == "done" and state().get("note") == "hello there friend",
          f"{st} {msg} {state()}")

    c = one("Password")
    st, msg = uiact.set_text(c, "secret") if c else ("missing", "no password control")
    check("password field refused, nothing typed", st == "failed" and "password" in msg
          and state().get("password") == "", f"{st} {msg}")

    c = one("Locked")
    st, msg = uiact.press(c, proc.pid) if c else ("missing", "no Locked control")
    check("disabled button refused", st == "failed" and "disabled" in msg
          and state().get("clicks") == 1, f"{st} {msg}")

    c = one("Apple", roles=("combobox",))
    names, why = uiact.options(c) if c else ([], "no drop-down showing Apple")
    check("options of the drop-down", names[:3] == ["Apple", "Banana", "Cherry"], f"{names} {why}")
    st, msg = uiact.choose(c, "cherry") if c else ("missing", "")
    if NATIVE:
        check("choose an entry", state().get("fruit") == "Cherry" and st == "done", f"{st} {msg} {state()}")
    else:
        check("choose (Qt ignores it): says so, list closed again, nothing changed",
              st == "failed" and "without the mouse" in msg and uiact.backend().is_open(c) is False
              and state().get("fruit") == "Apple", f"{st} {msg} {state()}")

    # The action the voice model calls, end to end, still in the background.
    import actions.app_control as ac
    ac._target_pid = lambda app: (proc.pid, "UIAct Fixture")
    ac._is_risky = lambda el, view: (False, "")
    r = ac.app_control({"action": "press", "control": "Count", "app": "fixture"})
    check("app_control press", r.startswith("status=unverified" if NATIVE else "status=done")
          and state().get("clicks") == 2, r)
    r = ac.app_control({"action": "toggle", "control": "dark mode", "text": "on", "app": "fixture"})
    check("app_control toggle", r.startswith("status=done") and state().get("dark") is True, r)
    r = ac.app_control({"action": "set", "control": "volume", "text": "75", "app": "fixture"})
    check("app_control set", r.startswith("status=done") and state().get("volume") == 75, r)
    r = ac.app_control({"action": "type", "control": "Note", "text": "new text",
                        "replace": True, "app": "fixture"})
    check("app_control type (replace)", r.startswith("status=done")
          and state().get("note") == "new text", r)
    r = ac.app_control({"action": "choose", "control": state().get("fruit"), "text": "Banana", "app": "fixture"})
    if NATIVE:
        check("app_control choose", state().get("fruit") == "Banana" and r.startswith("status=done"), r)
    else:
        check("app_control choose (Qt): honest failure pointing to the screen agent",
              r.startswith("status=failed") and "screen_agent" in r, r)
    r = ac.app_control({"action": "type", "control": "Password", "text": "x", "app": "fixture"})
    check("app_control refuses the password field", r.startswith("status=failed")
          and state().get("password") == "", r)

    check("the fixture stayed in the background throughout",
          screenview.foreground_pid() != proc.pid)
finally:
    proc.terminate()
    proc.wait(5)

print(f"\n{len(fails)} failed" if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
