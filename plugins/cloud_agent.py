"""
plugins/cloud_agent.py — this computer's side of the assistant's cloud body.

The cloud body is core/cloud.py running on the user's OWN server (a small VPS):
the same assistant, same name, same memory, awake around the clock. It answers
the phone over Telegram while this computer is off, runs long tasks, delivers
reminders and the morning briefing. This plugin pairs with it, keeps the link
up, and carries things both ways:

    desk -> cloud   settings (name, key, Telegram bot, wake-on-LAN, briefing),
                    memory, the computer's presence ("I'm here")
    cloud -> desk   finished tasks, reminders, "while you were away", memory
                    the cloud learnt, and requests from the phone for this
                    computer (only when the user switched that on here)

The link is core.cloud.Client: JSON over TLS pinned to the certificate named in
the pairing code, plus a 256-bit secret. core/cloud.py is imported only inside
calls, so an install that never pastes a pairing code never loads it.

Telegram belongs to whichever body is awake. While this computer is connected
and has the bot configured, it asks the server to let go and runs
plugins/telegram_remote.py itself; on exit (atexit) it hands the bot back.

actions/reminder.py calls mirror_reminder(message, "YYYY-MM-DD HH:MM") so a
one-off reminder also reaches the phone when this computer is off.
"""
from __future__ import annotations

import atexit
import base64
import gc
import hashlib
import json
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

NS = "cloud_agent"
POLL_WAIT = 25

PLUGIN = {
    "name": "cloud_agent",
    "description": (
        "The assistant's cloud body: the same assistant on the user's OWN server, awake around the "
        "clock. delegate = hand it long work that should continue even if this computer is turned off "
        "(research, comparisons, reports); notify = 'desk' to announce the result here, 'phone' when "
        "the request came from the phone (a [Telegram …] message), 'both' when unsure. REMINDERS: a "
        "one-off reminder is set with the `reminder` tool (it reaches this computer AND the phone). Use "
        "remind here only for REPEATING reminders, which only the cloud can repeat: text = the exact "
        "words in the user's language, plus time 'HH:MM' with optional days (1=Mon … 7=Sun, e.g. "
        "'1,2,3,4,5'), or monthly_day + time, or every_minutes with optional between 'HH:MM-HH:MM'. "
        "reminders = list them; cancel_reminder = by id. briefing = show or change the phone briefing "
        "(pass only what the user wants changed); briefing_now = send one now. jobs / result = the "
        "cloud's work. status, connect, disconnect, sync. export_data = save what the cloud knows into a "
        "file here. delete_cloud = wipe the cloud — only when the user explicitly asks."
    ),
    "brief": "Long tasks, repeating reminders and the phone briefing on the user's own always-on server.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": [
                "status", "connect", "disconnect", "sync", "delegate", "jobs", "result", "remind",
                "reminders", "cancel_reminder", "briefing", "briefing_now", "export_data",
                "delete_cloud"]},
            "task": {"type": "STRING", "description": "delegate: the whole task, self-contained"},
            "notify": {"type": "STRING", "enum": ["desk", "phone", "both"]},
            "job_id": {"type": "STRING", "description": "result: which job (empty = latest)"},
            "text": {"type": "STRING", "description": "remind: the reminder's exact words"},
            "at": {"type": "STRING", "description": "remind: YYYY-MM-DD HH:MM, local time (one-off)"},
            "time": {"type": "STRING", "description": "remind: HH:MM of a repeating reminder"},
            "days": {"type": "STRING", "description": "remind / briefing: days as numbers, 1=Monday … 7=Sunday, comma-separated"},
            "monthly_day": {"type": "INTEGER", "description": "remind: day of the month, 1-31"},
            "every_minutes": {"type": "INTEGER", "description": "remind: repeat every N minutes"},
            "between": {"type": "STRING", "description": "remind with every_minutes: 'HH:MM-HH:MM'"},
            "id": {"type": "STRING", "description": "cancel_reminder: the reminder's id"},
            "enabled": {"type": "BOOLEAN", "description": "briefing: on or off"},
            "times": {"type": "STRING", "description": "briefing: HH:MM times, comma-separated"},
            "city": {"type": "STRING", "description": "briefing: city for the weather"},
            "topics": {"type": "STRING", "description": "briefing: news topics, comma-separated"},
            "weather": {"type": "BOOLEAN"}, "reminders": {"type": "BOOLEAN"}, "tasks": {"type": "BOOLEAN"},
            "extra": {"type": "STRING", "description": "briefing: anything else, in the user's words"},
            "length": {"type": "STRING", "enum": ["short", "detailed"]},
            "language": {"type": "STRING", "description": "briefing: its language, empty = usual"},
        },
        "required": ["action"],
    },
}


