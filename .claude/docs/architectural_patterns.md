# Architectural patterns

Read before changing startup, the voice session, threading, the window, or
before adding a new subsystem.

## Runtime shape

- `main()` — `main.py:3052` builds `JarvisUI` (creates the one `QApplication`,
  `ui.py:100`), starts a daemon thread that waits for an API key then runs
  `asyncio.run(JarvisLive(ui).run())`, and blocks the main thread in Qt's
  event loop. Closing the window → `jarvis.finish()` → `os._exit`.
- `JarvisLive.run` (`main.py:2696`) is a reconnect loop: pick model
  (`gemini.live_model()`), resolve engine caps (`_resolve_engine_caps`,
  `main.py:2587` — `None` means Gemini Live), build config (`_build_config`,
  `main.py:1376`), open the session (`_open_session`, `main.py:2647`) and run a
  `TaskGroup` of send/listen/receive/play/watchdog tasks.
- Reconnect is signalled, not forced: `request_reconnect(keep_context, reason)`
  (`main.py:1122`) sets an event; `_watch_reconnect` raises `_ReconnectSignal`
  to unwind the TaskGroup. Errors step through a ladder (rejected resume
  handle → quota step-down → schema-drift feature disable → bad key →
  exponential backoff, `main.py:2904-3012`).
- System prompt: `core/prompt.txt` with `{tokens}` filled by `_render_prompt`
  (`main.py:324`, plain `str.replace`, not `str.format` — literal braces are
  safe). Tokens: `assistant_name`, `platform`, `capabilities`, `limits`.
  Edit wording in prompt.txt, not in Python.

## Threads (the rule that breaks most generated code)

| Thread | Owns |
|---|---|
| Qt main | every widget; `gui/` package |
| asyncio (daemon) | Live session, dashboard server, `main.py` coroutines |
| executor/worker | every action and plugin call |
| named daemons | task scheduler `task-*`, `routines`, `gemini-live-oneshot`, wake word |

Worker → UI only through thread-safe `JarvisUI` methods, each of which emits a
`pyqtSignal` on `MainWindow`:

- Signal `MainWindow._content_sig` (`gui/window.py:100`), connected at
  `gui/window.py:398`, slot `_show_content` (`gui/window.py:1862`), emitter
  `JarvisUI.show_content` (`ui.py:265`). All signals declared at
  `gui/window.py:98-119`, connected at `396-417`. Same shape: `write_log`,
  `set_state`, `show_quiz`, `show_review`, `show_video`, `show_image`.
- To add one: signal on MainWindow → connect in `__init__` → slot → thin
  emitting method on `JarvisUI` with a "Thread-safe:" docstring.
- Known exceptions that touch widgets directly from other threads:
  `notify_phone_connected` (`ui.py:252`) and the `muted` setter (`ui.py:113`).
  Don't copy them.
- UI → session goes the other way: a `threading.Thread` calls a callback which
  uses `asyncio.run_coroutine_threadsafe` (`main.py:1231`).

## Window (`gui/`)

- Split out of a single `ui.py` in Mark LVII. `ui.py:38-43` re-exports every
  top-level gui name so `from ui import X` keeps working — keep names that
  plugins may import at module level.
- Palette is class `C` with attributes (`C.PRI`, not `C["PRI"]`) —
  `gui/theme.py:99`. Live re-theme string-replaces hex values in every
  stylesheet (`retheme_all_widgets`, `gui/theme.py:177`), so stylesheets may
  contain only exact `C.*` hex values; translucent/mixed colours must be
  painted (`gui/style.py:12-15`). Fonts: `hud_font`/`px_font` (`gui/style.py:44`).
  Hard-coded `Courier New` / hex remain in a few places (e.g.
  `gui/window.py:1917`, `gui/widgets.py:236`) — legacy, don't add more.

## Cross-cutting conventions (≥3 files each)

- **Optional means free when unused.** Features load lazily and fail soft:
  `providers/` is never imported unless mode is `pipeline`
  (`main.py:2587-2600`, enforced by a test); dashboard, routines and tasks
  are wrapped so a failure disables only themselves (`main.py:729-750`,
  `main.py:2721-2730`). Lazy third-party imports via `core/lazyimport.py`.
- **Never raise across a boundary.** Loaders, config reads, model calls and
  tools return a value or a degraded result: `gemini.call` → `None`
  (`core/gemini.py:714`), `store.read_json` → default, `registry._load` records
  failures, plugin/action registries convert crashes to sentences.
- **Declared, discovered, not registered.** `TOOL` (actions), `PLUGIN`
  (plugins), `PROVIDER` (providers, though `providers/registry.py:35` lists
  modules by hand), agent `.md` files. Dropping a file in is the integration.
- **Atomic JSON writes** (tmp + `os.replace`): `core/tasks/store.py:31`,
  `memory/config_manager.py:49`, reused by routines. Exceptions:
  `memory/memory_manager.py` writes in place; reports use `write_text`.
- **Explicit deadlines** on every external call (gemini 10 s minimum, task
  brain 120 s, web fetch 20 s, test runs 180 s).
- **Injectable `log`/`clock`** on engines for testability
  (`core/tasks/engine.py:125`, `core/routines/engine.py:71`).
- **Logging:** console `print("[Tag] ...")`; user-visible
  `player.write_log("SYS: ...")`.
- **Long "why" docstrings** at the top of modules record measured decisions —
  read them before "simplifying" something that looks over-engineered.

## Dead / legacy code (verified: no importers)

`core/llm_client.py`, `core/installer.py` (both "MARK XL"), `core/stt.py`,
`core/tts.py` — superseded by `providers/`. `core/screenview.py` and
`core/screenact.py` are only imported by each other (used by the absent
`screen_agent` plugin). `dashboard/static/crypto-js.min.js` is unreferenced.
`core/cloud.py` is imported by nothing on the desktop; it is the server CLI
(see `remote_surfaces.md`).
