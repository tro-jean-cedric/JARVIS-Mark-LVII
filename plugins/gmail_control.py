"""
plugins/gmail_control.py — Gmail by voice: check, read, search, send, draft,
mark as read.

Sign-in is shared with the other Google plugins through plugins/_google_core.py
(⚙ → PLUGIN SETTINGS → Google). Sending is irreversible, so it goes through
core.confirm: the user presses CONFIRM on the HUD before anything leaves.
Drafts and mark-as-read are reversible and happen at once.
"""
from __future__ import annotations

import base64
import html
import re
import threading
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from plugins import _google_core as g

PLUGIN = {
    "name": "gmail_control",
    "description": (
        "Reads, searches, sends and drafts the user's GMAIL. Use for: 'check my "
        "email', 'any new mail?', 'read my latest email', 'anything from <person>?', "
        "'search my mail for <topic>', 'send an email to <person> saying ...', "
        "'draft an email to ...', 'mark it as read'. Put Gmail search syntax in "
        "'query' (e.g. 'from:anna', 'invoice newer_than:7d'). Only use action='send' "
        "when the user clearly says send; otherwise use 'draft'. Sending asks the "
        "user to confirm on screen."
    ),
    "brief": "Check, read, search, draft and send Gmail.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "check (unread summary, default), read (one message), "
                                      "search, send, draft, mark_read."},
            "query": {"type": "STRING",
                      "description": "For read/search/mark_read: Gmail search terms or the "
                                     "user's words, e.g. 'from:anna', 'invoice', "
                                     "'is:unread newer_than:2d'."},
            "sender": {"type": "STRING",
                       "description": "Optional: a person or address the mail is from."},
            "to": {"type": "STRING",
                   "description": "For send/draft: recipient email address(es), comma separated. "
                                  "A name is looked up in recent mail."},
            "subject": {"type": "STRING", "description": "For send/draft: subject line."},
            "body": {"type": "STRING",
                     "description": "For send/draft: the message text, in the user's language."},
            "count": {"type": "INTEGER",
                      "description": "How many messages to list for check/search (default 5)."},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = g.PLUGIN_SETTINGS_SHARED

SPEAK_BODY_CHARS = 600
SHOW_BODY_CHARS = 3800
_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")

_last_lock = threading.Lock()
_last_ids: list[str] = []     # messages most recently read / listed, newest first


# ── Query building ───────────────────────────────────────────────────────────

_PHRASES = [
    (r"\bunread\b|\bnew\b", "is:unread"),
    (r"\bstarred\b", "is:starred"),
    (r"\bimportant\b", "is:important"),
    (r"\b(with|has|having) (an )?attachments?\b", "has:attachment"),
    (r"\btoday\b", "newer_than:1d"),
    (r"\byesterday\b", "newer_than:2d"),
    (r"\bthis week\b|\blast week\b|\bpast week\b", "newer_than:7d"),
    (r"\bthis month\b|\blast month\b|\bpast month\b", "newer_than:31d"),
]
_FILLER = {"email", "emails", "mail", "mails", "message", "messages", "my", "the", "a",
           "an", "any", "for", "about", "me", "show", "find", "search", "in", "inbox",
           "please", "all", "some", "of", "is", "are", "there", "do", "i", "have", "from"}
_TIME_WORDS = {"today", "yesterday", "this", "last", "past", "the", "my", "a", "an"}


