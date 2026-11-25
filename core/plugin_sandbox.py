"""
core/plugin_sandbox.py — the app's side of a user plugin that runs in its own
process (core/script_host.py, which explains why and what it allows).

    host = SandboxedPlugin("my_plugin")
    decl, error = host.start()          # the PLUGIN declaration, or why not
    text = host.run({"city": "Oslo"}, player=ui)
    host.stop()

core/plugin_loader.py builds a PluginRecord whose `run` is `host.run`, so
the registry, main.py and the Plugin Manager see an ordinary plugin. Only
plugins the USER added go through here; the bundled plugins/ folder is part
of the app and stays in its process. `"plugin_sandbox": false` in the config
puts user plugins back in the app's process as before.

WHEN THE PROCESS GOES WRONG
    * The plugin raises in run(): the error comes back as the reply, and the
      registry reports it like any plugin failure. The process stays.
    * The process dies (a crash in a C library, sys.exit, os._exit): the
      call in progress fails with the exit code, and the next call starts a
      fresh process.
    * run() does not answer within RUN_TIMEOUT_S: the process is stopped (a
      thread cannot be stopped any other way) and the call fails saying so.
    The voice session never sees more than a failed tool call.

`player` calls the plugin makes are carried out here, on the thread that
received them — the same thing as a plugin calling player from one of its
own worker threads. Only the methods in script_host.PLAYER_METHODS exist.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core.script_host import PLAYER_METHODS, ROOT, Wire

START_TIMEOUT_S = 30.0       # importing a plugin that loads numpy/cv2 takes a few seconds
RUN_TIMEOUT_S = 120.0
ACTION_TIMEOUT_S = 60.0


def enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_sandbox_enabled
        return get_plugin_sandbox_enabled()
    except Exception:
        return True


def host_command(stem: str) -> list[str]:
    """How to start a host. A frozen build has no `python`: its own program
    takes --plugin-host (see the top of main.py)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--plugin-host", stem]
    return [sys.executable, "-m", "core.script_host", stem]


class SandboxedPlugin:
    def __init__(self, stem: str, logger: Callable[[str], None] = print,
                 env: Optional[dict] = None):
        self.stem = stem
        self.name = stem
        self._log = logger
        self._env = env or {}
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._wire: Optional[Wire] = None
        self._player = None
        self.decl: Optional[dict] = None
        self.error = ""

    # ── process ──────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and \
            self._wire is not None and not self._wire.closed.is_set()

    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def start(self) -> tuple[Optional[dict], str]:
        """Start the process (if it is not running) and return the plugin's
        declaration, or None and a sentence saying why not. Never raises."""
        with self._lock:
            if self.running and self.decl is not None:
                return self.decl, ""
            self._kill()
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                   **self._env}
            try:
                proc = subprocess.Popen(
                    host_command(self.stem), cwd=str(ROOT), env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            except Exception as e:
                self.error = f"its process could not be started ({type(e).__name__}: {e})"
                return None, self.error
            first = {"msg": None}
            got = threading.Event()

            def on_other(msg):
                if msg.get("op") in ("ready", "fail") and first["msg"] is None:
                    first["msg"] = msg
                    got.set()

            wire = Wire(proc.stdout, proc.stdin, self._on_request, name=f"plugin {self.stem}")
            threading.Thread(target=wire.serve, args=(on_other,), daemon=True,
                             name=f"plugin-host-{self.stem}").start()
            threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True,
                             name=f"plugin-log-{self.stem}").start()
            self._proc, self._wire = proc, wire
            # Woken early if the process ends before saying anything.
            deadline = time.monotonic() + START_TIMEOUT_S
            while not got.wait(0.05):
                if wire.closed.is_set() or time.monotonic() > deadline:
                    break
            msg = first["msg"]
            if msg is None:
                code = proc.poll()
                self._kill()
                self.error = (f"its process ended while loading (exit code {code})"
                              if code is not None else
                              f"it did not finish loading within {START_TIMEOUT_S:.0f} s")
                return None, self.error
            if msg["op"] == "fail":
                self._kill()
                self.error = str(msg.get("error") or "it could not be loaded")
                return None, self.error
            self.decl = msg.get("decl") or {}
            self.name = self.decl.get("name") or self.stem
            self.error = ""
            return self.decl, ""

    def stop(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._wire = None
        if proc is None:
            return
        try:
            proc.stdin.close()          # the host leaves by itself when stdin ends
        except Exception:
            pass
        try:
            proc.wait(1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(2.0)
            except Exception:
                pass

    def _pump_stderr(self, proc) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    self._log(f"[plugin {self.stem}] {line}")
        except Exception:
            pass

    # ── requests from the plugin ─────────────────────────────────────────────
    def _on_request(self, msg: dict):
        if msg.get("op") != "call":
            raise RuntimeError(f"unknown request {msg.get('op')!r}")
        method = str(msg.get("method") or "")
        if method not in PLAYER_METHODS:
            raise RuntimeError(f"player.{method} is not available to added plugins")
        player = self._player
        if player is None:
            raise RuntimeError("there is no window to show it in")
        fn = getattr(player, method, None)
        if not callable(fn):
            raise RuntimeError(f"player.{method} does not exist in this version")
        wire = self._wire
        args = wire.unpack(msg.get("args") or []) if wire else []
        kwargs = wire.unpack(msg.get("kwargs") or {}) if wire else {}
        return fn(*args, **kwargs)

    # ── calls into the plugin ────────────────────────────────────────────────
    def _ready_wire(self) -> Wire:
        decl, err = self.start()
        if decl is None:
            raise RuntimeError(f"it could not be started: {err}")
        return self._wire

    def _ask(self, msg: dict, timeout: float):
        wire = self._ready_wire()
        proc = self._proc
        try:
            return wire.request(msg, timeout=timeout)
        except ConnectionError:
            try:
                code = proc.wait(2.0) if proc else None
            except Exception:
                code = None
            with self._lock:
                if self._proc is proc:
                    self._kill()
            raise RuntimeError(f"its process stopped (exit code {code}); it starts again "
                               f"on the next request") from None
        except TimeoutError:
            with self._lock:
                if self._proc is proc:
                    if proc is not None:
                        proc.kill()
                    self._kill()
            raise RuntimeError(f"it did not answer within {timeout:.0f} s, so its process "
                               f"was stopped") from None

    def run(self, parameters, player=None, session_memory=None) -> str:
        """PluginRecord.run for a sandboxed plugin. Raises RuntimeError with a
        readable sentence; the registry turns that into the tool's answer."""
        if player is not None:
            self._player = player
        wire = self._ready_wire()
        return self._ask({"op": "run", "params": wire.pack(parameters or {}),
                          "player": player is not None}, RUN_TIMEOUT_S)

    def action(self, values: dict) -> tuple[bool, str]:
        """The settings form's test button."""
        try:
            got = self._ask({"op": "action", "values": dict(values or {})}, ACTION_TIMEOUT_S)
            return bool(got[0]), str(got[1])
        except Exception as e:
            return False, str(e)
