"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

# Optional, and the same contract actions use: a plugin that takes a moment can
# say so, and the model carries on talking instead of waiting on it. See
# core/action_loader.py for what each value means.
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None

def _opt_tier(value) -> Optional[str]:
    """"core" or "extra" from a declaration, or None when it did not say.

    Anything else is treated as "did not say" rather than as an error: a typo
    should fall back to the default ranking, not stop a working tool loading.
    """
    text = str(value or "").strip().lower()
    return text if text in ("core", "extra") else None


def _opt_text(value) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _guide_for(namespace: str, plugin_guide=None) -> Optional[dict]:
    """The setup guide shown under a settings section, or None. Never raises:
    a missing or broken guides module only costs the "HOW TO SET UP" button."""
    try:
        from core import setup_guides
        return setup_guides.for_section(namespace, plugin_guide)
    except Exception:
        return plugin_guide if isinstance(plugin_guide, dict) else None


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None   # optional PLUGIN_SETTINGS schema (config fields)
    behavior: Optional[str] = None    # None = the API's default (blocking)
    scheduling: Optional[str] = None  # None = the API's default (WHEN_IDLE)
    # Read by providers/schema.py when a small model gets a tool budget; never
    # sent to any API — main.py strips both before the Gemini config is built.
    #   tier   "core" | "extra" — this file's own opinion of whether it should
    #          survive a tight budget, instead of that being decided by the
    #          order files happen to be discovered in.
    #   brief  a short description used in place of the long one when a budget
    #          is in force. Nothing is deleted: Gemini still gets the full text.
    tier: Optional[str] = None
    brief: Optional[str] = None
    # True for a file from user_plugins/ (added by the user, not shipped).
    # `review` = refused until the user approves it again (unapproved or
    # changed since approval); `stem` names the file for that approval.
    user: bool = False
    review: bool = False
    stem: str = ""
    # For a user plugin running in its own process (core/plugin_sandbox.py):
    # the SandboxedPlugin that `run` calls into. None when it runs in ours.
    sandbox: object = None


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None],
                 notify: Callable[[str], None] | None = None):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger
        # Where user-facing notices go. `logger` is the console transcript and
        # carries everything; `notify` reaches the activity log, so only things
        # the user has to know about are sent to it. Defaults to dropping them,
        # which keeps every existing single-sink caller working unchanged.
        self._notify = notify or (lambda _msg: None)
        self._user_names: set[str] = set()     # plugins loaded from user_plugins/
        self._core_names: set[str] = set()
        self._plugins_dir: Optional[Path] = None

    def _enabled(self, name: str) -> bool:
        # A user-added plugin starts OFF; bundled ones keep the opt-out default.
        return get_plugin_enabled(name, name not in self._user_names)

    @staticmethod
    def _enabled_user(name: str) -> bool:
        return get_plugin_enabled(name, False)

    # -- called by the Plugin Manager after a user plugin was added/approved/removed --
    def refresh_user_plugin(self, stem: str) -> tuple[bool, str]:
        """Bring this running registry in line with user_plugins/<stem>.py:
        import it when it is approved and unchanged, list it for REVIEW when
        not, drop it when the file is gone. Never raises."""
        try:
            for n, r in list(self._plugins.items()):
                if r.user and r.stem == stem:
                    _stop_host(r)
                    del self._plugins[n]
                    self._user_names.discard(n)
            self._all_records = [r for r in self._all_records
                                 if not (r.user and r.stem == stem)]
            path = USER_PLUGINS_DIR / f"{stem}.py"
            if not path.is_file():
                return True, f"{stem} is no longer installed."
            problem = check_user_plugin(stem)
            if problem:
                rec = PluginRecord(name=_record_name(stem), file=path.name, user=True,
                                   review=True, stem=stem, error=problem)
            else:
                _extend_package_path()
                rec = _load_user(path, self._plugins_dir or BUNDLED_PLUGINS_DIR,
                                 self._core_names, self._plugins)
                if rec.valid and not self._enabled_user(rec.name):
                    _stop_host(rec)
            self._all_records.append(rec)
            if rec.valid:
                self._plugins[rec.name] = rec
                self._user_names.add(rec.name)
                return True, (f"'{rec.name}' is installed and switched OFF. Turn it on "
                              f"here; the assistant picks it up on its next session.")
            return False, rec.error
        except Exception as e:
            traceback.print_exc()
            return False, f"Could not load it: {e}"

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if self._enabled(name):
                decl = {
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                }
                if rec.behavior:
                    decl["behavior"] = rec.behavior
                # Budget metadata. NOT part of any API's schema — main.py calls
                # providers.schema.strip_meta() before the Gemini config is
                # built, and budget() consumes them on the local-engine path.
                if rec.tier:
                    decl["tier"] = rec.tier
                if rec.brief:
                    decl["brief"] = rec.brief
                decls.append(decl)
        return decls

    def switched_off(self) -> list[tuple[str, str]]:
        """(name, short summary) of every working plugin the user turned off.

        For the tool router (core/tool_router.py), which keeps listing them so
        the assistant answers "that is switched off, here is how to turn it
        on" instead of improvising with another tool. Plugins waiting for
        review are left out: they are not something to switch on."""
        out = []
        for name, rec in self._plugins.items():
            if rec.valid and not rec.review and not self._enabled(name):
                text = " ".join(str(rec.brief or rec.description or "").split())
                out.append((name, text[:110]))
        return out

    def has(self, name: str) -> bool:
        return name in self._plugins

    def scheduling(self, name: str) -> Optional[str]:
        """How this plugin's result should re-enter the conversation, if it said."""
        rec = self._plugins.get(name)
        return rec.scheduling if rec else None

    # -- called by main.py from _execute_tool's else branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not self._enabled(name):
            return f"The '{name}' plugin is currently disabled."
        try:
            return _call_run(rec.run, parameters, player, session_memory) or "Done."
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            self._notify(f"Plugin '{name}' failed — see the console for details.")
            traceback.print_exc()
            return f"Sir, the '{name}' plugin failed: {e}"

    # -- called by ui.py's settings tab to render per-plugin config forms --
    def settings_schemas(self) -> list[dict]:
        """One entry per settings SECTION, for enabled plugins that declare a
        PLUGIN_SETTINGS schema. Sections are deduped by namespace so a suite of
        plugins sharing one namespace (e.g. the printer trio) shows a single
        form. Current stored values are merged in so the UI can pre-fill fields.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not self._enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen:
                continue
            seen.add(ns)
            out.append({
                "plugin":    rec.name,
                "namespace": ns,
                "title":     rec.settings.get("title") or rec.name,
                "fields":    rec.settings.get("fields", []),
                "values":    get_plugin_config(ns),
                "action":    rec.settings.get("action"),   # optional test/connect button
                # optional "❓ HOW TO SET UP" steps: the plugin's own, else the
                # built-in guide for this namespace (core/setup_guides.py)
                "guide":     _guide_for(ns, rec.settings.get("guide")),
            })
        return out

    # -- called by ui.py's Plugin Manager overlay --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": self._enabled(rec.name) if rec.valid else False,
                "user": rec.user,          # added by the user (user_plugins/)
                "review": rec.review,      # refused until approved again
                "stem": rec.stem or Path(rec.file).stem,
            })
        return out


def _call_run(run_fn, parameters, player, session_memory):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="Missing PLUGIN dict constant.")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] missing or not a valid identifier "
                                   "(letters/digits/underscore, must start with letter/underscore).")

    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] missing or empty.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    # Optional, self-describing settings schema (rendered by the settings UI).
    # A malformed schema is ignored, never fatal — the plugin still loads.
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="", settings=settings,
                         behavior=_opt_upper(plugin_meta.get("behavior"), _BEHAVIORS),
                         scheduling=_opt_upper(plugin_meta.get("scheduling"), _SCHEDULING),
                         tier=_opt_tier(plugin_meta.get("tier")),
                         brief=_opt_text(plugin_meta.get("brief")))


# The app's own top-level packages. Named explicitly rather than only probed on
# disk, because the case that matters most is the folder being ABSENT: a plugin
# importing core.undo on a build with no core/ at all must still be told to
# update, not to install something called "core".
_FIRST_PARTY = {"core", "memory", "actions", "plugins", "dashboard", "config"}


def _is_first_party(top: str, plugins_dir: Path) -> bool:
    """True when `top` names part of this app rather than a third-party package.

    The disk probe is the second half so a package added to the project later is
    recognised without anyone remembering to edit the set above.
    """
    if top in _FIRST_PARTY:
        return True
    root = plugins_dir.parent
    try:
        return (root / top).is_dir() or (root / f"{top}.py").is_file()
    except OSError:
        return False


def _load_error(path: Path, plugins_dir: Path, exc: Exception) -> str:
    """Turn an import failure into something the person who downloaded the file
    can act on.

    Plugins are shared one file at a time, but some of them sit on a helper —
    anything named with a leading underscore, which this loader deliberately
    skips so it is never treated as a plugin of its own. Download the plugin
    without its helper and Python reports `No module named 'plugins._x'`, which
    is accurate and tells a non-programmer nothing. Naming the missing file, and
    saying it belongs next to this one, turns a support question into a
    thirty-second fix. Nothing here is specific to any plugin: the helper's name
    comes from the exception itself.
    """
    if isinstance(exc, ModuleNotFoundError):
        missing = (getattr(exc, "name", "") or "").split(".")
        if len(missing) == 2 and missing[0] == "plugins" and missing[1].startswith("_"):
            helper = missing[1] + ".py"
            return (f"Needs the shared file '{helper}', which is not in "
                    f"{plugins_dir.name}/. It comes with this plugin — download "
                    f"'{helper}' into the same folder as {path.name} and restart.")
        if missing and missing[0] not in ("plugins",):
            if _is_first_party(missing[0], plugins_dir):
                # 'pip install core' is not merely unhelpful — it is dangerous
                # advice. These names belong to this project, and telling
                # somebody to install a same-named stranger from an index is a
                # supply-chain hazard dressed up as a fix. A plugin written
                # against a newer Mark reaches this line, and the honest answer
                # is that the app is behind the plugin, not that a package is
                # absent.
                needed = "/".join(missing) + ".py"
                return (f"Needs '{needed}', which is part of this app rather than "
                        f"something to install — your copy doesn't have it, so it "
                        f"comes with a newer version. Update the app; there is "
                        f"nothing to pip install.")
            return (f"Needs a package that is not installed: "
                    f"pip install {missing[0]}")
    return f"Failed to load: {exc}"


def _load_file(path: Path, plugins_dir: Path, core_tool_names: set[str],
               valid: dict[str, PluginRecord], user: bool = False) -> PluginRecord:
    """Import one plugin file and validate it. Never raises."""
    try:
        module_name = f"plugins.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("could not build import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

        rec = _check_name(_validate(module, path.name), core_tool_names, valid)

    except Exception as e:
        rec = PluginRecord(name=path.stem, file=path.name,
                            error=_load_error(path, plugins_dir, e))
        traceback.print_exc()
    rec.user = user
    rec.stem = path.stem
    return rec


def _check_name(rec: PluginRecord, core_tool_names: set[str],
                valid: dict[str, PluginRecord]) -> PluginRecord:
    if rec.valid and rec.name in core_tool_names:
        return PluginRecord(name=rec.name, file=rec.file,
                            error=f"Name '{rec.name}' collides with a core tool — rejected.")
    if rec.valid and rec.name in valid:
        other = valid[rec.name].file
        return PluginRecord(name=rec.name, file=rec.file,
                            error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")
    return rec


def _stop_host(rec: PluginRecord) -> None:
    host = getattr(rec, "sandbox", None)
    if host is not None:
        try:
            host.stop()
        except Exception:
            pass


def _load_sandboxed(path: Path, core_tool_names: set[str],
                    valid: dict[str, PluginRecord]) -> PluginRecord:
    """Start a user plugin in its own process (core/script_host.py) and build
    its record from the declaration that process sends back. Never raises."""
    try:
        from core.plugin_sandbox import SandboxedPlugin
        host = SandboxedPlugin(path.stem)
        decl, err = host.start()
        if decl is None:
            rec = PluginRecord(name=path.stem, file=path.name, error=err)
        else:
            settings = decl.get("settings")
            if isinstance(settings, dict) and isinstance(settings.get("action"), dict) \
                    and settings["action"].get("run") == "host":
                settings = {**settings, "action": {**settings["action"], "run": host.action}}
            rec = PluginRecord(
                name=decl.get("name") or path.stem, description=decl.get("description") or "",
                parameters=decl.get("parameters") or dict(_DEFAULT_PARAMS), run=host.run,
                file=path.name, valid=True, settings=settings,
                behavior=decl.get("behavior"), scheduling=decl.get("scheduling"),
                tier=decl.get("tier"), brief=decl.get("brief"), sandbox=host)
            checked = _check_name(rec, core_tool_names, valid)
            if checked is not rec:
                host.stop()
                rec = checked
    except Exception as e:
        traceback.print_exc()
        rec = PluginRecord(name=path.stem, file=path.name,
                           error=f"Could not start its own process: {e}")
    rec.user = True
    rec.stem = path.stem
    return rec


def _load_user(path: Path, plugins_dir: Path, core_tool_names: set[str],
               valid: dict[str, PluginRecord]) -> PluginRecord:
    """An approved user plugin: in its own process unless the user switched
    the sandbox off ("plugin_sandbox": false), then in ours as before."""
    try:
        from core.plugin_sandbox import enabled as _sandbox_on
        sandboxed = _sandbox_on()
    except Exception:
        sandboxed = True
    if sandboxed:
        return _load_sandboxed(path, core_tool_names, valid)
    return _load_file(path, plugins_dir, core_tool_names, valid, user=True)


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print,
                      notify: Callable[[str], None] | None = None) -> PluginRegistry:
    """
    Scans plugins_dir for *.py files (skips files starting with '_', e.g. __init__.py,
    _template.py, and any shared-helper modules an author prefixes with '_').
    Import errors, validation errors, and name collisions are logged and the offending
    file is skipped — they NEVER raise out of this function and never abort the scan
    of remaining files.

    Then scans user_plugins/ (plugins the user added through the Plugin
    Manager). Those load only when the file — and every helper it came with —
    still has the SHA-256 recorded when the user approved it; otherwise they are
    listed with "REVIEW" and never imported. Bundled plugins win name clashes
    because they are scanned first.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []
    user_names: set[str] = set()

    files = sorted(plugins_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        rec = _load_file(path, plugins_dir, core_tool_names, valid)
        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Plugin loaded: {rec.name} ({path.name})")
        else:
            logger(f"Plugin rejected: {path.name} — {rec.error}")

    # ── user-added plugins ──
    if USER_PLUGINS_DIR.is_dir():
        _extend_package_path()
        bundled_stems = {p.stem for p in files}
        for path in sorted(USER_PLUGINS_DIR.glob("*.py"), key=lambda p: p.name):
            if path.name.startswith("_"):
                continue
            if path.stem in bundled_stems:
                rec = PluginRecord(name=path.stem, file=path.name, user=True, stem=path.stem,
                                   error=f"File name '{path.name}' is already used by a "
                                         f"bundled plugin — rename it and add it again.")
            else:
                problem = check_user_plugin(path.stem)
                if problem:
                    rec = PluginRecord(name=_record_name(path.stem), file=path.name,
                                       user=True, review=True, stem=path.stem, error=problem)
                else:
                    rec = _load_user(path, plugins_dir, core_tool_names, valid)
                    # A switched-off plugin's process is not kept waiting; it
                    # starts on its first call once switched on.
                    if rec.valid and not PluginRegistry._enabled_user(rec.name):
                        _stop_host(rec)
            all_records.append(rec)
            if rec.valid:
                valid[rec.name] = rec
                user_names.add(rec.name)
                logger(f"User plugin loaded: {rec.name} ({path.name})")
            else:
                logger(f"User plugin refused: {path.name} — {rec.error}")
        stray = _unapproved_helpers()
        if stray:
            logger("Helper files in user_plugins/ that no approved plugin brought: "
                   + ", ".join(stray))

    notify = notify or (lambda _msg: None)
    registry = PluginRegistry(valid, logger, notify)
    registry._all_records = all_records
    registry._user_names = user_names
    registry._core_names = set(core_tool_names)
    registry._plugins_dir = plugins_dir
    rejected = len(all_records) - len(valid)
    logger(f"Plugin discovery complete: {len(valid)} active, "
           f"{rejected} rejected, {len(all_records)} total.")
    # The activity log is the user's conversation, not a boot transcript: a
    # plugin that loaded correctly is not news, so only a failure surfaces there
    # — and then as one line, because the per-plugin detail is on the console.
    if rejected:
        notify(f"{rejected} plugin(s) could not be loaded — see the console.")
    review = [r.file for r in all_records if r.review]
    if review:
        notify(f"Plugin(s) waiting for your review in the Plugin Manager: {', '.join(review)}")
    return registry


# ═════════════════════════════════════════════════════════════════════════════
# User-added plugins
#
# The bundled plugins/ folder keeps its drop-in model: a file there is part of
# the app. A plugin the USER adds goes through the Plugin Manager instead: the
# file (and the _helper.py files it imports) is copied into user_plugins/
# (git-ignored), the user is told plainly that it will run with their
# permissions, and approving records each file's SHA-256 in the settings. The
# plugin starts OFF. If any of those files changes afterwards, it is not
# imported again until the user reviews and approves it once more.
# ═════════════════════════════════════════════════════════════════════════════
import ast as _ast
import hashlib as _hashlib
import shutil as _shutil
import time as _time

from memory.config_manager import BASE_DIR as _BASE_DIR

USER_PLUGINS_DIR = _BASE_DIR / "user_plugins"
BUNDLED_PLUGINS_DIR = _BASE_DIR / "plugins"
_MAX_PLUGIN_BYTES = 2_000_000


def _extend_package_path() -> None:
    """Let `from plugins import _helper` find helpers living in user_plugins/.
    Appended AFTER the bundled folder, so a bundled helper always wins."""
    try:
        import plugins as _pkg
        p = str(USER_PLUGINS_DIR)
        if p not in list(_pkg.__path__):
            _pkg.__path__.append(p)
    except Exception as e:
        print(f"[Plugins] could not add user_plugins to the package path: {e}")


def sha256_file(path: Path) -> str:
    h = _hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _records() -> dict:
    try:
        from memory.config_manager import get_user_plugin_records
        return get_user_plugin_records()
    except Exception:
        return {}


def _record_name(stem: str) -> str:
    return str(_records().get(stem, {}).get("name") or stem)


def check_user_plugin(stem: str) -> str:
    """'' when user_plugins/<stem>.py and its helpers match what was approved,
    otherwise a sentence saying why it is refused."""
    rec = _records().get(stem)
    path = USER_PLUGINS_DIR / f"{stem}.py"
    if not rec or not rec.get("sha256"):
        return "Not approved yet — REVIEW it in the Plugin Manager before it can run."
    try:
        if sha256_file(path) != rec["sha256"]:
            return "Changed since you added it — REVIEW before it can run again."
        for helper, digest in dict(rec.get("helpers") or {}).items():
            hp = USER_PLUGINS_DIR / helper
            if not hp.is_file():
                return f"Its helper file {helper} is missing — add the plugin again."
            if sha256_file(hp) != digest:
                return f"Its helper {helper} changed since you added it — REVIEW before it can run again."
    except OSError as e:
        return f"Could not read it: {e}"
    return ""


def _unapproved_helpers() -> list[str]:
    known: set[str] = set()
    for rec in _records().values():
        known.update(dict(rec.get("helpers") or {}).keys())
    try:
        return sorted(p.name for p in USER_PLUGINS_DIR.glob("_*.py") if p.name not in known)
    except OSError:
        return []


def _static_meta(tree) -> dict:
    """PLUGIN's literal 'name' / 'description', read WITHOUT running the file."""
    for node in getattr(tree, "body", []):
        if isinstance(node, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id == "PLUGIN" for t in node.targets):
            if isinstance(node.value, _ast.Dict):
                out = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, _ast.Constant) and k.value in ("name", "description"):
                        try:
                            out[k.value] = str(_ast.literal_eval(v))
                        except Exception:
                            pass
                return out
    return {}


