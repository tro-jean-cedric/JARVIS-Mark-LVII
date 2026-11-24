"""
actions/setup_guide.py — "how do I set up Telegram / Gmail / WhatsApp…?"

Finds the step-by-step guide in core/setup_guides.py for what the user wants to
connect, puts the full steps (with links) in the content panel under the head,
and returns a short summary for the assistant to say. The same guides appear
under "❓ HOW TO SET UP" in ⚙ PLUGIN SETTINGS.
"""
from __future__ import annotations

from core import setup_guides


def _first_step(g: dict) -> str:
    steps = g.get("steps") or []
    if not steps:
        return ""
    s = steps[0]
    return s[0] if isinstance(s, (tuple, list)) else str(s)


def _live_status(key: str) -> str:
    """What this machine actually uses, when that makes the guide moot.

    The WhatsApp guide covers two routes. With the desktop app installed the
    QR-code steps do not apply, but read as a list they sent the model (and the
    user) hunting for a QR code that never appears. Cheap: names the transport,
    never touches the WhatsApp window."""
    if key != "whatsapp_call":
        return ""
    try:
        from plugins import _whatsapp_core as wa
        transport, _why = wa.get()
    except Exception:
        return ""
    if transport is None or "desktop" not in transport.label.lower():
        return ""
    return (f"status=already_set_up via=\"{transport.label}\" -- WhatsApp is ALREADY usable: the "
            "signed-in desktop app on this computer is driven directly. There is NO QR code to "
            "scan and nothing to link or configure; ignore the WhatsApp Web steps. Tell the user "
            "that, and that they can simply ask to send a message or call someone. To test it, "
            "call whatsapp_call with action='check'.")


def setup_guide(parameters: dict, player=None) -> str:
    try:
        topic = str((parameters or {}).get("topic") or "").strip()
        key, guide = setup_guides.find(topic)
        if guide is None:
            names = sorted({g["title"].split(" —")[0].split(" (")[0]
                            for g in setup_guides.all_guides().values()})
            listing = "; ".join(names)
            if player is not None:
                try:
                    player.show_content("SETUP GUIDES",
                                        "Ask \"how do I set up …?\" for any of these:\n\n- "
                                        + "\n- ".join(names))
                except Exception:
                    pass
            return (f"status=no_match topic=\"{topic}\" available=\"{listing}\" -- ask which one "
                    "they mean, or offer the list now on screen.")

        text = setup_guides.as_text(guide)
        if player is not None:
            try:
                player.show_content(f"HOW TO SET UP · {guide['title'].upper()}", text)
            except Exception:
                pass
        where = ("in ⚙ PLUGIN SETTINGS under " + guide["title"].split(" —")[0]
                 if key in setup_guides.GUIDES else "by voice and in SETUP")
        live = _live_status(key)
        if live:
            return live
        return (f"status=shown guide=\"{guide['title']}\" steps={len(guide.get('steps', []))} "
                f"time=\"{guide.get('time', '')}\" first_step=\"{_first_step(guide)}\" "
                f"settings_location=\"{where}\" -- The full steps with links are on screen now "
                "(the same guide is under ❓ HOW TO SET UP in plugin settings). Tell the user, "
                "briefly, how long it takes and the first step; offer to go through it with them "
                "step by step. Do not read every step out.")
    except Exception as e:
        return f"status=failed reason=\"{e}\""


TOOL = {
    "name": "setup_guide",
    "description": (
        "Shows step-by-step instructions for connecting or setting up an account, device or "
        "feature, including where to get each value (tokens, keys, IDs, QR codes) with links: "
        "Telegram bot, Gmail / Google Calendar / subscriptions (Google sign-in), WhatsApp, the "
        "cloud server, Tuya/Smart Life lights, 3D printers, Obsidian, connected apps (MCP), "
        "planet watch, face lock, live subtitles (BlackHole), screen-agent permissions, the Gemini "
        "API key and 'out of quota', Remote Mode / Wake-on-LAN. Use it for 'how do I connect / "
        "link / set up X', 'where do I get the token / client id / key', 'help me configure X'. "
        "Not for using a feature that is already set up."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {"type": "STRING",
                      "description": "What they want to set up, in their words (e.g. 'telegram', "
                                     "'gmail', 'whatsapp', 'smart light', 'api key')."},
        },
        "required": ["topic"],
    },
    "handler": setup_guide,
}
