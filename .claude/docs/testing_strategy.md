# Testing strategy

Read before adding tests or verifying a change. There is **no pytest, no
`tests/` directory and no CI**. Verification is a handful of self-test
entry points plus running the app.

## Self-tests

| Command | What it checks | Needs |
|---|---|---|
| `python -m providers._tests` | providers layer end to end against local mocks; main.py's real call shapes; that `providers/` costs nothing when unused | full `requirements.txt` (imports `google.genai`, PyQt6); no network or key |
| `python -m providers.schema` | tool declarations from main.py and actions convert cleanly; prints the tool budget for small models | stdlib only (verified) |
| `python -m plugins._holo_desk_tests` | Holo Desk logic: panels, throw physics (release speed measured without the last 50 ms), every gesture (carry, fling, scroll, V-scroll over a list, two-hand resize, pull nearer, clap/spread, ring, dock with cursor snapping, point and hover focus, rotate, crush to close and the Undo chip / `reopen`), mouse, hand filter, saved desks — synthetic hands at 30 fps | stdlib only; no camera, window or network |
| `python -m plugins._gesture_tests` | the shared hand pipeline (`plugins/_gesture_core.py`): aspect-corrected 3D pinch ratio, pinch detector (median, closing motion, tip contact, confidence), pose voting, One Euro filter run once per frame, knuckle anchor and frozen offset, retro-dated aim, swipe and crush detectors, `HandStream` with a fake camera; hands built by `plugins/_gesture_synth.py` | stdlib only; no camera or models |
| `python -m core._os_input_tests` | `core/os_input.py` with a recording backend: coordinate mapping per monitor, move / drag / button / click counts / smooth scroll sign and fractions, key names and modifiers, hotkeys, `own_app_focused`, the InputPump (latest move wins, FIFO posts, slow lane, cursor sampling, close releases everything), the OS shortcuts per system | stdlib only; never touches the real mouse or keyboard |
| `python -m core._dictation_tests` | `core/dictation.py`: speech cut into phrases, spoken commands (new line, send it, scratch that, stop), typing at the caret, undo of the last phrase, Enter only on request, secure fields refused, events, engine choice | stand-in transcriber and typing; no microphone, network or key |
| `python -m plugins._air_control_tests` | Air Control's engine (`plugins/_air_control_core.py`): mapping box, rubber edges, precision, clutch, retro-dated click, double click, every void press (re-acquire, clutch exit, fast, young hand, pose change), drag and its auto-release, right click, hold menu, V-scroll (natural sign, axis lock, inertia), swipes and crush (real detectors when `_gesture_core` imports), two-hand exit, lost hand, physical-mouse pause, blocked input, the type bar (hysteresis, secure field, Dictate tap / hold), the air keyboard (rollback point, hover hysteresis, shift, suggestions), the Dispatcher (fast / slow lanes, JARVIS-focus guard, dictation ops), word predictor, settings, and the `air_control` voice tool with a fake os_input / player | stdlib only; no camera, window, mouse or network |
| `QT_QPA_PLATFORM=offscreen python -m gui._air_gui_tests` | the Air Control overlay window (flags, never activated, nothing sent until click-through is confirmed, filter once per camera frame, every `AirView` state painted, `STATE`, exit by gesture / Esc, idempotent teardown) and the Holo Desk window on a fake stream (overlay flags, crush marks, Undo chip, V-scroll); MainWindow's `_on_air` / `_on_holo` slots and `JarvisUI.air_control` on stand-ins (one camera mode at a time, failed open cleaned up). The real macOS / Windows click-through set-up is NOT exercised: check it in the running app | PyQt6 (offscreen); no camera or real input |
| `python -m plugins._face_lock_tests` | Face Lock's unlock PIN (wrong tries, 30 s wait, no PIN) and when the lock screen covers the computer | no camera, window or saved face |
| `python -m core._tool_router_tests` | tool router + argument checker: which declarations move behind `use_feature`, argument repair, resolve with a stand-in model (good / repaired / no answer / crash / switched off), savings on the real registries, and main.py's real `_execute_tool` routing a call | stdlib for the logic; the main.py section needs `requirements.txt` (skipped otherwise); no network or key |
| `python -m plugins._screen_eyes_tests [--save DIR]` | screen agent's eyes: control tags mapped onto the picture, OpenCV shape tags (not in photos, not panels), numbering, drawing, pixel settle with a spinner / a drawing panel / a whole page loading (fake clock), change at the acted point, and the loop's tag targets and measurements | pillow; OpenCV for the shape part (skipped without); no screen, mouse or model |
| `python -m core._uiact_tests [--live]` | working programs without the mouse: finding controls, press / toggle / text / number / list choices against a fake program that misbehaves, the secret-field and disabled refusals, `app_control`'s routing and confirm, `screen_agent` quick steps. `--live` (macOS) drives a native AppKit test window (`core/_uiact_fixture_mac.py`) in the background and checks its real state | no screen for the default run; `--live` needs pyobjc and the Accessibility permission |
| `python -m core._on_it_tests [--live]` | "on it" lines (cleaning, recording, rotation, per-language cache, background preparation) and the see-or-do verdicts, plus main.py's real `_execute_tool`: a line queued for a slow tool, once per turn, never over speech, mic released after; `screen_process` redirected for "do", unchanged for "see", never held past the wait cap; the settings' defaults. `--live` writes and records real English/French lines and asks real verdicts | stand-ins by default (no network); `--live` needs the Gemini key |
| `python -m core.selftest [--offline] [check ...]` | NOT a code test: checks THIS computer — packages, native libraries, languages, HTTPS, the Gemini key, Playwright, yt-dlp/ffmpeg, audio, sound capture, speech detector, wake word, screen permissions, app files, every tool loads — and prints a fix for each problem | the real environment; `--offline` sends nothing |
| `python -m core._selftest_tests` | the self-test's machinery: requirements per OS, deadlines, crashes, summary, `assistant_help full_check` in the background | no network |
| `python -m providers._speakable_tests [--live]` | numbers as words for local voices: 19 written sentences in 8 languages (money, units, dates, times, phones, ranges, sums, versions, negatives, Japanese without spaces), what is left alone, learning the connecting words (once, validated, never raised), and the pipeline's real `_speak` (local voice converted, ElevenLabs not, language chosen from speech / active / text). `--live` learns German, French and Spanish words with the key | babel + num2words; no network by default |
| `python -m core._notify_tests [--live]` | desktop notifications: the command per system (macOS text as arguments, Linux notify-send, Windows child process), failures reported, shown only when the user is elsewhere and switched on, tasks and reminders calling it. `--live` shows one real notification | stand-ins by default; `--live` shows a banner |
| `python -m core._brave_tests [--live]` | Brave Search with the user's own key: request shape (address, headers, query trimming and caps, country / language from the locale and active languages, Brave's own language names), normalisation and the http(s)-only filter, errors / timeouts / unknown key (Brave's real 422 shape) give None, the 422 retry without country, `web_search` asking Brave first and falling back to Gemini / DDG, the selftest check, the setup guide, and the key never in a log line. `--live` runs one real query only when a key is set | httpx with a stand-in transport; no network by default |
| `python -m core._sandbox_tests` | added plugins in their own process: the host starts (`--ping`), the import allow-list (standard library, requirements libraries by import name, app packages; Qt / window refused), declaration, run, player calls carried across with a quiz-grade callback, methods off the list refused, prints to the log, calls side by side, an exception / os._exit / hang / sys.exit at import / Qt or unlisted import / syntax error, trust refusals in real mode, and the registry (discovery, settings TEST button, switched-off plugins keep no process, restart after a kill, refresh, sandbox switched off) | real child processes and a temporary plugin folder (`JARVIS_PLUGIN_HOST_TEST_DIR`); no network, no window |
| `python -m core._park_tests` | "sleep now, wake at a time": reading wake times, what each OS can do (sudo / admin rights, missing tools), the commands per system and their order, the wake taken back when the sleep is refused, the resume judged from a jumping clock (on time / early / no sleep), the `remote_mode` `park` action through the confirm, Remote Mode's settings section and APPLY NOW, main.py adding it, the selftest check | stand-in clock and commands; nothing sleeps |
| `python -m core._plug_tests` | the smart plug: settings and what is missing, Shelly (address and body, key only to *.shelly.cloud, errors), off-pause-on and its failures, Tuya (token, status, command, its signature against Tuya's two published examples, token reuse, refused keys), Voice Monkey, a real unreachable address naming no key, standard library only; the server's `home_computer` (asks on the phone, declined = nothing, confirmed = cycle + wait for check-in, nothing queued behind a power cut, Wake-on-LAN preferred), settings sync, the server binding core.confirm, the Telegram bridge's Yes / No on the server, CHECK CONNECTION, keys never logged | stand-in HTTP per service; no network, no account |
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
