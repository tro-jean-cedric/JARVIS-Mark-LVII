# Torvis → JARVIS feature parity — progress

Source analysed: **Torvis v0.6.3** (`infos/Torvis-x86_64.AppImage`, Linux x86_64).

## How the features were extracted

- The AppImage was extracted (after zeroing the 3 AppImage magic bytes at ELF
  offset 8 in a *copy*, so Rosetta/qemu binfmt accepts it) and the app was run
  in an amd64 container under Xvfb to confirm it starts.
- Torvis is **Nuitka-compiled** (Python → C, one 312 MB ELF). There is no
  bytecode to decompile, so features were recovered from:
  - the bundled user manual (`manual/en.md`), plugin kit (`plugin_kit/`),
    release notes (`locales/whatsnew/*.json`) and UI strings (`locales/en.json`);
  - module names and per-module string constants left in the binary (tool
    descriptions, parameter docs, user-facing messages).
- **Clean-room rule:** that material was used as a *behaviour specification
  only*. Every file in this repo was written from scratch for JARVIS's own
  architecture (`core.gemini`, `player` thread-safe API, `core.confirm`,
  `PLUGIN_SETTINGS`). No Torvis code, prompts, models or assets were copied.
- Torvis's paid back-end (relay server, subscription/allowance, signed updates,
  hosted GPU models, "cloud body") is a service, not app code, and is listed as
  out of scope below.

## Licensing

In Torvis, what a licence/plan gates is **use of its hosted servers**: voice
credits per rolling 5-hour and 30-day window, a computers-per-licence limit, a
one-time free trial, and "needs an active subscription" for connected apps and
the phone (cloud). No feature is locked in the desktop app itself beyond that.

In JARVIS **every feature below is available unconditionally** — there is no
licence, plan, allowance, trial or device check anywhere. The work Torvis does
on its paid servers runs here on the user's own Gemini key / configured
provider (`core.gemini`), the user's own accounts (Google, Telegram bot), and
JARVIS's own self-hosted cloud (`core/cloud.py`). Torvis's binary itself was
not patched or bypassed.

Status legend: ✅ done · 🟡 partial (works, reduced scope — see note) ·
⬜ to do · ➖ already in JARVIS · ⛔ out of scope

## Plugins (Torvis `plugins/*.py`)

