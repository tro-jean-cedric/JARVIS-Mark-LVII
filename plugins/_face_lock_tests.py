"""
Self-test for Face Lock's PIN and lock-screen decision.

    python -m plugins._face_lock_tests

No camera, no window, no saved face: the lock state is set directly and the
settings are stubbed. (Leading underscore: the plugin loader skips this file.)
"""
from __future__ import annotations

import sys

from plugins import _face_lock_core as fl

_ok = _fail = 0


def check(name: str, cond, detail="") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


def with_pin(pin: str):
    fl._pin = lambda: pin
    fl._pin_state.update(wrong=0, until=0.0)


def test_pin():
    print("\n[1] the unlock PIN")
    real_time = fl.time.time
    now = [1000.0]
    fl.time.time = lambda: now[0]
    try:
        with_pin("4321")
        fl._set_locked(True)
        ok, msg = fl.try_pin("0000")
        check("a wrong PIN does not unlock, and says how many tries are left",
              not ok and fl.is_locked() and "4 tries left" in msg, msg)
        ok, msg = fl.try_pin(" 4321 ")
        check("the right PIN unlocks (spaces ignored)", ok and not fl.is_locked(), msg)

        fl._set_locked(True)
        for _ in range(4):
            fl.try_pin("1111")
        ok, msg = fl.try_pin("1111")
        check("five wrong in a row -> a 30 s wait", not ok and "wait 30" in msg, msg)
        ok, msg = fl.try_pin("4321")
        check("even the right PIN waits until then", not ok and fl.is_locked()
              and "wait" in msg, msg)
        now[0] += 31
        ok, msg = fl.try_pin("4321")
        check("after the wait the right PIN works", ok and not fl.is_locked(), msg)

        with_pin("")
        fl._set_locked(True)
        ok, msg = fl.try_pin("")
        check("no PIN set: nothing unlocks, and it says why", not ok and "No unlock PIN" in msg
              and fl.is_locked(), msg)
        check("pin_set() reflects the setting", not fl.pin_set())
        with_pin("9")
        check("pin_set() with a PIN", fl.pin_set())
    finally:
        fl.time.time = real_time
        fl._set_locked(False)


def test_cover_decision():
    print("\n[2] the lock screen covers the computer only with a PIN to get past it")
    import plugins.face_lock as plugin
    calls, hud = [], []

    class P:
        def show_lock_screen(self, locked, owner=""):
            calls.append((locked, owner))

    from plugins import _announce
    _announce.remember(P())
    settings = {"lock_screen": True, "owner_name": "Alex"}
    plugin._setting = lambda k, d=None: settings.get(k, d)
    real_show = plugin._show_camera
    plugin._show_camera = lambda: hud.append(1)
    try:
        with_pin("4321")
        fl._set_locked(True)
        fl._set_locked(False)
        check("locked with a PIN: the cover, with the owner's name; unlocked: it goes",
              calls == [(True, "Alex"), (False, "")] and not hud, (calls, hud))
        calls.clear()
        with_pin("")
        fl._set_locked(True)
        check("no PIN: no cover (a broken camera would lock the owner out) - the HUD view instead",
              not any(c[0] for c in calls) and hud == [1], (calls, hud))
        fl._set_locked(False)
        calls.clear()
        hud.clear()
        with_pin("4321")
        settings["lock_screen"] = False
        fl._set_locked(True)
        check("cover switched off in the settings: the HUD view", not any(c[0] for c in calls)
              and hud == [1], (calls, hud))
    finally:
        plugin._show_camera = real_show
        fl._set_locked(False)
        _announce.remember(None)


def main() -> int:
    print("face lock self-test")
    for fn in (test_pin, test_cover_decision):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, e)
    print(f"\n{_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