def build_query(text: str, sender: str = "") -> str:
    """Natural words → Gmail search syntax. Text that already uses operators
    (from:, subject:, is:...) is passed through untouched."""
    text = (text or "").strip()
    parts: list[str] = []
    if sender.strip():
        s = sender.strip()
        parts.append(f"from:{s}" if " " not in s else f'from:"{s}"')
    if not text:
        return " ".join(parts)
    if re.search(r"\b[a-z_]+:\S", text) or '"' in text:
        # The model writes operators glued together — 'subject:"LinkedIn"is:unread'
        # was observed — which Gmail reads as one odd term. Split them.
        text = re.sub(r'(")(?=[a-z_]+:)', r"\1 ", text)
        text = re.sub(r"(\S)(?=(?:from|to|subject|is|in|label|has|after|before|"
                      r"newer_than|older_than|category|filename):)", r"\1 ", text)
        return " ".join(parts + [text])
    low = text.lower()
    m = re.search(r"\bfrom ([\w.@+'-]+(?: [A-Z][\w'-]+)?)", text)
    if m and m.group(1).lower() in _TIME_WORDS:
        m = None
    if m and not sender:
        who = m.group(1)
        parts.append(f"from:{who}" if " " not in who else f'from:"{who}"')
        low = low.replace(m.group(0).lower(), " ")
    m = re.search(r"\bto ([\w.@+'-]+@[\w.-]+)", low)
    if m:
        parts.append(f"to:{m.group(1)}")
        low = low.replace(m.group(0), " ")
    for pat, op in _PHRASES:
        if re.search(pat, low):
            if op not in parts:
                parts.append(op)
            low = re.sub(pat, " ", low)
    words = [w for w in re.findall(r"[\w@.'-]+", low) if w not in _FILLER]
    return " ".join(parts + words).strip()


# ── Message helpers ──────────────────────────────────────────────────────────

def _headers(msg: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (msg.get("payload", {}) or {}).get("headers", []) or []}


def _sender_name(raw: str) -> str:
    name, addr = parseaddr(raw or "")
    return (name or addr or "someone").strip().strip('"')


def _when(raw: str) -> str:
    try:
        dt = parsedate_to_datetime(raw).astimezone()
        return dt.strftime("%a %d %b %H:%M")
    except Exception:
        return ""


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _html_to_text(s: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(s, "html.parser").get_text("\n")
    except Exception:
        s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
        s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s)
        return html.unescape(re.sub(r"<[^>]+>", " ", s))


def extract_body(payload: dict) -> str:
    """Plain text of a message payload: text/plain preferred, else HTML stripped."""
    plain, rich = [], []

    def walk(p):
        mime = (p.get("mimeType") or "").lower()
        data = (p.get("body") or {}).get("data")
        if data and not (p.get("filename") or ""):
            if mime == "text/plain":
                plain.append(_b64(data))
            elif mime == "text/html":
                rich.append(_b64(data))
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload or {})
    text = "\n".join(plain) if plain else _html_to_text("\n".join(rich))
    return clean_text(text)


