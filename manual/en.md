# JARVIS — the assistant's own manual

This file is read by the `assistant_help` tool. Each `## ` section is one topic;
the tool hands the best-matching sections to the assistant when the user asks
about JARVIS itself. Write sections in plain words, about what the program
really does, and keep each one short enough to be read aloud in a sentence or
two. Name the exact button or setting wherever there is one.

## What JARVIS is

JARVIS is a voice assistant that runs as a desktop program on Windows, macOS and
Linux. You talk to it (or type in the command box), and it answers with speech
while acting on your computer: opening apps, controlling windows and settings,
searching the web, reading files, showing things on its screen, setting
reminders and running longer jobs in the background. The animated head in the
middle of the window is its face. It is an open-source project ("Mark LVII"),
written in Python with a PyQt6 interface.

## Who made it

JARVIS is an open-source project started by FatihMakes and extended by its
community. Each release is called a "Mark". It is not a product of any company
named in films; the name is a tribute.

## How it works

The conversation runs on Google's Gemini Live by default: your voice is streamed
to the model and its spoken answer comes straight back. When it needs to do
something, the model calls one of JARVIS's tools (built-in actions in
`actions/`, plugins in `plugins/`), which run on your computer and report back
whether the thing really happened (done, unverified or failed). Short side
questions — reading a picture, summarising a page — go through a "model ladder"
that steps to another model when one is out of quota or not answering. You can
replace Gemini with your own speech recognition, language model and voice in
⚙ Setup → ENGINE & PROVIDERS.

## Privacy and data

Your voice, the text of the conversation and any picture JARVIS is asked to look
at go to the model provider you use (Google Gemini by default, or the provider
you chose). Everything else stays on your computer: settings and API keys in
`config/api_keys.json`, long-term memory in `memory/`, plugin data in
`memory/plugin_data/`, and the record of what the assistant did in
`memory/plugin_data/action_history.json` (tool names and outcomes only, kept for
a week, never message text). The microphone is not streamed while the wake word
is waiting or the mic is muted. You can see and delete every stored fact in
⚙ Setup → MEMORY, and ask "forget what you did" to erase the action record.

## What you can do — overview

Ask in your own words; there is no fixed command list. Main areas:
- Apps and windows: open or close apps, minimise, maximise, snap, focus or move a
  window to another monitor, set one app's volume (Windows), press buttons inside
  an app by name.
- Computer settings: volume, brightness, media keys, Wi-Fi, dark mode, lock,
  screenshot, restart and shut down (these ask for confirmation on screen).
- Information: web search, news, prices, weather, the time anywhere in the world,
  deep research with sources, reading and summarising files you drop in.
- On screen: tables, charts, checklists and countdowns (visual panel), live
  system and weather panels, videos and pictures where the face is, live
  subtitles of whatever your computer is playing.
- Memory and character: it remembers facts about you, and keeps how you want it
  to behave (name, tone, humour, answer length) for every conversation.
- Time: reminders, routines (things done at a time or on a schedule), background
  tasks that keep working while you talk.
- Plugins: timers, quizzes, flashcards, Gmail and Calendar, Obsidian, smart
  lights, 3D printers, webcam helpers and more — see the plugin sections.

## Shaping its character (persona)

Tell JARVIS how you want it to be and it keeps it for every future conversation:
"call me Captain", "be more casual", "no jokes", "keep answers short", "use a
catchphrase". Each wish is stored under its own aspect (address, tone, humour,
length, proactivity, catchphrase, style…). Say "go back to normal humour" to
undo one, or "be yourself again" to clear them all. "Undo that" takes back the
last change. Your name and the assistant's name are set in ⚙ Setup → CUSTOMISE
ASSISTANT.

## What it did earlier (action history)

JARVIS keeps its own record of the tools it used for the last seven days: which
tool, a short purpose (like the app or city), when, and how it ended — never the
content of messages or searches. Ask "what did you do this morning?", "when did
you last open Spotify?" or "did that reminder get set?". Say "forget what you
did" to erase the record.

