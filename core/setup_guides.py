"""
core/setup_guides.py — step-by-step "how do I set this up?" guides.

One place for every guide, used in two ways:
  * ⚙ PLUGIN SETTINGS shows a "❓ HOW TO SET UP" button under each section that
    has one (core/plugin_loader.py attaches the guide to the section; the panel
    in gui/overlays.py renders it, links clickable).
  * The `setup_guide` voice tool (actions/setup_guide.py) finds the right guide
    from what the user said ("how do I connect Telegram?") and shows it.

Guides are keyed by the settings NAMESPACE of the section they explain. A
plugin may also carry its own guide inside PLUGIN_SETTINGS["guide"] (useful
for plugins the user adds); that one wins over the entry here.

A guide:
    {
      "title":   shown as the heading,
      "aliases": words people use for it (for the voice tool),
      "time":    rough time it takes,
      "need":    what you need before starting (optional),
      "steps":   [ "plain step", ("step with a link", "https://…"), … ],
      "done":    how to know it worked / what to say first (optional),
      "note":    anything worth knowing afterwards (optional),
    }
Field and button names in the steps are the real labels of the settings panel,
in capitals, so the user can find them.
"""
from __future__ import annotations

import html
import re
from typing import Optional

GUIDES: dict[str, dict] = {

    # ── Telegram ─────────────────────────────────────────────────────────────
    "telegram_remote": {
        "title": "Telegram — use the assistant from your phone",
        "aliases": ["telegram", "phone", "bot", "botfather", "remote", "mobile"],
        "time": "about 3 minutes",
        "need": "The Telegram app on your phone (or Telegram Desktop).",
        "steps": [
            ("In Telegram, open a chat with @BotFather (Telegram's official bot for making bots).",
             "https://t.me/BotFather"),
            "Send /newbot. Give it a display name (e.g. My Jarvis), then a username that ends in "
            "'bot' (e.g. myjarvis_home_bot).",
            "BotFather answers with a token that looks like 123456789:AAF…. Copy it and paste it into "
            "BOT TOKEN here. Treat it like a password.",
            "Type any secret phrase into PAIRING CODE (three random words are fine). You will send it "
            "to your bot once so it knows the phone is yours.",
            "Leave LET APPROVED PHONES SEND REQUESTS ON. Switch on screenshots / webcam pictures / "
            "files only if you want them. Press SAVE.",
            "Press CHECK BOT — it confirms the token and gives you a t.me/… link to your bot.",
            "Say \"start the Telegram remote\" (or switch on START LISTENING WHEN THE ASSISTANT "
            "LAUNCHES). Pairing stays open for 10 minutes.",
            "Open your bot in Telegram, press START, and send your pairing code. APPROVED CHATS "
            "fills itself in.",
        ],
        "done": "Send /sys to the bot, or write \"what's on my screen?\". Send /help for all commands.",
        "note": "Turn on two-step verification in Telegram (Settings → Privacy and Security). "
                "Lost your phone? Say \"Telegram panic\" at the computer: it stops the bridge and "
                "forgets every approved phone.",
    },

    # ── Google: Gmail, Calendar, subscription radar ─────────────────────────
    "google": {
        "title": "Gmail, Google Calendar and Subscription radar",
        "aliases": ["gmail", "email", "mail", "google", "calendar", "agenda", "subscriptions",
                    "oauth", "client id", "client secret"],
        "time": "about 10 minutes, once for all three",
        "need": "Your Google account. Google makes you create your own small 'app' so the "
                "assistant can read your mail with your permission — nobody else sees it.",
        "steps": [
            ("Open Google Cloud Console and create a project (any name, e.g. Jarvis).",
             "https://console.cloud.google.com/projectcreate"),
            ("Enable the Gmail API for that project (press ENABLE).",
             "https://console.cloud.google.com/apis/library/gmail.googleapis.com"),
            ("Enable the Google Calendar API too.",
             "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"),
            ("Open Google Auth Platform → Get started: app name 'Jarvis', your email as support "
             "email, Audience = External, your email as contact, then Create.",
             "https://console.cloud.google.com/auth/overview"),
            ("In Audience → Test users, press ADD USERS and add your own Gmail address.",
             "https://console.cloud.google.com/auth/audience"),
            ("In Clients → CREATE CLIENT, choose Application type = Desktop app, any name, Create.",
             "https://console.cloud.google.com/auth/clients"),
            "Copy the Client ID into OAUTH CLIENT ID and the Client secret into OAUTH CLIENT SECRET "
            "(or download the JSON and put its path in the third box). Press SAVE.",
            "Press AUTHORIZE. Your browser opens: choose your account. Google warns that it has not "
            "verified the app — it is your own, so press Continue — and tick every permission.",
        ],
        "done": "Say \"check my email\", \"what's on my calendar today?\" or \"scan my subscriptions\".",
        "note": "While the app is in Testing, Google asks you to sign in again every 7 days — the "
                "assistant tells you when. To stop that, press PUBLISH APP under Audience (fine for "
                "personal use).",
    },

    # ── WhatsApp ─────────────────────────────────────────────────────────────
    "whatsapp_call": {
        "title": "WhatsApp — calls and messages",
        "aliases": ["whatsapp", "whats app", "wa", "call", "video call", "message"],
        "time": "about 1 minute",
        "need": "WhatsApp on your phone. On Windows and Mac the WhatsApp desktop app (signed in) "
                "is used when it is installed; otherwise, and on Linux, WhatsApp Web inside the "
                "JARVIS window (or in Google Chrome / Microsoft Edge when the embedded browser, "
                "PyQt6-WebEngine, is not installed).",
        "steps": [
            "Leave HOW TO REACH WHATSAPP on 'auto' (the WhatsApp desktop app on Windows and Mac "
            "if installed, otherwise WhatsApp Web). With the desktop app, sign in to it, open any "
            "chat and press CHECK in ⚙ → WHATSAPP CALLS (or just say \"check WhatsApp\") — on a "
            "Mac, allow Accessibility for the app JARVIS runs from (System Settings → Privacy & "
            "Security → Accessibility) if asked; that is all, nothing to scan.",
            "WhatsApp Web only: press CHECK (or say \"check WhatsApp\"). The WHATSAPP panel opens "
            "under the head with a QR code (CONTROLS → WHATSAPP shows or hides it any time).",
            "On your phone open WhatsApp → Settings (iPhone) or the ⋮ menu (Android) → Linked "
            "devices → Link a device, and scan the QR code.",
            "Wait until your chats appear in the panel. You can hide it with its ✕ — WhatsApp keeps "
            "running inside JARVIS. The link is remembered in config/whatsapp_web_embedded on this "
            "computer only.",
        ],
        "done": "Say \"send a WhatsApp to Anna saying I'm on my way\" or \"call Anna on WhatsApp\". "
                "It always asks you to confirm before it sends or dials.",
        "note": "Calling from WhatsApp Web depends on WhatsApp offering calls in the browser for "
                "your account; if the call button is missing there, calls need the WhatsApp desktop "
                "app (Windows or Mac). Messages work either way.",
    },

    # ── Cloud agent (your own server) ───────────────────────────────────────
    "cloud_agent": {
        "title": "Cloud agent — your own always-on server",
        "aliases": ["cloud", "server", "vps", "cloud agent", "always on", "pair code"],
        "time": "about 15 minutes",
        "need": "A small Linux server you control (any VPS) that you can reach with SSH, and the "
                "Telegram remote set up first if you want the phone to work while this computer is off.",
        "steps": [
            "In a terminal, in the JARVIS folder on this computer, run:  "
            "python -m core.cloud deploy root@YOUR_SERVER_IP",
            "It installs the cloud body on the server and prints a pairing code. (Lost it? On the "
            "server run: python3 -m core.cloud --pair-code)",
            "Paste the code into PAIRING CODE FROM YOUR SERVER. Choose what to share (Gemini key, "
            "memory, Telegram on the server). Press SAVE.",
            "Press CHECK CONNECTION.",
        ],
        "done": "Say \"cloud status\", or \"remind me every Monday at 9 to water the plants\" — "
                "repeating reminders reach your phone even when this computer is off.",
    },

    # ── Tuya / Smart Life light ─────────────────────────────────────────────
    "home_assistant": {
        "title": "Smart light (Tuya / Smart Life)",
        "aliases": ["light", "lamp", "bulb", "tuya", "smart life", "smart light", "home"],
        "time": "about 10 minutes",
        "need": "The bulb already working in the Smart Life (or Tuya Smart) app.",
        "steps": [
            ("Create a free account on the Tuya Developer Platform.", "https://platform.tuya.com"),
            "Cloud → Development → Create Cloud Project. Industry: Smart Home, Development "
            "Method: Smart Home, Data Center: the region of your Smart Life account.",
            "In the project open Devices → Link App Account → Add App Account and scan the QR code "
            "with the Smart Life app (Me → scan icon at the top). Your bulb appears in the list.",
            "From the project's Overview copy the Access ID and Access Secret into CLOUD: ACCESS ID "
            "and CLOUD: ACCESS SECRET, and choose the same CLOUD: DATA CENTRE.",
            "From Devices copy the bulb's Device ID into DEVICE ID. Set CONNECTION = cloud. SAVE.",
            "Press CONNECT & TEST.",
            ("Optional (faster, works without internet): run  python -m tinytuya wizard  in the "
             "JARVIS folder with the same Access ID/Secret. It prints the bulb's IP and local key — "
             "put them in LOCAL: BULB IP ADDRESS and LOCAL: LOCAL KEY, set CONNECTION = local, "
             "version 3.3 (try 3.4/3.5 for newer bulbs).", "https://github.com/jasonacox/tinytuya"),
        ],
        "done": "Say \"turn the light warm white at 40%\" or \"set the light to movie mode\".",
        "note": "Tuya's free cloud plan has to be extended now and then on the platform "
                "(Cloud → Cloud Services). If the light stops answering in cloud mode, check that first.",
    },

    # ── 3D printer ───────────────────────────────────────────────────────────
    "printer_control": {
        "title": "3D printer (Bambu Lab, OctoPrint, Klipper/Moonraker)",
        "aliases": ["printer", "3d printer", "bambu", "octoprint", "klipper", "moonraker", "print"],
        "time": "about 3 minutes",
        "need": "The printer on the same network as this computer.",
        "steps": [
            "Choose PRINTER TYPE.",
            "Bambu Lab: on the printer's screen open Settings → Network/WLAN: copy the IP address "
            "into IP ADDRESS / HOST and the Access Code into BAMBU: LAN ACCESS CODE. The serial "
            "number is under Settings → Device (or in Bambu Studio → Device) → BAMBU: SERIAL NUMBER. "
            "On recent firmware also switch on LAN Only Mode and Developer Mode so other apps may "
            "control it.",
            "OctoPrint: put the OctoPrint address in IP ADDRESS / HOST. In OctoPrint open Settings "
            "(wrench) → Application Keys (or API) → generate a key → OCTOPRINT / MOONRAKER: API KEY.",
            "Klipper / Moonraker: the address in IP ADDRESS / HOST, port 7125. An API key only if "
            "you set one up in moonraker.conf.",
            "Press SAVE, then CONNECT & TEST.",
        ],
        "done": "Say \"how's my print going?\" or \"tell me when the print is done\".",
    },

    # ── Obsidian ─────────────────────────────────────────────────────────────
    "obsidian_vault": {
        "title": "Obsidian notes",
        "aliases": ["obsidian", "notes", "vault", "journal", "daily note"],
        "time": "under a minute",
        "steps": [
            "Leave VAULT FOLDER empty and press CHECK VAULT — it finds the vault Obsidian used last.",
            "Or paste the vault's folder: in Obsidian, open the vault switcher (bottom left) → "
            "Reveal vault in Finder / Show in system explorer, and copy that path.",
            "Optional: copy DAILY NOTES FOLDER and the name format from Obsidian → Settings → "
            "Daily notes. Press SAVE.",
        ],
        "done": "Say \"add to my daily note: call the bank\" or \"what did I write about the trip?\".",
    },

    # ── Connected apps (MCP) ─────────────────────────────────────────────────
    "connected_apps": {
        "title": "Connected apps (MCP servers: GitHub, Notion, files…)",
        "aliases": ["mcp", "connected apps", "github", "notion", "linear", "app", "integration"],
        "time": "a few minutes per app",
        "need": "The app's MCP server: either an address (remote server) or a command that "
                "starts it on this computer.",
        "steps": [
            ("Find the MCP server for the app (many apps publish one; there is a list of "
             "reference servers here).", "https://github.com/modelcontextprotocol/servers"),
            "Paste a block in the Claude Desktop format into MCP SERVERS, for example a local one: "
            '{"mcpServers": {"files": {"command": "npx", "args": ["-y", '
            '"@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]}}}',
            "A remote one with a token: "
            '{"mcpServers": {"myapp": {"url": "https://example.com/mcp", '
            '"headers": {"Authorization": "Bearer YOUR_TOKEN"}}}}',
            "Already use Claude Desktop? Put the path of its claude_desktop_config.json in "
            "…OR A PATH TO SUCH A JSON FILE instead.",
            "Press SAVE, then TEST SERVERS.",
        ],
        "done": "Say \"in GitHub, list my open pull requests\". Anything that changes something "
                "asks you first.",
        "note": "Servers that need a browser sign-in (OAuth) can't sign in by themselves here: use a "
                "token, or run them through  npx mcp-remote <url>  as the command.",
    },

    # ── Planet watch ─────────────────────────────────────────────────────────
    "planet_watch": {
        "title": "Planet watch (earthquakes, disasters, aurora, aircraft)",
        "aliases": ["planet", "earthquake", "earthquakes", "quake", "quakes", "aurora", "aircraft",
                    "planes", "flights overhead", "disaster", "disasters"],
        "time": "under a minute",
        "steps": [
            "Type your city (or coordinates like 48.85, 2.35) into YOUR LOCATION — needed for "
            "anything 'near me'. Press SAVE.",
            ("Optional, for more reliable live aircraft: create a free OpenSky account, then "
             "Account → API client, and paste the client id and secret.",
             "https://opensky-network.org"),
        ],
        "done": "Say \"any earthquakes near me this week?\" or \"can I see the aurora tonight?\".",
    },

    # ── Face lock ────────────────────────────────────────────────────────────
    "face_lock": {
        "title": "Face lock",
        "aliases": ["face", "face lock", "face recognition", "lock"],
        "time": "about 1 minute",
        "need": "A webcam and good light on your face.",
        "steps": [
            "Say \"set up face lock\" (or \"remember my face\") and look at the camera while it "
            "takes a few pictures. Only numbers describing your face are kept, on this computer.",
            "To lock at every start: type an UNLOCK PIN first (used if the camera can't see you), "
            "then switch on LOCK AT EVERY START. Press SAVE.",
            "While it scans or is locked, the middle of the screen shows the camera with a box "
            "round each face (green: you, red: someone else). Switch off SHOW THE CAMERA ON THE "
            "HUD to keep the avatar there instead.",
        ],
        "done": "Say \"lock\" — it unlocks by itself when it sees you, or with \"unlock\" and your PIN.",
    },

    # ── Live subtitles ───────────────────────────────────────────────────────
    "live_subtitles": {
        "title": "Live subtitles (hear what the computer plays)",
        "aliases": ["subtitles", "captions", "translate video", "blackhole", "loopback"],
        "time": "about 5 minutes on Mac, usually none on Windows/Linux",
        "steps": [
            ("Mac: install BlackHole 2ch (free), or run  brew install blackhole-2ch.",
             "https://existential.audio/blackhole/"),
            "Mac: open Audio MIDI Setup → + (bottom left) → Create Multi-Output Device, tick your "
            "speakers/headphones AND BlackHole 2ch, then choose that Multi-Output Device as the "
            "sound output in System Settings → Sound.",
            "Windows: nothing needed in most cases. If it says no source, enable 'Stereo Mix' in "
            "Sound settings → More sound settings → Recording.",
            "Linux: PulseAudio/PipeWire 'monitor' sources are found automatically.",
            "Leave SOUND SOURCE empty (automatic) or type part of the device's name, e.g. BlackHole.",
        ],
        "done": "Play a video and say \"subtitles in English\".",
    },

    # ── Screen agent / app control ──────────────────────────────────────────
    "screen_recording": {
        "title": "Screen Recording (let the assistant see your screen)",
        "aliases": ["screen recording", "enregistrement de l'écran", "enregistrement d'écran",
                    "enregistrement", "screenshot", "capture d'écran", "see my screen", "voir l'écran",
                    "screen capture", "recording permission", "screen permission"],
        "time": "about 1 minute",
        "need": "macOS. Without it every screenshot shows only the wallpaper and the assistant's own window.",
        "steps": [
            "System Settings → Privacy & Security → Screen & System Audio Recording (NOT Accessibility).",
            "Switch on the program that started the assistant — Terminal if you use run_jarvis.command. "
            "There is no entry called JARVIS. Missing? Press + and add it.",
            "Quit and restart the assistant: macOS applies this permission only to a new start.",
        ],
        "done": "Say \"what's on my screen?\" — it now sees every app, without its own window in the way.",
    },

    "screen_agent": {
        "title": "Screen agent (clicks and types for you)",
        "aliases": ["screen agent", "click", "mouse", "keyboard", "accessibility", "accessibilité"],
        "time": "about 1 minute",
        "steps": [
            "Mac: System Settings → Privacy & Security → Accessibility: switch on the app that runs "
            "the assistant (Terminal, PyCharm or Python).",
            "Mac: in the same place, Screen & System Audio Recording: switch it on too, then restart "
            "the assistant.",
            "Press CHECK WHAT I CAN SEE, switch to any window within 5 seconds: it reports what it "
            "can see and clicks nothing.",
        ],
        "done": "Say \"in Spotify, play my liked songs\". Paying, sending and deleting always ask first.",
    },
}

