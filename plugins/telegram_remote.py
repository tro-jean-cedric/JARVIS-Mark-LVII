"""
plugins/telegram_remote.py — talk to this computer from your phone, through
YOUR OWN Telegram bot.

WHAT IT IS
    A bridge between a Telegram bot the user created with @BotFather and the
    assistant. A long-polling thread asks api.telegram.org for new messages;
    what an approved phone writes (or says, in a voice note) is handed to the
    assistant exactly as if it had been typed into the window, and the
    assistant's answer goes back to the phone. The phone can also ask for a
    screenshot, a webcam picture, the hardware readout, or a file out of one
    shared folder, and can send files that land in Downloads/<assistant name>.

    The same file runs unchanged on the user's own cloud server
    (core/cloud.py, TelegramFront), with a headless player.

THE WALLS — this file is a door into the computer, so:
     1. Nothing is accepted from a chat that is not on the approved list.
     2. A chat gets on the list only by sending the pairing code, in a private
        chat, while pairing is open — ten minutes after a start with no phone
        approved, or after "pair my phone" said AT the computer.
     3. Five wrong codes close pairing.
     4. Everything Telegram queued before the bridge started is discarded, so
        a code (or a command) sent earlier cannot be replayed.
     5. A message dated well before the start, or far in the future (a wrong
        clock), is dropped.
     6. Screenshots, the webcam, receiving files and sending files are each a
        switch that only the settings screen at the computer can turn on — a
        request that arrives from the phone cannot flip any of them.
     7. Opening pairing is refused while a phone turn is in progress.
     8. Irreversible steps still go through core/confirm; the phone is asked
        twice, with buttons, and only for a step that its own request raised.
     9. /get only sends files from inside the shared folder.
    10. The bot token never appears in a log line or an error message.
    11. "/stop" from the phone, or "panic" at the computer, closes the door;
        panic also forgets every approved phone.
    12. The recent-steps log records WHAT happened and when, never content.
    13. Another process polling the same bot (HTTP 409) stops this one rather
        than the two fighting over messages.

Settings (⚙ → TELEGRAM REMOTE): bot token, pairing code, and the switches.
"""
from __future__ import annotations

import hmac
import itertools
import json
import queue
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path

try:
    from plugins import _telegram_ops as ops
except Exception:                       # the plugin still loads; extras say why
    ops = None

NS = "telegram_remote"
API = "https://api.telegram.org"
POLL_SECONDS = 20
PAIR_SECONDS = 600
TURN_SECONDS = 150          # how long after a phone message its answers are relayed
MAX_WRONG_CODES = 5
MAX_VOICE_SECONDS = 300
MAX_TEXT = 4000

PLUGIN = {
    "name": "telegram_remote",
    "description": (
        "Controls the Telegram phone remote: the user's own Telegram bot that lets them talk to "
        "this assistant and this computer from their phone while away. Use for 'start the "
        "telegram remote', 'let me control the computer from my phone', 'send a screenshot to my "
        "phone', 'send the system status to my phone', 'send this file to my phone', 'pair my "
        "phone', 'stop the telegram bridge', 'stop everything' (panic). Actions: start, stop, "
        "status, send_screenshot, send_camera, send_status, send_file, send_text, pair, revoke, "
        "panic, log. It never messages other people — messaging a contact is send_message."
    ),
    "brief": "Phone remote over the user's own Telegram bot: start/stop, push screen/status/files to the phone.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["start", "stop", "status", "send_screenshot", "send_camera",
                         "send_status", "send_file", "send_text", "pair", "revoke", "panic", "log"],
                "description": "What to do. panic = stop the bridge AND forget every approved phone.",
            },
            "target": {
                "type": "STRING",
                "description": ("send_screenshot: a monitor number or 'all' (empty = main screen). "
                                "send_file: the file's full path on this computer."),
            },
            "text": {"type": "STRING", "description": "send_text: the message for the phone."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "TELEGRAM REMOTE",
    "fields": [
        {"key": "bot_token", "label": "Bot token (from @BotFather)", "type": "password"},
        {"key": "pairing_code", "label": "Pairing code — any secret phrase; send it to the bot once",
         "type": "password"},
        {"key": "allowed_chat_ids", "label": "Approved chats (filled in by pairing)", "type": "text",
         "placeholder": "empty = nothing is accepted"},
        {"key": "phone_control", "label": "Let approved phones send requests to the assistant",
         "type": "toggle", "default": True},
        {"key": "relay_replies", "label": "Send the assistant's answers back to Telegram",
         "type": "toggle", "default": True},
        {"key": "voice_replies", "label": "Speak answers to typed messages too (voice notes always are)",
         "type": "toggle", "default": False},
        {"key": "allow_screenshot", "label": "Allow screenshots to approved phones", "type": "toggle",
         "default": False},
        {"key": "allow_camera", "label": "Allow webcam pictures to approved phones", "type": "toggle",
         "default": False},
        {"key": "accept_files", "label": "Accept files from the phone", "type": "toggle", "default": True},
        {"key": "inbox_folder", "label": "Folder for files from the phone", "type": "text",
         "placeholder": "empty = Downloads/<assistant name>"},
        {"key": "share_folder", "label": "Folder the phone may fetch files from with /get", "type": "text",
         "placeholder": "empty = /get is off"},
        {"key": "allow_send_files", "label": "Let the assistant send files from this computer to the phone",
         "type": "toggle", "default": False},
        {"key": "start_on_launch", "label": "Start listening when the assistant launches", "type": "toggle",
         "default": False},
    ],
}

_DEFAULTS = {f["key"]: f.get("default", "") for f in PLUGIN_SETTINGS["fields"]}

