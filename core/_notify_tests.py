"""
Self-test for desktop notifications (core/notify.py) and where they are used
(core/tasks/desktop.py, main.py's reminders).

    python -m core._notify_tests            # stand-ins for the system commands
    python -m core._notify_tests --live     # also show one real notification here
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import notify  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class Run:
    def __init__(self, code=0, err=""):
        self.calls, self.code, self.err = [], code, err

    def __call__(self, args, **kw):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.code, "", self.err)


def test_commands() -> None:
    saved = notify.shutil.which
    notify.shutil.which = lambda name: f"/usr/bin/{name}"
    try:
        r = Run()
        status, _ = notify.show("Reminder", 'Call "Mum" & (do shell script "rm -rf ~")', system="Darwin", run=r)
        args = r.calls[0]
        check("macOS: osascript with a fixed script; title and message are arguments, not code",
              status == "shown" and args[:2] == ["osascript", "-e"] and args[2] == notify._APPLESCRIPT
              and args[3] == "Reminder" and args[4].startswith('Call "Mum"') and "rm -rf" not in args[2], str(args))
        r = Run(code=1, err="execution error: not allowed")
        status, detail = notify.show("T", "M", system="Darwin", run=r)
        check("macOS: a refused notification is reported as failed, with the reason",
              status == "failed" and "not allowed" in detail)
        r = Run()
        status, _ = notify.show("Task done", "x" * 1000, system="Linux", run=r)
        args = r.calls[0]
        check("Linux: notify-send, 15 s, the message cut to a banner's length",
              status == "shown" and args[0] == "notify-send" and "--expire-time=15000" in args
              and len(args[-1]) <= notify._MESSAGE_MAX and args[-1].endswith("…"))
        spawned = []
        saved_method = notify.method
        notify.method = lambda system=None: ("win10toast", "")   # as on a Windows machine
        try:
            status, _ = notify.show("T", "M\nline two", system="Windows",
                                    spawn=lambda a, **k: spawned.append(a))
        finally:
            notify.method = saved_method
        check("Windows: the toast runs in a child process; reported as requested, not shown",
              status == "requested" and spawned and spawned[0][1] == "-c" and spawned[0][-2:] == ["T", "M line two"])
    finally:
        notify.shutil.which = saved

    notify.shutil.which = lambda name: None
    try:
        status, detail = notify.show("T", "M", system="Linux", run=Run())
        check("no notify-send: failed, with what to install", status == "failed" and "libnotify" in detail)
    finally:
        notify.shutil.which = saved
    status, _ = notify.show("T", "M", system="Darwin", run=lambda *a, **k: 1 / 0)
    check("a crash is a failure, never raised", status == "failed")


def test_away() -> None:
    shown = []

    def shower(t, m):
        shown.append((t, m))
        return "shown", ""

    saved = notify.enabled
    try:
        notify.enabled = lambda: True
        check("away: shown when the user is in another window",
              notify.away("Reminder", "stretch", here=lambda: False, shower=shower) is True)
        time.sleep(0.2)
        check("away: on a background thread, with the text", shown == [("Reminder", "stretch")])
        check("away: not shown when JARVIS is in front",
              notify.away("Reminder", "stretch", here=lambda: True, shower=shower) is False)
        notify.enabled = lambda: False
        check("away: not shown when switched off",
              notify.away("Reminder", "stretch", here=lambda: False, shower=shower) is False)
        notify.enabled = lambda: True
        check("away: a broken check never raises",
              notify.away("R", "s", here=lambda: 1 / 0, shower=shower) is False)
    finally:
        notify.enabled = saved
    import memory.config_manager as cm
    saved_load = cm.load_api_keys
    try:
        cm.load_api_keys = lambda: {}
        check("setting: on by default", cm.get_desktop_notifications_enabled() is True)
        cm.load_api_keys = lambda: {"desktop_notifications": False}
        check("setting: can be switched off", cm.get_desktop_notifications_enabled() is False)
    finally:
        cm.load_api_keys = saved_load


def test_tasks() -> None:
    from core.tasks import desktop
    sent = []
    saved = notify.away
    notify.away = lambda t, m, **k: sent.append((t, m)) or True

    class Player:
        def __getattr__(self, name):
            return lambda *a, **k: None

    saved_player = desktop._player
    desktop._player = Player()
    try:
        task = {"title": "Flights to Lisbon", "deliver": "voice",
                "result": "\n\n3 flights found under 200 EUR.\nDetails follow.", "report": "r.md"}
        desktop._notify("done", task, "")
        check("tasks: a finished task notifies with its title and first line",
              sent == [("Background task finished", "Flights to Lisbon: 3 flights found under 200 EUR.")], str(sent))
        sent.clear()
        desktop._notify("failed", {"title": "Flights", "deliver": "voice", "error": "no model answered"}, "")
        check("tasks: a failed task notifies with the reason",
              sent == [("Background task failed", "Flights: no model answered")], str(sent))
        sent.clear()
        desktop._notify("done", dict(task, deliver="silent"), "")
        desktop._notify("failed", {"title": "x", "deliver": "silent"}, "")
        desktop._notify("started", task, "")
        check("tasks: silent tasks and other events notify nothing", sent == [], str(sent))
    finally:
        notify.away = saved
        desktop._player = saved_player


def test_reminder() -> None:
    try:
        import main
    except Exception as e:
        print(f"SKIP  main.py ({type(e).__name__}: {e})")
        return
    sent, said = [], []
    saved = notify.away
    notify.away = lambda t, m, **k: sent.append((t, m)) or True
    try:
        j = main.JarvisLive.__new__(main.JarvisLive)
        j.plugin_say = said.append
        j._routine_say("Take the cake out of the oven")
        check("reminder: notified and still spoken",
              sent == [("Reminder", "Take the cake out of the oven")]
              and said and "Take the cake out of the oven" in said[0], f"{sent} {said}")
    finally:
        notify.away = saved


def live() -> None:
    status, detail = notify.show("JARVIS self-test", "If you can read this, desktop notifications work.")
    print(f"INFO  live: {status} ({detail})")
    check("live: the system accepted the notification", status in ("shown", "requested"), detail)


def main_() -> int:
    test_commands()
    test_away()
    test_tasks()
    test_reminder()
    if "--live" in sys.argv:
        live()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
