# Air Control & Holo Desk hand control — session notes (2026-11-25/26)

What was built, what live testing on a Mac showed, what was fixed, and how to
continue on Windows. Everything below is on `main` (last commit `12a660b`).

## What exists

| Piece | Files |
|---|---|
| Shared gesture engine (Qt-free): pinch measure, One Euro smoothing, knuckle-anchored cursor, pinch detector, voted poses, swipe and crush detectors | `plugins/_gesture_core.py`, tests `plugins/_gesture_tests.py`, synthetic hands `plugins/_gesture_synth.py` |
| Camera → hands loop shared by both modes | `plugins/_hand_stream.py` (tracker: `plugins/_hands_onnx.py`) |
| Holo Desk (controls cards on the desk) | `gui/holo_desk.py`, `plugins/_holo_desk_core.py`, `plugins/holo_desk.py`, tests `plugins/_holo_desk_tests.py` |
| Air Control (controls the whole computer) | engine `plugins/_air_control_core.py`, voice tool `plugins/air_control.py`, overlay window `gui/air_control.py`, tests `plugins/_air_control_tests.py`, `gui/_air_gui_tests.py` |
| OS input (cursor, clicks, scroll, keys, desktops, focused text field) | `core/os_input.py` (macOS Quartz/AX, Windows SendInput/UIA, Linux XTest), tests `core/_os_input_tests.py` |
| Dictation into the focused field | `core/dictation.py`, tests `core/_dictation_tests.py` |
| Flight recorder (off unless `JARVIS_GESTURE_TRACE` is set) | `plugins/_gesture_trace.py` |

## Air Control gestures (current)

| Gesture | Does |
|---|---|
| Any open / relaxed hand | moves the cursor |
| Quick pinch (thumb + index), or a quick tap of the tips | click; two quick ones = double click |
| Pinch held still ~0.9 s (normal drift is fine) | menu: right click, keyboard, back, forward, browser, launcher, Mission Control / Task View, **Centre cursor**, close window, exit. Let go in its middle = click |
| Pinch, then move | drag |
| Thumb on middle finger | right click |
| Two fingers up (V) | scroll, with momentum |
| Fist | pause the cursor (reposition the hand, like lifting a mouse) |
| Fist held still 1 s | cursor back to the middle of the screen, anchored to the hand |
| Quick fist flick left / right / up / down | switch desktop / overview / app windows (Windows: show desktop) |
| Both hands open and still 1 s, Esc, or "stop air control" | exit |
| Text field focused | Type bar: Dictate (verbatim; "new line", "send it", "scratch that", "stop") or the air keyboard. Refused in password fields |

Voice: "air control" (start), "centre the cursor", "stop air control".
Holo Desk keeps its own gestures plus: grab a card anywhere, V-scroll lists,
**open palm still over a card then fist = close it** (Undo chip / "bring it back").

## Problems found by live testing (macOS) and fixed

Each was found in the flight recorder's numbers, not guessed.

| Symptom | Cause | Fix | Commit |
|---|---|---|---|
| App crashed 3 s after Air Control started | text-field poll asked the system-wide AX element; with JARVIS focused, AppKit answered in-process on a worker thread → SIGSEGV in Qt accessibility | ask the focused app's element, skip our own pid | `2811f8f` |
| (robustness) one bad Qt slot could abort the app | PyQt6 qFatal with the default excepthook | logging excepthook | `84130ae` |
| "air control" answered with the status | tool description | bare "air control" = start | `d11fd26` |
| Cursor froze at random; a pinch right after was dropped | open hand was the clutch; a real resting hand flickers open ↔ relaxed (spread 10-25° around the 12° line) | any hand moves; fist = clutch; swipes need a fist | `9d541a4` |
| Pinches with a curled index refused | "thumb at the tip" ratio too strict | tight gap always counts | `9d541a4` |
| Slow drags started 0.8-1 s late, then jumped | fixed move threshold | (superseded by `fbc9ea3`) | `0e0ebc9` |
| Long pinches (1-1.4 s) did nothing | fell between click, drag and menu | release in the menu's middle = click | `0e0ebc9` |
| Pinch with the other fingers curled refused | "closing into a fist" guard too broad | guard needs a fist-like index reach (< 1.15) | `719fa8e` |
| Pinch sometimes clicks, sometimes nothing; quick taps never | world (3D) landmarks never close (0.26-0.30 while the picture shows 0.07-0.10) and r = max(world, picture); 0.5 s "came from open" window too short for slow pinches | picture decides, world only vetoes (`r = max(r_img, min(r_world, 2·r_img+0.05))`); tip check in the picture too; 1.2 s window; closed-hand pinches must be tight | `fbc9ea3` |
| Menu "sometimes" | a still hold drifts 1-4.5 % of the box in 0.9 s; menu allowed 1.2 % | menu tolerates 4.5 %; drag needs a real move or steady speed (> 0.05 /s after 0.2 s) | `fbc9ea3` |
| Wanted: put the cursor back in the middle | — | still fist 1 s, menu item, voice | `12a660b` |