# ── state (read by core/cloud.py — keep these names) ─────────────────────────
_state: dict = {
    "running": False, "stopping": False, "bot": "", "seen": 0, "skew": None,
    "turn_chat": None, "turn_open": False, "turn_until": 0.0, "turn_voice": False,
    "pairing_until": 0.0, "wrong_codes": 0, "started": 0.0, "last_chat": None, "gen": 0,
}
_lock = threading.RLock()
_stop_ev = threading.Event()
_out: "queue.Queue[dict]" = queue.Queue()
_player = None
_relay: dict = {}                 # {"player": obj, "orig": callable}
_steps: deque = deque(maxlen=80)  # recent steps, never content
_asks: dict = {}                  # confirmation questions on the phone, by nonce
_nonce = itertools.count(1)
_refused_at: dict = {}

# Other plugins (cloud_agent) may claim a message before it reaches the
# assistant: fn(chat_id, text) -> True when handled.
TEXT_HOOKS: list = []


# ═════════════════════════════════════════════════════════════════════════════
#  small helpers
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


def _save(values: dict) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, values)


def _token(cfg: dict | None = None) -> str:
    return "".join(str((cfg or _cfg()).get("bot_token") or "").split())


def _parse_ids(raw) -> list:
    if isinstance(raw, (list, tuple, set)):
        raw = ",".join(map(str, raw))
    out = []
    for part in re.split(r"[,;\s]+", str(raw or "")):
        part = part.strip()
        if part.lstrip("-").isdigit():
            n = int(part)
            if n not in out:
                out.append(n)
    return out


def _on(cfg: dict, key: str) -> bool:
    v = cfg.get(key)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _clean(text) -> str:
    """Error text with the bot token taken out — requests puts the URL in it."""
    s = str(text)
    tok = _token()
    if tok:
        s = s.replace(tok, "<token>")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot<token>", s)[:300]


def _assistant_name() -> str:
    try:
        from memory.config_manager import get_assistant_name
        return (get_assistant_name() or "JARVIS").strip()
    except Exception:
        return "JARVIS"


def _headless(player) -> bool:
    return type(player).__name__ == "HeadlessPlayer"


def _is_desktop() -> bool:
    return "PyQt6.QtWidgets" in sys.modules


def _log(text: str) -> None:
    """A line for the activity log, through the ORIGINAL write_log so it is
    never mistaken for an answer to relay."""
    p = _relay.get("player") or _player
    fn = _relay.get("orig") or (getattr(p, "write_log", None) if p is not None else None)
    try:
        if fn:
            fn(text)
            return
    except Exception:
        pass
    print(text)


def _step(what: str) -> None:
    _steps.append(f"{time.strftime('%H:%M:%S')}  {what}")


def _turn_active() -> bool:
    return bool(_state.get("turn_open")) and time.time() < float(_state.get("turn_until") or 0)


def _phone_turn_now() -> bool:
    """True while a phone message's turn is open — run() is then most likely
    being called because of something the phone asked for."""
    return bool(_state.get("running")) and _turn_active()


def _cloud():
    """cloud_agent, when it is loaded on this desktop and paired."""
    if not _is_desktop():
        return None
    ca = sys.modules.get("plugins.cloud_agent")
    try:
        return ca if ca is not None and ca.linked() else None
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Telegram API
# ═════════════════════════════════════════════════════════════════════════════