def clean_text(text: str) -> str:
    lines, out = text.replace("\r", "").split("\n"), []
    for ln in lines:
        s = ln.strip()
        if s.startswith(">"):                 # quoted reply history
            continue
        if re.match(r"^On .+ wrote:$", s):    # start of the quoted thread
            break
        out.append(s)
    text = "\n".join(out)
    text = re.sub(r"https?://\S{40,}", "[link]", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    if end > limit * 0.6:
        cut = cut[:end + 1]
    return cut.rstrip() + " …"


def _remember(ids: list[str]) -> None:
    with _last_lock:
        _last_ids[:] = list(ids)[:25]


def _list(svc, query: str, count: int) -> tuple[list[dict], int]:
    resp = svc.users().messages().list(userId="me", q=query or None,
                                       maxResults=max(1, min(count, 50))).execute()
    ids = [m["id"] for m in resp.get("messages", []) or []]
    msgs = []
    for mid in ids:
        msgs.append(svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute())
    return msgs, int(resp.get("resultSizeEstimate") or len(ids))


def _summary_lines(msgs: list[dict]) -> list[str]:
    out = []
    for m in msgs:
        h = _headers(m)
        out.append(f"• {_sender_name(h.get('from'))} — {h.get('subject') or '(no subject)'}"
                   f"  [{_when(h.get('date', ''))}]\n  {html.unescape(m.get('snippet', ''))[:160]}")
    return out


def _spoken_list(msgs: list[dict], limit: int = 5) -> str:
    bits = []
    for m in msgs[:limit]:
        h = _headers(m)
        bits.append(f"{_sender_name(h.get('from'))}: {h.get('subject') or 'no subject'}")
    return "; ".join(bits)


def _show(player, title: str, text: str) -> None:
    if player is None:
        return
    try:
        player.show_content(title, text[:SHOW_BODY_CHARS])
    except Exception:
        pass


# ── Actions ──────────────────────────────────────────────────────────────────

def _check(svc, params, player) -> str:
    count = _count(params)
    extra = build_query(params.get("query", ""), params.get("sender", ""))
    q = " ".join(x for x in ("is:unread in:inbox", extra) if x)
    msgs, total = _list(svc, q, count)
    _remember([m["id"] for m in msgs])
    if not msgs:
        return "You have no unread email" + (" matching that." if extra else " in your inbox.")
    _show(player, f"Unread mail ({total})", "\n".join(_summary_lines(msgs)))
    n = max(total, len(msgs))
    return f"You have {'about ' if n > len(msgs) else ''}{n} unread. Latest: {_spoken_list(msgs)}."


def _search(svc, params, player) -> str:
    q = build_query(params.get("query", ""), params.get("sender", ""))
    if not q:
        return "What should I search your mail for?"
    msgs, total = _list(svc, q, _count(params))
    _remember([m["id"] for m in msgs])
    if not msgs:
        return "I couldn't find any emails matching that."
    _show(player, f"Mail: {q}"[:48], "\n".join(_summary_lines(msgs)))
    return f"I found {total if total > len(msgs) else len(msgs)} matching. " \
           f"Most recent: {_spoken_list(msgs)}."


def _read(svc, params, player) -> str:
    q = build_query(params.get("query", ""), params.get("sender", ""))
    mid = None
    if not q:
        with _last_lock:
            mid = _last_ids[0] if _last_ids else None
        if mid is None:
            q = "in:inbox"
    if mid is None:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
        ids = [m["id"] for m in resp.get("messages", []) or []]
        if not ids:
            return "I couldn't find an email to read."
        mid = ids[0]
    msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    _remember([mid])
    h = _headers(msg)
    body = extract_body(msg.get("payload") or {}) or html.unescape(msg.get("snippet", ""))
    who, subj = _sender_name(h.get("from")), h.get("subject") or "(no subject)"
    _show(player, subj[:48],
          f"From: {h.get('from', '')}\nDate: {_when(h.get('date', ''))}\n"
          f"Subject: {subj}\n\n{trim(body, SHOW_BODY_CHARS - 300)}")
    unread = "UNREAD" in (msg.get("labelIds") or [])
    tail = " It's still marked unread." if unread else ""
    return (f"Email from {who}, subject '{subj}', {_when(h.get('date', ''))}. "
            f"It says: {trim(body, SPEAK_BODY_CHARS) or '(no text)'}{tail}")


def _mark_read(svc, params, player) -> str:
    q = build_query(params.get("query", ""), params.get("sender", ""))
    if q:
        n = _count(params, default=25)
        resp = svc.users().messages().list(userId="me", q=f"is:unread {q}",
                                           maxResults=n).execute()
        ids = [m["id"] for m in resp.get("messages", []) or []]
    else:
        with _last_lock:
            ids = list(_last_ids[:1])
    if not ids:
        return "There's nothing matching to mark as read."
    svc.users().messages().batchModify(
        userId="me", body={"ids": ids, "removeLabelIds": ["UNREAD"]}).execute()
    _push_undo(ids)
    return f"Marked {len(ids)} email{'s' if len(ids) != 1 else ''} as read."


def _push_undo(ids: list[str]) -> None:
    try:
        from core.undo import push_undo
    except Exception:
        return

    def undo():
        svc, err = g.service("gmail")
        if svc is None:
            return err
        try:
            svc.users().messages().batchModify(
                userId="me", body={"ids": ids, "addLabelIds": ["UNREAD"]}).execute()
            return "Marked them unread again."
        except Exception as e:
            return g.explain_error(e, "gmail")

    push_undo(f"marked {len(ids)} email(s) read", undo)


def resolve_recipients(svc, raw: str) -> tuple[list[str], str]:
    """Addresses from what the user said. Names are looked up in recent mail.
    Returns (addresses, problem)."""
    raw = (raw or "").strip()
    if not raw:
        return [], "Who should I send it to? I need a recipient email address."
    out: list[str] = []
    for part in re.split(r"[;,]| and ", raw):
        part = part.strip()
        if not part:
            continue
        found = _EMAIL_RE.findall(part)
        if found:
            out.extend(found)
            continue
        addr = _lookup_address(svc, part) if svc is not None else ""
        if not addr:
            return [], (f"I don't have an email address for {part}. "
                        "Tell me the address and I'll use it.")
        out.append(addr)
    return out, ""


def _lookup_address(svc, name: str) -> str:
    want = name.lower().strip()
    for q in (f'from:"{name}"', f'to:"{name}"'):
        try:
            resp = svc.users().messages().list(userId="me", q=q, maxResults=5).execute()
        except Exception:
            continue
        for m in resp.get("messages", []) or []:
            try:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "To", "Cc"]).execute()
            except Exception:
                continue
            h = _headers(full)
            # Empty headers must be left out: newer Pythons parse the joined
            # list strictly and return nothing at all when one is blank.
            fields = [v for v in (h.get("from"), h.get("to"), h.get("cc")) if v]
            for disp, addr in getaddresses(fields):
                if addr and (want in disp.lower() or want in addr.lower().split("@")[0]):
                    return addr
    return ""