Result on the last Mac session: 21 of 21 pinches did something (9 clicks,
4 menus, 7 drags, 1 type-bar press); the session before: 2 of ~30.

Voice-side fixes made along the way (local Ollama pipeline): first turn no
longer lost to the 30 s watchdog (`66fb43a`: 90 s for a local pipeline), model
warm-up with the real prompt (merged from `worktree-whisper-mps`, `ba5350e`).
Still open: with `qwen3.5:35b-a3b` a turn whose prompt is not cached takes
40-60 s and can hit the 60 s read timeout; `qwen3.5:9b` or a `timeout` of ~180
in the provider settings would fix the experience.

## Continuing on Windows

### Run it with the recorder on

```powershell
$env:JARVIS_GESTURE_TRACE = "$env:TEMP\jarvis_trace"
python main.py
```

The recorder writes `trace.jsonl` (one line per camera frame: pinch gap in the
picture `ri` and in 3D `rw`, fused `r`, finger states `fing`, pose, pinch
state, cursor; plus every event and OS action) and `snap.jpg` (latest camera
picture with landmarks, every 0.5 s).

### What has NOT been tested on Windows yet (check these first)

1. **Click-through overlay** — `gui/air_control.py` `_win_overlay_native`
   sets `WS_EX_LAYERED | WS_EX_TRANSPARENT`. Clicks must reach the app under
   the overlay, and the overlay must never take the keyboard focus.
2. **OS input** — `core/os_input.py` `_WinBackend` (SendInput). Check cursor
   moves, left / right / double click, drag, vertical and horizontal scroll.
   Display scaling (125 % / 150 %) is the usual trap: the cursor must land
   where the ring is drawn.
3. **Desktops** — Win+Ctrl+Left/Right, Task View (Win+Tab), show desktop.
4. **Focused text field** — UIA via pywinauto (`_win_focused`); the Type bar
   should appear beside a focused field, and not for password fields.
5. **Esc to exit** — the global key check.
6. **Camera** — a different webcam changes the hand size and frame rate; the
   thresholds were tuned on a MacBook camera at ~18-22 fps.

### How to diagnose a gesture that misbehaves

- `python <job tmp>/analyze.py` style summaries are easy to rebuild: for each
  frame line in `trace.jsonl`, look at `pinch` (open / armed / pressed), `r`,
  `ri`, `rw`, `tip`, `tq`, `fing`, `reach`, `pose`; event lines (`ev`) say
  what fired, including `["blocked", reason]` (`jump`, `fast`, `new hand`,
  `pose`, `mouse`).
- "Pinched but nothing": look for frames with `r < 0.30` that stay `armed` —
  then check `tip`, `fing == "cccc"` with `reach`, and whether the fingers
  were seen open (r ≥ 0.41) in the previous 1.2 s.
- "Menu / drag confusion": compare how far the cursor moved during the press
  with `DRAG_MOVE` (0.045 of the box) and the hand speed with `DRAG_SPEED`.
- Tunables: `plugins/_gesture_core.py` (`SENSITIVITY`, `TIGHT_PINCH`,
  `FIST_REACH`, PinchDetector `came_window`) and
  `plugins/_air_control_core.py` (`DRAG_MOVE`, `DRAG_SPEED`, `HOLD_TIME`,
  `RECENTER_HOLD`, `GUARD`, `BOX`).

### Tests (no camera needed)

```powershell
python -m plugins._gesture_tests      # 103
python -m plugins._holo_desk_tests    # 105
python -m plugins._air_control_tests  # 244
python -m core._os_input_tests
python -m core._dictation_tests
$env:QT_QPA_PLATFORM="offscreen"; python -m gui._air_gui_tests   # 50
python -m providers.schema
```

All pass on macOS at `12a660b`.