def _api(method: str, data: dict | None = None, files: dict | None = None,
         timeout: float = 30, token: str = "") -> dict:
    import requests

    tok = token or _token()
    if not tok:
        return {"ok": False, "error_code": 0, "description": "no bot token"}
    url = f"{API}/bot{tok}/{method}"
    try:
        if files:
            form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                    for k, v in (data or {}).items() if v is not None}
            r = requests.post(url, data=form, files=files, timeout=timeout)
        else:
            r = requests.post(url, json=data or {}, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error_code": -1, "description": _clean(f"{type(e).__name__}: {e}")}
    try:
        doc = r.json()
    except ValueError:
        doc = {"ok": False, "error_code": r.status_code, "description": f"HTTP {r.status_code}"}
    if not isinstance(doc, dict):
        doc = {"ok": False, "error_code": r.status_code, "description": "unexpected answer"}
    doc.setdefault("error_code", 0 if doc.get("ok") else r.status_code)
    return doc


def _download(file_id: str, limit: int) -> bytes:
    import requests

    info = _api("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path") if info.get("ok") else None
    if not path:
        raise RuntimeError("Telegram returned no path for that file"
                           + (f" ({info.get('description')})" if info.get("description") else ""))
    size = int((info.get("result") or {}).get("file_size") or 0)
    if size and size > limit:
        raise RuntimeError("that file is too big for a bot to fetch (20 MB at most)")
    try:
        r = requests.get(f"{API}/file/bot{_token()}/{path}", timeout=120)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(_clean(f"{type(e).__name__}: {e}")) from None
    return r.content


def _keyboard(cfg: dict | None = None) -> dict:
    """The reply keyboard under the phone's text box."""
    cfg = cfg or _cfg()
    row = []
    if _on(cfg, "allow_screenshot"):
        row.append("/screen")
    if _on(cfg, "allow_camera"):
        row.append("/cam")
    row.append("/sys")
    second = (["/get"] if str(cfg.get("share_folder") or "").strip() else []) + ["/help"]
    return {"keyboard": [row, second], "resize_keyboard": True, "is_persistent": False}


# ── the sender: one thread, in order, retried on flood limits ───────────────

def _put(item: dict) -> None:
    _out.put(item)


def _enqueue(chat, text: str, markup: dict | None = None) -> None:
    text = str(text or "").strip()
    while text:
        part, text = text[:MAX_TEXT], text[MAX_TEXT:]
        item = {"kind": "text", "chat": chat, "text": part}
        if markup and not text:
            item["markup"] = markup
        _put(item)


def _send_now(item: dict) -> dict:
    kind, chat = item.get("kind"), item.get("chat")
    caption = str(item.get("caption") or "")[:1024] or None
    if kind == "text":
        data = {"chat_id": chat, "text": item.get("text") or "…"}
        if item.get("markup"):
            data["reply_markup"] = item["markup"]
        return _api("sendMessage", data)
    method, field = {"photo": ("sendPhoto", "photo"), "voice": ("sendVoice", "voice"),
                     "audio": ("sendDocument", "document")}.get(kind, ("sendDocument", "document"))
    mime = item.get("mime") or {"photo": "image/jpeg", "voice": "audio/ogg",
                                "audio": "audio/wav"}.get(kind, "application/octet-stream")
    files = {field: (item.get("name") or "file", item.get("data") or b"", mime)}
    return _api(method, {"chat_id": chat, "caption": caption}, files=files, timeout=180)


def _sender_loop(gen: int) -> None:
    while _state.get("gen") == gen:
        try:
            item = _out.get(timeout=1)
        except queue.Empty:
            if _stop_ev.is_set():
                return
            continue
        if item is None:
            return
        for attempt in range(4):
            res = _send_now(item)
            if res.get("ok"):
                break
            if res.get("error_code") == 429:
                wait = float(((res.get("parameters") or {}).get("retry_after")) or 3)
                time.sleep(min(wait, 30))
                continue
            if res.get("error_code") == -1 and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            print(f"[TelegramRemote] send failed: {res.get('description')}")
            break


def _push(chat, kind: str, data: bytes, name: str, caption: str = "", mime: str = "") -> None:
    """Send bytes to one chat — through the queue while running, directly otherwise."""
    item = {"kind": kind, "chat": chat, "data": data, "name": name, "caption": caption}
    if mime:
        item["mime"] = mime
    if _state.get("running"):
        _put(item)
    else:
        res = _send_now(item)
        if not res.get("ok"):
            raise RuntimeError(str(res.get("description") or "Telegram refused it"))


def _say_to(chat, text: str, markup: dict | None = None) -> None:
    if _state.get("running"):
        _enqueue(chat, text, markup)
    else:
        data = {"chat_id": chat, "text": str(text)[:MAX_TEXT]}
        if markup:
            data["reply_markup"] = markup
        res = _api("sendMessage", data)
        if not res.get("ok"):
            raise RuntimeError(str(res.get("description") or "Telegram refused it"))


# ═════════════════════════════════════════════════════════════════════════════
#  relaying the assistant's answers
# ═════════════════════════════════════════════════════════════════════════════

def _install_relay(player) -> None:
    if _relay.get("player") is player:
        return
    _remove_relay()
    orig = player.write_log

    def wrapped(text, *a, **kw):
        try:
            return orig(text, *a, **kw)
        finally:
            try:
                _maybe_relay(str(text))
            except Exception as e:
                print(f"[TelegramRemote] relay error: {e}")

    wrapped._telegram_relay = True
    try:
        player.write_log = wrapped
    except Exception as e:
        print(f"[TelegramRemote] cannot relay answers on this interface: {e}")
        return
    _relay.update(player=player, orig=orig)


def _remove_relay() -> None:
    p, orig = _relay.get("player"), _relay.get("orig")
    if p is not None and orig is not None:
        try:
            if getattr(p.write_log, "_telegram_relay", False):
                if _headless(p) or getattr(orig, "__self__", None) is p:
                    try:
                        del p.write_log         # back to the class method
                    except AttributeError:
                        p.write_log = orig
                else:
                    p.write_log = orig
        except Exception:
            pass
    _relay.clear()


def _maybe_relay(line: str) -> None:
    if not _state.get("running") or not _turn_active():
        return
    chat = _state.get("turn_chat")
    prefix = f"{_assistant_name()}: "
    if not chat or not line.startswith(prefix):
        return
    answer = line[len(prefix):].strip()
    if not answer:
        return
    cfg = _cfg()
    # A conversation goes on: every answer keeps the window open a little longer.
    _state["turn_until"] = max(float(_state.get("turn_until") or 0), time.time() + 60)
    if _on(cfg, "relay_replies"):
        _enqueue(chat, answer, _keyboard(cfg))
        _step("answer relayed to the phone")
    if _state.get("turn_voice") or _on(cfg, "voice_replies"):
        threading.Thread(target=_speak_to, args=(chat, answer), daemon=True,
                         name="telegram-voice").start()


def _speak_to(chat, text: str) -> None:
    if ops is None:
        return
    try:
        audio, kind = ops.synthesize(text)
        _put({"kind": kind, "chat": chat, "data": audio,
              "name": "answer.ogg" if kind == "voice" else "answer.wav"})
        _step("spoken answer sent")
    except Exception as e:
        _log(f"SYS: Telegram remote could not speak an answer — {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  incoming
# ═════════════════════════════════════════════════════════════════════════════

def _fold_code(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(ch for ch in t if ch.isalnum())


def _code_matches(sent: str, code: str) -> bool:
    a, b = _fold_code(sent), _fold_code(code)
    return len(b) >= 4 and hmac.compare_digest(a.encode(), b.encode())


def _poll_loop(token: str) -> None:
    offset = None
    failures, warned = 0, False
    try:
        # Wall 4: whatever waited in Telegram's queue before now is skipped.
        first = _api("getUpdates", {"offset": -1, "timeout": 0}, token=token, timeout=20)
        if first.get("ok") and first.get("result"):
            offset = int(first["result"][-1]["update_id"]) + 1
        while not _stop_ev.is_set():
            data = {"timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                data["offset"] = offset
            res = _api("getUpdates", data, token=token, timeout=POLL_SECONDS + 15)
            if _stop_ev.is_set():
                break
            if not res.get("ok"):
                code = res.get("error_code")
                if code == 409:
                    print("[Telegram] another poller is using this bot — this bridge stops.")
                    _log("SYS: Telegram remote stopped — another program is reading this bot's "
                         "messages (only one may).")
                    break
                if code in (401, 404):
                    _log(f"SYS: Telegram rejected the bot token ({res.get('description')}).")
                    break
                failures += 1
                print(f"[TelegramRemote] poll error: {res.get('description')}")
                if failures >= 5 and not warned:
                    warned = True
                    _log("SYS: Telegram remote is having trouble reaching Telegram — still trying.")
                _stop_ev.wait(min(60, 2 * failures))
                continue
            failures, warned = 0, False
            for upd in res.get("result") or []:
                offset = int(upd.get("update_id", 0)) + 1
                try:
                    if upd.get("callback_query"):
                        _on_button(upd["callback_query"])
                    elif upd.get("message"):
                        _on_message(upd["message"])
                except Exception as e:
                    print(f"[TelegramRemote] handler error: {type(e).__name__}: {_clean(e)}")
    finally:
        _finish_stop()


def _finish_stop() -> None:
    was_desk = _is_desktop()
    with _lock:
        _state.update(running=False, stopping=False, turn_open=False, pairing_until=0.0)
        _out.put(None)
        _remove_relay()
        _asks.clear()
    print("[Telegram] remote stopped listening.")
    _step("bridge stopped")
    ca = _cloud() if was_desk else None
    if ca is not None:
        try:
            ca.release_telegram()
        except Exception as e:
            print(f"[TelegramRemote] could not hand Telegram back to the cloud: {e}")


def _approved(chat) -> bool:
    return chat in _parse_ids(_cfg().get("allowed_chat_ids"))


def _on_message(msg: dict) -> None:
    chat_obj = msg.get("chat") or {}
    chat = chat_obj.get("id")
    if chat is None:
        return
    sent_at = float(msg.get("date") or 0)
    now = time.time()
    if sent_at and sent_at > now + 120:
        _state["skew"] = int(sent_at - now)
        _log("SYS: Telegram remote dropped a message dated in the future — this computer's clock "
             "is behind Telegram's. Fix the system clock.")
        return
    if sent_at and sent_at < float(_state.get("started") or 0) - 5:
        _log("SYS: Telegram remote dropped a message that was sent before it started listening.")
        return
    text = str(msg.get("text") or msg.get("caption") or "").strip()
    cfg = _cfg()

    if not _approved(chat):
        _maybe_pair(chat, chat_obj, text, cfg)
        return

    _state["seen"] = int(_state.get("seen") or 0) + 1
    _state["last_chat"] = chat

    for hook in list(TEXT_HOOKS):
        try:
            if text and hook(chat, text):
                _step("message handled by another module")
                return
        except Exception as e:
            print(f"[TelegramRemote] hook error: {e}")

    if msg.get("voice") or msg.get("audio"):
        threading.Thread(target=_on_voice, args=(chat, msg), daemon=True).start()
        return
    if msg.get("document") or msg.get("photo") or msg.get("video"):
        threading.Thread(target=_on_file, args=(chat, msg, cfg), daemon=True).start()
        return
    if not text:
        return
    if text.startswith("/"):
        _command(chat, text, cfg)
        return
    _forward(chat, text, voice=False, cfg=cfg)


def _maybe_pair(chat, chat_obj: dict, text: str, cfg: dict) -> None:
    open_ = time.time() < float(_state.get("pairing_until") or 0)
    code = str(cfg.get("pairing_code") or "")
    if open_ and text and chat_obj.get("type") == "private" and code:
        if _code_matches(text, code):
            ids = _parse_ids(cfg.get("allowed_chat_ids"))
            ids.append(chat)
            try:
                _save({"allowed_chat_ids": ",".join(map(str, ids))})
            except Exception as e:
                _log(f"SYS: Telegram remote could not save the pairing: {e}")
                return
            _state.update(pairing_until=0.0, wrong_codes=0)
            who = chat_obj.get("first_name") or chat_obj.get("username") or "a phone"
            _log(f"SYS: Telegram remote paired with chat {chat} ({who}).")
            _step(f"paired chat {chat}")
            _enqueue(chat, "Paired. This chat can now talk to " + _assistant_name()
                     + ". Send /help for what it can do.", _keyboard(cfg))
            ca = _cloud()
            if ca is not None:
                threading.Thread(target=_safe_sync, args=(ca,), daemon=True).start()
            return
        _state["wrong_codes"] = int(_state.get("wrong_codes") or 0) + 1
        _step("wrong pairing code")
        if _state["wrong_codes"] >= MAX_WRONG_CODES:
            _state["pairing_until"] = 0.0
            _log("SYS: Telegram remote closed pairing — too many wrong codes. Say \"pair my phone\" "
                 "at the computer to open it again.")
        return
    last = _refused_at.get(chat, 0)
    if time.time() - last > 60:
        _refused_at[chat] = time.time()
        _log(f"SYS: Telegram remote refused an unapproved chat ({chat}).")
        _step(f"refused unapproved chat {chat}")


def _safe_sync(ca) -> None:
    try:
        ca.sync_now()
    except Exception as e:
        print(f"[TelegramRemote] cloud sync after pairing failed: {e}")


def _help_text(cfg: dict) -> str:
    name = _assistant_name()
    lines = [f"Write to {name} here as you would at the computer — in any language.",
             "Or hold the microphone button and say it; the answer then comes back out loud too.",
             "", "/sys — CPU, memory, disk, uptime"]
    if _on(cfg, "allow_screenshot"):
        lines.append("/screen [2|all] — a picture of the screen")
    if _on(cfg, "allow_camera"):
        lines.append("/cam — a webcam picture")
    if str(cfg.get("share_folder") or "").strip():
        lines.append("/get [name] — list or fetch a file from the shared folder")
    if _on(cfg, "accept_files"):
        lines.append("Send a file or photo — it is saved on the computer")
    lines += ["/confirm — is anything waiting for a confirmation?", "/undo — take back the last change",
              "/id — this chat's number", "/stop — close the bridge (restart it at the computer)"]
    return "\n".join(lines)


def _command(chat, text: str, cfg: dict) -> None:
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()
    arg = arg.strip()
    _step(f"command {cmd}")
    if cmd in ("/start", "/help"):
        _enqueue(chat, _help_text(cfg), _keyboard(cfg))
    elif cmd == "/id":
        _enqueue(chat, f"This chat's ID is {chat}.")
    elif cmd == "/stop":
        _enqueue(chat, "Closing the remote bridge. Start it again from the computer.")
        _log("SYS: Telegram remote closed from the phone.")
        threading.Thread(target=_stop, daemon=True).start()
    elif cmd == "/screen":
        threading.Thread(target=_phone_screen, args=(chat, arg, cfg), daemon=True).start()
    elif cmd in ("/cam", "/camera", "/webcam"):
        threading.Thread(target=_phone_camera, args=(chat, cfg), daemon=True).start()
    elif cmd in ("/sys", "/status"):
        threading.Thread(target=_phone_status, args=(chat,), daemon=True).start()
    elif cmd == "/get":
        threading.Thread(target=_phone_get, args=(chat, arg, cfg), daemon=True).start()
    elif cmd == "/confirm":
        title = ops.pending_confirmation() if ops else ""
        _enqueue(chat, f"Waiting for a confirmation: {title}" if title
                 else "Nothing is waiting for a confirmation.")
    elif cmd == "/undo":
        if not _on(cfg, "phone_control"):
            _enqueue(chat, "Controlling the computer from the phone is switched off at the computer.")
        elif ops is None:
            _enqueue(chat, "Undo is unavailable (the file _telegram_ops.py is missing).")
        else:
            try:
                _enqueue(chat, ops.undo_last())
            except Exception as e:
                _enqueue(chat, str(e))
    else:
        _forward(chat, text, voice=False, cfg=cfg)


def _need_ops(chat) -> bool:
    if ops is None:
        _enqueue(chat, "That needs the file _telegram_ops.py next to telegram_remote.py.")
        return False
    return True


def _phone_screen(chat, which: str, cfg: dict) -> None:
    if not _on(cfg, "allow_screenshot"):
        _enqueue(chat, "Screenshots are switched off. Only the settings at the computer can turn them on.")
        return
    if not _need_ops(chat):
        return
    try:
        for data, cap in ops.screenshot(which):
            _put({"kind": "photo", "chat": chat, "data": data, "name": "screen.jpg", "caption": cap})
        _step("screenshot sent")
    except Exception as e:
        _enqueue(chat, str(e))


def _phone_camera(chat, cfg: dict) -> None:
    if not _on(cfg, "allow_camera"):
        _enqueue(chat, "Webcam pictures are switched off. Only the settings at the computer can turn them on.")
        return
    if not _need_ops(chat):
        return
    try:
        _put({"kind": "photo", "chat": chat, "data": ops.webcam(), "name": "camera.jpg",
              "caption": f"Webcam · {time.strftime('%H:%M:%S')}"})
        _step("webcam picture sent")
    except Exception as e:
        _enqueue(chat, str(e))


def _phone_status(chat) -> None:
    if not _need_ops(chat):
        return
    try:
        _enqueue(chat, ops.system_status())
        _step("system status sent")
    except Exception as e:
        _enqueue(chat, str(e))


def _phone_get(chat, name: str, cfg: dict) -> None:
    folder = str(cfg.get("share_folder") or "").strip()
    if not folder:
        _enqueue(chat, "No shared folder is set at the computer, so /get is off.")
        return
    if not _need_ops(chat):
        return
    try:
        if not name:
            _enqueue(chat, ops.list_folder(folder))
            return
        path = ops.file_in_folder(folder, name)
        _put({"kind": "document", "chat": chat, "data": path.read_bytes(), "name": path.name,
              "caption": path.name})
        _step("shared file sent")
    except Exception as e:
        _enqueue(chat, str(e))


def _on_voice(chat, msg: dict) -> None:
    cfg = _cfg()
    v = msg.get("voice") or msg.get("audio") or {}
    if int(v.get("duration") or 0) > MAX_VOICE_SECONDS:
        _enqueue(chat, f"That voice note is longer than {MAX_VOICE_SECONDS // 60} minutes. "
                       "Say it in a shorter one.")
        return
    if not _need_ops(chat):
        return
    _api("sendChatAction", {"chat_id": chat, "action": "typing"})
    try:
        audio = _download(v.get("file_id", ""), 20 * 1024 * 1024)
        heard = ops.transcribe(audio, v.get("mime_type") or "audio/ogg")
    except Exception as e:
        _log(f"SYS: Telegram remote could not transcribe a voice note — {_clean(e)}")
        _enqueue(chat, f"I could not make out that voice note: {_clean(e)}")
        return
    if not heard:
        _enqueue(chat, "I could not hear anything in that.")
        return
    _step("voice note transcribed")
    _enqueue(chat, f"🎙 {heard}")
    _forward(chat, heard, voice=True, cfg=cfg)


def _on_file(chat, msg: dict, cfg: dict) -> None:
    if not _on(cfg, "accept_files"):
        _enqueue(chat, "Files from the phone are switched off at the computer.")
        return
    if not _need_ops(chat):
        return
    if msg.get("photo"):
        best = msg["photo"][-1]
        fid, name = best.get("file_id"), f"photo_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    else:
        doc = msg.get("document") or msg.get("video") or {}
        fid = doc.get("file_id")
        name = doc.get("file_name") or f"file_{time.strftime('%Y%m%d_%H%M%S')}"
    folder = str(cfg.get("inbox_folder") or "").strip()
    target_dir = Path(folder).expanduser() if folder else ops.default_inbox()
    try:
        data = _download(fid or "", 20 * 1024 * 1024)
        target = ops.unique_path(target_dir, name)
        target.write_bytes(data)
    except Exception as e:
        _log(f"SYS: Telegram remote could not save a file — {_clean(e)}")
        _enqueue(chat, f"I could not save that file: {_clean(e)}")
        return
    _log(f"SYS: Telegram remote saved a file from the phone: {target}")
    _step(f"file received ({len(data) // 1024} KB)")
    _enqueue(chat, f"Saved as {target.name} in {target.parent}.")
    caption = str(msg.get("caption") or "").strip()
    if caption:
        _forward(chat, f"{caption}\n(The file I just sent is saved at: {target})", voice=False, cfg=cfg)


def _forward(chat, text: str, voice: bool, cfg: dict) -> None:
    """Hand the phone's words to the assistant, and open the reply window."""
    if not _on(cfg, "phone_control"):
        _enqueue(chat, "Requests from the phone are switched off at the computer.")
        return
    p = _player
    cb = getattr(p, "on_text_command", None) if p is not None else None
    if not callable(cb):
        _enqueue(chat, "Nobody is home: the assistant on the computer is not running a session yet.")
        return
    _state.update(turn_chat=chat, turn_open=True, turn_until=time.time() + TURN_SECONDS,
                  turn_voice=bool(voice))
    _api("sendChatAction", {"chat_id": chat, "action": "typing"})
    line = text if _headless(p) else (
        f"[Telegram — the user, writing from their phone{' (voice note)' if voice else ''}] {text}")
    _step("voice request forwarded" if voice else "text request forwarded")
    try:
        cb(line)
    except Exception as e:
        _enqueue(chat, f"The assistant could not take that: {type(e).__name__}.")
        print(f"[TelegramRemote] forwarding failed: {e}")


# ── confirmations on the phone ───────────────────────────────────────────────

def _ask_markup(nonce: int, step: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": f"cf:{nonce}:{step}:y"},
                                 {"text": "✖ No", "callback_data": f"cf:{nonce}:{step}:n"}]]}


def _watch_loop(gen: int) -> None:
    """Turn expiry, and confirmations raised by a phone request."""
    while not _stop_ev.wait(1.0) and _state.get("gen") == gen:
        try:
            if _state.get("turn_open") and time.time() >= float(_state.get("turn_until") or 0):
                _state["turn_open"] = False
            title = ops.pending_confirmation() if ops else ""
            asked = next((a for a in _asks.values() if a["title"] == title), None) if title else None
            if title and asked is None and _turn_active() and _on(_cfg(), "phone_control"):
                nonce = next(_nonce)
                chat = _state.get("turn_chat")
                _asks[nonce] = {"title": title, "chat": chat, "at": time.time()}
                _enqueue(chat, f"Shall I do this? (1/2)\n{title}", _ask_markup(nonce, 1))
                _step("confirmation asked on the phone")
            for nonce, a in list(_asks.items()):
                if a["title"] != title:
                    _asks.pop(nonce, None)
                    if not a.get("answered"):
                        _enqueue(a["chat"], f"“{a['title']}” is no longer waiting.")
        except Exception as e:
            print(f"[TelegramRemote] watcher error: {e}")


def _on_button(cq: dict) -> None:
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    _api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
    if chat is None or not _approved(chat):
        return
    parts = str(cq.get("data") or "").split(":")
    if len(parts) != 4 or parts[0] != "cf":
        return
    try:
        nonce, step = int(parts[1]), int(parts[2])
    except ValueError:
        return
    yes = parts[3] == "y"
    ask = _asks.get(nonce)
    msg_id = (cq.get("message") or {}).get("message_id")
    if ask is None or ask["chat"] != chat or ops is None or ops.pending_confirmation() != ask["title"]:
        _edit(chat, msg_id, "This question has expired. Nothing was done.")
        return
    if not yes:
        ask["answered"] = True
        _asks.pop(nonce, None)
        _resolve(False)
        _edit(chat, msg_id, "Cancelled. Nothing was done.")
        _step("confirmation declined on the phone")
        return
    if step == 1:
        _edit(chat, msg_id, f"Are you sure? This may not be undoable. (2/2)\n{ask['title']}",
              _ask_markup(nonce, 2))
        return
    ask["answered"] = True
    _asks.pop(nonce, None)
    _edit(chat, msg_id, f"Confirmed: {ask['title']}")
    _step("confirmation accepted on the phone")
    _resolve(True, chat, ask["title"])


def _edit(chat, msg_id, text: str, markup: dict | None = None) -> None:
    data = {"chat_id": chat, "message_id": msg_id, "text": text[:MAX_TEXT]}
    if markup:
        data["reply_markup"] = markup
    if msg_id is None or not _api("editMessageText", data).get("ok"):
        _enqueue(chat, text, markup)


def _resolve(accepted: bool, chat=None, title: str = "") -> None:
    try:
        from core import confirm
    except Exception:
        return
    if accepted and chat is not None:
        # Hear how it went: the gate logs its result through a callback bound
        # before this bridge existed, so the stored action is wrapped instead.
        try:
            with confirm._lock:
                p = confirm._pending
                if p is not None and p.title == title:
                    run_fn = p.run

                    def reporting():
                        try:
                            result = run_fn() or "Done."
                        except Exception as e:
                            _enqueue(chat, f"✖ {title} failed: {e}")
                            raise
                        _enqueue(chat, f"✅ {title}: {str(result)[:1500]}")
                        return result
                    p.run = reporting
        except Exception as e:
            print(f"[TelegramRemote] could not follow the confirmed step: {e}")
    confirm.resolve(accepted)


# ═════════════════════════════════════════════════════════════════════════════
#  start / stop
# ═════════════════════════════════════════════════════════════════════════════

def _start(player) -> str:
    cfg = _cfg()
    token = _token(cfg)
    with _lock:
        if _state.get("stopping"):
            return "The Telegram remote is still closing the last connection — give it a moment and ask again."
        if _state.get("running") or _state.get("starting"):
            return "The Telegram remote is already listening."
        if not token:
            return ("There's no bot token set. Create a bot with Telegram's @BotFather, paste its token "
                    "into ⚙ → TELEGRAM REMOTE, set a pairing code, and ask again.")
        if player is None or not (_headless(player) or callable(getattr(player, "on_text_command", None))):
            return "I have no interface to route remote commands into."
        ids = _parse_ids(cfg.get("allowed_chat_ids"))
        code = str(cfg.get("pairing_code") or "").strip()
        if not ids and len(_fold_code(code)) < 4:
            return ("No approved phone and no pairing code (4+ letters or digits), so nothing could ever be "
                    "accepted. Set a pairing code in ⚙ → TELEGRAM REMOTE first.")
        me = _api("getMe", token=token, timeout=20)
        if not me.get("ok"):
            return f"Telegram refused the token: {me.get('description') or 'no answer'}."
        bot = (me.get("result") or {}).get("username") or ""
        ca = _cloud()
        if ca is None or getattr(ca, "holding", lambda: False)():
            return _begin(player, token, bot, ids)
        # The cloud server holds the bot while this computer does not; asking it
        # to let go can take a while, so that happens off the caller's thread.
        _state["starting"] = True

    def claim():
        try:
            ok, why = ca.claim_telegram()
        except Exception as e:
            ok, why = False, str(e)
        try:
            if ok:
                said = _begin(player, token, bot, ids)
                _log(f"SYS: {said}")
            else:
                _report_failure(f"the cloud server did not hand the bot over ({why}); it keeps answering "
                                f"the phone")
        finally:
            _state["starting"] = False
    threading.Thread(target=claim, daemon=True, name="telegram-claim").start()
    return ("Taking the Telegram bot over from your cloud server; I'll note it in the log once this "
            "computer is listening.")


def _begin(player, token: str, bot: str, ids: list) -> str:
    global _player
    with _lock:
        if _state.get("running"):
            return "The Telegram remote is already listening."
        _player = player
        _stop_ev.clear()
        while not _out.empty():
            try:
                _out.get_nowait()
            except queue.Empty:
                break
        _install_relay(player)
        gen = int(_state.get("gen") or 0) + 1
        _state.update(running=True, stopping=False, bot=bot, seen=0, started=time.time(),
                      turn_open=False, wrong_codes=0, gen=gen,
                      pairing_until=(0.0 if ids else time.time() + PAIR_SECONDS))
        threading.Thread(target=_poll_loop, args=(token,), daemon=True, name="telegram-poll").start()
        threading.Thread(target=_sender_loop, args=(gen,), daemon=True, name="telegram-send").start()
        threading.Thread(target=_watch_loop, args=(gen,), daemon=True, name="telegram-watch").start()
    print("[Telegram] remote is listening.")
    _step("bridge started")
    if ids:
        return (f"The Telegram remote is listening as @{bot}. Message it from any of your {len(ids)} "
                f"approved chat(s) and I'll act on it here.")
    return (f"The Telegram remote is listening as @{bot}, but no phone is approved yet. Send your pairing "
            f"code to @{bot} within {PAIR_SECONDS // 60} minutes; anything sent before now was discarded.")


def _stop() -> str:
    with _lock:
        if not _state.get("running"):
            return "The Telegram remote is not running."
        if _state.get("stopping"):
            return "The Telegram remote is already closing."
        _state["stopping"] = True
        _stop_ev.set()
    return "Telegram remote closing. Nothing can reach the computer through it now."


def _status() -> str:
    cfg = _cfg()
    ids = _parse_ids(cfg.get("allowed_chat_ids"))
    if not _token(cfg):
        return "The Telegram remote has no bot token yet (⚙ → TELEGRAM REMOTE)."
    if not _state.get("running"):
        where = ""
        ca = sys.modules.get("plugins.cloud_agent")
        try:
            if ca is not None and ca.linked():
                where = " Your cloud server answers the bot while this computer does not."
        except Exception:
            pass
        return f"The Telegram remote is not running. {len(ids)} approved chat(s).{where}"
    bits = [f"Listening as @{_state.get('bot')}", f"{len(ids)} approved chat(s)",
            f"{_state.get('seen', 0)} message(s) accepted this run"]
    left = float(_state.get("pairing_until") or 0) - time.time()
    if left > 0:
        bits.append(f"pairing open for another {int(left // 60) + 1} min")
    if _turn_active():
        bits.append("a phone conversation is in progress")
    title = ops.pending_confirmation() if ops else ""
    if title:
        bits.append(f"a confirmation is waiting: {title}")
    return "; ".join(bits) + "."


def _targets() -> list:
    return _parse_ids(_cfg().get("allowed_chat_ids"))


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        return _run(parameters or {}, player)
    except Exception as e:
        print(f"[TelegramRemote] {type(e).__name__}: {_clean(e)}")
        return f"The Telegram remote hit a problem: {_clean(e)}"


def _run(p: dict, player) -> str:
    action = str(p.get("action") or "status").strip().lower()
    if player is not None and not _headless(player):
        try:
            from plugins import _announce
            _announce.remember(player)
        except Exception:
            pass
    if action == "start":
        return _start(player)
    if action == "stop":
        return _stop()
    if action == "status":
        return _status()
    if action == "log":
        text = "\n".join(_steps) or "Nothing has happened yet."
        if player is not None and hasattr(player, "show_content"):
            player.show_content("TELEGRAM REMOTE", text)
            return "The recent steps of the Telegram remote are on screen."
        return text
    if action == "pair":
        if _phone_turn_now():
            return "Pairing another phone can only be opened at the computer, not from a remote request."
        if not str(_cfg().get("pairing_code") or "").strip():
            return "There's no pairing code set. Put one in ⚙ → TELEGRAM REMOTE first."
        if not _state.get("running"):
            return "The bridge isn't listening yet — start the Telegram remote first, then send the code."
        _state.update(pairing_until=time.time() + PAIR_SECONDS, wrong_codes=0)
        _step("pairing opened")
        return (f"Pairing is open for {PAIR_SECONDS // 60} minutes. Send the pairing code to "
                f"@{_state.get('bot')} from the phone you want to approve.")
    if action in ("revoke", "panic"):
        if action == "panic":
            _stop()
        try:
            _save({"allowed_chat_ids": ""})
        except Exception as e:
            return f"I could not clear the approved chats: {e}"
        _state["pairing_until"] = 0.0
        _log("SYS: Telegram remote — " + ("PANIC: bridge closed, every approved chat erased."
                                          if action == "panic" else "every approved chat revoked."))
        _step(action)
        if action == "panic":
            return ("The bridge is closed and every approved phone is forgotten. Nothing can reach this "
                    "computer through Telegram until a phone is paired again.")
        return "Every approved chat has been revoked. No phone can command this computer until one is paired again."

    if action not in ("send_text", "send_status", "send_screenshot", "send_camera", "send_file"):
        return (f"'{action}' is not an action for the Telegram remote. I can start, stop, report status, "
                f"send a screenshot, webcam picture, status, file or text, pair, revoke, or panic.")

    # Everything below sends something to the phone.
    targets = _targets()
    if not _token():
        return "There's no bot token set, so there's nothing to send through."
    if not targets:
        return "No phone is approved yet, so there's nobody to send it to. Pair one first."
    cfg = _cfg()
    if action == "send_text":
        text = str(p.get("text") or p.get("target") or "").strip()
        if not text:
            return "What should I send to the phone?"
        for c in targets:
            _say_to(c, text)
        _step("text sent to the phone")
        return f"Sent to your phone{'s' if len(targets) > 1 else ''}."
    if ops is None:
        return "That needs the file _telegram_ops.py next to telegram_remote.py; copy it into plugins/ and restart."
    if action == "send_status":
        text = ops.system_status()
        for c in targets:
            _say_to(c, text)
        _step("system status sent")
        return "The system status is on its way to your phone."
    if action in ("send_screenshot", "send_camera"):
        key, what = (("allow_screenshot", "Screenshots") if action == "send_screenshot"
                     else ("allow_camera", "Webcam pictures"))
        if not _on(cfg, key):
            return (f"{what} to the phone are switched off. Only the settings at the computer "
                    f"(⚙ → TELEGRAM REMOTE) can turn them on.")
        threading.Thread(target=_push_pictures, args=(action, str(p.get("target") or ""), targets),
                         daemon=True).start()
        return "Taking it now; it will be on your phone in a moment."
    if action == "send_file":
        if not _on(cfg, "allow_send_files"):
            return ("Sending files from this computer is switched off. Only the settings at the computer "
                    "(⚙ → TELEGRAM REMOTE) can turn it on.")
        raw = str(p.get("target") or "").strip().strip('"')
        if not raw:
            return "Which file? Give its full path."
        path = Path(raw).expanduser()
        try:
            ops.check_sendable(path)
        except Exception as e:
            return str(e)
        threading.Thread(target=_push_file, args=(path, targets), daemon=True).start()
        return f"Sending {path.name} to your phone."
    return (f"'{action}' is not an action for the Telegram remote. I can start, stop, report status, "
            f"send a screenshot, webcam picture, status, file or text, pair, revoke, or panic.")


def _push_pictures(action: str, which: str, targets: list) -> None:
    try:
        shots = ops.screenshot(which) if action == "send_screenshot" else [
            (ops.webcam(), f"Webcam · {time.strftime('%H:%M:%S')}")]
        for c in targets:
            for data, cap in shots:
                _push(c, "photo", data, "picture.jpg", cap)
        _step("screenshot pushed" if action == "send_screenshot" else "webcam picture pushed")
    except Exception as e:
        _report_failure(f"the picture could not be sent: {_clean(e)}")


def _push_file(path: Path, targets: list) -> None:
    try:
        data = path.read_bytes()
        for c in targets:
            _push(c, "document", data, path.name, path.name)
        _step("file pushed")
    except Exception as e:
        _report_failure(f"{path.name} could not be sent: {_clean(e)}")


def _report_failure(text: str) -> None:
    try:
        from plugins import _announce
        _announce.say(f"Sending to the user's phone over Telegram failed — {text}.")
    except Exception:
        _log(f"SYS: Telegram remote — {text}")


# ═════════════════════════════════════════════════════════════════════════════
#  start on launch (desktop only)
# ═════════════════════════════════════════════════════════════════════════════

def on_app_ready(ui) -> None:
    """Called once by main.py, on its own thread, when the window exists and
    this plugin is ON. Starts the bridge if 'start on launch' is set."""
    try:
        if not (_is_desktop() and _on(_cfg(), "start_on_launch") and _token()):
            return
        # on_text_command is attached just before plugins load; give the voice
        # session a moment, and let cloud_agent connect first if it is paired.
        deadline = time.time() + 60
        while time.time() < deadline and not callable(getattr(ui, "on_text_command", None)):
            time.sleep(1)
        time.sleep(5)
        if _state.get("running"):
            return
        ca = sys.modules.get("plugins.cloud_agent")
        if ca is not None and getattr(ca, "will_hold_telegram", lambda: False)():
            return                        # cloud_agent starts it once the server let go
        print(f"[TelegramRemote] start on launch: {_start(ui)}")
    except Exception as e:
        print(f"[TelegramRemote] auto-start failed: {e}")