## Windows and apps

"Minimise Chrome", "snap Word to the left", "maximise this", "bring Spotify to
the front", "move this window to the other monitor", "which apps are open?",
"turn Discord down to 20", "close everything except Spotify". Closing everything
else shows a confirmation on the HUD first. On macOS, window control needs
Accessibility permission (System Settings → Privacy & Security → Accessibility,
enable the program running JARVIS — Terminal, Python or your IDE). On Linux it
needs `wmctrl` and `xdotool` and an X11 session; Wayland does not let one program
move another's windows. Moving windows between monitors is not available on
macOS yet; one app's own volume is Windows only.

## Pressing buttons inside an app

JARVIS can operate an app through the controls it publishes for screen readers:
"press play in Spotify", "what does the status bar in Excel say", "type hello
into the search box". Anything that sends, pays, deletes, submits or accepts asks
you to confirm on screen first. Password fields are never typed into. Some apps
(games, custom-drawn apps) publish nothing; then JARVIS uses the screen instead.
On Windows this uses pywinauto, on macOS Accessibility permission, on Linux
AT-SPI (`python3-pyatspi`).

## The time anywhere

"What time is it in Tokyo?", "is it night in New York?", "when it is 3 pm in
London, what time is it in Los Angeles?". Times are computed from the time-zone
database with daylight saving; towns not in the built-in list are looked up
online (Open-Meteo, no key). On Windows, install the `tzdata` package for fully
offline answers.

## Showing things on screen (visual panel)

Ask JARVIS to show something and it draws a board where its face is: tables,
comparisons, big numbers, bar/line/pie charts, lists, checklists, progress bars
and countdowns. "Compare these three laptops in a table", "make me a packing
checklist", "show a countdown to 18:00". Say "close it" to bring the face back.
If the picture cannot be drawn, the same content appears as text in the panel
below the head.

## Live panels: system and weather

"How is my computer doing?" shows a live panel of CPU, memory, disk, network,
GPU and temperatures (where the system reports them) and the busiest programs,
refreshed every few seconds. "Show me the weather in Lisbon" shows now, the next
hours and the coming days (from Open-Meteo, no key). "Close the panel" ends it.

## Live subtitles

"Turn on subtitles" or "translate this video into Spanish" shows a small bar at
the bottom of the screen with the speech your computer is playing, translated
into your language. Drag the bar to move it; press ✕ or say "stop the subtitles".
It needs a way to record the computer's own sound: on Windows a WASAPI loopback
device (usually available automatically), on Linux the "Monitor of …" source of
PulseAudio/PipeWire, on macOS a loopback driver such as BlackHole set up as a
multi-output device. Each few seconds of audio is sent to the model to be
transcribed and translated, so subtitles trail the speech by a few seconds.

## Plugins

A plugin is one Python file. The ones that come with the assistant (in
`plugins/`) are enabled automatically; switch any off in ⚙ Setup → PLUGINS, and
fill in the ones that need an account or address (Gmail, Telegram, smart lights,
printers…) in ⚙ Setup → PLUGIN SETTINGS. Plugins in this release include:
pomodoro, water reminders, shopping list, flashcards (spaced repetition), quiz,
Excel writer, Obsidian vault, disk map, Gmail, Google Calendar, subscription
radar, Telegram remote, cloud agent, WhatsApp calls, calorie counter, fridge
chef, paper reminders, build guide, document review, video summariser, posture
guard, push-up counter, face lock, gesture control, holo desk, home lights
(Tuya / Smart Life), 3D printer control, shopping assistant, connected apps
(MCP), screen agent, planet watch, visual panel, live panels and live
subtitles. Ask "what can the X plugin do?" for details.

Setting one up: every plugin that needs an account, a key or a device has a
"❓ HOW TO SET UP" button under its name in ⚙ PLUGIN SETTINGS. It explains where
to get each value (Telegram bot token, Google client ID, WhatsApp QR code, Tuya
keys, printer access code…) step by step, with links. The user can also just
ask "how do I set up Telegram?" — the setup_guide tool shows the same steps on
screen. There are also guides for the Gemini API key / "out of quota" and for
Remote Mode / Wake-on-LAN.