def _helper_imports(tree) -> set[str]:
    """Names of plugins._helper modules a file imports (any import form)."""
    out: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            if mod == "plugins":
                out.update(a.name for a in node.names if a.name.startswith("_"))
            elif mod.startswith("plugins._"):
                out.add(mod.split(".")[1])
        elif isinstance(node, _ast.Import):
            for a in node.names:
                if a.name.startswith("plugins._"):
                    out.add(a.name.split(".")[1])
    out.discard("__init__")
    return out


def _parse(path: Path):
    if path.stat().st_size > _MAX_PLUGIN_BYTES:
        raise ValueError(f"{path.name} is larger than 2 MB — not a plugin file.")
    return _ast.parse(path.read_text(encoding="utf-8"), filename=path.name)


def inspect_user_plugin(src) -> dict:
    """Look at a file the user picked, WITHOUT running it.

    Returns {ok, error, stem, name, description, file, helpers (to copy),
    bundled_helpers (already in the app), missing (helpers not found),
    sha256, update (a plugin with this file name was added before)}."""
    out = {"ok": False, "error": "", "stem": "", "name": "", "description": "",
           "file": "", "helpers": [], "bundled_helpers": [], "missing": [],
           "sha256": "", "update": False}
    try:
        src = Path(str(src)).expanduser().resolve()
        out["file"] = str(src)
        stem = src.stem
        out["stem"] = stem
        if src.suffix.lower() != ".py" or not src.is_file():
            out["error"] = "Pick a plugin's .py file."
            return out
        if stem.startswith("_"):
            out["error"] = ("Files starting with '_' are helpers, not plugins — pick the "
                            "plugin file itself; its helpers come along automatically.")
            return out
        if not _NAME_RE.match(stem):
            out["error"] = "The file name must be letters, digits and underscores only."
            return out
        if (BUNDLED_PLUGINS_DIR / f"{stem}.py").exists():
            out["error"] = (f"{src.name} has the same file name as a plugin that comes with "
                            f"the app. Rename the file and try again.")
            return out
        tree = _parse(src)
        meta = _static_meta(tree)
        out["name"] = meta.get("name") or stem
        out["description"] = meta.get("description") or ""
        if not meta:
            out["error"] = "This file has no PLUGIN = {...} declaration, so it is not a plugin."
            return out
        # Helpers it imports, followed through helpers that import helpers.
        todo, seen = list(_helper_imports(tree)), set()
        while todo:
            h = todo.pop()
            if h in seen:
                continue
            seen.add(h)
            if (BUNDLED_PLUGINS_DIR / f"{h}.py").exists():
                out["bundled_helpers"].append(f"{h}.py")
                continue
            hp = src.parent / f"{h}.py"
            if hp.is_file():
                out["helpers"].append(f"{h}.py")
                todo.extend(_helper_imports(_parse(hp)))
            else:
                out["missing"].append(f"{h}.py")
        out["helpers"].sort(); out["missing"].sort(); out["bundled_helpers"].sort()
        out["sha256"] = sha256_file(src)
        out["update"] = stem in _records() or (USER_PLUGINS_DIR / src.name).exists()
        out["ok"] = True
    except SyntaxError as e:
        out["error"] = f"The file has a Python syntax error (line {e.lineno}) — it cannot run."
    except Exception as e:
        out["error"] = f"Could not read the file: {e}"
    return out