| Plugin | What it does | Status | Notes |
|---|---|---|---|
| pomodoro | Focus/break timer, speaks at transitions, daily focus stats | ✅ | Long break every 4th block; per-task minutes |
| water_reminder | Interval water reminders + daily intake vs goal | ✅ | Understands ml/cl/l/glasses/cups/oz; quiet 23:00–07:00 |
| shopping_list | Persistent shopping list by voice | ✅ | Clear/remove are undoable (core/undo) rather than confirmed |
| spaced_repetition | Flashcard deck with SM-2 style scheduling, AI-generated cards | ✅ | Named decks; cards from a topic or the uploaded document |
| quiz | On-screen quiz on any topic / uploaded document | ✅ | `show_quiz`; built-in fallback when the model returns nothing |
| excel_writer | Spoken description → real .xlsx (headers, totals, chart) on Desktop | ✅ | Real Desktop (OneDrive/XDG aware), SUM row, bar/line/pie chart |
| obsidian_vault | Read / search / write / daily notes in an Obsidian vault | ✅ | Auto-detects vault; writes undoable; never deletes |
| disk_map | What takes space on a drive/folder, drill down, never deletes | 🟡 | Full scan/focus/up as a text report in the content panel; Torvis's clickable HUD treemap not built |
| gmail_control | Check, read, search, send, draft Gmail (OAuth) | ✅ | Send behind confirm; mark_read undoable. Needs your own Google "Desktop app" OAuth client (⚙ → Plugin settings → Google) |
| calendar_control | Read Google Calendar, create events (OAuth) | ✅ | agenda/next/search/add; natural dates (ISO → model → rule parser); local TZ; add is undoable |
| subscription_radar | Find paid subscriptions from Gmail receipts, monthly total | ✅ | Background scan (≤400 mails/12 months), model + rule fallback, per-currency monthly total, renewal reminders |
| telegram_remote | Control the PC from the user's own Telegram bot; push screenshots/status | ✅ | Own BotFather token; pairing code; text + voice notes; /screen /cam /sys /get; risky steps confirmed twice on the phone; panic switch; content-free step log. Matches the interface `core/cloud.py` expects |
| whatsapp_call | Place / end a WhatsApp (video) call to a contact | 🟡 | Desktop app (Windows, pywinauto) or WhatsApp Web (Playwright); call behind confirm; refuses wrong chat. **Untested against real WhatsApp** |
| calorie_counter | Food on webcam → calories & macros | ✅ | Webcam/photo/dropped file; daily log (today/undo/clear) |
| fridge_chef | Fridge photo → 3 dishes → step-by-step cooking with timers | ✅ | look → cook → next/repeat/stop; spoken step timers |
| paper_reminders | Paper/photo/PDF → reminders (bills, appointments, medicine) | ✅ | One-offs via `reminder`, repeating via routines engine; no duplicates. (No `help` action — any call reads a paper) |
| build_guide | Look at parts/wiring/assembly via webcam and explain visually | 🟡 | PIL wiring diagram + photo marking, mistake check, refuses mains/gas wiring. No online datasheet lookup |
| document_review | Contract/terms → what you commit to, ranked points | ✅ | `show_review` with severity-ordered findings |
| video_summarizer | Any video/audio link or file → summary / Q&A / key points | 🟡 | YouTube transcript → yt-dlp captions → audio to model; local files via ffmpeg. "Video on screen" read from the address bar by the model |
| posture_guard | Webcam posture watch + 20-20-20 eye breaks | ✅ | YuNet (auto-downloaded from opencv_zoo) or Haar fallback; calibrates 6 s; lean/slump/tilt with 1-min persistence + cooldowns |
| pushup_counter | Count push-ups live via webcam, session history | ✅ | MediaPipe pose elbow angle if installed, else face-dip tracking with hysteresis |
| face_lock | Assistant only works for the owner's face (enroll/lock/unlock) | ✅ | SFace enroll/lock/unlock(PIN)/forget; main.py mic callback is gated while locked; lock-at-start via `on_startup`; lock screen (`gui/lock_screen.py`): full-screen cover on every screen with live camera + face boxes, kept on top, keys swallowed, 'Unlock another way' = PIN (constant-time, 5 tries then 30 s); only shown when a PIN is set |
| gesture_control | Webcam hand gestures: stop talking, tracks, volume, yes/no | ✅ | Full set with `pip install mediapipe`; without it only swipe + open palm |
| holo_desk | Full-screen camera view with hand-controlled floating panels | ✅ | Own full-screen window (`gui/holo_desk.py`), camera or overlay mode. Hands with OpenCV alone: MediaPipe palm + 21-point hand models from the OpenCV zoo on cv2.dnn (`plugins/_hands_onnx.py`), two hands. Panels: clock, system, weather, news, notes (Obsidian), coming up, 3D model (.obj wireframe), image, video, text, guide. Gestures: pinch-carry by title bar, throw / fling off screen to close, list scroll, two-hand resize, pull nearer to grow, clap to clear + spread to restore, pinch-hold panel ring, dock pull-out, quick-pinch press, point = "this", hand turn rotates the model; mouse, wheel and Esc too. Snap to edges, sounds, saved desks. Not ported: closing when the computer locks; the 3D model is a wireframe, not shaded |
| home_assistant | Tuya / Smart Life light: on/off, colour, brightness, scenes | ✅ | Local (tinytuya) **or** your own Tuya cloud project; ~20 moods. Untested on a real bulb |
| printer_control | 3D printer over LAN (status, pause, temps, fans, light) | ✅ | Bambu (MQTT/TLS), OctoPrint, Moonraker; stop behind confirm; progress announcements. Camera = snapshot, not live stream. Untested on a real printer |
| shop_assistant | Find real prices/stock/delivery across shops, add to cart (never buy) | 🟡 | Search → page extraction (JSON-LD/meta/text) → model check; baskets; Playwright add-to-cart that blocks checkout/pay. Missing: variant choice, comparison-site follow-through, price tracking |
| connected_apps | Work inside connected services (Notion, GitHub…) via MCP | 🟡 | Remote (streamable HTTP) and local (stdio) MCP servers, Claude-Desktop `mcpServers` config; changes behind confirm. Missing: OAuth sign-in (use a token or mcp-remote) |
| screen_agent | Mouse/keyboard task agent driven by the screen | ✅ | look → decide → act loop on screenview/screenact; verifies each step; risky/paying steps confirmed. Eyes (Torvis `plugins._screen_eyes`, `plugins/_screen_eyes.py`): numbered tags on controls + OpenCV shapes (not in photos, not panels), the model answers with a tag; pixel settle that ignores and reports what keeps moving (spinners, video); change right at the acted point. Not ported: snapping an x/y click onto the nearest shape. Live clicking untested by design |
| planet_watch | Globe/map with earthquakes, disasters, aurora, aircraft | 🟡 | USGS+EMSC quakes, GDACS+EONET disasters, NOAA aurora, OpenSky/adsb.lol aircraft, OSM-tile maps, quake watch. No interactive 3-D globe, ISS/satellites |
| visual_panel | Show tables, charts, big numbers, checklists, countdowns on the HUD | ✅ | PIL board in the HUD palette: text/stats/list/table/bars/line/pie/progress/checklist/countdown/image/gallery. Ticking a checklist is by voice (it's a picture) |
| live_panels | Live interactive panels (system diagnostics, weather board) | ✅ | System (psutil, 3 s refresh) and weather (Open-Meteo, no key) |
| live_subtitles | Live translated subtitles of computer audio in an overlay bar | 🟡 | New `gui/subtitles.py` bar + `player.show_subtitle`. Needs a loopback device (WASAPI/Stereo Mix, Pulse monitor, BlackHole on macOS); capture untested |
| code_helper | Code help | ➖ | `actions/code_helper.py` |
| dev_agent | Coding agent | ➖ | `actions/dev_agent.py` |
| flight_finder | Flight search | ➖ | `actions/flight_finder.py` |
| game_updater | Steam/game updates | ➖ | `actions/game_updater.py` |
| cloud_agent | "Cloud body": repeating phone reminders, briefings, sync, export | 🟡 | Client for JARVIS's own self-hosted `core/cloud.py` (pinned TLS): sync, Telegram hand-over, delegate/jobs, repeating reminders, briefing, `mirror_reminder`. export/delete cover only what the server exposes (no server routes for memory export / wipe) |

## Built-in actions (Torvis `actions/*.py`)

| Action | What it does | Status | Notes |
|---|---|---|---|
| world_time | Exact time in any city/country incl. DST | ✅ | ~350-place table + Open-Meteo geocoding; time conversion between places |
| persona | Persistent character: how to address the user, tone, humour, length | ✅ | `[PERSONA]` block added to the system prompt (main.py `_build_config`); undoable |
| action_history | Week-long log of what the assistant did; "what did you do yesterday?" | ✅ | Recorded in main.py `_execute_tool` (no content, whitelisted params only); `[RECENT ACTIONS]` in prompt |
| window_control | Windows by app name: min/max/snap/focus/move monitor/list/close others/per-app volume | 🟡 | Windows full (per-app volume via pycaw); macOS via System Events without move_to_monitor; Linux X11 via wmctrl/xdotool, not Wayland |
| app_control | Operate an app through its accessibility interface | ✅ | list/press/read/type via screenview/screenact; model risk check → confirm; never types into password fields. Without the mouse (Torvis `core.uiact`, `core/uiact.py`): press / toggle / type / set number / options / choose through AX, UIA or AT-SPI on background windows, read back after every change; tried first, pointer as fallback; also for `screen_agent` quick steps. Live-tested on macOS only |
| assistant_help | Assistant's own manual + diagnose | ✅ | New `manual/en.md` written for JARVIS; diagnose shows live state |
| open_app, browser_control, computer_control, computer_settings, desktop, file_controller, file_processor, reminder, routines, proactive, send_message, system_monitor, video_player, weather_report, web_search, screen_processor, background_tasks, background_monitor | — | ➖ | Already in JARVIS |

## App-level features (release notes / manual)

| Feature | Status | Notes |
|---|---|---|
| Moments ("when I say I'm home, do …" / at a time) | ➖ | JARVIS `routines` + `core/routines` |
| Morning brief shaped by voice | ✅ | `core/briefing.py`: parts weather/calendar/reminders/email/news/focus, language, news topic/region, city; parts fetched in parallel and skipped when no data; `routines` tool `add_parts`/`remove_parts` |
| Weekly "what I did for you" recap | ✅ | `core/recap.py` + built-in routine (Sunday 18:00): week summary from action_history + one untried feature |
| "Things to say" suggestions under the head / First steps | 🟡 | Clickable chips under the head (`core/suggest.py`, local, no model call); First steps overlay with 6 steps ticked on real success (`core/first_steps.py`). Steps are fixed, not tailored per country/browser |
| Plugin manager: add own plugin with hash approval, REVIEW on change | ✅ | "+ ADD PLUGIN" → warning → APPROVE; `user_plugins/` (git-ignored); SHA-256 of plugin + helpers checked before import; starts OFF; REVIEW / ✕ |
| Suggest a feature / Report a problem | ⛔ | Sends to Torvis's developer |
| Phone via Telegram bot (own BotFather token) | ✅ | `plugins/telegram_remote.py` |
| Phone control safety (confirm risky steps on phone) | ✅ | Two-step Yes/No buttons on the phone for `core.confirm` requests from a phone turn |
| Remote Mode (keep awake, hibernate, Wake-on-LAN, smart plug) | 🟡 | `actions/remote_mode.py` + `core/remote_mode.py`: keep awake, lock when idle, screen off, hibernate (confirm), WoL info/setup/send; phone `/awake`, `/hibernate`. No settings panel (voice/phone only); "lock when screen off" is idle-based; no smart-plug or sleep-then-self-wake flow |
| Shared webcam for HUD preview + plugins | ✅ | `gui/window.py` camera preview now uses `plugins/_camera.py` |
| Choose the camera by name, next to microphone and speakers (0.6.0) | ✅ | 🎧 AUDIO & CAMERA panel: WEBCAM row lists cameras by real name (Qt `QMediaDevices`) + Automatic; TEST opens it via OpenCV and reports size / black picture / no permission; name re-matched when indexes move; APPLY restarts the shared stream (`_camera.restart()`) without a reconnect |
| Open the program the user means: installed-apps list + model pick (Torvis `core.installed_apps`) | ✅ | `core/installed_apps.py`: macOS .app bundles, Windows Get-StartApps, Linux .desktop (Flatpak/Snap); exact name, unique word match, else a model pick that must be on the list (never a similar but different program); `open_app` launches that exact program and verifies by its real name |
| OpenAI-compatible voice: Kokoro locally, OpenAI, any /v1/audio/speech (Torvis providers) | ✅ | `providers/tts_openai.py`: raw 24 kHz PCM streamed, WAV fallback learned once, server voice list, TEST. Gemini STT/TTS providers and Silero VAD not ported |
| Holo Desk closes when the computer locks; last layout comes back | ✅ | `core/screen_lock.py` (macOS CGSession, Windows LogonUI, Linux LockedHint); `_holo_desk_core.save_last/load_last` |
| Tool router: rarely used features behind one `use_feature` tool, second model writes the arguments (Torvis `core.tool_budget`, `core.tool_args`) | 🟡 | `core/tool_router.py` + `core/tool_args.py`: declarations marked `"tier": "extra"` (not NON_BLOCKING ones, not untiered user plugins) leave the live list for one `use_feature(feature, request)` tool whose enum + one-line menu lists them; switched-off plugins stay listed as "(off)" and answer how to switch them on; on a call a FAST `core.gemini` request writes the arguments from the full schema, current time and last turns, `tool_args.check` repairs names/numbers/booleans/enum spelling and reports the rest, one repair round, then the real tool runs through the usual chain (answer returned as `use_feature`). With this checkout's tools: 62 → 41 declarations, 90,621 → 63,696 characters. **Off by default** (`"tool_router": true` in config). Not ported: the live model's own first-try arguments and an earlier tool result as extra context; untiered bundled plugins stay direct. Never tried against live Gemini |
| Instant "on it" lines while a slow job runs; see-or-do check before a screenshot (Torvis `_on_it_phrases`, `_screen_job_owner`) | ✅ | `core/on_it.py`: three lines per language from the model, recorded once in the Live voice (`core.gemini.say_audio`, transcript must match), cached in `memory/on_it/`, played from the speaker queue after 1.2 s of tool time, once per user turn, never over speech; Gemini Live only. `core/see_or_do.py`: the user's words judged in parallel with the screen capture; "do" drops the picture and names the feature (through use_feature when it is behind the router). Settings `on_it_lines` (on), `see_or_do` (off by default: +1.2-1.9 s per look, measured) |
| Capability self-test: what the features rely on really works here, and what to fix (Torvis `core.selftest`) | ✅ | `core/selftest.py`: 15 checks, each on its own thread with a deadline; pass / fail / off (optional part not set up) with a fix; packages read from requirements.txt for this OS; the local speech detector and the wake-word model exercised on synthetic audio; two network checks skipped with --offline. Voice: `assistant_help` `full_check=true`. Not ported (Torvis-only): checks of its compiled build, bundled libraries, relay and interface translations |
| Numbers, money, units, dates and times said as words for local voices (Torvis `providers._numbers`, `providers._speakable`) | ✅ | `providers/_numbers.py` (num2words, with decimals built by hand where it is wrong: Turkish, Vietnamese, Arabic) and `providers/_speakable.py` (Babel CLDR for unit, currency and month names, decimal and group symbols, date order); applied in `PipelineEngine._speak` for every voice except ElevenLabs (`SPEAKS_NUMBERS`). Words for ranges / sums / clock times learnt once per language (memory/speech_words.json, git-ignored). Not ported: caching the audio of short replies |
| Desktop notifications for finished tasks and reminders (Torvis `core.notify`) | ✅ | `core/notify.py`: macOS osascript with the text as arguments (Torvis had no macOS path), Linux notify-send, Windows win10toast in a child process; only while the user is in another window; called from `core/tasks/desktop.py` (done / failed, not silent tasks) and main.py's reminders. Setting `desktop_notifications` |
| "Search YouTube for…" searches on that site itself, any site named (0.5.1) | ✅ | `core/sitesearch.py` + `browser_control` `search` with `site`: table of ~35 well-known sites (country domains of Amazon/eBay/Wikipedia kept), else the site's OpenSearch description (cached), else a `site:` web search that the reply says is one |
| Active languages (listen/answer only in chosen languages) | ✅ | Beyond Torvis's "stays in your language": `core/languages.py` — pick languages in Customise → LANGUAGES or by voice (`languages` tool); `[ACTIVE LANGUAGES]` rule last in the system prompt; spoken replies checked with py3langid and re-said in an allowed language (20 s cooldown); Whisper locked to the active set; greeting/briefing/recap/notices/summaries clamped |
| Setup guides ("?" next to each plugin's setup) | ✅ | `core/setup_guides.py`: "❓ HOW TO SET UP" in ⚙ PLUGIN SETTINGS for the 12 account/device sections (steps + links), `setup_guide` voice tool, extra guides for Gemini key/quota and Wake-on-LAN, Telegram CHECK BOT button |
| Subscription, plans, allowance, trial, signed auto-update, relay | ⛔ | Torvis commercial service |
| Interface in 10 languages | ⛔ | Not requested |

## Log

- 2026-11-24 — Inventory extracted; shared webcam helper `plugins/_camera.py` added.
- 2026-11-24 — Shared `plugins/_announce.py` (speak later / plugin JSON storage in `memory/plugin_data/`).
- 2026-11-24 — Fixed `.gitignore`: trailing comments had disabled the patterns for `config/api_keys.json`, `config/certs/`, `config/whatsapp_web/`, `**/token*.json`, `**/client_secret*.json`. Note: `config/certs/jarvis.key` is in git history (commit 067f528) — regenerate the dashboard certificate if the repo is public.
- 2026-11-24 — Google plugins done; shared OAuth in `plugins/_google_core.py`, token in `config/token_google.json` (git-ignored).
- 2026-11-24 — All 33 missing Torvis plugins and 6 missing actions added (productivity, Google, Telegram/cloud/WhatsApp, vision, webcam watchers, devices/web/screen agent, HUD panels).
- 2026-11-24 — Wave 2: main.py hooks (face-lock mic gate, `on_app_ready` start-up), briefing by voice, weekly recap, suggestion chips + First steps, user plugins with hash approval, Remote Mode, shared HUD camera. AGENTS.md + manual updated for user plugins.

- 2026-11-24 — Wave 3: camera choice by name (device panel) and on-site search (`core/sitesearch.py`). Module-name diff of the Torvis binary against JARVIS re-run; remaining Torvis-only modules are its commercial service (account, credits, trial, updater, relay, cloud memory), Linux packaging/bundling, the 10-language UI, and internals already covered under other names.

- 2026-11-25 — Holo Desk full port (own window, ONNX hands, every gesture) and improvements; installed-apps resolver for open_app; OpenAI-compatible voice provider. Still open from the Torvis comparison: Face Lock lock screen (`gui.lock_screen`), tool router (`core.tool_budget`), screen-agent marks (`plugins._screen_eyes`), mouse-free app control (`core.uiact`), speakable numbers for local voices (`providers._numbers`, needs babel), Remote Mode park/smart plug, MCP OAuth sign-in, planet watch globe/ISS/satellites.

- 2026-11-25 — Tool router (`core/tool_router.py`, `core/tool_args.py`, self-test `python -m core._tool_router_tests`); off by default.

- 2026-11-25 — Screen-agent eyes (`plugins/_screen_eyes.py`, self-test `python -m plugins._screen_eyes_tests`).

- 2026-11-25 — App controls without the mouse (`core/uiact.py`, self-test `python -m core._uiact_tests [--live]`).

- 2026-11-25 — "On it" lines + see-or-do (`core/on_it.py`, `core/see_or_do.py`, self-test `python -m core._on_it_tests [--live]`).

- 2026-11-25 — Capability self-test (`core/selftest.py`, self-test `python -m core._selftest_tests`).

- 2026-11-25 — Numbers said as words for local voices (`providers/_speakable.py`; babel + num2words added).

- 2026-11-25 — Desktop notifications (`core/notify.py`, self-test `python -m core._notify_tests`).

## Totals (2026-11-24)

- Plugins: 34 added — 25 ✅, 9 🟡 (disk_map, build_guide, video_summarizer, whatsapp_call, cloud_agent, shop_assistant, connected_apps, planet_watch, live_subtitles); 4 already in JARVIS.
- Actions: 6 added — 5 ✅, 1 🟡 (window_control) — plus the new `remote_mode` action.
- App-level: 8 ✅, 2 🟡, 1 ➖ (Moments), 3 ⛔ (Torvis's own commercial services / feedback channel / 10-language UI).
- Nothing in JARVIS is gated by a licence, plan, allowance or trial.

## Not verified on real hardware/accounts (needs a person)

Real Google account (Gmail/Calendar/subscriptions), Telegram bot, WhatsApp
calls, Tuya bulb, 3D printer, screen-agent clicking, shop add-to-cart on a live
shop, loopback audio for subtitles, webcam accuracy (posture, push-ups,
gestures, face lock), Windows/Linux window control.