def _test_connection(values: dict):
    """The settings screen's CHECK button: (ok, message)."""
    code = str((values or {}).get("pair_code") or "").strip()
    if not code:
        return False, "Paste the pairing code your server printed (python -m core.cloud --pair-code)."
    try:
        cloud = _cloud_mod()
        client = cloud.Client.from_code(code, pem=_store().get("pem", ""))
        hello = client.request("POST", "hello", {}, timeout=12)
        _remember_pem(client)
    except Exception as e:
        return False, str(e)
    bits = [f"Connected to {hello.get('name') or 'the assistant'} on {hello.get('host') or 'your server'}",
            f"server clock reads your time as {hello.get('user_time')}"]
    if not hello.get("gemini_key"):
        bits.append("no Gemini key on the server yet — turn on 'give the server this Gemini key'")
    bits.append(f"{hello.get('telegram_phones', 0)} phone(s) approved there")
    return True, "; ".join(bits) + "."


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "CLOUD AGENT (your own server)",
    "fields": [
        {"key": "pair_code", "label": "Pairing code from your server", "type": "password",
         "placeholder": "jarvis-cloud://host:7443/…#…"},
        {"key": "auto_connect", "label": "Connect automatically when the assistant starts", "type": "toggle",
         "default": True},
        {"key": "share_gemini_key", "label": "Give the server this Gemini API key (it needs one to think)",
         "type": "toggle", "default": True},
        {"key": "share_memory", "label": "Share memory with the cloud (both ways)", "type": "toggle",
         "default": True},
        {"key": "telegram_on_server", "label": "Telegram on the server while this computer is off "
         "(uses TELEGRAM REMOTE's bot)", "type": "toggle", "default": True},
        {"key": "accept_commands", "label": "Let the cloud hand my phone's requests to this computer",
         "type": "toggle", "default": False},
        {"key": "allow_screenshot", "label": "Let the cloud send screenshots of this computer to my phone",
         "type": "toggle", "default": False},
        {"key": "briefing_enabled", "label": "Phone briefing", "type": "toggle", "default": False},
        {"key": "briefing_times", "label": "Briefing times (HH:MM, comma-separated)", "type": "text",
         "default": "08:00"},
        {"key": "briefing_days", "label": "Briefing days (1=Mon … 7=Sun)", "type": "text",
         "default": "1,2,3,4,5,6,7"},
        {"key": "briefing_city", "label": "Briefing city for the weather", "type": "text",
         "placeholder": "empty = the city the assistant remembers"},
        {"key": "briefing_topics", "label": "Briefing news topics (comma-separated)", "type": "text"},
        {"key": "briefing_extra", "label": "Briefing: anything else to include", "type": "text"},
        {"key": "briefing_length", "label": "Briefing length", "type": "choice",
         "options": ["short", "detailed"], "default": "short"},
        {"key": "briefing_language", "label": "Briefing language", "type": "text",
         "placeholder": "empty = the language you use with the assistant"},
        {"key": "wake_host", "label": "Wake-on-LAN: your home's public IP or DDNS name", "type": "text",
         "placeholder": "router forwards a UDP port to this PC; empty = no wake"},
        {"key": "wake_port", "label": "Wake-on-LAN port (forwarded on the router)", "type": "text",
         "default": "9"},
        {"key": "wake_mac", "label": "This computer's MAC address", "type": "text",
         "placeholder": "empty = detected automatically"},
        {"key": "wake_url", "label": "OR a URL that wakes this computer (smart plug, Home Assistant…)",
         "type": "text"},
    ],
    "action": {"label": "CHECK CONNECTION", "run": _test_connection},
}

_DEFAULTS = {f["key"]: f.get("default", "") for f in PLUGIN_SETTINGS["fields"]}
_BRIEF_KEYS = ("enabled", "times", "days", "city", "topics", "extra", "length", "language")

_link: dict = {"client": None, "thread": None, "online": False, "last_contact": 0.0, "error": "",
               "holding": False, "server": {}, "connecting": False}
_stop = threading.Event()
_lock = threading.RLock()
_player = None
_captures: list = []           # pending "command" replies: {"event", "lines"}
_mirrored: list = []           # (text, ts) one-off reminders also set on the server
_wrapped: set = set()


# ═════════════════════════════════════════════════════════════════════════════
#  config and storage
# ═════════════════════════════════════════════════════════════════════════════

def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in stored.items() if v is not None})
    return out


def _on(cfg: dict, key: str) -> bool:
    v = cfg.get(key)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _store() -> dict:
    try:
        from plugins import _announce
        doc = _announce.load("cloud_agent", {}) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _store_update(**fields) -> None:
    try:
        from plugins import _announce
        doc = _store()
        doc.update(fields)
        _announce.save("cloud_agent", doc)
    except Exception as e:
        print(f"[CloudAgent] could not save link state: {e}")


def _cloud_mod():
    try:
        from core import cloud
        return cloud
    except ImportError:
        raise RuntimeError("core/cloud.py is missing from this install — it ships with the app. "
                           "Update the app; there is nothing to pip install.") from None


def _remember_pem(client) -> None:
    pem = getattr(client, "pem", "") or ""
    if pem and pem != _store().get("pem"):
        _store_update(pem=pem)


def _log(text: str) -> None:
    p = _player
    try:
        if p is not None:
            p.write_log(text)
            return
    except Exception:
        pass
    print(text)


def _say(instruction: str) -> None:
    try:
        from plugins import _announce
        _announce.say(instruction, prefix="")
    except Exception:
        _log(instruction)


def _show(title: str, text: str) -> None:
    p = _player
    if p is not None and hasattr(p, "show_content"):
        try:
            p.show_content(title, text)
        except Exception:
            pass


def _assistant_name() -> str:
    try:
        from memory.config_manager import get_assistant_name
        return (get_assistant_name() or "JARVIS").strip()
    except Exception:
        return "JARVIS"


