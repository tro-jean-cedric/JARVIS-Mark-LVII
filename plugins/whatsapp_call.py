"""
plugins/whatsapp_call.py — "call Mom on WhatsApp", "video call Ali", "hang up".

Places a WhatsApp voice or video call by driving the WhatsApp desktop app
(Windows, macOS) or WhatsApp Web (Linux, no desktop app, or by choice) through
plugins/_whatsapp_core.py, and ends a call in progress.

Ringing somebody cannot be taken back, so placing a call goes through
core/confirm: the HUD shows CONFIRM / CANCEL and nothing is dialled until the
user presses CONFIRM. Then, on a worker thread:

    1. open the conversation with that name;
    2. READ BACK the conversation that opened — if it is somebody else's, do
       not dial; say so and let the user decide (confirm=true afterwards);
    3. press the voice- or video-call button, found by its label.

The tool answers at once (outcome.ask / the confirmation sentence) and the
ending is reported through the voice channel — including when it failed.
"""
from __future__ import annotations

import threading

PLUGIN = {
    "name": "whatsapp_call",
    "description": (
        "Places a WHATSAPP CALL to one of the user's contacts, or ends the call in progress. Use when "
        "the user wants to call, ring, phone or video call somebody — 'call Yusuf on WhatsApp', 'ring "
        "mum', 'video call my brother', 'hang up', 'end the call'. Also READS a WhatsApp chat: 'read my "
        "conversation with Ali', 'what did she write?' -> action='read' (never Gmail for WhatsApp). Put the name as saved in their phone "
        "into 'contact', without possessives ('call my dude' -> contact 'dude'). call_type defaults to "
        "'voice'. Do NOT use send_message for this — that only types a text. If this tool reports that "
        "a different conversation opened than the one asked for, do not retry silently: ask the user, "
        "and call again with confirm=true only once they clearly agreed to call that person."
    ),
    "brief": "WhatsApp voice/video call to a contact, or hang up.",
    "tier": "extra",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "contact": {"type": "STRING",
                        "description": "Who to call, as saved in WhatsApp; a phone number works too. "
                                       "Not needed for action='hangup'."},
            "call_type": {"type": "STRING", "enum": ["voice", "video"],
                          "description": "'voice' (default) or 'video'."},
            "action": {"type": "STRING", "enum": ["call", "hangup", "check", "read"],
                       "description": "'call' (default) rings the contact; 'hangup' ends the call in "
                                      "progress; 'check' tests that WhatsApp can be reached (the setup "
                                      "guide's CHECK) and rings nobody; 'read' returns the latest "
                                      "messages of a chat (contact given) or of the chat open now."},
            "limit": {"type": "INTEGER", "description": "For read: how many recent messages (default 12)."},
            "confirm": {"type": "BOOLEAN",
                        "description": "Only true after the tool reported a different contact than asked "
                                       "for AND the user agreed to call that person."},
        },
        "required": [],
    },
}


def _check(values: dict):
    """The settings screen's CHECK button."""
    try:
        from plugins import _whatsapp_core as wa
        transport, why = wa.get()
        if transport is None:
            return False, why
        ok, msg = transport.diagnose()
        return ok, f"{transport.label}: {msg}"
    except Exception as e:
        return False, str(e)


PLUGIN_SETTINGS = {
    "namespace": "whatsapp_call",
    "title": "WHATSAPP CALLS",
    "fields": [
        {"key": "transport", "label": "How to reach WhatsApp", "type": "choice",
         "options": ["auto", "desktop", "web"], "default": "auto"},
        {"key": "strict_match", "label": "Refuse to dial if the wrong chat opened", "type": "toggle",
         "default": True},
        {"key": "launch_wait", "label": "Seconds to wait after launching WhatsApp", "type": "text",
         "default": "8"},
        {"key": "chat_wait", "label": "Seconds to wait for the chat to open", "type": "text", "default": "6"},
    ],
    "action": {"label": "CHECK", "run": _check},
}

_busy = threading.Lock()


def _say(text: str) -> None:
    try:
        from plugins import _announce
        _announce.say(text)
    except Exception:
        print(f"[WhatsAppCall] {text}")


def _outcome():
    from core import outcome
    return outcome


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        return _run(parameters or {}, player)
    except Exception as e:
        return _outcome().failed("whatsapp_call", f"{type(e).__name__}: {e}")


