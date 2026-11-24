#!/usr/bin/env python3
"""
run.py — start JARVIS with one command (or one double-click).

    python3 run.py              set up if needed, then start the assistant
    python3 run.py --setup      only set up / repair, do not start
    python3 run.py --reinstall  delete the private environment and set up again

What it does, every time:
  1. Uses a private Python environment in .venv/ next to this file, creating it
     on the first run from the best Python on this computer (3.11–3.13 are the
     tested versions; a newer one is used when nothing else is installed).
  2. Runs setup.py inside that environment — but only on the first run and
     whenever requirements.txt or setup.py changed since the last setup — so
     normal starts take a second, not minutes.
  3. Starts main.py with that environment.

The double-click launchers (run_jarvis.command on macOS, run_jarvis.sh on
Linux, run_jarvis.bat on Windows) only call this file. Standard library only:
it has to work before anything is installed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
STAMP = VENV / ".jarvis-setup-stamp"
MIN_PY = (3, 11)
TESTED = [(3, 13), (3, 12), (3, 11)]
WINDOWS = os.name == "nt"


def say(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def fail(msg: str) -> "None":
    print(f"\n[run] ❌ {msg}", flush=True)
    sys.exit(1)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def version_of(python: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run([python, "-c", "import sys; print(*sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        major, minor = map(int, out.stdout.split())
        return major, minor
    except ValueError:
        return None


def pick_base_python() -> tuple[str, tuple[int, int]]:
    """A tested Python if one is installed, else the newest usable one."""
    candidates: list[str] = []
    if WINDOWS and shutil.which("py"):
        for major, minor in TESTED:
            candidates.append(f"py -{major}.{minor}")
    for major, minor in TESTED:
        exe = shutil.which(f"python{major}.{minor}")
        if exe:
            candidates.append(exe)
    candidates.append(sys.executable)
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            candidates.append(exe)

    usable: list[tuple[tuple[int, int], str]] = []
    for cand in candidates:
        argv = cand.split(" ") if cand.startswith("py -") else [cand]
        if cand.startswith("py -"):
            try:
                out = subprocess.run(argv + ["-c", "import sys; print(sys.executable)"],
                                     capture_output=True, text=True, timeout=20)
                if out.returncode != 0:
                    continue
                cand = out.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                continue
        v = version_of(cand)
        if v and v >= MIN_PY:
            if v in TESTED:
                return cand, v
            usable.append((v, cand))
    if usable:
        v, cand = max(usable)
        return cand, v
    fail(f"No Python {MIN_PY[0]}.{MIN_PY[1]} or newer was found. Install Python 3.12 from "
         "https://www.python.org/downloads/ and run this again.")
    raise SystemExit(1)  # for type checkers


def setup_fingerprint() -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "setup.py"):
        p = HERE / name
        if p.exists():
            h.update(name.encode())
            h.update(p.read_bytes())
    h.update(f"{sys.platform}".encode())
    return h.hexdigest()


def venv_works() -> bool:
    py = venv_python()
    return py.exists() and version_of(str(py)) is not None


def create_venv() -> None:
    base, v = pick_base_python()
    say(f"Creating the private environment in .venv with Python {v[0]}.{v[1]} ({base})…")
    if VENV.exists():
        shutil.rmtree(VENV)
    r = subprocess.run([base, "-m", "venv", str(VENV)])
    if r.returncode != 0 or not venv_works():
        fail("Could not create the environment. On Debian/Ubuntu install it with: "
             "sudo apt install python3-venv")


def run_setup() -> None:
    py = str(venv_python())
    say("Installing what JARVIS needs (first run or requirements changed — this takes a few "
        "minutes)…")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], cwd=HERE)
    r = subprocess.run([py, "setup.py"], cwd=HERE)
    if r.returncode != 0:
        fail("Setup did not finish (see the messages above). Fix the problem it names and "
             "run this again — it continues where it stopped.")
    STAMP.write_text(setup_fingerprint(), encoding="utf-8")
    say("Setup complete.")


def needs_setup() -> bool:
    try:
        return STAMP.read_text(encoding="utf-8").strip() != setup_fingerprint()
    except OSError:
        return True


def main(argv: list[str]) -> int:
    os.chdir(HERE)
    if not (HERE / "main.py").exists():
        fail(f"main.py was not found next to run.py ({HERE}).")

    reinstall = "--reinstall" in argv
    # --reinstall alone only rebuilds; add --start to launch right after.
    setup_only = ("--setup" in argv) or (reinstall and "--start" not in argv)
    passthrough = [a for a in argv if a not in ("--reinstall", "--setup", "--start")]

    if reinstall or not venv_works():
        create_venv()
    if reinstall or needs_setup():
        run_setup()

    if setup_only:
        say("Ready. Start JARVIS with: python3 run.py (or double-click the launcher).")
        return 0

    py = str(venv_python())
    say("Starting JARVIS… (close this window or press Ctrl+C to quit)")
    cmd = [py, str(HERE / "main.py"), *passthrough]
    if WINDOWS:
        return subprocess.run(cmd, cwd=HERE).returncode
    os.execv(py, cmd)  # replace this process: Ctrl+C and the window close reach JARVIS directly
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