def _tz_offset_min() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


# ═════════════════════════════════════════════════════════════════════════════
#  the interface other modules use
# ═════════════════════════════════════════════════════════════════════════════

def linked() -> bool:
    """Paired and the link thread is running (telegram_remote asks this)."""
    t = _link.get("thread")
    return bool(_link.get("client") and t is not None and t.is_alive() and not _stop.is_set())


def will_hold_telegram() -> bool:
    """True when this plugin is about to start the Telegram bridge itself."""
    cfg = _cfg()
    return bool(str(cfg.get("pair_code") or "").strip() and _on(cfg, "auto_connect") and _telegram_token())


def _client():
    c = _link.get("client")
    if c is None:
        code = str(_cfg().get("pair_code") or "").strip()
        if not code:
            raise RuntimeError("The cloud is not set up yet: paste your server's pairing code in "
                               "⚙ → CLOUD AGENT.")
        c = _cloud_mod().Client.from_code(code, pem=_store().get("pem", ""))
        _link["client"] = c
    return c


def _req(route: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    c = _client()
    out = c.request("POST", route, body or {}, timeout=timeout)
    _remember_pem(c)
    _link["last_contact"] = time.time()
    return out


def mirror_reminder(message: str, when: str):
    """Set the same one-off reminder on the server, so the phone gets it even if
    this computer is off. `when` is local 'YYYY-MM-DD HH:MM'. -> (ok, detail)."""
    try:
        message = str(message or "").strip()
        if not message:
            return False, "no reminder text"
        datetime.strptime(str(when).strip(), "%Y-%m-%d %H:%M")
        res = _req("schedule", {"action": "add", "reminder": message, "at": str(when).strip(),
                                "tz_offset_min": _tz_offset_min()}, timeout=12)
        result = str(res.get("result") or res.get("error") or "")
        if "status=done" not in result:
            return False, _short_reason(result) or "the server did not accept it"
        _mirrored.append((message, datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").timestamp()))
        del _mirrored[:-50]
        return True, result
    except Exception as e:
        return False, str(e)[:200]


def claim_telegram():
    """Ask the server to stop polling the bot so this computer can. -> (ok, why)."""
    try:
        res = _req("telegram", {"hold": True}, timeout=55)
    except Exception as e:
        return False, str(e)
    if res.get("released"):
        _link["holding"] = True
        return True, ""
    _link["holding"] = False
    return False, "the server did not stop its Telegram poller in time"


def release_telegram() -> None:
    """Give the bot back to the server (it starts polling again)."""
    if not _link.get("holding"):
        return
    _link["holding"] = False
    try:
        _req("telegram", {"hold": False}, timeout=10)
    except Exception as e:
        print(f"[CloudAgent] could not hand Telegram back: {e}")


def sync_now() -> str:
    return _sync()


# ═════════════════════════════════════════════════════════════════════════════
#  sync
# ═════════════════════════════════════════════════════════════════════════════

def _telegram_cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config("telegram_remote")
    except Exception:
        return {}


def _telegram_token() -> str:
    return "".join(str(_telegram_cfg().get("bot_token") or "").split())


def _briefing_local(cfg: dict) -> dict:
    return {"enabled": _on(cfg, "briefing_enabled"),
            "times": str(cfg.get("briefing_times") or "08:00"),
            "days": [int(d) for d in str(cfg.get("briefing_days") or "").replace(" ", "").split(",")
                     if d.isdigit() and 1 <= int(d) <= 7] or [1, 2, 3, 4, 5, 6, 7],
            "city": str(cfg.get("briefing_city") or ""),
            "topics": str(cfg.get("briefing_topics") or ""),
            "extra": str(cfg.get("briefing_extra") or ""),
            "length": str(cfg.get("briefing_length") or "short"),
            "language": str(cfg.get("briefing_language") or "")}


def _hash(doc) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _mac() -> str:
    node = uuid.getnode()
    if (node >> 40) & 1:            # a random number, not a real address
        return ""
    return ":".join(f"{(node >> s) & 0xff:02X}" for s in range(40, -1, -8))


def _sync() -> str:
    cfg = _cfg()
    settings: dict = {"assistant_name": _assistant_name(), "tz_offset_min": _tz_offset_min()}
    try:
        from memory.config_manager import get_user_name
        settings["user_name"] = get_user_name()
    except Exception:
        pass
    if _on(cfg, "share_gemini_key"):
        try:
            from core import gemini
            key = gemini.api_key()
            if key:
                settings["gemini_api_key"] = key
        except Exception:
            pass
    tg = _telegram_cfg()
    if _on(cfg, "telegram_on_server") and _telegram_token():
        settings["telegram"] = {"bot_token": _telegram_token(),
                                "relay_replies": tg.get("relay_replies", True),
                                "voice_replies": bool(tg.get("voice_replies", False)),
                                "allowed_chat_ids": str(tg.get("allowed_chat_ids") or ""),
                                "pairing_code": str(tg.get("pairing_code") or "")}
    elif not _on(cfg, "telegram_on_server"):
        settings["telegram"] = {"disabled": True}
    settings["wake"] = {"wake_mac": str(cfg.get("wake_mac") or "").strip() or _mac(),
                        "wake_host": str(cfg.get("wake_host") or "").strip(),
                        "wake_port": str(cfg.get("wake_port") or "9").strip(),
                        "wake_url": str(cfg.get("wake_url") or "").strip()}
    # The briefing goes up only when it was changed HERE since the last sync;
    # otherwise the server's copy (perhaps changed from the phone) stands.
    local = _briefing_local(cfg)
    st = _store()
    if _hash(local) != st.get("brief_hash"):
        settings["briefing"] = dict(local, updated=time.time())
    body: dict = {"settings": settings, "memory_seq": int(st.get("memory_seq") or 0)}
    if _on(cfg, "share_memory"):
        try:
            from memory.memory_manager import load_memory
            body["memory"] = load_memory()
        except Exception as e:
            print(f"[CloudAgent] memory not shared: {e}")
    res = _req("sync", body, timeout=30)
    _store_update(brief_hash=_hash(local))
    # Phones paired on the server are approved here too.
    ids = str((res.get("telegram") or {}).get("allowed_chat_ids") or "")
    if ids:
        try:
            from memory.config_manager import save_plugin_config
            cur = {x.strip() for x in str(tg.get("allowed_chat_ids") or "").split(",") if x.strip()}
            new = {x.strip() for x in ids.split(",") if x.strip()}
            if new - cur:
                save_plugin_config("telegram_remote", {"allowed_chat_ids": ",".join(sorted(cur | new))})
        except Exception as e:
            print(f"[CloudAgent] approved phones not merged: {e}")
    _apply_server_briefing(res.get("briefing"))
    _link["server"] = res.get("hello") or _link.get("server") or {}
    hello = _link["server"]
    note = ""
    if hello and not hello.get("gemini_key"):
        note = " The server has no Gemini key — turn on 'give the server this Gemini key'."
    return f"Synced with the cloud.{note}"


def _apply_server_briefing(b) -> None:
    """A briefing changed from the phone shows up in these settings."""
    if not isinstance(b, dict):
        return
    try:
        from memory.config_manager import save_plugin_config
        values = {"briefing_enabled": bool(b.get("enabled")),
                  "briefing_times": ",".join(b.get("times") or ["08:00"]),
                  "briefing_days": ",".join(map(str, b.get("days") or [])),
                  "briefing_city": b.get("city") or "",
                  "briefing_topics": ",".join(b.get("topics") or []),
                  "briefing_extra": b.get("extra") or "",
                  "briefing_length": b.get("length") or "short",
                  "briefing_language": b.get("language") or ""}
        cur = _cfg()
        if any(str(cur.get(k)) != str(v) for k, v in values.items()):
            save_plugin_config(NS, values)
        _store_update(brief_hash=_hash(_briefing_local(_cfg())))
    except Exception as e:
        print(f"[CloudAgent] briefing not merged: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  the link loop
# ═════════════════════════════════════════════════════════════════════════════

def _telegram_mod():
    mod = sys.modules.get("plugins.telegram_remote")
    if mod is None:
        try:
            from plugins import telegram_remote as mod  # noqa: F811
        except Exception:
            return None
    return mod


def _desk_telegram_running() -> bool:
    tr = sys.modules.get("plugins.telegram_remote")
    return bool(tr and tr._state.get("running"))


def _desk_telegram_starting() -> bool:
    tr = sys.modules.get("plugins.telegram_remote")
    return bool(tr and tr._state.get("starting"))


def holding() -> bool:
    """True while the server has let go of the bot for this computer."""
    return bool(_link.get("holding"))


def _info() -> dict:
    cfg = _cfg()
    return {"accept_commands": _on(cfg, "accept_commands"),
            "screenshot": _on(cfg, "allow_screenshot"),
            "holds_telegram": bool(_link.get("holding") and (_desk_telegram_running() or _desk_telegram_starting())),
            "host": __import__("platform").node(), "app": "jarvis"}


def _take_telegram() -> None:
    """While connected, this computer answers the bot."""
    tr = _telegram_mod()
    if tr is None or not _telegram_token() or _player is None:
        return
    if tr._state.get("running"):
        claim_telegram()             # the bridge already runs here: the server must not poll too
        return
    if not callable(getattr(_player, "on_text_command", None)):
        return
    ok, why = claim_telegram()
    if not ok:
        print(f"[CloudAgent] the server has not released Telegram yet ({why}); it keeps answering.")
        return
    said = tr.run({"action": "start"}, player=_player)
    print(f"[CloudAgent] desk Telegram: {said}")
    if not tr._state.get("running") and not tr._state.get("starting"):
        release_telegram()


def _loop() -> None:
    cloud = _cloud_mod()
    backoff, said_online = 3.0, False
    first = True
    while not _stop.is_set():
        try:
            if first or not _link.get("online"):
                hello = _req("hello", {}, timeout=15)
                _link["server"] = hello
                _sync()
                first = False
            if not _link.get("online"):
                _link.update(online=True, error="")
                if not said_online:
                    _log("SYS: Cloud link up — your phone is answered even while this computer is off.")
                    said_online = True
                # Connected (again): this computer answers the bot while it is here.
                _take_telegram()
            st = _store()
            res = _req("poll", {"info": _info(), "event_seq": int(st.get("event_seq") or 0),
                                "memory_seq": int(st.get("memory_seq") or 0), "wait": POLL_WAIT},
                       timeout=POLL_WAIT + 20)
            backoff = 3.0
            _handle_poll(res)
        except Exception as e:
            status = getattr(e, "status", 0)
            _link.update(online=False, error=str(e)[:300])
            if isinstance(e, getattr(cloud, "LinkError", ())) and status == 401:
                _log(f"SYS: Cloud link stopped — {e} Paste the current pairing code in ⚙ → CLOUD AGENT.")
                break
            print(f"[CloudAgent] link: {e}")
            if _stop.wait(backoff):
                break
            backoff = min(backoff * 2, 120.0)
    _link["online"] = False


def _handle_poll(res: dict) -> None:
    for cmd in res.get("commands") or []:
        threading.Thread(target=_run_command, args=(cmd,), daemon=True, name="cloud-command").start()
    events = res.get("events") or []
    for ev in events:
        try:
            _on_event(ev)
        except Exception as e:
            print(f"[CloudAgent] event {ev.get('kind')} failed: {e}")
    if events or res.get("event_seq") is not None:
        _store_update(event_seq=int(res.get("event_seq") or 0))
    deltas = res.get("deltas") or []
    if deltas and _on(_cfg(), "share_memory"):
        _apply_deltas(deltas)
    if res.get("memory_seq") is not None:
        _store_update(memory_seq=int(res.get("memory_seq") or 0))


def _on_event(ev: dict) -> None:
    kind = ev.get("kind")
    if kind == "job_done":
        task, status, result = str(ev.get("task") or ""), ev.get("status"), str(ev.get("result") or "")
        _show("CLOUD RESULT", f"{task}\n[{status}]\n\n{result}")
        _say(f"[CLOUD RESULT] The cloud body finished a task the user gave it: '{task[:200]}' "
             f"(status {status}). Tell the user in one or two sentences what came of it; the full text is "
             f"on the screen. Say it in the language the user speaks with you. Do not call any tools.")
    elif kind == "reminder":
        text = str(ev.get("text") or "")
        if _was_mirrored(text):
            return                          # this computer's own reminder says it already
        _say(f"[REMINDER] A reminder the user set is due now: {text}\nSay it to them now, in one short "
             f"sentence, in the language it is written in. Do not call any tools.")
    elif kind == "away":
        entries = ev.get("entries") or []
        since = datetime.fromtimestamp(float(ev.get("since") or time.time())).strftime("%d.%m %H:%M")
        lines = []
        for e in entries[-40:]:
            at = datetime.fromtimestamp(float(e.get("at") or 0)).strftime("%d.%m %H:%M")
            lines.append(f"{at}  {e.get('kind')}: {str(e.get('text') or '')[:200]}")
        _show("WHILE YOU WERE AWAY", f"Since {since}\n\n" + "\n".join(lines))
        if entries:
            _say(f"[WHILE YOU WERE AWAY] This computer was off or disconnected since {since}, and the cloud "
                 f"body kept working; the list is on the screen. Welcome the user back in one short "
                 f"sentence, then give the single most useful item (a finished task's headline, a reminder "
                 f"they may have missed) in one or two sentences, and offer details. Do not read the list "
                 f"out. Use the user's language. Do not call any tools.")


def _was_mirrored(text: str) -> bool:
    now = time.time()
    for msg, ts in list(_mirrored):
        if msg == text and abs(now - ts) < 15 * 60:
            _mirrored.remove((msg, ts))
            return True
    return False


def _apply_deltas(deltas: list) -> None:
    try:
        from memory.memory_manager import forget, load_memory, save_memory, update_memory
    except Exception as e:
        print(f"[CloudAgent] memory delta skipped: {e}")
        return
    for d in deltas:
        try:
            op, cat, key = d.get("op"), d.get("category") or "notes", d.get("key") or ""
            if op == "set":
                update_memory({cat: {key: {"value": d.get("value") or ""}}})
            elif op == "delete":
                forget(key, cat)
            elif op in ("monitor_add", "monitor_remove", "monitor_state"):
                mem = load_memory()
                mons = mem.setdefault("monitors", {})
                if op == "monitor_add":
                    mons.setdefault(key, {"topic": d.get("value") or key, "added": d.get("updated", ""),
                                          "last_check": "", "last_hash": ""})
                elif op == "monitor_remove":
                    mons.pop(key, None)
                elif key in mons:
                    data = d.get("data") or {}
                    mons[key].update(last_check=data.get("last_check", ""), last_hash=data.get("last_hash", ""))
                save_memory(mem)
        except Exception as e:
            print(f"[CloudAgent] memory delta skipped: {e}")


# ── requests from the phone, for this computer ───────────────────────────────

def _output(cmd_id: str, **payload) -> None:
    try:
        _req("output", {"cmd_id": cmd_id, "done": True, **payload}, timeout=60)
    except Exception as e:
        print(f"[CloudAgent] could not return output: {e}")


def _run_command(cmd: dict) -> None:
    cfg, cid, kind = _cfg(), str(cmd.get("id") or ""), cmd.get("kind")
    try:
        if kind == "screenshot":
            if not _on(cfg, "allow_screenshot"):
                return _output(cid, error="This computer does not share its screen with the cloud. The switch "
                                          "is at the computer: ⚙ → CLOUD AGENT.")
            from plugins import _telegram_ops as ops
            data, cap = ops.screenshot("")[0]
            _output(cid, photo_b64=base64.b64encode(data).decode(), caption=cap)
            _log("SYS: Cloud agent sent a screenshot to the phone.")
        elif kind == "system":
            from plugins import _telegram_ops as ops
            _output(cid, text=ops.system_status())
        elif kind == "command":
            if not _on(cfg, "accept_commands"):
                return _output(cid, error="This computer does not accept requests from the cloud. The switch "
                                          "is at the computer: ⚙ → CLOUD AGENT.")
            _output(cid, **_ask_assistant(str(cmd.get("text") or "")))
        else:
            _output(cid, error="This computer's app does not know that request.")
    except Exception as e:
        _output(cid, error=f"{type(e).__name__}: {e}")


def _ensure_capture_hook() -> None:
    p = _player
    if p is None or id(p) in _wrapped:
        return
    orig = p.write_log

    def wrapped(text, *a, **kw):
        try:
            return orig(text, *a, **kw)
        finally:
            if _captures:
                prefix = f"{_assistant_name()}: "
                line = str(text)
                if line.startswith(prefix):
                    for c in list(_captures):
                        c["lines"].append(line[len(prefix):])
                        c["event"].set()
    try:
        p.write_log = wrapped
        _wrapped.add(id(p))
    except Exception as e:
        print(f"[CloudAgent] cannot read answers on this interface: {e}")


def _ask_assistant(text: str) -> dict:
    p = _player
    cb = getattr(p, "on_text_command", None) if p is not None else None
    if not callable(cb):
        return {"error": "No live session on the computer yet."}
    _ensure_capture_hook()
    cap = {"event": threading.Event(), "lines": []}
    _captures.append(cap)
    try:
        cb(f"[Request from the user's phone, relayed by the cloud] {text}")
        if not cap["event"].wait(90):
            return {"error": "The computer's assistant did not answer in time."}
        time.sleep(2)                      # a second sentence may follow
        return {"text": "\n".join(cap["lines"])[:3800]}
    finally:
        _captures.remove(cap)


# ═════════════════════════════════════════════════════════════════════════════
#  connect / disconnect
# ═════════════════════════════════════════════════════════════════════════════

def _connect(player) -> str:
    global _player
    if player is not None:
        _player = player
    if not str(_cfg().get("pair_code") or "").strip():
        return ("The cloud is not set up yet: on your server run  python -m core.cloud --pair-code  and "
                "paste the line it prints into ⚙ → CLOUD AGENT.")
    if linked():
        return "The cloud link is already up."
    try:
        _cloud_mod()
        _link["client"] = None
        _client()
    except Exception as e:
        return f"I can't connect to the cloud: {e}"
    _stop.clear()
    _link["thread"] = threading.Thread(target=_loop, daemon=True, name="cloud-link")
    _link["thread"].start()
    _hook_buttons()
    return "Connecting to your cloud server; the activity log says so once the link is up."


def _disconnect(bye: bool = True, timeout: float = 10.0) -> str:
    if not _link.get("thread"):
        return "The cloud link is not running."
    _stop.set()
    tr = sys.modules.get("plugins.telegram_remote")
    if _link.get("holding") and tr is not None and tr._state.get("running"):
        try:
            tr.run({"action": "stop"})
        except Exception:
            pass
    _link["holding"] = False
    if bye:
        try:
            _req("bye", {}, timeout=timeout)
        except Exception as e:
            print(f"[CloudAgent] bye failed: {e}")
    _link.update(thread=None, online=False)
    return "Disconnected from the cloud. Telegram goes back to the server; its tasks carry on."


def _at_exit() -> None:
    if _link.get("thread"):
        try:
            _disconnect(bye=True, timeout=5)
        except Exception:
            pass


atexit.register(_at_exit)


def _hook_buttons() -> None:
    """Reminder buttons (Done / +10 min …) pressed while this computer holds the
    bot belong to the server, which knows which reminder they answer."""
    tr = _telegram_mod()
    if tr is None or getattr(_hook_buttons, "done", False):
        return

    def tapped(chat, text) -> bool:
        try:
            cloud = _cloud_mod()
        except Exception:
            return False
        if cloud.reminder_button(text) is None or not linked():
            return False

        def send():
            try:
                _req("reminder_button", {"text": text, "chat": chat}, timeout=20)
            except Exception as e:
                print(f"[CloudAgent] the reminder button could not reach the server: {e}")
        threading.Thread(target=send, daemon=True).start()
        return True
    tr.TEXT_HOOKS.append(tapped)
    _hook_buttons.done = True


# ═════════════════════════════════════════════════════════════════════════════
#  run()
# ═════════════════════════════════════════════════════════════════════════════

def _short_reason(result: str) -> str:
    if "reason=" in result:
        return result.split("reason=", 1)[1].split(" -- ", 1)[0].strip().strip('"')
    return result[:200]


def _status() -> str:
    cfg = _cfg()
    if not str(cfg.get("pair_code") or "").strip():
        return "The cloud is not set up: no pairing code in ⚙ → CLOUD AGENT."
    if not _link.get("thread"):
        return "The cloud link is not running. Say 'connect to the cloud' to start it."
    bits = []
    if _link.get("online"):
        ago = int(time.time() - float(_link.get("last_contact") or time.time()))
        bits.append(f"Cloud link up; last contact {ago}s ago")
    else:
        bits.append(f"Cloud link is trying to reach the server ({_link.get('error') or 'connecting'})")
    hello = _link.get("server") or {}
    if hello:
        bits.append(f"server {hello.get('host')}, its clock reads your time as {hello.get('user_time')}")
        if not hello.get("gemini_key"):
            bits.append("the server has no Gemini key")
    if _link.get("holding") and _desk_telegram_running():
        bits.append("Telegram is answered by this computer")
    elif hello.get("telegram_running"):
        bits.append("Telegram is answered by the server")
    bits.append("accepts requests from the cloud" if _on(cfg, "accept_commands")
                else "does not accept requests from the cloud")
    return "; ".join(bits) + "."


def _days_list(raw) -> list | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, list):
        return [int(x) for x in raw if str(x).isdigit()]
    return [int(x) for x in str(raw).replace(" ", "").split(",") if x.isdigit()]


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _player
    p = parameters or {}
    if player is not None:
        _player = player
        try:
            from plugins import _announce
            _announce.remember(player)
        except Exception:
            pass
    action = str(p.get("action") or "status").strip().lower()
    try:
        return _run(action, p, player)
    except Exception as e:
        reason = str(e) or type(e).__name__
        return f"The cloud did not answer that: {reason[:300]}"


def _run(action: str, p: dict, player) -> str:
    if action == "status":
        return _status()
    if action == "connect":
        if player is None and _player is None:
            return "I have no interface to connect the cloud to."
        return _connect(player)
    if action == "disconnect":
        return _disconnect()
    if action == "delete_cloud":
        return _delete_cloud()
    if not str(_cfg().get("pair_code") or "").strip():
        return ("The cloud is not set up yet: paste your server's pairing code in ⚙ → CLOUD AGENT "
                "(python -m core.cloud --pair-code prints it on the server).")
    if action == "sync":
        return _sync()
    if action == "delegate":
        task = str(p.get("task") or "").strip()
        if not task:
            return "What should the cloud work on? Give me the whole task."
        res = _req("delegate", {"task": task, "notify": p.get("notify") or "both"})
        if res.get("error"):
            return f"The cloud refused the task: {res['error']}"
        job = res.get("job") or {}
        return (f"Handed to the cloud as job {job.get('id', '?')}. It keeps working even if this computer "
                f"is turned off, and the result will be announced when it is done.")
    if action == "jobs":
        jobs = _req("jobs", {}).get("jobs") or []
        if not jobs:
            return "The cloud has no jobs yet."
        return "\n".join(f"{j.get('id')} [{j.get('status')}] {str(j.get('task') or '')[:80]}" for j in jobs[:10])
    if action == "result":
        jid = str(p.get("job_id") or "").strip()
        if jid:
            job = _req("jobs", {"job_id": jid}).get("job")
        else:
            job = next(iter(_req("jobs", {}).get("jobs") or []), None)
        if not job:
            return "There is no such job on the cloud."
        text = str(job.get("result") or "")
        if text and len(text) > 600:
            _show("CLOUD RESULT", f"{job.get('task')}\n[{job.get('status')}]\n\n{text}")
            text = text[:600] + " … (the full text is on screen)"
        return f"Job {job.get('id')} [{job.get('status')}]: {text or 'no result yet'}"
    if action == "remind":
        text = str(p.get("text") or "").strip()
        if not text:
            return "What should the reminder say?"
        body = {"action": "add", "reminder": text, "tz_offset_min": _tz_offset_min()}
        for k in ("at", "time", "monthly_day", "every_minutes", "between"):
            if p.get(k) not in (None, ""):
                body[k] = p[k]
        days = _days_list(p.get("days"))
        if days:
            body["days"] = days
        if not any(k in body for k in ("at", "time", "monthly_day", "every_minutes")):
            return "remind needs at 'YYYY-MM-DD HH:MM', or time 'HH:MM' (with optional days), or every_minutes."
        return str(_req("schedule", body).get("result") or "The server did not answer.")
    if action == "reminders":
        return str(_req("schedule", {"action": "list"}).get("result") or "Nothing scheduled.")
    if action == "cancel_reminder":
        rid = str(p.get("id") or "").strip()
        if not rid:
            return "Which reminder? Give its id (ask for the list first)."
        return str(_req("schedule", {"action": "remove", "id": rid}).get("result") or "")
    if action == "briefing":
        changes = {}
        for k in ("enabled", "times", "city", "topics", "extra", "length", "language", "weather",
                  "reminders", "tasks"):
            if p.get(k) not in (None, ""):
                changes[k] = p[k]
        days = _days_list(p.get("days"))
        if days:
            changes["days"] = days
        if changes:
            body = {"settings": {"briefing": dict(changes_full(changes), updated=time.time()),
                                 "tz_offset_min": _tz_offset_min()}}
            out = _req("sync", body)
            _apply_server_briefing(out.get("briefing"))
            b = out.get("briefing") or {}
        else:
            b = _req("briefing", {}).get("briefing") or {}
        return _describe_briefing(b)
    if action == "briefing_now":
        _req("briefing", {"action": "now"})
        return "The briefing is being put together on the server and sent to the phone."
    if action == "export_data":
        return _export()
    return f"'{action}' is not an action for the cloud agent."


def changes_full(changes: dict) -> dict:
    """The server replaces the whole briefing with the newer one, so a change of
    one field is sent on top of the current settings, never alone."""
    try:
        current = _req("briefing", {}).get("briefing") or {}
    except Exception:
        current = {}
    base = {k: v for k, v in current.items() if k not in ("updated", "sent")}
    base.update(changes)
    return base


def _describe_briefing(b: dict) -> str:
    if not b:
        return "The server did not return the briefing settings."
    if not b.get("enabled"):
        return "The phone briefing is off."
    days = b.get("days") or []
    day_txt = "every day" if len(days) == 7 else "days " + ",".join(map(str, days))
    parts = [f"The phone briefing is on at {', '.join(b.get('times') or [])}, {day_txt}"]
    if b.get("city"):
        parts.append(f"weather for {b['city']}")
    if b.get("topics"):
        parts.append("news on " + ", ".join(b["topics"]))
    parts.append(f"{b.get('length', 'short')} length")
    return "; ".join(parts) + "."


def _export() -> str:
    data: dict = {"exported_at": datetime.now().isoformat(timespec="seconds")}
    for route, body, key in (("hello", {}, "server"), ("jobs", {}, "jobs"),
                             ("schedule", {"action": "list"}, "scheduled"),
                             ("briefing", {}, "briefing"), ("diag", {}, "diagnostics")):
        try:
            data[key] = _req(route, body, timeout=30)
        except Exception as e:
            data[key] = {"error": str(e)}
    # Full job results, one by one.
    full = []
    for j in (data.get("jobs") or {}).get("jobs") or []:
        try:
            full.append(_req("jobs", {"job_id": j.get("id")}, timeout=20).get("job") or j)
        except Exception:
            full.append(j)
    data["jobs"] = full
    try:
        from plugins._telegram_ops import default_inbox
        folder = default_inbox()
    except Exception:
        folder = Path.home() / "Downloads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cloud-export-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return (f"Saved what the cloud reports about you to {path}. It holds no password or key. Memory and phone "
            f"conversations live in /opt/jarvis-cloud/cloud_state on the server itself.")


def _delete_cloud() -> str:
    if not str(_cfg().get("pair_code") or "").strip():
        return "There is no cloud paired, so there is nothing to delete."

    def do_it() -> str:
        done = []
        try:
            _req("sync", {"settings": {"telegram": {"disabled": True}}}, timeout=20)
            done.append("Telegram switched off on the server")
        except Exception as e:
            done.append(f"Telegram could not be switched off there ({e})")
        try:
            listing = str(_req("schedule", {"action": "list"}).get("result") or "")
            ids = [line.split(" ", 1)[0] for line in listing.splitlines() if line.strip() and "[" in line]
            for rid in ids:
                _req("schedule", {"action": "remove", "id": rid}, timeout=10)
            done.append(f"{len(ids)} scheduled item(s) removed")
        except Exception as e:
            done.append(f"scheduled items not removed ({e})")
        _disconnect(bye=True, timeout=5)
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NS, {"pair_code": ""})
            _store_update(pem="", event_seq=0, memory_seq=0)
            done.append("this computer forgot the pairing")
        except Exception as e:
            done.append(f"pairing not forgotten ({e})")
        _link["client"] = None
        _show("CLOUD — FINISH ON THE SERVER",
              "This is your own server, so the rest is deleted there. Log in and run:\n\n"
              "  sudo systemctl disable --now jarvis-cloud\n"
              "  sudo rm -rf /opt/jarvis-cloud /etc/systemd/system/jarvis-cloud.service\n"
              "  sudo systemctl daemon-reload\n\n"
              "That removes the memory copy, phone conversations, reports and files it kept.")
        return "; ".join(done) + ". The rest is removed on the server — the commands are on screen."

    try:
        from core import confirm
    except Exception:
        return "This version of the app has no confirmation gate, so I won't delete anything."
    return confirm.request(
        "cloud_delete", "Delete the cloud body's data",
        "Stops Telegram on your server, removes its reminders and unpairs this computer. "
        "The memory copy on the server is then deleted by a command you run there. Cannot be undone.",
        do_it)


# ═════════════════════════════════════════════════════════════════════════════
#  connect on launch (desktop only)
# ═════════════════════════════════════════════════════════════════════════════

def _find_ui():
    for obj in gc.get_objects():
        try:
            if type(obj).__name__ == "JarvisUI":
                return obj
        except Exception:
            continue
    return None


def _autoconnect() -> None:
    try:
        deadline, ui = time.time() + 120, None
        while time.time() < deadline and ui is None:
            time.sleep(3)
            ui = _find_ui()
        if ui is None:
            return
        # on_text_command is attached by main.py just before plugins load; the
        # session itself may take a few more seconds.
        time.sleep(4)
        print(f"[CloudAgent] {_connect(ui)}")
    except Exception as e:
        print(f"[CloudAgent] auto-connect failed: {e}")


def _maybe_autoconnect() -> None:
    try:
        cfg = _cfg()
        if ("PyQt6.QtWidgets" in sys.modules and str(cfg.get("pair_code") or "").strip()
                and _on(cfg, "auto_connect")):
            from memory.config_manager import get_plugin_enabled
            if get_plugin_enabled(NS):
                threading.Thread(target=_autoconnect, daemon=True, name="cloud-autoconnect").start()
    except Exception:
        pass


_maybe_autoconnect()
