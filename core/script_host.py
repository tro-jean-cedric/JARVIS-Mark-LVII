"""
core/script_host.py — the separate process a user-added plugin runs in.

    python -m core.script_host <stem>       # started by core/plugin_sandbox.py
    python -m core.script_host --ping       # can a host start here? (self-test)

A plugin the user added through the Plugin Manager (user_plugins/<stem>.py)
used to be imported into the app itself. One bad line there — a crash in a C
extension, sys.exit(), an endless loop holding the GIL, a Qt widget built on
a worker thread — took the voice session down with it. Here each one runs in
a process of its own: if it dies, the app says so and starts it again on the
next call.

WHAT THIS PROCESS DOES
    1. Checks, again and on its own, that user_plugins/<stem>.py and its
       helpers still match the SHA-256 the user approved
       (core.plugin_loader.check_user_plugin). The app checked too; this
       process does not take its word for it.
    2. Installs the import allow-list (below), imports the file, validates
       PLUGIN exactly as the loader does, and sends the declaration back.
    3. Serves run() calls, each on its own thread (as the app runs tools),
       and the settings form's TEST button.

THE IMPORT ALLOW-LIST
    Code in the plugin's own files (the plugin and the helpers it came with)
    may import: Python's standard library; the libraries in requirements.txt
    (the ones this app installs, mapped to their import names); and the
    app's own core / memory / plugins / actions / providers / config
    packages. Qt (PyQt6), the window (gui, ui) and main are refused: a plugin
    shows things through `player`, which this process forwards to the app.
    There is no pip here — a missing library is reported as "not available
    to added plugins", never as something to install. Libraries the plugin
    is allowed to use may import whatever THEY need; only the plugin's own
    import statements are checked.

    This is a boundary for mistakes and crashes, not against a hostile file:
    the standard library has os, subprocess and ctypes. What keeps a hostile
    file out is the approval step, which tells the user it will run with
    their permissions.

THE WIRE
    One JSON object per line. This process writes to a private copy of its
    original stdout; fd 1 and sys.stdout are pointed at stderr, so a plugin's
    print() (or a C library writing to fd 1) lands in the app's log instead
    of in the middle of a message. stdin is read the same way and fd 0 is
    replaced with an empty file. When the app goes away, stdin ends and this
    process exits — it never outlives the app.

    app  -> host  {"op": "run", "id", "params", "player": bool}
                  {"op": "action", "id", "values"}       settings TEST button
                  {"op": "callback", "id", "cb", "args"} a function the host passed
                  {"op": "reply", "id", "value" | "error"}
    host -> app   {"op": "ready", "decl"} | {"op": "fail", "error"}
                  {"op": "reply", "id", "value" | "error"}
                  {"op": "call", "id", "method", "args", "kwargs"}   player.<method>
    A function among a call's arguments (show_quiz's grade) travels as
    {"__callback__": n}; the other side calls it back with op "callback".
"""
from __future__ import annotations

import builtins
import importlib
import importlib.util
import itertools
import json
import os
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What a plugin may ask the app's window to do. Only methods that take plain
# values (text, numbers, lists, dicts) and functions, never bytes or widgets.
PLAYER_METHODS = frozenset({
    "write_log", "show_content", "show_quiz", "hide_quiz", "show_review",
    "show_video", "show_image", "stop_video", "pause_video", "seek_video",
    "set_video_muted", "video_is_playing", "video_state",
    "show_subtitle", "hide_subtitles", "subtitles_visible", "request_say",
})

APP_PACKAGES = frozenset({"core", "memory", "plugins", "actions", "providers", "config"})
REFUSED = frozenset({"PyQt6", "PyQt5", "PySide6", "PySide2", "gui", "ui", "main", "dashboard"})


# ── the allow-list ───────────────────────────────────────────────────────────

def _requirement_names(path: Path) -> set[str]:
    out = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].split(";")[0].strip()
            if not line:
                continue
            for sep in "<>=!~[ ":
                line = line.split(sep)[0]
            if line:
                out.add(line.lower().replace("_", "-"))
    except OSError:
        pass
    return out


def allowed_modules() -> set[str]:
    """Top-level module names a user plugin may import."""
    allowed = set(getattr(sys, "stdlib_module_names", ())) | set(APP_PACKAGES)
    wanted = _requirement_names(ROOT / "requirements.txt")
    try:
        from importlib.metadata import packages_distributions
        for top, dists in packages_distributions().items():
            if any(d.lower().replace("_", "-") in wanted for d in dists):
                allowed.add(top)
    except Exception:
        pass
    allowed.discard("__main__")
    return allowed - REFUSED


class NotAllowed(ImportError):
    """An import a user plugin is not allowed to make."""


def _refusal(top: str) -> str:
    if top in REFUSED:
        return (f"'{top}' cannot be used by an added plugin: plugins show things through "
                f"player (player.show_content, player.write_log, ...), never their own window.")
    return (f"'{top}' is not available to added plugins. They can use Python's standard "
            f"library and the libraries this app already installs; nothing can be "
            f"installed for them.")