def _compose(to: list[str], subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _send_or_draft(svc, params, player, send: bool) -> str:
    to, problem = resolve_recipients(svc, params.get("to", ""))
    if problem:
        return problem
    subject = (params.get("subject") or "").strip()
    body = (params.get("body") or "").strip()
    if not body and not subject:
        return "What should the email say?"
    if not subject:
        subject = trim(body.split("\n")[0], 60).rstrip(" …") or "(no subject)"
    raw = _compose(to, subject, body)

    if not send:
        svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        if player is not None:
            _show(player, "Draft saved", f"To: {', '.join(to)}\nSubject: {subject}\n\n{body}")
        return f"I saved a draft to {', '.join(to)} with the subject '{subject}'."

    try:
        from core import confirm
    except Exception:
        return "I can't ask you to confirm right now, so I haven't sent it. I can save it as a draft."

    def do_send() -> str:
        s, err = g.service("gmail")
        if s is None:
            return err
        try:
            s.users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as e:
            msg = g.explain_error(e, "gmail")
            _say(f"The email to {', '.join(to)} was NOT sent: {msg}")
            return msg
        _say(f"The email to {', '.join(to)} was sent.")
        return "Sent."

    _show(player, "Send this email?", f"To: {', '.join(to)}\nSubject: {subject}\n\n{body}")
    detail = f"To {', '.join(to)} — {subject}: {trim(body, 140)}"
    return confirm.request("gmail_send", f"Send email to {', '.join(to)}"[:80], detail, do_send)


def _say(text: str) -> None:
    try:
        from plugins import _announce
        _announce.say(text)
    except Exception:
        print(f"[gmail] {text}")


def _count(params, default: int = 5) -> int:
    try:
        n = int(params.get("count") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, 25))


_ACTIONS = {
    "check": _check, "unread": _check, "new": _check,
    "read": _read, "open": _read, "latest": _read,
    "search": _search, "find": _search, "list": _search,
    "mark_read": _mark_read, "mark": _mark_read, "markread": _mark_read,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    try:
        from plugins import _announce
        _announce.remember(player)
    except Exception:
        pass
    action = str(params.get("action") or "check").strip().lower().replace(" ", "_").replace("-", "_")
    if action in ("help", "status", "setup"):
        return ("I can check, read, search, send, draft, or mark your email as read. "
                f"Connection: {g.status_text()}")
    if action in ("send", "draft", "compose", "write"):
        is_send = action == "send"
    elif action not in _ACTIONS:
        return "I can check, read, search, send, draft, or mark your email as read."
    svc, err = g.service("gmail", player)
    if svc is None:
        return err
    try:
        if action in ("send", "draft", "compose", "write"):
            return _send_or_draft(svc, params, player, send=is_send)
        return _ACTIONS[action](svc, params, player)
    except Exception as e:
        return g.explain_error(e, "gmail", player)