def _run(p: dict, player) -> str:
    out = _outcome()
    try:
        from plugins import _announce
        _announce.remember(player)
    except Exception:
        pass
    try:
        from plugins import _whatsapp_core as wa
    except Exception as e:
        return out.failed("whatsapp_call", f"the WhatsApp driver could not load ({e})")

    action = str(p.get("action") or "call").strip().lower()
    if action == "check":
        # The settings screen's CHECK, by voice: the setup guide tells the user
        # to press it, and without this the model went looking for a "CHECK"
        # button inside WhatsApp. Bounded by the driver's own waits (~4 s).
        ok, msg = _check({})
        return (out.done("whatsapp_call", check=msg) if ok
                else out.failed("whatsapp_call", msg))
    if action == "hangup":
        threading.Thread(target=_hangup_worker, daemon=True, name="whatsapp-hangup").start()
        return out.started("whatsapp_call", action="hangup")
    if action == "read":
        # Reading changes nothing, so no confirmation. Without this the model
        # answered "read the conversation" with Gmail.
        transport, why = wa.get()
        if transport is None:
            return out.failed("whatsapp_call", f"I can't reach WhatsApp on this system: {why}")
        who = wa.clean_contact(p.get("contact") or "")
        if who:
            ok, title = transport.open_chat(who)
            if not ok:
                return out.failed("whatsapp_call", f"{who}'s chat could not be opened ({title})")
            if not wa.same_contact(who, title):
                return out.failed("whatsapp_call", f"the chat that opened is '{title}', not {who}'s")
        ok, title, msgs = transport.read_messages(int(p.get("limit") or 12))
        if not ok:
            return out.failed("whatsapp_call", title)
        body = f"Chat: {title}\n" + ("\n".join(f"- {m}" for m in msgs) or "(no messages on screen)")
        return out.untrusted("whatsapp_messages", body)

    contact = wa.clean_contact(p.get("contact") or "")
    if not contact:
        return out.ask("whatsapp_call", "Who should I call on WhatsApp?")
    kind = "video" if str(p.get("call_type") or "").lower().startswith("vid") else "voice"
    force = str(p.get("confirm")).lower() in ("true", "1", "yes")

    transport, why = wa.get()
    if transport is None:
        return out.failed("whatsapp_call", f"I can't reach WhatsApp on this system: {why}",
                          contact=contact)
    try:
        from core import confirm
    except Exception:
        return out.failed("whatsapp_call", "this version has no confirmation gate, so nothing is dialled")

    def dial() -> str:
        return _dial(transport, contact, kind, force)

    title = f"WhatsApp {kind} call to {contact}"
    return confirm.request("whatsapp_call", title,
                           f"Opens {contact}'s chat in {transport.label}, checks it is the right person, "
                           f"and rings them.", dial)


def _dial(transport, contact: str, kind: str, force: bool) -> str:
    """Runs on the confirmation gate's worker thread after CONFIRM."""
    from plugins import _whatsapp_core as wa

    if not _busy.acquire(blocking=False):
        _say("A WhatsApp call is already being placed; nothing new was dialled.")
        return "busy"
    try:
        ok, title = transport.open_chat(contact)
        if not ok:
            _say(f"Tell the user, in one short sentence in their own language: {contact}'s WhatsApp chat could "
                 f"not be opened ({title}), so {contact} was NOT called and nothing was dialled.")
            return f"not dialled: {title}"
        strict = bool(wa.settings().get("strict_match", True))
        if strict and not force and not wa.same_contact(contact, title):
            _say(f"Tell the user, in one short sentence in their own language: you were asked to call "
                 f"'{contact}' but the conversation that opened is '{title}', so you have NOT dialled. Ask "
                 f"whether to call '{title}' instead (if yes, call whatsapp_call again with contact "
                 f"'{title}' and confirm=true).")
            return f"not dialled: opened '{title}'"
        ok, detail = transport.call(kind)
        if not ok:
            _say(f"Tell the user, in one short sentence in their own language: the {kind}-call button in "
                 f"{title}'s chat could not be pressed ({detail}), so nothing was dialled.")
            return f"not dialled: {detail}"
        # Success needs no announcement: the call window is ringing in front of them.
        return f"{kind} call placed to {title}"
    except Exception as e:
        _say(f"Tell the user, in one short sentence in their own language: the WhatsApp call to {contact} "
             f"failed ({type(e).__name__}: {e}), so nothing was dialled.")
        return f"failed: {e}"
    finally:
        _busy.release()


def _hangup_worker() -> None:
    try:
        from plugins import _whatsapp_core as wa
        transport, why = wa.get()
        if transport is None:
            _say(f"Tell the user briefly in their language: I can't reach WhatsApp to hang up ({why}).")
            return
        ok, detail = transport.hangup()
        if not ok:
            _say("Tell the user briefly in their language: I couldn't find the hang-up button "
                 f"({detail}); they should end the call from the call window.")
    except Exception as e:
        _say(f"Tell the user briefly in their language: hanging up failed ({e}).")
