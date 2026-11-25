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
| `python -m plugins._screen_eyes_tests [--save DIR]` | screen agent's eyes: control tags mapped onto the picture, OpenCV shape tags (not in photos, not panels), numbering, drawing, pixel settle with a spinner / a drawing panel / a whole page loading (fake clock), change at the acted point, and the loop's tag targets and measurements | pillow; OpenCV for the shape part (skipped without); no screen, mouse or model |
| `python -m core._uiact_tests [--live]` | working programs without the mouse: finding controls, press / toggle / text / number / list choices against a fake program that misbehaves, the secret-field and disabled refusals, `app_control`'s routing and confirm, `screen_agent` quick steps. `--live` (macOS) drives a native AppKit test window (`core/_uiact_fixture_mac.py`) in the background and checks its real state | no screen for the default run; `--live` needs pyobjc and the Accessibility permission |
| `python -m core._on_it_tests [--live]` | "on it" lines (cleaning, recording, rotation, per-language cache, background preparation) and the see-or-do verdicts, plus main.py's real `_execute_tool`: a line queued for a slow tool, once per turn, never over speech, mic released after; `screen_process` redirected for "do", unchanged for "see", never held past the wait cap; the settings' defaults. `--live` writes and records real English/French lines and asks real verdicts | stand-ins by default (no network); `--live` needs the Gemini key |
| `python -m core.selftest [--offline] [check ...]` | NOT a code test: checks THIS computer — packages, native libraries, languages, HTTPS, the Gemini key, Playwright, yt-dlp/ffmpeg, audio, sound capture, speech detector, wake word, screen permissions, app files, every tool loads — and prints a fix for each problem | the real environment; `--offline` sends nothing |
| `python -m core._selftest_tests` | the self-test's machinery: requirements per OS, deadlines, crashes, summary, `assistant_help full_check` in the background | no network |
| `python -m providers._speakable_tests [--live]` | numbers as words for local voices: 19 written sentences in 8 languages (money, units, dates, times, phones, ranges, sums, versions, negatives, Japanese without spaces), what is left alone, learning the connecting words (once, validated, never raised), and the pipeline's real `_speak` (local voice converted, ElevenLabs not, language chosen from speech / active / text). `--live` learns German, French and Spanish words with the key | babel + num2words; no network by default |
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