Holo Desk: say "open the holo desk". Your camera picture fills the screen and glass panels float in it - clock, system, weather, news, notes, what is coming up, a 3D model, pictures, videos, any text ("put the recipe on the holo desk"). Pinch anywhere on a panel to carry it (the cursor snaps to buttons and the ✕ near it), let go while moving to throw it, throw it off the screen to close it. To close a panel without throwing it, hold an open hand over it for a moment, then make a fist; an "Undo close" chip appears for 8 seconds (or say "reopen it"). Two fingers up (V) over a list scrolls it. Both hands on one panel resize it; pull a held panel towards you to make it bigger. Clap both open hands to put everything away, pull them apart to bring it back. Pinch and hold on empty space for a ring of new panels, or pinch a button in the dock at the bottom. Point at a panel and say "read this" or "close this". "Put the desk over my screen" floats the panels over your own programs. The mouse works too (drag, wheel, Esc). The first time, it downloads two small hand models (~8 MB).

Air Control: say "control my computer with my hand" (or "start air control"). A see-through layer covers the screen and your hand drives the real mouse in every program: any open or relaxed hand moves the cursor, a quick pinch (thumb and index) clicks, two pinches double-click, pinch and move drags, thumb on the middle finger right-clicks, two fingers up (V) scroll. A fist pauses the cursor so you can reposition your hand, like lifting a mouse; flick the fist quickly left or right to change desktop, up for all windows, down for the app's windows. Pinch and hold still for a menu (back, forward, browser, launcher, keyboard, close window, exit). When a text field has focus a small bar appears beside it: Dictate types what you say ("new line", "send it", "scratch that", "stop" are commands), or open the air keyboard and pinch keys, with word suggestions. Password fields never get dictation. To stop: show both open hands for a second, press Esc, or say "stop air control". On a Mac it needs the Accessibility permission (System Settings > Privacy & Security > Accessibility) for the app that started JARVIS. It and the Holo Desk share the camera, so opening one closes the other.

Your own plugins: in the Plugin Manager press "+ ADD PLUGIN", pick the .py file,
read the warning and APPROVE. It is listed as "added by you" and starts OFF.
A plugin runs with your own permissions and can do anything you can — only add
ones you wrote or fully trust. If the file (or a helper it came with) changes
afterwards, it stops running until you REVIEW and approve it again.

## Settings: where things are

The window has two drawers. ⚙ Setup (things you set once): REMOTE CONTROL (phone
dashboard), CREATE DESKTOP SHORTCUT, AUTO-START, CUSTOMISE ASSISTANT (assistant
name, your name, voice, colour), AUDIO DEVICES (microphone and speakers, with a
test meter), MEMORY, PLUGINS, PLUGIN SETTINGS, ENGINE & PROVIDERS. Controls (the
everyday switches): wake word, push-to-talk, face or reactor style, fullscreen
(F11). Keys: F4 mutes the microphone, Esc interrupts the assistant, Ctrl+Space is
push-to-talk. Everything is stored in `config/api_keys.json`.

## Changing the name, voice, colour or look

⚙ Setup → CUSTOMISE ASSISTANT changes the assistant's name, what it calls you,
its voice (five Gemini voices; with your own engine, the voice chosen in ENGINE &
PROVIDERS) and the colour of the whole interface. The change applies at once. The
Controls drawer switches between the face and the reactor-core look. How it
speaks — tone, humour, answer length — is changed by simply telling it (see
"Shaping its character").

## Languages: which ones it listens to and speaks

By default it is AUTOMATIC: it answers in whatever language the user speaks.
The user can limit it to the languages they actually use — ⚙ Setup → CUSTOMISE
ASSISTANT → LANGUAGES (tap languages on, APPLY), or by voice: "only speak
French", "French and English only", "add Japanese", "remove Portuguese",
"answer in any language again", "which languages are on?".