# Guides for things that are not plugin settings sections.
EXTRA_GUIDES: dict[str, dict] = {
    "gemini": {
        "title": "Gemini API key — and 'out of quota'",
        "aliases": ["gemini", "api key", "key", "quota", "out of quota", "model", "provider",
                    "openrouter", "groq", "ollama", "billing"],
        "time": "2–5 minutes",
        "steps": [
            ("Get a key in Google AI Studio (Create API key) and paste it in the assistant's first "
             "setup screen.", "https://aistudio.google.com/apikey"),
            "'Out of quota' means the free daily allowance for that key is used up. Talking keeps "
            "working (it uses a separate voice model), but features that look at pictures, write "
            "quizzes, read documents or summarise videos stop until it resets.",
            "Fix 1: in AI Studio, set up billing for the key's project — then the limits are much higher.",
            ("Fix 2 (free): add a second provider in SETUP → 🔌 ENGINE & PROVIDERS, e.g. an "
             "OpenRouter key…", "https://openrouter.ai/keys"),
            ("…or a Groq key…", "https://console.groq.com/keys"),
            ("…or Ollama running on this computer (no key, fully local).", "https://ollama.com/download"),
        ],
        "done": "Ask for something that needs a model, e.g. \"make me a 5-question quiz on the solar system\".",
    },
    "remote_mode": {
        "title": "Remote Mode — keep awake, hibernate, Wake-on-LAN",
        "aliases": ["remote mode", "wake on lan", "wol", "wake", "hibernate", "keep awake",
                    "switch on", "turn on computer"],
        "time": "about 10 minutes for Wake-on-LAN",
        "steps": [
            "Say \"keep the computer awake while I'm away\" — it stops sleeping until you say otherwise.",
            "Say \"show me how to wake this computer\" — it tells you this computer's MAC address.",
            "Wake-on-LAN needs a network CABLE (not Wi-Fi) and 'Wake on LAN' switched on in the "
            "computer's BIOS/UEFI and in the network adapter's settings (Windows: Device Manager → "
            "adapter → Power Management / Advanced).",
            "From outside your home the router must forward a UDP port (e.g. 9) to this computer, or "
            "use a smart plug / Home Assistant URL instead — fill those in CLOUD AGENT's Wake-on-LAN "
            "fields so the server can wake it.",
        ],
        "done": "From the phone: /awake on, or ask the bot to hibernate the computer (you confirm it twice).",
    },
}


