# Tools: actions, plugins, inline tools

Read before adding or changing anything the voice model can call. `AGENTS.md`
already states the plugin and threading rules; this file covers what it leaves
out (the action contract, outcomes, undo, confirm) and where the code drifts.

## Three kinds of tool, one dispatch chain

`JarvisLive._execute_tool` — `main.py:1574` tries, in order:

1. **Inline tools** declared in `TOOL_DECLARATIONS` (`main.py:385`): save/recall
   memory, undo, screen_process, close_camera, system_status, manage_monitor,
   shutdown_jarvis. These need session state; don't add new ones here unless
   they do too.
2. **Bundled actions** — `actions/*.py`, via `ActionRegistry.run` on an executor
   thread (`main.py:1706-1739`). Things that drive *the computer* go here.
3. **Plugins** — `plugins/*.py`, via `PluginRegistry.run` (`main.py:1742`).
   Everything else (integrations, hobbies) goes here. The repo ships only
   `plugins/_template.py`; the readme's plugin list describes files that are
   not in this checkout.

Name clashes are rejected at discovery: actions may not shadow inline tools,
plugins may not shadow either (`core/action_loader.py:257`, `core/plugin_loader.py:321`).

## Action contract (`core/action_loader.py:9-20`)

Module-level `TOOL` dict, found by `discover_actions` (`core/action_loader.py:217`):

- `name`, `description`, `parameters` (`{"type": "OBJECT", ...}` — Gemini
  upper-case types, enforced at `core/action_loader.py:200`), `handler` (callable).
- Optional: `tier` (`"core"`/`"extra"`) and `brief` (short description for
  small local models). Both are stripped by `strip_meta`
  (`core/action_loader.py:64`) before Gemini sees them and are used by
  `providers.schema.budget`. Optional `behavior` / `scheduling` exist but no
  action sets them.
- The handler is called as `fn(parameters=..., **ctx)` — **first parameter must
  be named `parameters`** — with only the ctx keys it declares out of
  `player`, `speak`, `response`, `session_memory` (`_call_handler`,
  `core/action_loader.py:169`). A falsy return becomes "Done."
- Files starting with `_` and files without `TOOL` are skipped. Four helper
  modules (`background_monitor`, `proactive`, `screen_processor`,
  `system_monitor`) have no `TOOL`; main.py imports them directly.
- Examples: `actions/open_app.py:372`, `actions/background_tasks.py:121`
  (uses `tier`/`brief`), `actions/game_updater.py:1063`.

Plugins differ: handler is module-level `run(parameters, player=None,
session_memory=None)` (parameters positional, `core/plugin_loader.py:172`), no
`speak`; optional `PLUGIN_SETTINGS` builds a settings form; plugins can be
toggled off at runtime via `plugins_enabled` (default on).

## What a tool returns: `core/outcome.py`

The model is told what was *observed*, not attempted. Use:

- `outcome.done(action, **facts)` — `core/outcome.py:97`, only after checking
- `outcome.unverified(action, reason, ...)` — request went out, couldn't confirm
- `outcome.failed(action, reason, ...)`, `outcome.ask(action, question, ...)`
- `outcome.started(action, ...)` — background work; the tool must later report
  through `player.request_say(...)` (pattern: `actions/open_app.py:238`,
  `actions/send_message.py:397`)
- `outcome.untrusted(source, content)` — `core/outcome.py:188`. Wrap any
  web/file/third-party text before returning it to the model
  (`actions/web_search.py:399`, `actions/file_processor.py:844`).

**Mid-migration:** 11 actions use outcome (e.g. `computer_settings`, `desktop`,
`browser_control`); 8 still return free prose with "sir" and sometimes claim
success unchecked (`code_helper`, `dev_agent`, `file_controller`,
`flight_finder`, `game_updater`, `reminder`, `weather_report`, `youtube_video`).
New code uses outcome; convert an old action when you touch its return paths.

## Reversible vs irreversible changes

- Reversible change → read old value, apply, then `push_undo(label, fn)`
  (`core/undo.py:60`). See `actions/file_controller.py:289` (delete),
  `actions/computer_settings.py:1070` (volume).
- Irreversible → `confirm.request(key, title, detail, run)` (`core/confirm.py:82`);
  the UI issues the approval, not the model. Only user so far:
  `actions/computer_settings.py:1040-1055` (restart/shutdown/wifi). Don't add a
  model-filled `"confirmed"` parameter as a safety gate (the existing one in
  `computer_control` close_app only disambiguates).

## Air Control: user-driven input, not model actions

`plugins/air_control.py` (voice side) and `plugins/_air_control_core.py` (gesture engine)
turn the user's own hand into mouse and keyboard input through `core/os_input.py`
(Quartz / SendInput / XTest, queued on its `InputPump`). This is the user
operating their computer, so it does not go through `core/screenact.py`'s guarded
model actions, undo or confirm. The model only starts, stops and queries it (and
starts dictation, `action=dictate`); it never moves the pointer through it. The
engine returns action tuples and the `Dispatcher` sends them; keys, text and
Cmd/Ctrl+W are dropped (with on-screen feedback) while JARVIS's own window has the
keyboard (`os_input.own_app_focused()`), so a gesture can never type into or close
JARVIS. `start` reports `done` only after the overlay confirmed click-through and
`os_input.self_check()` saw the cursor move.

Dictation (`core/dictation.py`) takes the microphone inside main.py's mic
callback while `dictation.active()`: blocks go to its own STT, never to the
model, and are dropped while JARVIS speaks. Both Air Control and Holo Desk
read hands through `plugins/_hand_stream.HandStream` and filter them with
`plugins/_gesture_core.HandFilter` (profile `air` / `desk`); tune pinch and
pose rules there, not in the windows.

## Recurring implementation patterns in actions

- **Per-OS branching:** module constant `_OS = platform.system()` compared to
  `"Windows"/"Darwin"/"Linux"` (`actions/browser_control.py:84`,
  `actions/computer_settings.py:30`, `actions/desktop.py:19`). Older files use a
  per-file `_get_os()` or `config.get_os()` (`config/__init__.py:20`); some
  default to `"windows"` when config lacks `os_system` — don't copy that.
  What the OS *permits* (Wayland, macOS permissions) is `core/platform_support.detect()`.
- **Heavy imports deferred:** `name = LazyModule("pkg", configure)` guarded by
  `installed("pkg")` (`core/lazyimport.py:74`; used in
  `actions/computer_control.py:24-35`, `actions/send_message.py:14-24`,
  `actions/open_app.py:47`). Optional deps are imported inside try/except at
  point of use so a missing extra disables one feature, not the app.
- **Frozen-build-aware base dir:** `_base_dir()` with `getattr(sys, "frozen")` in
  ~11 action files (`actions/reminder.py:15`, `actions/send_message.py:28`).
- **Model calls:** through `core.gemini` — in practice `gemini.call(...)`
  (`actions/web_search.py:90`, `actions/code_helper.py:34`); see
  `model_and_providers.md`. Several actions keep a dead `_get_api_key()`.

## Known drift (don't propagate)

- 11 actions read `config/api_keys.json` directly instead of
  `memory.config_manager`; `actions/screen_processor.py:59 _save_config_key`
  even writes it non-atomically. New code: use config_manager getters/`update_config`.
- `gui/` helpers or Qt widgets must never be imported from `actions/`/`plugins/`
  — `core/code_checks.py` (`_JarvisProfile.static`) flags this for dev_agent.