def install_user_plugin(src) -> tuple[bool, str, str]:
    """Copy an approved plugin (and its helpers) into user_plugins/, record the
    SHA-256 of every file, and switch it OFF. Call only after the user saw the
    warning and approved. Returns (ok, message, stem)."""
    info = inspect_user_plugin(src)
    if not info["ok"]:
        return False, info["error"], info["stem"]
    try:
        srcp = Path(info["file"])
        USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        _ensure_ignored()
        records = _records()
        # A helper of the same name that another added plugin brought and that
        # differs would silently change that plugin — refuse instead.
        for h in info["helpers"]:
            dst = USER_PLUGINS_DIR / h
            if dst.exists() and sha256_file(dst) != sha256_file(srcp.parent / h):
                owners = [s for s, r in records.items()
                          if h in dict(r.get("helpers") or {}) and s != info["stem"]]
                if owners:
                    return False, (f"A different {h} is already installed for "
                                   f"'{owners[0]}'. Rename the helper or remove that "
                                   f"plugin first."), info["stem"]
        copies = [(srcp, USER_PLUGINS_DIR / srcp.name)] + \
                 [(srcp.parent / h, USER_PLUGINS_DIR / h) for h in info["helpers"]]
        for a, b in copies:
            if a.resolve() != b.resolve():
                _shutil.copy2(a, b)
        record = {
            "name": info["name"],
            "sha256": sha256_file(USER_PLUGINS_DIR / srcp.name),
            "helpers": {h: sha256_file(USER_PLUGINS_DIR / h) for h in info["helpers"]},
            "added": int(_time.time()),
        }
        if record["sha256"] != info["sha256"]:
            return False, "The file changed while it was being copied — try again.", info["stem"]
        from memory.config_manager import save_plugin_enabled, save_user_plugin_record
        save_user_plugin_record(info["stem"], record)
        save_plugin_enabled(info["name"], False)       # starts OFF
        _extend_package_path()
        msg = f"'{info['name']}' was added and is switched OFF."
        if info["missing"]:
            msg += f" It also needs {', '.join(info['missing'])}, which was not next to it."
        return True, msg, info["stem"]
    except Exception as e:
        traceback.print_exc()
        return False, f"Could not add it: {e}", info["stem"]