# ── lookups ──────────────────────────────────────────────────────────────────

def for_section(namespace: str, plugin_guide: Optional[dict] = None) -> Optional[dict]:
    """The guide for a settings section: the plugin's own, else the built-in one."""
    if isinstance(plugin_guide, dict) and plugin_guide.get("steps"):
        return plugin_guide
    return GUIDES.get(namespace)


def all_guides() -> dict[str, dict]:
    return {**GUIDES, **EXTRA_GUIDES}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def find(query: str) -> tuple[Optional[str], Optional[dict]]:
    """Best guide for what the user said, by aliases and title words."""
    q = (query or "").lower().strip()
    if not q:
        return None, None
    qw = _words(q)
    best, best_key, best_score = None, None, 0.0
    for key, g in all_guides().items():
        score = 0.0
        if q == key or q.replace(" ", "_") == key:
            score += 10
        for alias in g.get("aliases", []):
            a = alias.lower()
            if " " in a and a in q:
                score += 4
            elif a in qw:
                score += 3
        score += 0.5 * len(qw & _words(g.get("title", "")))
        if score > best_score:
            best, best_key, best_score = g, key, score
    return (best_key, best) if best_score >= 1.5 else (None, None)


def as_text(g: dict) -> str:
    lines = [g.get("title", "Setup")]
    if g.get("time"):
        lines.append(f"Takes {g['time']}.")
    if g.get("need"):
        lines.append(f"You need: {g['need']}")
    for i, step in enumerate(g.get("steps", []), 1):
        if isinstance(step, (tuple, list)):
            text, url = step[0], step[1] if len(step) > 1 else ""
            lines.append(f"{i}. {text}" + (f"  → {url}" if url else ""))
        else:
            lines.append(f"{i}. {step}")
    if g.get("done"):
        lines.append(f"Try it: {g['done']}")
    if g.get("note"):
        lines.append(f"Good to know: {g['note']}")
    return "\n".join(lines)


def as_html(g: dict, link_color: str = "#00d4ff", dim_color: str = "#7fa9b8") -> str:
    """Rich text for a QLabel (links open in the browser)."""
    e = html.escape
    out = []   # the time is already on the HOW TO SET UP button
    if g.get("need"):
        out.append(f"<div><b>You need:</b> {e(g['need'])}</div>")
    out.append("<ol style='margin-left:-18px'>")
    for step in g.get("steps", []):
        if isinstance(step, (tuple, list)):
            text, url = step[0], step[1] if len(step) > 1 else ""
            link = (f" <a href='{e(url, quote=True)}' style='color:{link_color}'>open ↗</a>"
                    if url else "")
            out.append(f"<li style='margin-bottom:4px'>{e(text)}{link}</li>")
        else:
            out.append(f"<li style='margin-bottom:4px'>{e(step)}</li>")
    out.append("</ol>")
    if g.get("done"):
        out.append(f"<div><b>Try it:</b> {e(g['done'])}</div>")
    if g.get("note"):
        out.append(f"<div style='color:{dim_color}'><b>Good to know:</b> {e(g['note'])}</div>")
    return "".join(out)
