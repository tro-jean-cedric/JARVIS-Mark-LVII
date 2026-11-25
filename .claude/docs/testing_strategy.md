# Testing strategy

Read before adding tests or verifying a change. There is **no pytest, no
`tests/` directory and no CI**. Verification is a handful of self-test
entry points plus running the app.

## Self-tests

| Command | What it checks | Needs |
|---|---|---|
| `python -m providers._tests` | providers layer end to end against local mocks; main.py's real call shapes; that `providers/` costs nothing when unused | full `requirements.txt` (imports `google.genai`, PyQt6); no network or key |
| `python -m providers.schema` | tool declarations from main.py and actions convert cleanly; prints the tool budget for small models | stdlib only (verified) |
| `python -m plugins._holo_desk_tests` | Holo Desk logic: panels, throw physics, every gesture (carry, fling, scroll, two-hand resize, pull nearer, clap/spread, ring, dock, point, rotate), mouse, hand filter, saved desks — synthetic hands at 30 fps | stdlib only; no camera, window or network |
| `python -m plugins._face_lock_tests` | Face Lock's unlock PIN (wrong tries, 30 s wait, no PIN) and when the lock screen covers the computer | no camera, window or saved face |
| `python -m core._tool_router_tests` | tool router + argument checker: which declarations move behind `use_feature`, argument repair, resolve with a stand-in model (good / repaired / no answer / crash / switched off), savings on the real registries, and main.py's real `_execute_tool` routing a call | stdlib for the logic; the main.py section needs `requirements.txt` (skipped otherwise); no network or key |
| `python -m core.screenview` / `python -m core.screenact` | screen-reading / input capabilities on this machine (`screenact` moves the mouse) | GUI session |

`providers/_tests.py` is a hand-rolled harness: `check(name, cond, detail)`
(`providers/_tests.py:47`) prints PASS/FAIL, and each `test_*` is called by
hand from `main()` (`:1078`). To add a case, write `test_x()` and add a call
there. To run one case:
`python -c "import providers._tests as t; t.test_schema()"`, wrapping async
ones in `asyncio.run`.

Its rule (docstring `:12-28`): **call code the way main.py actually calls it.**
Take argument shapes from main.py's own source (e.g. `types.Blob`, not a dict)
and run tasks inside a TaskGroup like main.py does. Bugs that slipped past
earlier suites did so because tests used imagined call shapes.

Side effect: `test_panel_settings_survive` writes the real
`config/api_keys.json` and restores it in `finally`.

## Plugin and code smoke checks (used by the in-app coding agent)

`core/code_checks.py` is what `actions/dev_agent.py` runs after it edits code:

- `static_problems` (`core/code_checks.py:721`): `compile()` syntax checks
  plus the JARVIS profile (no PyQt widget imports in `actions/`/`plugins/`, no
  `QApplication` outside `ui.py`, plugins define `PLUGIN` and `run`).
- `runtime_problems` (`:448`): launches `core/_smoke_harness.py` in a
  subprocess with `QT_QPA_PLATFORM=offscreen`. The harness imports changed
  modules and calls changed plugins' `run()` **on a worker thread while the
  main thread runs a Qt loop**, with a recording `Player` stub and
  `core.gemini` stubbed to return nothing. This is the cheapest way to prove a
  plugin obeys the threading rule:
  `python core/_smoke_harness.py . '{"imports": [], "plugins": ["plugins/x.py"]}'`
  (read from source; not run here because dependencies are absent).

`detect_stack` only looks for `test_*.py` / `tests/`, so dev_agent never runs
`providers/_tests.py`.

## What to verify by hand

UI, audio and OS-automation changes have no automated coverage. Run
`python main.py` and exercise the feature. For anything an action claims to
have done, confirm it actually happened. That is the whole point of
`core/outcome.py`.