def install_guard(own_dir: Path, allowed: set[str]) -> None:
    """Check the imports made by code in `own_dir` (the user's plugin files)."""
    own = str(own_dir.resolve())
    real_import = builtins.__import__
    real_import_module = importlib.import_module

    seen: dict[str, bool] = {}          # every import in the process passes here

    def from_plugin(globs) -> bool:
        f = (globs or {}).get("__file__") or ""
        if not f:
            return False
        hit = seen.get(f)
        if hit is None:
            try:
                hit = str(Path(f).resolve()).startswith(own + os.sep)
            except Exception:
                hit = False
            seen[f] = hit
        return hit

    def check(name: str) -> None:
        top = (name or "").split(".")[0]
        if top and top not in allowed:
            raise NotAllowed(_refusal(top))

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and from_plugin(globals):
            check(name)
        return real_import(name, globals, locals, fromlist, level)

    def guarded_import_module(name, package=None):
        caller = sys._getframe(1).f_globals
        if not name.startswith(".") and from_plugin(caller):
            check(name)
        return real_import_module(name, package)

    builtins.__import__ = guarded_import
    importlib.import_module = guarded_import_module


# ── the wire ─────────────────────────────────────────────────────────────────

def _plain(value):
    """What json can carry; anything else becomes its text."""
    return json.loads(json.dumps(value, default=str))