def approve_user_plugin(stem: str) -> tuple[bool, str]:
    """Re-approve a plugin already in user_plugins/ after the user reviewed it
    (it changed, or was put there by hand). Starts it OFF again."""
    path = USER_PLUGINS_DIR / f"{stem}.py"
    if not path.is_file():
        return False, f"{stem}.py is not in the user plugins folder."
    ok, msg, _ = install_user_plugin(path)
    return ok, msg


def remove_user_plugin(stem: str) -> tuple[bool, str]:
    """Delete a user-added plugin and forget its approval. Helpers another
    added plugin still uses are kept."""
    try:
        records = _records()
        rec = records.get(stem, {})
        still_used: set[str] = set()
        for s, r in records.items():
            if s != stem:
                still_used.update(dict(r.get("helpers") or {}).keys())
        path = USER_PLUGINS_DIR / f"{stem}.py"
        if path.is_file():
            path.unlink()
        for h in dict(rec.get("helpers") or {}):
            hp = USER_PLUGINS_DIR / h
            if h not in still_used and hp.is_file():
                hp.unlink()
        from memory.config_manager import save_user_plugin_record
        save_user_plugin_record(stem, None)
        sys.modules.pop(f"plugins.{stem}", None)
        return True, f"{stem} was removed. Restart to unload it completely."
    except Exception as e:
        return False, f"Could not remove it: {e}"


def _ensure_ignored() -> None:
    """user_plugins/ holds the user's own files — keep them out of git."""
    try:
        gi = USER_PLUGINS_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text("# Plugins you added yourself — never part of the repository.\n*\n",
                          encoding="utf-8")
    except OSError:
        pass
