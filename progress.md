# Torvis → JARVIS: progress

What has been brought over from Torvis (v0.6.3), what is partly there, and what
is left. Update this file in the same commit as every feature you add.
To continue the work, use the prompt in [`TORVIS_PORT_PROMPT.md`](TORVIS_PORT_PROMPT.md).

The detailed, per-feature notes (how each one was built, what it depends on)
stay in [`TORVIS_PARITY.md`](TORVIS_PARITY.md). This file is the short list.

**Ground rule — clean room.** Torvis is a commercial, closed product. Its
behaviour (manual, help text, tool descriptions, module names) is a
*specification only*. Nothing is copied: no code, prompts, user-facing text,
models or assets. Write every string in your own words and check it with the
wording check in `TORVIS_PORT_PROMPT.md`. Third-party models Torvis also ships
(OpenCV zoo, MediaPipe, Silero) are fetched from their own upstream, under
their own licences.

Status: ✅ done · 🟡 partly done · ⬜ to do · ⛔ out of scope

---

## Done ✅

### Added in the 2026-11-25 session (on `main`)

| Feature | Where | Commit |
|---|---|---|
| Instant "on it" lines + see-or-do check: three very short lines per language written once by the model, recorded once in the assistant's own Live voice (`gemini.say_audio`, transcript checked) and played from the speaker queue when a tool is still running after 1.2 s (once per user turn, never over speech, Gemini Live only, mic released after); before a screenshot, the user's words are judged alongside the capture and a request one picture cannot answer is sent to the right feature instead (**off by default**, `"see_or_do": true`: right on every request tried, but +1.2-1.9 s on every look and its fast model often refused under load) | `core/on_it.py`, `core/see_or_do.py`, `core/gemini.py`, `main.py` | COMMIT |
| App controls without the mouse: press, toggle, set text, set a number, list and choose drop-down entries through the accessibility interface (macOS AX, Windows UIA, Linux AT-SPI), on windows in the background; every change read back; secret fields and disabled controls refused. `app_control` tries it first (new toggle / set / options / choose actions), `screen_agent` quick steps too. Tested for real on macOS (Qt + native AppKit test windows, Calculator). Also fixed: Qt password fields on macOS were not recognised as secret (`core/screenview.py`) | `core/uiact.py`, `actions/app_control.py`, `plugins/screen_agent.py`, `core/screenview.py` | `40cd700` |
| Screen-agent eyes: numbered tags on what can be pressed (accessibility controls + OpenCV shapes, none inside photos or on panels), wait until the pixels settle while ignoring what keeps moving, spinners / videos reported as still moving, change measured right where a step acted. Not ported: snapping an x/y click onto the nearest shape (the existing closer look covers most of it) | `plugins/_screen_eyes.py`, `plugins/_screen_loop.py`, `plugins/screen_agent.py` (setting) | `7aa1c0b` |
| Holo Desk, full version: own full-screen window, camera or overlay mode, ONNX two-hand tracking (no MediaPipe), 11 card types, every gesture (carry, fling, scroll, two-hand resize, pull nearer, clap/spread, card circle, dock, point = "this", wrist twist), mouse/wheel/Esc, sounds, saved layouts | `gui/holo_desk.py`, `plugins/holo_desk.py`, `plugins/_holo_desk_core.py`, `plugins/_holo_desk_content.py`, `plugins/_hands_onnx.py` | `40638dc` |
| Holo Desk: last layout comes back, closes when the computer locks, shaded 3D model, hand skeleton + pinch / hold feedback, speed-aware smoothing, settings (sensitivity, start mode, remember, show hands, sounds) | same + `core/screen_lock.py` | `ffa0805` |
| Open the program the user means (installed-apps list + model pick, never a look-alike) | `core/installed_apps.py`, `actions/open_app.py` | `33d405e` |
| OpenAI-compatible voice: Kokoro locally, OpenAI, any `/v1/audio/speech` | `providers/tts_openai.py` | `a1a5536` |
| Face Lock lock screen: cover on every screen, live camera + face boxes, "Unlock another way" PIN (5 tries, then 30 s) | `gui/lock_screen.py`, `plugins/_face_lock_core.py` | `08fe4e5` |
| Face Lock: camera on the HUD while scanning or locked (when no PIN / cover off) | `plugins/face_lock.py` | merged `e862746` |
| Screen-lock detection (macOS, Windows, Linux) | `core/screen_lock.py` | `ffa0805` |

