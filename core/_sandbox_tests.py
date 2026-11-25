"""
Self-test for added plugins running in their own process
(core/script_host.py, core/plugin_sandbox.py, core/plugin_loader.py).

    python -m core._sandbox_tests

Real child processes, a temporary plugin folder, no network, no window.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import plugin_sandbox as sb  # noqa: E402
from core import script_host  # noqa: E402

_failures: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="jarvis_sandbox_"))
os.environ["JARVIS_PLUGIN_HOST_TEST_DIR"] = str(TMP)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def plugin(stem: str, body: str, name: str = None) -> str:
    name = name or stem
    (TMP / f"{stem}.py").write_text(
        f'PLUGIN = {{"name": "{name}", "description": "test plugin {name}", '
        f'"parameters": {{"type": "OBJECT", "properties": {{}}}}}}\n' + body, encoding="utf-8")
    return stem


class FakePlayer:
    def __init__(self):
        self.calls = []
        self.graded = None

    def write_log(self, text):
        self.calls.append(("write_log", text))

    def show_content(self, title, text):
        self.calls.append(("show_content", title, text))

    def request_say(self, text):
        self.calls.append(("request_say", text))
        return True

    def show_quiz(self, topic, questions, grade=None):
        self.calls.append(("show_quiz", topic, len(questions)))
        if grade:
            self.graded = grade(questions[0], "4")

    def shutdown_everything(self):               # not on the allow-list
        self.calls.append(("shutdown",))


def capture():
    lines = []
    return lines, lines.append


# ── tests ────────────────────────────────────────────────────────────────────

def test_ping() -> None:
    r = subprocess.run([sys.executable, "-m", "core.script_host", "--ping"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    check("ping: a host starts and builds its allow-list", '"ok": true' in r.stdout, r.stdout + r.stderr)


def test_allow_list() -> None:
    a = script_host.allowed_modules()
    check("allow-list: standard library", {"json", "os", "re", "subprocess", "sqlite3"} <= a)
    check("allow-list: libraries the app installs, by import name",
          {"numpy", "httpx", "cv2", "PIL", "bs4"} <= a, str(sorted(m for m in a if m in ("cv2", "PIL", "bs4"))))
    check("allow-list: the app's own packages", {"core", "memory", "plugins", "actions"} <= a)
    check("allow-list: Qt and the window refused", not ({"PyQt6", "gui", "ui", "main"} & a))
    check("allow-list: something not in requirements refused", "pip" not in a)


def test_run_and_player() -> None:
    plugin("echo", '''
import json, httpx, numpy
from core import outcome
CALLS = []
def run(parameters, player=None):
    print("this goes to the log, not the wire")
    os_out = __import__("os").write(1, b"raw fd 1 write\\n")
    if player is not None:
        player.write_log("hello from the plugin")
        player.show_content("Title", "Body")
        said = player.request_say("say this")
        player.show_quiz("maths", [{"question": "2+2", "options": [], "answer": "4"}],
                         grade=lambda q, given: given == q["answer"])
        try:
            player.shutdown_everything()
        except AttributeError as e:
            CALLS.append(str(e))
        return f"ok {parameters.get('x')} said={said} refused={len(CALLS)}"
    return f"no player {parameters.get('x')}"
''')
    lines, log = capture()
    host = sb.SandboxedPlugin("echo", logger=log)
    decl, err = host.start()
    check("load: declaration comes back", decl is not None and decl["name"] == "echo"
          and decl["parameters"]["type"] == "OBJECT", err)
    pl = FakePlayer()
    out = host.run({"x": 7}, player=pl)
    check("run: answer and parameters", out == "ok 7 said=True refused=1", out)
    names = [c[0] for c in pl.calls]
    check("player: calls carried out in the app", names == ["write_log", "show_content", "request_say", "show_quiz"],
          str(pl.calls))
    check("player: a function argument calls back into the plugin (quiz grade)", pl.graded is True, str(pl.graded))
    check("player: methods off the list never reach the app", ("shutdown",) not in pl.calls)
    check("run: no player stays None", host.run({"x": 1}) == "no player 1")
    time.sleep(0.3)
    joined = "\n".join(lines)
    check("print() and raw fd-1 writes land in the log", "goes to the log" in joined and "raw fd 1 write" in joined,
          joined[-300:])
    pid = host.pid()

    # concurrency: a slow call does not hold up another
    plugin("slowfast", '''
import time
def run(parameters, player=None):
    time.sleep(float(parameters.get("s", 0)))
    return "done " + str(parameters.get("s"))
''')
    h2 = sb.SandboxedPlugin("slowfast", logger=log)
    h2.start()
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("slow", h2.run({"s": 1.5})))
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    fast = h2.run({"s": 0})
    check("run: calls run side by side", fast == "done 0" and time.monotonic() - t0 < 1.0)
    t.join()
    check("run: the slow one finishes too", box.get("slow") == "done 1.5", str(box))
    h2.stop()
    check("host: stays the same process between calls", host.pid() == pid)
    host.stop()
    check("stop: the process is gone", host.pid() is None)


def test_failures() -> None:
    lines, log = capture()
    plugin("boom", '''
import os
def run(parameters, player=None):
    if parameters.get("mode") == "raise":
        raise ValueError("bad input")
    if parameters.get("mode") == "die":
        os._exit(3)
    if parameters.get("mode") == "hang":
        while True:
            pass
    return "fine"
''')
    host = sb.SandboxedPlugin("boom", logger=log)
    host.start()
    pid = host.pid()
    try:
        host.run({"mode": "raise"})
        check("raise: reported", False)
    except RuntimeError as e:
        check("raise: reported with the error", "bad input" in str(e), str(e))
    check("raise: the process stays", host.pid() == pid and host.run({}) == "fine")
    try:
        host.run({"mode": "die"})
        check("crash: reported", False)
    except RuntimeError as e:
        check("crash: reported with the exit code", "exit code 3" in str(e), str(e))
    check("crash: next call starts a fresh process", host.run({}) == "fine" and host.pid() != pid)
    saved = sb.RUN_TIMEOUT_S
    sb.RUN_TIMEOUT_S = 1.5
    try:
        t0 = time.monotonic()
        host.run({"mode": "hang"})
        check("hang: reported", False)
    except RuntimeError as e:
        check("hang: stopped after the deadline", "did not answer within" in str(e)
              and time.monotonic() - t0 < 6, str(e))
    finally:
        sb.RUN_TIMEOUT_S = saved
    check("hang: next call works", host.run({}) == "fine")
    host.stop()

    cases = {
        "qt": ("from PyQt6.QtWidgets import QWidget\ndef run(parameters): return 'x'\n", "player"),
        "notlisted": ("import pip\ndef run(parameters): return 'x'\n", "not available to added plugins"),
        "exits": ("import sys\nsys.exit(4)\ndef run(parameters): return 'x'\n", "stopped while loading"),
        "dies": ("import os\nos._exit(5)\n", "exit code 5"),
        "norun": ("X = 1\n", "Missing callable run"),
        "syntax": ("def run(:\n", "Failed to load"),
    }
    for stem, (body, expect) in cases.items():
        plugin(stem, body)
        h = sb.SandboxedPlugin(stem, logger=log)
        decl, err = h.start()
        check(f"load refused: {stem}", decl is None and expect in err, err)
    plugin("optional", '''
try:
    import pip
    HAVE = True
except ImportError:
    HAVE = False
def run(parameters): return f"pip={HAVE}"
''')
    h = sb.SandboxedPlugin("optional", logger=log)
    h.start()
    check("an optional import in try/except just comes back missing", h.run({}) == "pip=False")
    h.stop()
    plugin("dynamic", '''
import importlib
def run(parameters):
    try:
        importlib.import_module("pip")
        return "imported"
    except ImportError as e:
        return "refused"
''')
    h = sb.SandboxedPlugin("dynamic", logger=log)
    h.start()
    check("importlib.import_module is checked too", h.run({}) == "refused")
    h.stop()


def test_trust() -> None:
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_PLUGIN_HOST_TEST_DIR"}
    for stem, expect in (("echo", "not among the plugins you added"),
                         ("../etc", "only plugin files added"), ("_helper", "only plugin files added")):
        h = sb.SandboxedPlugin(stem, logger=lambda _l: None)
        h._env = {}
        saved = os.environ.pop("JARVIS_PLUGIN_HOST_TEST_DIR")
        try:
            decl, err = h.start()
        finally:
            os.environ["JARVIS_PLUGIN_HOST_TEST_DIR"] = saved
        check(f"real mode refuses {stem!r}", decl is None and expect in err, err)
    del env


def test_registry() -> None:
    from core import plugin_loader as pl
    saved = (pl.USER_PLUGINS_DIR, pl.check_user_plugin, pl.get_plugin_enabled, pl._unapproved_helpers)
    state = {"on": {"settled"}}
    bundled = TMP / "_bundled"
    bundled.mkdir(exist_ok=True)
    for f in TMP.glob("*.py"):
        f.unlink()
    plugin("settled", '''
PLUGIN_SETTINGS = {"namespace": "settled", "title": "Settled", "fields": [],
                   "action": {"label": "TEST", "run": lambda values: (True, "tested " + str(values.get("k")))}}
def run(parameters, player=None, session_memory=None):
    return "registry ok"
''')
    plugin("dormant", "def run(parameters): return 'dormant ran'\n")
    plugin("clash", "def run(parameters): return 'x'\n", name="save_memory")
    try:
        pl.USER_PLUGINS_DIR = TMP
        pl.check_user_plugin = lambda stem: ""
        pl.get_plugin_enabled = lambda name, default=True: name in state["on"] if name in (
            "settled", "dormant") else default
        pl._unapproved_helpers = lambda: []
        lines, log = capture()
        reg = pl.discover_plugins(bundled, {"save_memory"}, logger=log)
        recs = {r.stem: r for r in reg._all_records}
        s = recs["settled"]
        check("registry: user plugin loaded in its own process", s.valid and s.sandbox is not None
              and s.sandbox.running, s.error)
        check("registry: not imported into the app", "plugins.settled" not in sys.modules)
        check("registry: run through the registry", reg.run("settled", {}, player=FakePlayer()) == "registry ok")
        action = [x for x in reg.settings_schemas() if x["namespace"] == "settled"][0]["action"]
        check("registry: settings TEST button reaches the plugin", action["run"]({"k": 5}) == (True, "tested 5"))
        d = recs["dormant"]
        check("registry: a switched-off plugin's process is not kept", d.valid and not d.sandbox.running)
        state["on"].add("dormant")
        check("registry: switched on, it starts on its first call", reg.run("dormant", {}) == "dormant ran"
              and d.sandbox.running)
        check("registry: name clash with a core tool rejected", not recs["clash"].valid
              and "collides" in recs["clash"].error and not recs["clash"].sandbox, recs["clash"].error)
        d.sandbox._proc.kill()
        d.sandbox._proc.wait(3)
        out = reg.run("dormant", {})
        check("registry: a dead process restarts on the next call", out == "dormant ran", out)

        ok, msg = reg.refresh_user_plugin("settled")
        check("refresh: old process stopped, new one loaded", ok and not s.sandbox.running, msg)
        for r in reg._all_records:
            pl._stop_host(r)

        import memory.config_manager as cm
        saved_flag = cm.get_plugin_sandbox_enabled
        cm.get_plugin_sandbox_enabled = lambda: False
        try:
            reg2 = pl.discover_plugins(bundled, {"save_memory"}, logger=log)
            r2 = {r.stem: r for r in reg2._all_records}["settled"]
            check("sandbox off: imported into the app as before", r2.valid and r2.sandbox is None
                  and "plugins.settled" in sys.modules and reg2.run("settled", {}) == "registry ok")
        finally:
            cm.get_plugin_sandbox_enabled = saved_flag
            sys.modules.pop("plugins.settled", None)
    finally:
        pl.USER_PLUGINS_DIR, pl.check_user_plugin, pl.get_plugin_enabled, pl._unapproved_helpers = saved


def test_selftest() -> None:
    from core import selftest
    st, detail, _ = selftest.check_plugin_sandbox()
    check("selftest: plugin_sandbox check passes", st == selftest.PASS, detail)
    check("selftest: listed", "plugin_sandbox" in [n for n, _f, _ in selftest.CHECKS])


def main() -> int:
    try:
        test_ping()
        test_allow_list()
        test_run_and_player()
        test_failures()
        test_trust()
        test_registry()
        test_selftest()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