- One language on: everything heard is taken as that language and every answer
  is in it, even if a word sounds foreign.
- Several on: each answer is in the active language the user just used;
  anything that sounds like another language is treated as a mishearing. The
  first language picked is used when it is unsure (greetings, briefings,
  reminders).
- If an answer still comes out in a language that is not on, it is said again
  in an allowed one.
- With your own engine (Whisper), recognition is locked to the one active
  language, or to the most likely active one.

27 languages are supported: English, French, Spanish, Portuguese, German,
Italian, Dutch, Polish, Romanian, Turkish, Indonesian, Vietnamese, Russian,
Ukrainian, Arabic, Hindi, Marathi, Bengali, Gujarati, Tamil, Telugu, Kannada,
Malayalam, Thai, Japanese, Korean and Mandarin Chinese. Changing the languages
restarts the voice session (a few seconds).

## The phone dashboard

⚙ Setup → REMOTE CONTROL shows a QR code. Scan it with a phone on the same
network to talk to JARVIS and see its log from the phone's browser. The
connection is encrypted with a certificate JARVIS makes for itself; the browser
may warn about it the first time. With the telegram_remote plugin and your own
bot token (PLUGIN SETTINGS), you can also reach it from anywhere through
Telegram.

## Troubleshooting: it does not hear me

Check the microphone icon is green (F4 toggles mute). Open ⚙ Setup → AUDIO
DEVICES, pick the right microphone and press TEST: the meter must move when you
speak. If the wake word is on, say the wake phrase first or turn the wake word
off in Controls. On macOS, allow microphone access for the program running
JARVIS in System Settings → Privacy & Security → Microphone. If it hears its own
voice, use headphones or lower the speaker volume.

## Troubleshooting: no answers / not connecting

Usually the API key or the network. Check that a Gemini API key is set (the setup
screen appears when it is missing or refused; you can enter a new one there) and
that the computer is online. A key that has used up its free quota answers
nothing for a while; JARVIS steps to other models for side questions, but the
live voice needs a working key. If you use your own engine (ENGINE &
PROVIDERS), make sure the local server (Ollama, LM Studio…) is running.

## Troubleshooting: a plugin does not work

Open ⚙ Setup → PLUGINS: a plugin marked broken shows the reason (often a missing
Python package — install it with pip and restart). Check it is switched on, and
that its settings are filled in under PLUGIN SETTINGS. Webcam plugins need camera
permission (macOS: System Settings → Privacy & Security → Camera). The activity
log on the right shows each plugin's messages.

## Troubleshooting: it cannot click or control windows

On macOS grant Accessibility and Screen Recording to the program running JARVIS
(System Settings → Privacy & Security), then restart it. On Linux use an X11
session and install `wmctrl`, `xdotool` and `python3-pyatspi`; under Wayland,
programs may not control other windows. On Windows, an app running as
administrator cannot be controlled by a JARVIS that is not.

## Troubleshooting: subtitles stay empty

No loopback device was found or it is silent. Windows: make sure the speakers in
use are the default output device. Linux: check `pactl list short sources` shows
a ".monitor" source. macOS: install a loopback driver such as BlackHole, create a
multi-output device in Audio MIDI Setup with your speakers and BlackHole, and
select it as the output. A Gemini key (or a provider that accepts audio) is
needed to transcribe.

## Writing your own plugin

Copy `plugins/_template.py` to `plugins/<name>.py`, fill in `PLUGIN` (name,
description, parameters) and `run(parameters, player=None, session_memory=None)`,
and restart JARVIS — it is discovered and enabled automatically. Rules are in
`AGENTS.md`: return a short string, never raise, never touch Qt widgets from a
plugin (use `player.show_content`, `show_image`, `write_log`…), and use
`core.gemini` for any model call. The built-in coding agent can write one for
you: "build me a plugin that…".

## Updates

JARVIS is updated by pulling the latest version of the project (for example with
`git pull`) and running `python setup.py` to install any new dependencies. Your
settings, memory and plugin data are kept, because they live in files that are
not part of the project.
