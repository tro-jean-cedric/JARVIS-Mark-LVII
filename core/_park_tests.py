"""
Self-test for "sleep now, wake by itself at a time" (core/park.py), its tool
action (actions/remote_mode.py park) and Remote Mode's settings section.

    python -m core._park_tests

Nothing sleeps: the system commands and the clock are stand-ins.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import park  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class Clock:
    """Wall clock that moves one second per tick, and jumps when 'asleep'."""

    def __init__(self, start: float):
        self.t = start
        self.lock = threading.Lock()

    def now(self) -> float:
        with self.lock:
            return self.t

    def tick(self, s: float) -> None:
        time.sleep(0.001)
        with self.lock:
            self.t += s

    def jump(self, s: float) -> None:
        with self.lock:
            self.t += s


class Runner:
    def __init__(self, clock: Clock = None, sleep_for: float = 0, fail: tuple = ()):
        self.calls, self.clock, self.sleep_for, self.fail = [], clock, sleep_for, fail

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if any(f in joined for f in self.fail):
            return subprocess.CompletedProcess(cmd, 1, "", "not permitted")
        if self.clock and ("suspend" in joined or "sleepnow" in joined) and self.sleep_for:
            clock, s = self.clock, self.sleep_for
            threading.Timer(0.05, lambda: clock.jump(s)).start()
        return subprocess.CompletedProcess(cmd, 0, "", "")


class Patch:
    def __init__(self, **kw):
        self.kw, self.saved = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            obj, name = (park, k) if "." not in k else (sys.modules[k.rsplit(".", 1)[0]], k.rsplit(".", 1)[1])
            self.saved[k] = (obj, name, getattr(obj, name))
            setattr(obj, name, v)
        return self

    def __exit__(self, *a):
        for obj, name, v in self.saved.values():
            setattr(obj, name, v)


NOON = dt.datetime(2026, 11, 25, 12, 0, 0).timestamp()


def test_wake_time() -> None:
    check("minutes from now", park.wake_time(minutes=30, now=NOON) == NOON + 1800)
    t = dt.datetime.fromtimestamp(park.wake_time("18:30", now=NOON))
    check("a clock time later today", (t.day, t.hour, t.minute) == (25, 18, 30), str(t))
    t = dt.datetime.fromtimestamp(park.wake_time("07:00", now=NOON))
    check("a clock time already past means tomorrow", (t.day, t.hour) == (26, 7), str(t))
    t = dt.datetime.fromtimestamp(park.wake_time("7h30", now=NOON))
    check("7h30 is read", (t.hour, t.minute) == (7, 30), str(t))
    check("a full date is read", park.wake_time("2026-11-27 06:15", now=NOON)
          == dt.datetime(2026, 11, 27, 6, 15).timestamp())
    check("nonsense is None", park.wake_time("whenever", now=NOON) is None
          and park.wake_time("25:00", now=NOON) is None and park.wake_time("", now=NOON) is None)


def test_judge() -> None:
    asked = NOON + 3600
    check("judge: no sleep", park.judge(asked, None)[0] == "no-sleep")
    check("judge: woken early by something else", park.judge(asked, asked - 1200)[0] == "woken-otherwise")
    check("judge: woken by the timer", park.judge(asked, asked + 30)[0] == "woken")
    st, text = park.judge(asked, asked + 3 * 3600)
    check("judge: slept past it, wake timers named", st == "late" and "wake timers" in text, text)


def test_watch() -> None:
    c = Clock(NOON)
    with Patch(_now=c.now, _tick=c.tick):
        threading.Timer(0.05, lambda: c.jump(3600)).start()
        back = park.watch_resume(NOON + 3600, NOON)
        check("watch: a jump in the wall clock is a sleep", back is not None and back >= NOON + 3600, str(back))
        c2 = Clock(NOON)
        with Patch(_now=c2.now, _tick=c2.tick):
            check("watch: no jump within the limit means no sleep", park.watch_resume(NOON + 999, NOON) is None)


def test_supported() -> None:
    which = lambda name: f"/usr/bin/{name}"
    with Patch(_OS="Linux", _run=Runner(), **{"shutil.which": which}):
        ok, how = park.supported()
        check("linux: rtcwake allowed through sudo", ok and "rtcwake" in how, how)
    with Patch(_OS="Linux", _run=Runner(fail=("sudo",)), **{"shutil.which": which}):
        ok, why = park.supported()
        check("linux: no sudo rights explained", not ok and "administrator" in why, why)
    with Patch(_OS="Linux", **{"shutil.which": lambda n: None if n == "rtcwake" else "/x"}):
        ok, why = park.supported()
        check("linux: no rtcwake explained", not ok and "util-linux" in why, why)
    with Patch(_OS="Darwin", _run=Runner(fail=("sudo",)), **{"shutil.which": which}):
        ok, why = park.supported()
        check("macOS: without admin rights the daily command is given",
              not ok and "pmset repeat wakeorpoweron" in why, why)
    with Patch(_OS="Windows"):
        ok, why = park.supported()
        check("windows functions missing (not Windows) -> explained", not ok and why, why)
    with Patch(_OS="Plan9"):
        check("other systems refused", park.supported()[0] is False)


def test_park() -> None:
    which = lambda name: f"/usr/bin/{name}"
    c = Clock(NOON)
    run = Runner(clock=c, sleep_for=3600)
    said = []
    with Patch(_OS="Linux", _run=run, _now=c.now, _tick=c.tick, **{"shutil.which": which}):
        st, text = park.park(NOON + 3600, on_resume=said.append, wait=True)
    flat = [" ".join(x) for x in run.calls]
    check("linux: alarm set through sudo, then suspend",
          any(f.startswith("sudo -n rtcwake -m no -s 3600") for f in flat)
          and flat[-1] == "systemctl suspend", str(flat))
    check("linux: woken by the timer reported", st == "woken" and said and "as planned" in said[0], f"{st} {said}")

    c = Clock(NOON)
    run = Runner(clock=c, sleep_for=600)
    said = []
    with Patch(_OS="Darwin", _run=run, _now=c.now, _tick=c.tick, **{"shutil.which": which}):
        st, text = park.park(NOON + 3600, on_resume=said.append, wait=True)
    stamp = dt.datetime.fromtimestamp(NOON + 3600).strftime("%m/%d/%y %H:%M:%S")
    flat = [" ".join(x) for x in run.calls]
    check("macOS: pmset schedule wake with the right time, then sleepnow",
          f"sudo -n pmset schedule wake {stamp}" in flat and flat[-1] == "pmset sleepnow", str(flat))
    check("macOS: woken early reported as such", st == "woken-otherwise", f"{st} {text}")

    c = Clock(NOON)
    run = Runner(clock=c, fail=("sleepnow",))
    said = []
    with Patch(_OS="Darwin", _run=run, _now=c.now, _tick=c.tick, **{"shutil.which": which}):
        st, text = park.park(NOON + 3600, on_resume=said.append)
        time.sleep(0.3)
    flat = [" ".join(x) for x in run.calls]
    check("sleep refused: failed, and the wake taken back",
          st == "failed" and any("schedule cancel wake" in f for f in flat), f"{st} {flat}")
    check("sleep refused: nothing reported later", said == [], str(said))

    c = Clock(NOON)
    run = Runner(clock=c, fail=("-n pmset schedule wake",))
    with Patch(_OS="Darwin", _run=run, _now=c.now, _tick=c.tick, **{"shutil.which": which}):
        st, text = park.park(NOON + 3600)
    check("wake refused: not put to sleep", st == "failed" and not any("sleepnow" in " ".join(x) for x in run.calls),
          f"{st} {run.calls}")

    with Patch(_now=lambda: NOON):
        check("too soon refused", park.park(NOON + 30)[0] == "failed")
        check("too far refused", park.park(NOON + 9 * 86400)[0] == "failed")
    with Patch(_now=lambda: NOON, supported=lambda: (False, "no way")):
        st, text = park.park(NOON + 3600)
        check("unsupported says why", st == "unsupported" and "no way" in text, text)


def test_action() -> None:
    from actions import remote_mode as act
    from core import confirm
    asked = {}
    saved = (confirm.request, confirm.pending_title, park.supported, park.park)
    try:
        confirm.pending_title = lambda: ""
        confirm.request = lambda key, title, detail, run: (asked.update(key=key, title=title, run=run)
                                                            or "ask: confirm on screen")
        park.supported = lambda: (False, "needs an administrator")
        out = act.remote_mode({"action": "park", "wake_at": "07:00"})
        check("action: unsupported answered without asking to confirm", "administrator" in out and not asked, out)
        park.supported = lambda: (True, "test")
        out = act.remote_mode({"action": "park"})
        check("action: no time -> asks for one", "When should it wake" in out and not asked, out)
        out = act.remote_mode({"action": "park", "minutes": 1})
        check("action: too soon refused before confirming", "2 minutes" in out and not asked, out)
        got = []
        park.park = lambda at, on_resume=None: (got.append((at, on_resume)) or ("asleep", "Going to sleep now"))

        class P:
            def request_say(self, text):
                pass
        player = P()
        out = act.remote_mode({"action": "park", "minutes": 60}, player=player)
        check("action: goes through the on-screen confirm", asked.get("key") == "remote_mode.park"
              and "wake by itself" in asked.get("title", "") and out.startswith("ask"), str(asked))
        res = asked["run"]()
        check("action: confirmed -> parks, resume spoken through request_say",
              res == "Going to sleep now" and got and got[0][1] == player.request_say, str(got))
        decl = act.TOOL["parameters"]["properties"]
        check("tool: park and wake_at declared", "park" in decl["action"]["enum"] and "wake_at" in decl)
    finally:
        confirm.request, confirm.pending_title, park.supported, park.park = saved


def test_settings() -> None:
    from core import remote_mode as rm
    sec = rm.settings_section()
    keys = [f["key"] for f in sec["fields"]]
    check("settings: section fields", keys == ["keep_awake", "lock_when_idle", "lock_minutes"], str(keys))
    check("settings: guide attached and mentions sleeping until a time",
          sec["guide"] and any("wake up at 7" in str(s) for s in sec["guide"]["steps"]))
    check("settings: APPLY NOW button", sec["action"]["label"] == "APPLY NOW" and callable(sec["action"]["run"]))
    calls = []
    saved = (rm.keep_awake, rm.lock_when_idle, rm.save_settings, rm.status_text)
    try:
        rm.keep_awake = lambda on: (calls.append(("awake", on)) or (True, ""))
        rm.lock_when_idle = lambda on: (calls.append(("lock", on)) or (True, ""))
        rm.save_settings = lambda **kw: calls.append(("save", kw))
        rm.status_text = lambda: "Remote Mode: status"
        ok, msg = rm.apply_settings({"keep_awake": True, "lock_when_idle": True, "lock_minutes": "999"})
        check("apply: switches set and minutes kept in range",
              ok and ("awake", True) in calls and calls[-1] == ("lock", True)
              and ("save", {"lock_minutes": 240}) in calls, str(calls))
        ok, msg = rm.apply_settings({"lock_minutes": "soon"})
        check("apply: bad minutes refused", not ok and "whole number" in msg, msg)
        rm.keep_awake = lambda on: (False, "keeping awake is not supported here")
        ok, msg = rm.apply_settings({"keep_awake": True, "lock_minutes": "10"})
        check("apply: a failure is reported", not ok and "not supported" in msg, msg)
    finally:
        rm.keep_awake, rm.lock_when_idle, rm.save_settings, rm.status_text = saved

    import main

    class Reg:
        def settings_schemas(self):
            return [{"namespace": "x", "plugin": "x", "fields": []}]
    j = main.JarvisLive.__new__(main.JarvisLive)
    j._plugin_registry = Reg()
    names = [s["namespace"] for s in j._settings_sections()]
    check("main: Remote Mode section added after the plugins'", names == ["x", "remote_mode"], str(names))


def test_selftest() -> None:
    from core import selftest
    saved = park.supported
    try:
        park.supported = lambda: (False, "needs an administrator")
        st, detail, fix = selftest.check_park()
        check("selftest: OFF with the reason as the fix", st == selftest.OFF and "administrator" in fix)
        park.supported = lambda: (True, "a scheduled wake")
        check("selftest: PASS when possible", selftest.check_park()[0] == selftest.PASS)
        check("selftest: listed", "park" in [n for n, _f, _ in selftest.CHECKS])
    finally:
        park.supported = saved


def main_() -> int:
    test_wake_time()
    test_judge()
    test_watch()
    test_supported()
    test_park()
    test_action()
    test_settings()
    test_selftest()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