class Wire:
    """One JSON object per line in each direction, requests matched by id,
    functions passed by handle. Used by both sides (core/plugin_sandbox.py)."""

    def __init__(self, reader, writer, on_request, name: str = "wire"):
        self._r, self._w = reader, writer
        self._on_request = on_request            # (msg) -> value   (raises = error)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, list] = {}      # id -> [Event, reply]
        self._callbacks: dict[int, object] = {}
        self._cb_ids = itertools.count(1)
        self.closed = threading.Event()
        self.name = name

    # outgoing
    def send(self, msg: dict) -> bool:
        line = json.dumps(msg, default=str, ensure_ascii=False)
        try:
            with self._lock:
                self._w.write(line + "\n")
                self._w.flush()
            return True
        except Exception:
            self.closed.set()
            return False

    def pack(self, value):
        """Arguments out: functions become handles the other side can call."""
        if callable(value):
            n = next(self._cb_ids)
            self._callbacks[n] = value
            return {"__callback__": n}
        if isinstance(value, (list, tuple)):
            return [self.pack(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self.pack(v) for k, v in value.items()}
        return _plain(value)

    def unpack(self, value):
        """Arguments in: handles become functions that call the other side."""
        if isinstance(value, dict) and set(value) == {"__callback__"}:
            cb = value["__callback__"]
            return lambda *a: self.request({"op": "callback", "cb": cb, "args": self.pack(list(a))})
        if isinstance(value, list):
            return [self.unpack(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unpack(v) for k, v in value.items()}
        return value

    def request(self, msg: dict, timeout: float = 60.0):
        """Send and wait for the reply. Raises RuntimeError on an error reply,
        TimeoutError when none comes, ConnectionError when the line is gone."""
        i = next(self._ids)
        slot = [threading.Event(), None]
        self._waiting[i] = slot
        try:
            if self.closed.is_set() or not self.send({**msg, "id": i}):
                raise ConnectionError(f"{self.name} is not running")
            deadline_hit = not slot[0].wait(timeout)
            if slot[1] is None:
                if self.closed.is_set():
                    raise ConnectionError(f"{self.name} stopped")
                if deadline_hit:
                    raise TimeoutError(f"no answer within {timeout:.0f} s")
            reply = slot[1]
        finally:
            self._waiting.pop(i, None)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply.get("value")

    # incoming
    def _answer(self, msg: dict) -> None:
        try:
            if msg.get("op") == "callback":
                fn = self._callbacks.get(msg.get("cb"))
                if fn is None:
                    raise RuntimeError("that function is no longer available")
                value = fn(*self.unpack(msg.get("args") or []))
            else:
                value = self._on_request(msg)
            self.send({"op": "reply", "id": msg.get("id"), "value": _plain(value)})
        except Exception as e:
            self.send({"op": "reply", "id": msg.get("id"), "error": f"{type(e).__name__}: {e}"})

    def serve(self, on_other=None) -> None:
        """Read until the other side goes away. Requests are answered on their
        own threads, so a slow one never holds up a reply the reader is
        waiting to deliver."""
        try:
            for line in self._r:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                op = msg.get("op")
                if op == "reply":
                    slot = self._waiting.get(msg.get("id"))
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
                elif op in ("run", "action", "call", "callback"):
                    threading.Thread(target=self._answer, args=(msg,), daemon=True,
                                     name=f"{self.name}-{op}").start()
                elif on_other is not None:
                    on_other(msg)
        except Exception:
            pass
        finally:
            self.closed.set()
            for slot in list(self._waiting.values()):
                slot[0].set()


# ── the plugin side ──────────────────────────────────────────────────────────

class PlayerProxy:
    """Stands in for the app's `player` inside this process."""

    def __init__(self, wire: Wire):
        self._wire = wire

    def __getattr__(self, name):
        if name.startswith("_") or name not in PLAYER_METHODS:
            raise AttributeError(f"player.{name} is not available to added plugins")

        def call(*args, **kwargs):
            return self._wire.request({"op": "call", "method": name,
                                       "args": self._wire.pack(list(args)),
                                       "kwargs": self._wire.pack(kwargs)}, timeout=120)
        call.__name__ = name
        return call


def _isolate_stdio():
    """Private copies of stdin/stdout for the wire; the plugin gets stderr
    for its prints and an empty stdin."""
    out_fd, in_fd = os.dup(1), os.dup(0)
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdout = sys.stderr
    writer = os.fdopen(out_fd, "w", encoding="utf-8", buffering=1)
    reader = os.fdopen(in_fd, "r", encoding="utf-8")
    return reader, writer


def _load(path: Path, own_dir: Path):
    """Import the plugin under the guard and validate it like the loader."""
    from core import plugin_loader as pl
    pl._extend_package_path()
    install_guard(own_dir, allowed_modules())
    spec = importlib.util.spec_from_file_location(f"plugins.{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError("could not build an import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rec = pl._validate(module, path.name)
    return module, rec


def _declaration(rec) -> dict:
    settings = rec.settings
    if settings and isinstance(settings.get("action"), dict):
        action = dict(settings["action"])
        if callable(action.get("run")):
            action["run"] = "host"         # the app calls back here with op "action"
        else:
            action.pop("run", None)
        settings = {**settings, "action": action}
    return {"name": rec.name, "description": rec.description, "parameters": rec.parameters,
            "settings": settings, "behavior": rec.behavior, "scheduling": rec.scheduling,
            "tier": rec.tier, "brief": rec.brief}


def serve_plugin(path: Path, own_dir: Path, reader, writer) -> int:
    state = {"module": None, "rec": None}
    wire: Wire

    def on_request(msg):
        rec, module = state["rec"], state["module"]
        if rec is None:
            raise RuntimeError("the plugin is not loaded")
        if msg["op"] == "run":
            from core.plugin_loader import _call_run
            player = PlayerProxy(wire) if msg.get("player") else None
            result = _call_run(rec.run, wire.unpack(msg.get("params") or {}), player, None)
            return "" if result is None else str(result)
        if msg["op"] == "action":
            action = (rec.settings or {}).get("action") or {}
            fn = action.get("run")
            if not callable(fn):
                raise RuntimeError("this plugin has no test button")
            res = fn(msg.get("values") or {})
            if isinstance(res, tuple) and len(res) == 2:
                return [bool(res[0]), str(res[1])]
            return [bool(res), str(res)]
        raise RuntimeError(f"unknown request {msg['op']!r}")

    wire = Wire(reader, writer, on_request, name=f"plugin {path.stem}")
    try:
        module, rec = _load(path, own_dir)
    except NotAllowed as e:
        wire.send({"op": "fail", "error": str(e)})
        return 1
    except BaseException as e:                   # SystemExit from a plugin too
        traceback.print_exc()
        from core.plugin_loader import _load_error
        msg = _load_error(path, ROOT / "plugins", e) if isinstance(e, Exception) else \
            f"The plugin stopped while loading ({type(e).__name__})."
        if "pip install" in msg:
            msg = _refusal(msg.rsplit(" ", 1)[-1])
        wire.send({"op": "fail", "error": msg})
        return 1
    if not rec.valid:
        wire.send({"op": "fail", "error": rec.error})
        return 1
    state.update(module=module, rec=rec)
    wire.send({"op": "ready", "decl": _plain(_declaration(rec)), "pid": os.getpid()})
    wire.serve()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    if argv[:1] == ["--ping"]:
        n = len(allowed_modules())
        print(json.dumps({"ok": n > 100, "allowed": n, "python": sys.version.split()[0]}))
        return 0
    reader, writer = _isolate_stdio()

    def fail(text: str) -> int:
        writer.write(json.dumps({"op": "fail", "error": text}) + "\n")
        writer.flush()
        return 2

    stem = argv[0] if argv else ""
    try:
        from core import plugin_loader as pl
    except Exception as e:
        return fail(f"The plugin host could not start: {e}")
    # Tests only: a folder of their own, every file in it trusted. The
    # variable is no way in for a stranger — whoever can set it for this
    # process can already run any code as this user.
    test_dir = os.environ.get("JARVIS_PLUGIN_HOST_TEST_DIR", "")
    own_dir = Path(test_dir) if test_dir else pl.USER_PLUGINS_DIR
    if not pl._NAME_RE.match(stem or "") or stem.startswith("_"):
        return fail("The plugin host runs only plugin files added in the Plugin Manager.")
    path = own_dir / f"{stem}.py"
    if not path.is_file():
        return fail(f"{stem}.py is not among the plugins you added.")
    if not test_dir:
        problem = pl.check_user_plugin(stem)
        if problem:
            return fail(problem)
    return serve_plugin(path, own_dir, reader, writer)


if __name__ == "__main__":
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()          # os._exit below does not
        except Exception:
            pass
    os._exit(code)          # plugin threads must not keep a dead host alive
