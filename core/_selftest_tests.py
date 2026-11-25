"""
Self-test for the capability self-test (core/selftest.py) and its voice
entry point (actions/assistant_help.py, full_check=true).

    python -m core._selftest_tests

No network. The checks themselves are exercised by running
`python -m core.selftest --offline` on a real computer; this file checks the
machinery around them with stand-ins: reading requirements.txt per OS, the
per-check deadline, a check that crashes, the summary, the voice path.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import selftest  # noqa: E402
from core.selftest import FAIL, OFF, PASS, Result  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def test_requirements() -> None:
    text = ("# comment line\n"
            "PyQt6>=6.6,<7\n"
            "uvicorn[standard]>=0.27,<1   # with extras\n"
            "yt-dlp\n"
            "comtypes;   sys_platform == \"win32\"\n"
            "pyobjc-framework-Quartz;  sys_platform == \"darwin\"\n"
            "pyatspi; sys_platform == 'linux'\n"
            "\n")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "requirements.txt"
        p.write_text(text, encoding="utf-8")
        mac = selftest.requirements(p, "Darwin")
        win = [n for n, _ in selftest.requirements(p, "Windows")]
        lin = [n for n, _ in selftest.requirements(p, "Linux")]
    check("requirements: names, version rules, extras dropped, comments ignored",
          mac[:3] == [("PyQt6", ">=6.6,<7"), ("uvicorn", ">=0.27,<1"), ("yt-dlp", "")], str(mac))
    check("requirements: only this OS's lines", [n for n, _ in mac][3:] == ["pyobjc-framework-Quartz"]
          and "comtypes" in win and "pyobjc-framework-Quartz" not in win
          and lin[-1] == "pyatspi" and "comtypes" not in lin)
    real = [n for n, _ in selftest.requirements()]
    check("requirements: the real file reads", len(real) > 25 and "PyQt6" in real)


def test_packages() -> None:
    saved = selftest.requirements
    try:
        selftest.requirements = lambda *a, **k: [("numpy", ""), ("surely-not-a-real-package-x9", ""),
                                                 ("numpy", ">=999")]
        status, detail, fix = selftest.check_packages()
        check("packages: missing and wrong versions named, with the fix",
              status == FAIL and "surely-not-a-real-package-x9" in detail and "wants >=999" in detail
              and "requirements.txt" in fix, detail)
        selftest.requirements = lambda *a, **k: [("numpy", ">=1")]
        check("packages: all present passes", selftest.check_packages()[0] == PASS)
    finally:
        selftest.requirements = saved


def test_runner() -> None:
    r = selftest.run_one("slow", lambda: time.sleep(5), False, True, timeout_s=0.3)
    check("runner: a check that hangs costs its deadline, not the run",
          r.status == FAIL and "did not finish" in r.detail and r.seconds < 1.0, f"{r}")
    r = selftest.run_one("crash", lambda: 1 / 0, False, True)
    check("runner: a crash is a FAIL with the error", r.status == FAIL and "ZeroDivisionError" in r.detail)
    seen = []
    r = selftest.run_one("net", lambda network: (PASS, f"network={network}", ""), True, False)
    check("runner: the network flag reaches checks that take it", r.detail == "network=False")
    saved = selftest.CHECKS
    try:
        selftest.CHECKS = [("a", lambda: (PASS, "ok", ""), False), ("b", lambda: (OFF, "x", "y"), False)]
        out = selftest.run(only=["b"], on_result=seen.append)
        check("runner: only the named checks, each reported as it finishes",
              [x.name for x in out] == ["b"] and seen and seen[0].name == "b")
    finally:
        selftest.CHECKS = saved


def test_summary_and_main() -> None:
    ok = [Result("a", PASS, "fine"), Result("b", PASS, "fine")]
    check("summary: all fine", selftest.summary(ok) == "2 of 2 checks passed; everything this computer needs works.")
    mixed = ok + [Result("tls", FAIL, "only 3 roots", "pip install certifi"),
                  Result("wake_word", OFF, "not downloaded", "Settings > wake word > INSTALL")]
    s = selftest.summary(mixed)
    check("summary: problems first, then what is not set up, each with its fix",
          s.startswith("2 of 4 checks passed.") and "Not working - tls: only 3 roots. Fix: pip install certifi." in s
          and s.index("Not working") < s.index("Not set up - wake_word"), s)
    line = Result("tls", FAIL, "only 3 roots", "pip install certifi").line()
    check("line: status, name, detail and fix", line == "FAIL  tls: only 3 roots\n      fix: pip install certifi")
    check("line: no fix shown for a pass", "fix" not in Result("a", PASS, "ok", "unused").line())
    check("main: an unknown check name is refused", selftest.main(["nonsense"]) == 2)
    check("main: one real offline check runs", selftest.main(["python", "--offline"]) == 0)


def test_real_offline_checks() -> None:
    """Checks that need nothing from this computer but the project itself."""
    for name in ("speech_detector", "app_files", "tools", "languages"):
        fn = dict((n, f) for n, f, _ in selftest.CHECKS)[name]
        r = selftest.run_one(name, fn, False, False)
        check(f"real: {name} passes in this checkout", r.status == PASS, r.line())


def test_voice() -> None:
    from actions import assistant_help as ah

    class Player:
        def __init__(self):
            self.shown, self.said = [], []

        def show_content(self, title, text):
            self.shown.append((title, text))

        def request_say(self, text):
            self.said.append(text)

    gate = threading.Event()

    def fake_run(network=True, only=None, on_result=None):
        gate.wait(3)
        return [Result("python", PASS, "Python 3.12"), Result("video", FAIL, "ffmpeg is missing", "brew install ffmpeg")]

    saved = selftest.run
    selftest.run = fake_run
    try:
        p = Player()
        r = ah.assistant_help({"full_check": True}, player=p)
        check("voice: returns at once, as started", r.startswith("status=started"), r)
        r2 = ah.assistant_help({"full_check": True}, player=p)
        check("voice: a second request while it runs is not a second run", "already running" in r2, r2)
        gate.set()
        deadline = time.monotonic() + 3
        while not p.said and time.monotonic() < deadline:
            time.sleep(0.05)
        check("voice: the full report is shown on screen",
              p.shown and p.shown[0][0] == "SELF-CHECK" and "FAIL  video" in p.shown[0][1], str(p.shown))
        check("voice: the summary is spoken, with the fix",
              p.said and "brew install ffmpeg" in p.said[0] and "1 of 2 checks passed" in p.said[0])
        time.sleep(0.1)
        r3 = ah.assistant_help({"full_check": True}, player=None)
        check("voice: can run again once finished, and without a window", r3.startswith("status=started")
              and "already" not in r3)
    finally:
        gate.set()
        time.sleep(0.2)
        selftest.run = saved
    decl = ah.TOOL["parameters"]["properties"]
    check("voice: the tool declares full_check", decl.get("full_check", {}).get("type") == "BOOLEAN")


def main() -> int:
    test_requirements()
    test_packages()
    test_runner()
    test_summary_and_main()
    test_real_offline_checks()
    test_voice()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
