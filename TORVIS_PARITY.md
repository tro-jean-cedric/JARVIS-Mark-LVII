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

Status legend: ✅ done · 🟡 partial (works, reduced scope — see note) ·
⬜ to do · ➖ already in JARVIS · ⛔ out of scope

## Plugins (Torvis `plugins/*.py`)

| Plugin | What it does | Status | Notes |
|---|---|---|---|
| pomodoro | Focus/break timer, speaks at transitions, daily focus stats | ⬜ | |
| water_reminder | Interval water reminders + daily intake vs goal | ⬜ | |
| shopping_list | Persistent shopping list by voice | ⬜ | |
| spaced_repetition | Flashcard deck with SM-2 style scheduling, AI-generated cards | ⬜ | |
| quiz | On-screen quiz on any topic / uploaded document | ⬜ | |
| excel_writer | Spoken description → real .xlsx (headers, totals, chart) on Desktop | ⬜ | |
| obsidian_vault | Read / search / write / daily notes in an Obsidian vault | ⬜ | |
| disk_map | What takes space on a drive/folder, drill down, never deletes | ⬜ | |
| gmail_control | Check, read, search, send, draft Gmail (OAuth) | ⬜ | |
| calendar_control | Read Google Calendar, create events (OAuth) | ⬜ | |
| subscription_radar | Find paid subscriptions from Gmail receipts, monthly total | ⬜ | |
| telegram_remote | Control the PC from the user's own Telegram bot; push screenshots/status | ⬜ | |
| whatsapp_call | Place / end a WhatsApp (video) call to a contact | ⬜ | |
| calorie_counter | Food on webcam → calories & macros | ⬜ | |
| fridge_chef | Fridge photo → 3 dishes → step-by-step cooking with timers | ⬜ | |
| paper_reminders | Paper/photo/PDF → reminders (bills, appointments, medicine) | ⬜ | |
| build_guide | Look at parts/wiring/assembly via webcam and explain visually | ⬜ | |
| document_review | Contract/terms → what you commit to, ranked points | ⬜ | |
| video_summarizer | Any video/audio link or file → summary / Q&A / key points | ⬜ | |
| posture_guard | Webcam posture watch + 20-20-20 eye breaks | ⬜ | |
| pushup_counter | Count push-ups live via webcam, session history | ⬜ | |
| face_lock | Assistant only works for the owner's face (enroll/lock/unlock) | ⬜ | |
| gesture_control | Webcam hand gestures: stop talking, tracks, volume, yes/no | ⬜ | |
| holo_desk | Full-screen camera view with hand-controlled floating panels | ⬜ | |
| home_assistant | Tuya / Smart Life light: on/off, colour, brightness, scenes | ⬜ | |
| printer_control | 3D printer over LAN (status, pause, temps, fans, light) | ⬜ | |
| shop_assistant | Find real prices/stock/delivery across shops, add to cart (never buy) | ⬜ | |
| connected_apps | Work inside connected services (Notion, GitHub…) via MCP | ⬜ | |
| screen_agent | Mouse/keyboard task agent driven by the screen | ⬜ | |
| planet_watch | Globe/map with earthquakes, disasters, aurora, aircraft | ⬜ | |
| visual_panel | Show tables, charts, big numbers, checklists, countdowns on the HUD | ⬜ | |
| live_panels | Live interactive panels (system diagnostics, weather board) | ⬜ | |
| live_subtitles | Live translated subtitles of computer audio in an overlay bar | ⬜ | |
| code_helper | Code help | ➖ | `actions/code_helper.py` |
| dev_agent | Coding agent | ➖ | `actions/dev_agent.py` |
| flight_finder | Flight search | ➖ | `actions/flight_finder.py` |
| game_updater | Steam/game updates | ➖ | `actions/game_updater.py` |
| cloud_agent | Torvis "cloud body": repeating phone reminders, briefings, sync, export | ⛔ | Needs Torvis's hosted server |

## Built-in actions (Torvis `actions/*.py`)

| Action | What it does | Status | Notes |
|---|---|---|---|
| world_time | Exact time in any city/country incl. DST | ⬜ | |
| persona | Persistent character: how to address the user, tone, humour, length | ⬜ | |
| action_history | Week-long log of what the assistant did; "what did you do yesterday?" | ⬜ | |
| window_control | Windows by app name: min/max/snap/focus/move monitor/list/close others/per-app volume | ⬜ | |
| app_control | Operate an app through its accessibility interface | ⬜ | |
| assistant_help | Assistant's own manual + diagnose | ⬜ | |
| open_app, browser_control, computer_control, computer_settings, desktop, file_controller, file_processor, reminder, routines, proactive, send_message, system_monitor, video_player, weather_report, web_search, screen_processor, background_tasks, background_monitor | — | ➖ | Already in JARVIS |

## App-level features (release notes / manual)

| Feature | Status | Notes |
|---|---|---|
| Moments ("when I say I'm home, do …" / at a time) | ➖ | JARVIS `routines` + `core/routines` |
| Morning brief shaped by voice | ⬜ | |
| Weekly "what I did for you" recap | ⬜ | built on action_history |
| "Things to say" suggestions under the head / First steps | ⬜ | |
| Plugin manager: add own plugin with hash approval, REVIEW on change | ⬜ | |
| Suggest a feature / Report a problem | ⛔ | Sends to Torvis's developer |
| Phone via Telegram bot (own BotFather token) | ⬜ | via telegram_remote |
| Phone control safety (confirm risky steps on phone) | ⬜ | via telegram_remote |
| Remote Mode (keep awake, hibernate, Wake-on-LAN, smart plug) | ⬜ | |
| Subscription, plans, allowance, trial, signed auto-update, relay | ⛔ | Torvis commercial service |
| Interface in 10 languages | ⛔ | Not requested |

## Log

- 2026-11-24 — Inventory extracted; shared webcam helper `plugins/_camera.py` added.