Also in that session, not from Torvis: Claude Code as a provider, coding agent
and background agent (`core/claude_code.py`, `providers/llm_claude_code.py`,
`caab17e`).

### Done earlier (see `TORVIS_PARITY.md` for notes)

Plugins: action_history · app_control · assistant_help · calendar_control ·
calorie_counter · document_review · excel_writer · face_lock · fridge_chef ·
gesture_control · gmail_control · holo_desk · home_assistant (Tuya) ·
live_panels · obsidian_vault · paper_reminders · persona · pomodoro ·
posture_guard · printer_control · pushup_counter · quiz · screen_agent ·
shopping_list · spaced_repetition · subscription_radar · telegram_remote ·
visual_panel · water_reminder · world_time.

App-level: plugin manager with hash approval · active languages · moments
(routines) · morning brief by voice · weekly recap · setup guides · shared
webcam · camera chosen by name · phone confirm for risky steps.

Already in JARVIS before the port: code_helper · dev_agent · flight_finder ·
game_updater · open_app · browser_control · computer_control and the other
built-in actions.

---

## Partly done 🟡 — what is missing

| Feature | Missing |
|---|---|
| planet_watch | 3-D globe lit by the real Sun with zoom to street map; ISS pass predictions + spoken alerts (needs `sgp4`); ~17,000 live satellites (CelesTrak / SatNOGS); country cards (flag, capital, currency, local time); routes with distance and flight time; globe steered by hand (pinch to turn, two hands to zoom) |
| shop_assistant | price tracking with alerts; choosing a product variant (size, colour) |
| disk_map | the clickable treemap on the HUD (today: text report) |
| build_guide | online datasheet lookup for parts |
| connected_apps | OAuth sign-in for MCP servers (Notion, GitHub…): discovery, auto-registration, token refresh; a setup / reconnect panel |
| video_summarizer | "the video on screen" without reading the address bar |
| window_control | macOS move to another monitor; Linux Wayland |
| cloud_agent | parity with Torvis's hosted "cloud body" where it makes sense self-hosted |
| Remote Mode | sleep, then wake on a timer (`core.park`); switch the computer's smart plug (Tuya cloud, Shelly cloud, VoiceMonkey); automatic Wake-on-LAN setup on Windows (adapter settings, Fast Startup off); a settings panel |
| Tool router (`core/tool_router.py`, `core/tool_args.py`, commit `c7f8738`) | built and self-tested, **off by default** (`"tool_router": true` in config) and never tried against live Gemini; not ported: the live model's own first-try arguments and an earlier tool result as extra context for the argument writer; bundled plugins with no `tier` (build_guide, calorie_counter, fridge_chef, planet_watch…) stay direct — tag them `"extra"` to move them behind the router |
| Voice providers | Gemini as speech-to-text and as voice (user's own key); Silero voice-activity detection as an option (keep the energy detector as default) |
| live_subtitles · whatsapp_call | built, but never tested against real audio loopback / real WhatsApp |

---

## To do ⬜ — ranked (value ÷ effort)

Evidence = line numbers in the Torvis string dump used for the comparison
(regenerate it as described in `TORVIS_PORT_PROMPT.md`).

1. **Capability self-test** — one command / voice request that checks
   languages, TLS, Playwright, yt-dlp, speaker capture, VAD and wake-word
   models and says what is missing. *Small–medium.* Evidence: `core.selftest`
   (~3293045–3293461). Extends `assistant_help`'s diagnose.
2. **Numbers said as words for local voices** — numbers, dates, times,
   ranges, money, units, phone numbers and sums spelled out in the user's
   language before Piper / system voices read them; cache short replies.
   *Medium*, needs `babel` (new dependency — ask first). Evidence:
   `providers._numbers`, `_speakable` (~3857518–3858394). Target:
   `providers/engine_pipeline.py` `_speakable`.
3. **Desktop notifications** — `notify-send` / macOS / Windows toast for
   finished tasks and reminders. *Small.* Evidence: `core.notify`.
4. **Brave Search with the user's own key** — an extra web-search source.
   *Small.* Evidence: `core.brave` (~3265044).
5. **Plugin sandbox** — user-added plugins run in a separate host process with
   an allow-list of libraries. *Medium.* Evidence: `core.script_host`
   (~3292662–3292892). Target: `core/plugin_loader.py`.
6. The 🟡 gaps above, roughly in this order: Remote Mode additions, voice
    providers, connected_apps OAuth, planet_watch (large — split it), the rest.

---

## Out of scope ⛔

Torvis's commercial service and packaging: account, credits / allowance,
licence and trial, edition, signed updater, relay, hosted "cloud memory",
footprint eraser, installers, telemetry, "report a problem" to its developer,
the 10-language interface translations.

---

## Needs a person to verify

Things built and tested in code but never tried for real:
- Holo Desk with a real webcam and live hands (gesture thresholds may need tuning)
- Holo Desk closing on a real screen lock; overlay transparency; sounds
- Face Lock lock screen with a real face, on more than one monitor, and how
  macOS treats it keeping focus
- OpenAI-compatible voice against a real Kokoro / OpenAI server
- open_app's resolver on Windows and Linux, and with the Gemini key
- the tool router switched on (`"tool_router": true`) in a real Gemini Live session: that the model calls `use_feature` for listed features and the written arguments are right
- the screen agent with the new tags on a real task: that the model answers with tag numbers and the tags land on the right things in other programs, on Windows and Linux, and with Accessibility granted on macOS
- app controls without the mouse on Windows (UI Automation) and Linux (AT-SPI): written, never run; and on macOS with real apps beyond the test windows and Calculator. Known limit: Qt drop-downs on macOS ignore every accessibility way of choosing an entry (reported as such; the screen agent then uses the pointer)
- the "on it" line heard by a person (the queue and the transcript were checked, the sound itself was not listened to), and with the local voice engine (switched off there by design)
- the items listed under "Not verified on real hardware/accounts" in
  `TORVIS_PARITY.md`

---

## Log

- 2026-11-25 — "On it" lines + see-or-do ✅. Run for real: English and French lines written and recorded (transcripts match the text), see/do verdicts right on every request tried with the app's 62 features once tuned (a first version refused to look at "what's on my screen right now?" — found in the running app, fixed), and in the running app a web search of 8.8 s got "Working on it." played while it ran, after the first request in English had prepared the lines in the background. See-or-do ships off by default: +1.2-1.9 s per look, and the fast model it needs kept answering 503. Self-test `python -m core._on_it_tests [--live]`. Wording check: 0 overlaps in new text.
- 2026-11-25 — App controls without the mouse ✅ (`core/uiact.py`). Run for real on macOS: Qt and native AppKit test windows in the background (native: 22/22, incl. choosing a pop-up entry; Qt: all but choosing, which Qt ignores and is reported honestly), Calculator 7+5=12 in the background. Then the whole app was run: a request typed into JARVIS ("In Calculator, press the 7 button") made Calculator show 7 while it stayed in the background, and JARVIS said so; no errors in the app's log. The live run found and fixed: Qt password fields not flagged as secret (also in screenview), slider values given as text, Qt pop-up open/close. Self-test `python -m core._uiact_tests [--live]`. Wording check: 0 overlaps in new text.
- 2026-11-25 — Screen-agent eyes (to-do #1 after the router) ✅: tags, pixel settle with moving-area detection, change at the acted point. Real screenshot of a macOS IDE without accessibility data: 60 sensible tags from shapes alone in 0.02 s. Self-test `python -m plugins._screen_eyes_tests`. Wording check: 0 overlaps in new text.
- 2026-11-25 — Tool router (to-do #1) built as 🟡: `use_feature` + argument writer + argument checker, off by default; 62 → 41 live declarations (−30 % characters) when on. Self-test `python -m core._tool_router_tests`. Wording check: 0 overlaps.
- 2026-11-25 — Holo Desk full port + improvements; installed-apps resolver;
  OpenAI-compatible voice; Face Lock lock screen; HUD camera for Face Lock.
  User-facing wording that had been taken from Torvis rewritten in our own
  words (wording check: 0 overlaps). This file and the continuation prompt
  created.
- 2026-11-24 — First port waves (see `TORVIS_PARITY.md` log).
