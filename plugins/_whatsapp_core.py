"""
plugins/_whatsapp_core.py — drive WhatsApp on this computer, and check every step.

Helper module (leading underscore: the plugin loader skips it). Used by
plugins/whatsapp_call.py (calls) and actions/send_message.py (messages):

    transport, why = get()          # (None, reason) when nothing can run here
    ok, title  = transport.open_chat("Mom")
    ok, reason = transport.call("voice")
    ok, reason = transport.hangup()
    sent, failure = transport.send_message_to("Mom", "on my way")

THREE TRANSPORTS
    WindowsDesktop   the WhatsApp desktop app on Windows, read and driven through
                     the accessibility tree (pywinauto, UIA backend).
    MacDesktop       the WhatsApp desktop app on macOS, read and driven through
                     the Accessibility API (pyobjc) by its stable AXIdentifiers.
    WebTransport     WhatsApp Web in a real Chrome/Edge window driven by
                     Playwright, with its own profile in config/whatsapp_web/
                     (git-ignored: it IS a linked WhatsApp session). Used on
                     Linux, on Windows/macOS when the desktop app is not
                     installed, and anywhere when set in settings.

THE RULE: NEVER ASSUME, ALWAYS READ BACK
    A wrong tap rings a real person. So the conversation that opened is read
    from the header and compared with the name that was asked for before any
    call button is pressed, and a message counts as sent only when the box it
    was typed into is empty again afterwards.

WHICH BUTTON IS WHICH
    WhatsApp labels its controls in the user's language. Known labels are
    matched first; when none match, the model is asked once to say which
    control starts a voice call, a video call, ends one, and the answer is
    cached per set of labels — so it is one request per WhatsApp language for
    the life of the install (core/gemini.py points here as the example).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from core.lazyimport import LazyModule, installed

NS = "whatsapp_call"
_OS = platform.system()
_BASE = Path(__file__).resolve().parent.parent
WEB_PROFILE = _BASE / "config" / "whatsapp_web"
WEB_URL = "https://web.whatsapp.com/"


def _configure_pyautogui(m):
    # This file settles on its own (it reads the screen back after each step),
    # so pyautogui's own pause would only add delay. See core/lazyimport.py.
    m.FAILSAFE = True
    m.PAUSE = 0.0


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")


# ═════════════════════════════════════════════════════════════════════════════
#  names
# ═════════════════════════════════════════════════════════════════════════════

_PUNCT = re.compile(r"[^\w]|_", re.UNICODE)


def fold(text: str) -> str:
    """A name reduced to what two spellings of it share: no accents, no
    punctuation or spaces, case-folded. core/appidentity.fold is the same rule."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _PUNCT.sub("", text).casefold()


def fold_words(text: str) -> list[str]:
    """The words of a name, each folded, empties dropped."""
    return [w for w in (fold(p) for p in re.split(r"\s+", str(text or ""))) if w]


def name_in_phrase(phrase: str, name: str) -> bool:
    """Is `name` in `phrase` as whole words — not cut out of a longer word?
    "Ali" is in "Ali Veli" but not in "Alican"."""
    words, want = fold_words(phrase), fold_words(name)
    if not want:
        return False
    n = len(want)
    return any(words[i:i + n] == want for i in range(len(words) - n + 1))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", str(text or ""))


def same_contact(asked: str, title: str) -> bool:
    """Does the open conversation's title belong to who was asked for?"""
    if not asked or not title:
        return False
    a, t = fold(asked), fold(title)
    if a and a == t:
        return True
    da, dt = _digits(asked), _digits(title)
    if len(da) >= 7 and len(dt) >= 7 and (da.endswith(dt[-9:]) or dt.endswith(da[-9:])):
        return True
    # "Mom" asked, "Mom ❤️" saved; "Ahmet" asked, "Ahmet Yılmaz" saved.
    return name_in_phrase(title, asked)


def clean_contact(spoken: str) -> str:
    """'my mom' -> 'mom'; 'call my dude' -> 'dude'. A possessive is not a name."""
    text = str(spoken or "").strip().strip("\"'")
    text = re.sub(r"^(?:call|ring|phone|video\s*call)\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:my|our|the)\s+", "", text, flags=re.I)
    return text.strip()


# ═════════════════════════════════════════════════════════════════════════════
#  which control is which
# ═════════════════════════════════════════════════════════════════════════════

# Lower-case fragments of labels WhatsApp is known to use. Kept to phrases
# that cannot mean anything else; everything else goes to the model.
_KNOWN = {
    "video": ("video call", "videoanruf", "appel vidéo", "videollamada", "videochamada",
              "görüntülü arama", "videochiamata", "videogesprek", "видеозвонок", "مكالمة فيديو"),
    "voice": ("voice call", "audio call", "sprachanruf", "appel vocal", "llamada de voz",
              "chamada de voz", "sesli arama", "chiamata vocale", "spraakoproep", "аудиозвонок",
              "голосовой звонок", "مكالمة صوتية"),
    "end": ("end call", "hang up", "leave call", "anruf beenden", "auflegen", "raccrocher",
            "finalizar llamada", "colgar", "encerrar chamada", "aramayı sonlandır", "termina chiamata",
            "завершить звонок", "إنهاء المكالمة"),
    "accept": ("accept", "answer", "annehmen", "accepter", "aceptar", "atender", "kabul et", "rispondi"),
    "decline": ("decline", "reject", "ablehnen", "refuser", "rechazar", "recusar", "reddet", "rifiuta"),
}
_EXCLUDE = ("mute", "camera", "kamera", "stumm", "sessiz", "silenc", "micro", "speaker")


def _heuristic(names: list[str]) -> dict:
    out = {k: None for k in _KNOWN}
    for name in names:
        low = name.lower()
        if any(x in low for x in _EXCLUDE):
            continue
        for key, frags in _KNOWN.items():
            if out[key] is None and any(f in low for f in frags):
                out[key] = name
                break
    return out


def classify_controls(names: list[str], need: str = "") -> dict:
    """{"video","voice","end","accept","decline"} -> one of `names` or None.

    Known labels first; the model only when the one control that is `need`ed
    was not recognised, and its answer is cached for that set of labels."""
    names = [n for n in dict.fromkeys(str(n).strip() for n in names) if n]
    found = _heuristic(names)
    if not names or (need and found.get(need)) or (not need and any(found.values())):
        return found
    key = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:20]
    cache = _cache_load()
    if key in cache:
        return {**found, **{k: v for k, v in cache[key].items() if v in names}}
    asked = _ask_model(names)
    if asked is None:
        return found
    cache[key] = asked
    _cache_save(cache)
    return {**found, **{k: v for k, v in asked.items() if v}}


def _ask_model(names: list[str]) -> dict | None:
    try:
        from core import gemini
    except Exception:
        return None
    listing = "\n".join(names)
    prompt = (
        "Below are the accessible names of the buttons in one WhatsApp window, in whatever language "
        "that WhatsApp uses:\n\n" + listing + "\n\n"
        "Answer with one JSON object with exactly the keys video, voice, end, accept, decline:\n"
        "video = the button that starts a video call; voice = the one that starts an audio call; "
        "end = the one that hangs up a call in progress; accept = the one that picks up an incoming "
        "call; decline = the one that refuses an incoming call.\n"
        "Each value is one name copied character for character from the list, or null. Mute, camera "
        "and speaker toggles, menus, search and contact names are none of these. If you are not sure, "
        "use null — a wrong answer rings or hangs up on a real person. No name may be used twice. "
        "Output the JSON object only."
    )
    data = gemini.as_json(prompt, tier=gemini.FAST)
    if not isinstance(data, dict):
        return None
    out, used = {}, set()
    for k in _KNOWN:
        v = data.get(k)
        if isinstance(v, str) and v in names and v not in used:
            out[k] = v
            used.add(v)
        else:
            out[k] = None
    return out


def _cache_load() -> dict:
    try:
        from plugins import _announce
        doc = _announce.load("whatsapp_controls", {}) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _cache_save(doc: dict) -> None:
    try:
        from plugins import _announce
        _announce.save("whatsapp_controls", dict(list(doc.items())[-30:]))
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  settings
# ═════════════════════════════════════════════════════════════════════════════

def settings() -> dict:
    out = {"transport": "auto", "app_name": "WhatsApp", "strict_match": True,
           "launch_wait": "8", "chat_wait": "6"}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in get_plugin_config(NS).items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _seconds(value, default: float) -> float:
    try:
        return max(1.0, min(60.0, float(value)))
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════════════════════
#  the transport interface
# ═════════════════════════════════════════════════════════════════════════════

class Transport:
    label = "WhatsApp"

    def open_chat(self, contact: str) -> tuple[bool, str]:
        """(True, title of the conversation that opened) or (False, reason)."""
        return False, "this transport cannot open a conversation"

    def call(self, kind: str = "voice") -> tuple[bool, str]:
        return False, "this transport cannot place calls"

    def hangup(self) -> tuple[bool, str]:
        return False, "this transport cannot end a call"

    def send_text(self, message: str) -> tuple[bool, str]:
        return False, "this transport cannot send messages"

    def diagnose(self) -> tuple[bool, str]:
        return False, "this transport has no self-check"

    def send_message_to(self, receiver: str, message: str) -> tuple[bool, str]:
        """Open the right conversation, prove it is the right one, type, send,
        and check the box emptied. (sent, failure-reason)."""
        receiver, message = str(receiver or "").strip(), str(message or "")
        if not receiver:
            return False, "there was nobody to send it to, so NOTHING was sent"
        if not message.strip():
            return False, "there was no message to send, so NOTHING was sent"
        ok, title = self.open_chat(clean_contact(receiver) or receiver)
        if not ok:
            return False, f"{receiver}'s conversation could not be opened ({title}), so NOTHING was sent"
        if not same_contact(clean_contact(receiver) or receiver, title):
            return False, (f"the conversation that opened was '{title}', not {receiver}'s, so NOTHING "
                           f"was sent")
        sent, why = self.send_text(message)
        if not sent:
            return False, f"the message could not be sent in {receiver}'s chat ({why}), so NOTHING was sent"
        return True, ""


# ═════════════════════════════════════════════════════════════════════════════
#  WhatsApp Web through Playwright
# ═════════════════════════════════════════════════════════════════════════════

_HEADER_JS = r"""
() => {
  const header = document.querySelector('#main header');
  if (!header) return null;
  let title = '', widest = 0;
  header.querySelectorAll('span[title], span[dir="auto"], h1, h2').forEach(el => {
    const b = el.getBoundingClientRect();
    const t = (el.getAttribute('title') || el.innerText || '').trim();
    if (t && b.width > widest) { widest = b.width; title = t; }
  });
  const buttons = [];
  header.querySelectorAll('button, [role="button"]').forEach(el => {
    const b = el.getBoundingClientRect();
    if (b.width < 4 || b.height < 4) return;
    const name = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (name) buttons.push(name);
  });
  return {title: title, buttons: buttons};
}
"""

_CALL_BUTTONS_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('button, [role="button"]').forEach(el => {
    if (el.closest('#main header')) return;
    const b = el.getBoundingClientRect();
    if (b.width < 4 || b.height < 4) return;
    const name = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (name) out.push(name);
  });
  return out;
}
"""

_SEARCH = ('#side div[contenteditable="true"]', '#side input[type="text"]',
           'div[contenteditable="true"][data-tab="3"]')
_COMPOSER = ('#main footer div[contenteditable="true"]',)


class _Worker:
    """Playwright's sync API belongs to the thread that started it, so every
    browser step runs on this one thread, with a time budget."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="whatsapp-web")
        self._thread.start()

    def _run(self):
        while True:
            fn, box, done = self._q.get()
            try:
                box["value"] = fn()
            except Exception as e:
                box["error"] = e
            done.set()

    def do(self, fn, timeout: float = 60.0):
        box, done = {}, threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise RuntimeError("the browser exceeded its time budget")
        if "error" in box:
            raise box["error"]
        return box.get("value")


class WebTransport(Transport):
    label = "WhatsApp Web"

    def __init__(self):
        self._w = _Worker()
        self._pw = self._ctx = self._page = None

    # -- browser ------------------------------------------------------------
    def _ensure(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        WEB_PROFILE.mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream"]
        last = None
        for channel in ("chrome", "msedge", None):
            try:
                kw = {"user_data_dir": str(WEB_PROFILE), "headless": False, "args": args,
                      "no_viewport": True, "permissions": ["microphone", "camera", "notifications"]}
                if channel:
                    kw["channel"] = channel
                self._ctx = self._pw.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:
                last = e
                self._ctx = None
        if self._ctx is None:
            raise RuntimeError("WhatsApp Web needs Google Chrome or Microsoft Edge installed (or run: "
                               f"playwright install chromium) — {last}")
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if "web.whatsapp.com" not in (self._page.url or ""):
            self._page.goto(WEB_URL, wait_until="domcontentloaded", timeout=45_000)
        return self._page

    def _logged_in(self, page, wait_s: float = 25.0) -> bool:
        try:
            page.wait_for_selector("#side", timeout=int(wait_s * 1000))
            return True
        except Exception:
            return False

    def _ready(self):
        page = self._ensure()
        if not self._logged_in(page):
            page.bring_to_front()
            raise RuntimeError("WhatsApp Web is not linked yet — a browser window is open with a QR code; "
                               "scan it from the phone (WhatsApp → Linked devices) and ask again")
        return page

    # -- steps --------------------------------------------------------------
    def open_chat(self, contact: str) -> tuple[bool, str]:
        wait = _seconds(settings().get("chat_wait"), 6.0)

        def work():
            page = self._ready()
            page.bring_to_front()
            box = None
            for sel in _SEARCH:
                loc = page.locator(sel).first
                if loc.count():
                    box = loc
                    break
            if box is None:
                return False, "the chat search box was not found"
            box.click()
            page.keyboard.press("Control+A" if _OS != "Darwin" else "Meta+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(contact)
            time.sleep(1.8)
            page.keyboard.press("Enter")
            deadline = time.time() + wait
            info = None
            while time.time() < deadline:
                info = page.evaluate(_HEADER_JS)
                if info and info.get("title"):
                    break
                time.sleep(0.3)
            if not info or not info.get("title"):
                return False, "the conversation header could not be read"
            return True, info["title"]
        try:
            return self._w.do(work, 90)
        except Exception as e:
            return False, str(e)

    def _press(self, name: str, in_header: bool) -> bool:
        page = self._page
        scope = page.locator("#main header") if in_header else page.locator("body")
        for attr in ("aria-label", "title"):
            loc = scope.locator(f"[{attr}={json.dumps(name)}]").first
            if loc.count():
                loc.click()
                return True
        return False

    def call(self, kind: str = "voice") -> tuple[bool, str]:
        def work():
            page = self._ready()
            info = page.evaluate(_HEADER_JS) or {}
            names = info.get("buttons") or []
            ctl = classify_controls(names, need=kind).get(kind)
            if not ctl:
                return False, (f"no {kind}-call button in this chat (the header offers: "
                               f"{', '.join(names) or 'nothing'})")
            if not self._press(ctl, in_header=True):
                return False, f"the button '{ctl}' could not be pressed"
            return True, ctl
        try:
            return self._w.do(work, 60)
        except Exception as e:
            return False, str(e)

    def hangup(self) -> tuple[bool, str]:
        def work():
            page = self._ready()
            names = page.evaluate(_CALL_BUTTONS_JS) or []
            ctl = classify_controls(names, need="end").get("end")
            if not ctl:
                return False, "no hang-up button is showing"
            return (True, ctl) if self._press(ctl, in_header=False) else (False, "the button could not be pressed")
        try:
            return self._w.do(work, 45)
        except Exception as e:
            return False, str(e)

    def send_text(self, message: str) -> tuple[bool, str]:
        def work():
            page = self._ready()
            box = page.locator(_COMPOSER[0]).first
            if not box.count():
                return False, "no message box in the open conversation"
            box.click()
            if (box.inner_text() or "").strip():
                page.keyboard.press("Control+A" if _OS != "Darwin" else "Meta+A")
                page.keyboard.press("Backspace")
            page.keyboard.insert_text(message)
            time.sleep(0.3)
            if fold(box.inner_text()) != fold(message):
                return False, "the message did not reach the box exactly"
            page.keyboard.press("Enter")
            for _ in range(20):
                time.sleep(0.25)
                if not (box.inner_text() or "").strip():
                    return True, ""
            return False, "Enter did not send it — the message is still in the box"
        try:
            return self._w.do(work, 60)
        except Exception as e:
            return False, str(e)

    def diagnose(self) -> tuple[bool, str]:
        def work():
            page = self._ensure()
            if not self._logged_in(page, 20):
                page.bring_to_front()
                return False, ("scan the QR code in the browser window that just opened (phone → Linked "
                               "devices), then press CHECK again")
            info = page.evaluate(_HEADER_JS)
            if not info:
                return True, "linked to your WhatsApp account; open any chat to check the call buttons"
            found = classify_controls(info.get("buttons") or [], need="voice")
            if not (found.get("voice") or found.get("video")):
                return True, ("linked, but no call buttons found in the open chat (WhatsApp Web offers "
                              "calls only in some browsers and regions)")
            return True, "linked, and the call buttons were found"
        try:
            return self._w.do(work, 90)
        except Exception as e:
            return False, str(e)


# ═════════════════════════════════════════════════════════════════════════════
#  WhatsApp desktop app on Windows (UI Automation)
# ═════════════════════════════════════════════════════════════════════════════

class WindowsDesktop(Transport):
    label = "WhatsApp desktop app (Windows)"

    def __init__(self):
        from pywinauto import Desktop   # noqa: F401  (fail early when missing)
        self._win = None

    def _window(self, launch: bool = True):
        from pywinauto import Desktop

        def find():
            for w in Desktop(backend="uia").windows(title_re=r"^WhatsApp", visible_only=True):
                try:
                    cls = (w.element_info.class_name or "").lower()
                    # A browser tab titled "WhatsApp" is not the app.
                    if any(b in cls for b in ("chrome_widgetwin_1",)) and "whatsapp" not in \
                            (w.element_info.name or "").lower():
                        continue
                    if w.rectangle().width() > 50:
                        return w
                except Exception:
                    continue
            return None
        win = find()
        if win is None and launch:
            print("[WhatsApp] bringing WhatsApp up.")
            try:
                os.startfile("whatsapp:")              # registered by the Store app
            except Exception:
                try:
                    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"])
                except Exception as e:
                    raise RuntimeError(f"WhatsApp could not be started ({e}) — start it by hand once") from None
            deadline = time.time() + _seconds(settings().get("launch_wait"), 8.0) + 10
            while time.time() < deadline and win is None:
                time.sleep(0.7)
                win = find()
        if win is None:
            raise RuntimeError("WhatsApp never showed a window")
        self._win = win
        return win

    def _focus(self, win) -> None:
        try:
            if win.is_minimized():
                win.restore()
            win.set_focus()
        except Exception as e:
            raise RuntimeError(f"WhatsApp would not come to the front ({e}) — refusing to type") from None
        time.sleep(0.3)

    def _foreground_is(self, win) -> bool:
        try:
            import win32gui
            return win32gui.GetForegroundWindow() == win.handle
        except Exception:
            return True

    def _paste(self, text: str) -> None:
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        time.sleep(0.1)
        if pyperclip.paste() != text:
            raise RuntimeError("the clipboard did not keep what was copied")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)
        if old is not None:
            threading.Timer(1.5, lambda: pyperclip.copy(old)).start()

    def _controls(self, win, top_fraction: float = 0.2) -> tuple[list, list]:
        """(buttons, texts) as (name, wrapper) pairs inside the top band of the
        conversation pane — the header."""
        rect = win.rectangle()
        band = rect.top + int(rect.height() * top_fraction)
        left = rect.left + int(rect.width() * 0.28)
        buttons, texts = [], []
        for el in win.descendants():
            try:
                info = el.element_info
                name = (info.name or "").strip()
                if not name:
                    continue
                r = el.rectangle()
                if r.top > band or r.left < left or r.width() < 3:
                    continue
                if info.control_type == "Button":
                    buttons.append((name, el))
                elif info.control_type in ("Text", "Hyperlink", "Group"):
                    texts.append((name, el, r.width()))
            except Exception:
                continue
        return buttons, texts

    def _title(self, win) -> str:
        buttons, texts = self._controls(win)
        labels = {n for n, _ in buttons}
        ctl = set(v for v in _heuristic(list(labels)).values() if v)
        cands = [(w, n) for n, _, w in texts if n not in ctl and len(n) < 80]
        # The header button that opens the contact's info often carries the name.
        cands += [(0, n) for n, _ in buttons if n not in ctl and len(n) < 80]
        cands.sort(reverse=True)
        return cands[0][1] if cands else ""

    def open_chat(self, contact: str) -> tuple[bool, str]:
        try:
            win = self._window()
            self._focus(win)
            if not self._foreground_is(win):
                return False, "WhatsApp would not come to the front"
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.4)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            self._paste(contact)
            time.sleep(1.6)
            if not self._foreground_is(win):
                return False, "focus left WhatsApp while searching"
            pyautogui.press("enter")
            deadline = time.time() + _seconds(settings().get("chat_wait"), 6.0)
            title, want = "", contact
            while time.time() < deadline:
                time.sleep(0.5)
                title = self._title(win)
                if title and same_contact(want, title):
                    break
            if not title:
                return False, "the conversation header could not be read"
            return True, title
        except Exception as e:
            return False, str(e)

    def _invoke(self, el) -> bool:
        try:
            el.invoke()
            return True
        except Exception:
            try:
                el.click_input()
                return True
            except Exception as e:
                print(f"[WhatsApp] could not press: {e}")
                return False

    def call(self, kind: str = "voice") -> tuple[bool, str]:
        try:
            win = self._win or self._window(launch=False)
            buttons, _ = self._controls(win)
            names = [n for n, _ in buttons]
            ctl = classify_controls(names, need=kind).get(kind)
            if not ctl:
                return False, f"no {kind}-call button in this chat (header offers: {', '.join(names) or 'nothing'})"
            el = next(e for n, e in buttons if n == ctl)
            return (True, ctl) if self._invoke(el) else (False, f"'{ctl}' could not be pressed")
        except Exception as e:
            return False, str(e)

    def hangup(self) -> tuple[bool, str]:
        from pywinauto import Desktop
        try:
            pairs = []
            for w in Desktop(backend="uia").windows(visible_only=True):
                try:
                    if "whatsapp" not in (w.element_info.name or "").lower():
                        continue
                    for el in w.descendants(control_type="Button"):
                        name = (el.element_info.name or "").strip()
                        if name:
                            pairs.append((name, el))
                except Exception:
                    continue
            ctl = classify_controls([n for n, _ in pairs], need="end").get("end")
            if not ctl:
                return False, "no hang-up button is showing"
            el = next(e for n, e in pairs if n == ctl)
            return (True, ctl) if self._invoke(el) else (False, "the hang-up button could not be pressed")
        except Exception as e:
            return False, str(e)

    def send_text(self, message: str) -> tuple[bool, str]:
        try:
            win = self._win or self._window(launch=False)
            self._focus(win)
            rect = win.rectangle()
            box = None
            for el in win.descendants(control_type="Edit"):
                r = el.rectangle()
                if r.top > rect.top + rect.height() * 0.7 and r.left > rect.left + rect.width() * 0.28:
                    box = el
            if box is None:
                return False, "no message box in the open conversation"
            box.click_input()
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            self._paste(message)
            if not self._foreground_is(win):
                return False, "the keyboard left WhatsApp with the message typed but unsent"
            pyautogui.press("enter")
            for _ in range(16):
                time.sleep(0.25)
                try:
                    if not str(box.get_value() or box.window_text() or "").strip():
                        return True, ""
                except Exception:
                    return True, ""
            return False, "Enter did not send it — the message is still in the box"
        except Exception as e:
            return False, str(e)

    def diagnose(self) -> tuple[bool, str]:
        try:
            win = self._window(launch=True)
        except Exception as e:
            return False, str(e)
        title = self._title(win)
        if not title:
            return False, "no conversation is open — click any normal chat and press CHECK again"
        names = [n for n, _ in self._controls(win)[0]]
        found = classify_controls(names, need="voice")
        if not (found.get("voice") or found.get("video")):
            return False, f"no call buttons in this chat (header offers: {', '.join(names) or 'nothing'})"
        return True, f"WhatsApp found; '{title}' is open and its call buttons were found"


# ═════════════════════════════════════════════════════════════════════════════
#  WhatsApp desktop app on macOS (Accessibility API)
# ═════════════════════════════════════════════════════════════════════════════
#
# The Mac app (net.whatsapp.WhatsApp, a Catalyst app) publishes stable
# AXIdentifiers for every control this file needs, in every UI language:
#
#   NavigationBar_NewChatButton      opens the contact picker
#   PickerView_SearchBar             its search field (focused, AXValue settable)
#   PickerView_ContactCell           one contact (AXPress opens the chat)
#   PickerView_CloseButton           closes the picker
#   NavigationBar_HeaderViewButton   the open conversation's name
#   NavigationBar_CallButton         voice call
#   NavigationBar_VideoCallButton    video call
#   ChatBar_ComposerTextView         the message box (AXValue readable)
#   menu item "end_call"             Call ▸ End Call
#
# Measured quirks that shape the code below:
#   * While WhatsApp is in the background it exposes NO windows to
#     Accessibility — it must be activated, then polled until one appears.
#   * Quartz Unicode key events do not reach WhatsApp's text fields; a
#     clipboard paste (⌘V) does, accents and emoji included.
#   * The chat-list search bar is unusable for automation (see open_chat).
#   * An empty field reports its placeholder in AXNumberOfCharacters; only
#     AXValue says what is really typed.

_MAC_BUNDLE = "net.whatsapp.WhatsApp"
_LRM = dict.fromkeys(map(ord, "‎‏⁨⁩‪‫‬"), None)


def _ax_clean(text) -> str:
    return str(text or "").translate(_LRM).strip()


class MacDesktop(Transport):
    label = "WhatsApp desktop app (macOS)"

    def __init__(self):
        import ApplicationServices  # noqa: F401  (fail early when missing)
        import Quartz               # noqa: F401
        import AppKit               # noqa: F401

    # ── accessibility primitives ────────────────────────────────────────────
    @staticmethod
    def _attr(node, name):
        import ApplicationServices as AS
        try:
            err, value = AS.AXUIElementCopyAttributeValue(node, name, None)
            return value if err == 0 else None
        except Exception:
            return None

    def _walk(self, root, budget_s: float = 4.0, limit: int = 4000):
        """(node, role, name, ident) breadth-first under `root`."""
        out, frontier = [], [root]
        end = time.monotonic() + budget_s
        while frontier and len(out) < limit and time.monotonic() < end:
            node = frontier.pop(0)
            role = str(self._attr(node, "AXRole") or "")
            name = _ax_clean(self._attr(node, "AXTitle") or self._attr(node, "AXDescription"))
            ident = _ax_clean(self._attr(node, "AXIdentifier"))
            out.append((node, role, name, ident))
            frontier.extend(self._attr(node, "AXChildren") or [])
        return out

    def _find(self, root, ident: str):
        for node, _role, _name, i in self._walk(root):
            if i == ident:
                return node
        return None

    def _app_el(self):
        import AppKit
        import ApplicationServices as AS
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(_MAC_BUNDLE)
        if not apps or len(apps) == 0:
            return None
        return AS.AXUIElementCreateApplication(apps[0].processIdentifier())

    def _frontmost(self, app) -> bool:
        """Is WhatsApp the app with the keyboard? Asked of the window server,
        not of WhatsApp: its own AXFrontmost goes False after some keystrokes
        while it is plainly still in front."""
        try:
            pid = 0
            import ApplicationServices as AS
            err, pid = AS.AXUIElementGetPid(app, None)
            from core import screenview
            front = screenview.foreground_pid()
            if front and pid:
                return front == pid
        except Exception:
            pass
        return bool(self._attr(app, "AXFrontmost"))

    def _window(self, launch: bool = True):
        """(app element, main window). Activates WhatsApp — it hides its windows
        from Accessibility while in the background."""
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            raise RuntimeError("macOS has not given this app Accessibility permission — allow it in "
                               "System Settings ▸ Privacy & Security ▸ Accessibility (the terminal "
                               "or app JARVIS was started from), then try again")
        app = self._app_el()
        if app is None and not launch:
            raise RuntimeError("WhatsApp is not running")
        r = subprocess.run(["open", "-b", _MAC_BUNDLE], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"WhatsApp could not be opened ({(r.stderr or '').strip() or r.returncode})")
        wait = _seconds(settings().get("launch_wait"), 8.0) if app is None else 4.0
        deadline = time.time() + wait
        while time.time() < deadline:
            app = app or self._app_el()
            if app is not None:
                win = self._attr(app, "AXMainWindow") or next(iter(self._attr(app, "AXWindows") or []), None)
                if win is not None and self._frontmost(app):
                    return app, win
            time.sleep(0.2)
        raise RuntimeError("WhatsApp did not show its window")

    @staticmethod
    def _center(node_attr, node) -> tuple[float, float] | None:
        import ApplicationServices as AS
        try:
            ok1, p = AS.AXValueGetValue(node_attr(node, "AXPosition"), AS.kAXValueCGPointType, None)
            ok2, s = AS.AXValueGetValue(node_attr(node, "AXSize"), AS.kAXValueCGSizeType, None)
            if ok1 and ok2 and s.width > 0 and s.height > 0:
                return p.x + s.width / 2, p.y + s.height / 2
        except Exception:
            pass
        return None

    def _click(self, app, node) -> bool:
        """A real click on `node` — only when the point is on screen and the
        window under it is WhatsApp's."""
        c = self._center(self._attr, node)
        if c is None:
            return False
        x, y = int(c[0]), int(c[1])
        w, h = pyautogui.size()
        if not (0 <= x < w and 0 <= y < h):
            return False
        try:
            from core import screenview
            import AppKit
            pid = screenview.window_pid_at(x, y)
            apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(_MAC_BUNDLE)
            if pid and apps and pid != apps[0].processIdentifier():
                return False
        except Exception:
            pass
        if not self._frontmost(app):
            return False
        pyautogui.click(x, y)
        return True

    @staticmethod
    def _key(code: int, command: bool = False, pause: float = 0.05) -> None:
        import Quartz
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
            if command:
                Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(pause)

    def _paste(self, app, text: str) -> bool:
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        time.sleep(0.05)
        if not self._frontmost(app):
            return False
        self._key(9, command=True)          # ⌘V (key position, layout-proof)
        time.sleep(0.35)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        return True

    def _title(self, win) -> str:
        node = self._find(win, "NavigationBar_HeaderViewButton")
        if node is None:
            return ""
        return _ax_clean(self._attr(node, "AXDescription") or self._attr(node, "AXTitle"))

    def _row_name(self, node) -> str:
        """A row reads "Anna, ‎2 unread messages": the marked tail is WhatsApp's
        annotation in the UI language, not part of the name."""
        raw = str(self._attr(node, "AXTitle") or self._attr(node, "AXDescription") or "")
        return _ax_clean(raw.split(", ‎", 1)[0])

    @staticmethod
    def _best(contact: str, rows: list) -> object | None:
        exact = [n for n, nm in rows if fold(nm) == fold(contact)]
        close = [n for n, nm in rows if same_contact(contact, nm)]
        return (exact or close or [None])[0]

    def _press_id(self, win, ident: str) -> bool:
        import ApplicationServices as AS
        node = self._find(win, ident)
        return node is not None and AS.AXUIElementPerformAction(node, "AXPress") == 0

    # ── the transport ───────────────────────────────────────────────────────
    def open_chat(self, contact: str) -> tuple[bool, str]:
        """Through New Chat's contact picker, not the chat-list search bar.
        Measured: that bar keeps old text after a chat opens, ignores
        AXSelectedTextRange, loses focus on ⌘A and, after one search, turns
        into "Ask Meta AI" where Return would send the name to Meta AI. The
        picker opens with an empty, focused text field every time."""
        import ApplicationServices as AS
        try:
            app, win = self._window()
            title = self._title(win)
            if title and same_contact(contact, title):
                return True, title                      # already open

            hit = None
            if self._press_id(win, "NavigationBar_NewChatButton"):
                field = None
                for _ in range(15):
                    time.sleep(0.2)
                    field = self._find(win, "PickerView_SearchBar")
                    if field is not None:
                        break
                if field is None:
                    return False, "WhatsApp's New Chat list did not open"
                if self._attr(field, "AXValue"):
                    AS.AXUIElementSetAttributeValue(field, "AXValue", "")
                if not self._attr(field, "AXFocused"):
                    AS.AXUIElementSetAttributeValue(field, "AXFocused", True)
                if not self._paste(app, contact):
                    self._press_id(win, "PickerView_CloseButton")
                    return False, "focus left WhatsApp while searching"
                if fold(self._attr(field, "AXValue")) != fold(contact):
                    self._press_id(win, "PickerView_CloseButton")
                    return False, "the name did not land in WhatsApp's search field"
                deadline = time.time() + _seconds(settings().get("chat_wait"), 6.0)
                while time.time() < deadline and hit is None:
                    time.sleep(0.4)
                    rows = [(n, self._row_name(n)) for n, r, _nm, i in self._walk(win, budget_s=2.0)
                            if i == "PickerView_ContactCell" and r == "AXButton"]
                    hit = self._best(contact, rows)
                if hit is None:
                    self._press_id(win, "PickerView_CloseButton")
                    time.sleep(0.4)

            if hit is None:
                # Groups are not in the contact picker; the chat list shows the
                # recent ones.
                rows = [(n, self._row_name(n)) for n, r, _nm, i in self._walk(win)
                        if r == "AXButton" and not i and self._row_name(n)]
                hit = self._best(contact, rows)
            if hit is None:
                return False, f"no contact or recent chat called {contact} in WhatsApp"

            AS.AXUIElementPerformAction(hit, "AXPress")
            title = ""
            deadline = time.time() + _seconds(settings().get("chat_wait"), 6.0)
            while time.time() < deadline:
                time.sleep(0.3)
                title = self._title(win)
                if title and same_contact(contact, title):
                    break
            if self._find(win, "PickerView_SearchBar") is not None:
                self._press_id(win, "PickerView_CloseButton")
            if not title:
                return False, "the conversation header could not be read"
            return True, title
        except Exception as e:
            return False, str(e)

    def send_text(self, message: str) -> tuple[bool, str]:
        import ApplicationServices as AS
        try:
            app, win = self._window(launch=False)
            box = self._find(win, "ChatBar_ComposerTextView")
            if box is None:
                return False, "no message box in the open conversation"
            if _ax_clean(self._attr(box, "AXValue")):
                AS.AXUIElementSetAttributeValue(box, "AXValue", "")   # a leftover draft
                time.sleep(0.2)
            if not self._click(app, box):
                AS.AXUIElementSetAttributeValue(box, "AXFocused", True)
            time.sleep(0.25)
            if not self._attr(box, "AXFocused"):
                return False, "the message box did not take the keyboard"
            if not self._paste(app, message):
                return False, "the keyboard left WhatsApp before the message was typed"
            typed = str(self._attr(box, "AXValue") or "").replace("\r", "")
            if typed.strip() != message.replace("\r", "").strip():
                AS.AXUIElementSetAttributeValue(box, "AXValue", "")
                return False, "the text did not land in the message box"
            # The mic button turns into Send once there is text. Pressing it
            # through Accessibility needs no keyboard focus; a synthetic Return
            # is measured NOT to reach this box, so it is only the fallback.
            if not self._press_id(win, "ChatBar_SendButton"):
                if not self._frontmost(app):
                    return False, "the keyboard left WhatsApp with the message typed but unsent"
                self._key(36)                             # Return
            for _ in range(16):
                time.sleep(0.25)
                if not str(self._attr(box, "AXValue") or "").strip():
                    return True, ""
            return False, "Return did not send it — the message is still in the box"
        except Exception as e:
            return False, str(e)

    def _header_buttons(self, win) -> list[tuple[str, str, object]]:
        return [(nm, i, n) for n, r, nm, i in self._walk(win)
                if r == "AXButton" and i.startswith("NavigationBar_") and nm]

    def call(self, kind: str = "voice") -> tuple[bool, str]:
        import ApplicationServices as AS
        try:
            _app, win = self._window(launch=False)
            buttons = self._header_buttons(win)
            want = "NavigationBar_VideoCallButton" if kind == "video" else "NavigationBar_CallButton"
            pick = next(((nm, n) for nm, i, n in buttons if i == want), None)
            if pick is None:
                names = [nm for nm, _, _ in buttons]
                ctl = classify_controls(names, need=kind).get(kind)
                pick = next(((nm, n) for nm, _, n in buttons if nm == ctl), None) if ctl else None
            if pick is None:
                return False, (f"no {kind}-call button in this chat (header offers: "
                               f"{', '.join(nm for nm, _, _ in buttons) or 'nothing'})")
            err = AS.AXUIElementPerformAction(pick[1], "AXPress")
            return (True, pick[0]) if err == 0 else (False, f"'{pick[0]}' could not be pressed")
        except Exception as e:
            return False, str(e)

    def hangup(self) -> tuple[bool, str]:
        import ApplicationServices as AS
        try:
            app = self._app_el()
            if app is None:
                return False, "WhatsApp is not running"
            items = self._walk(app, budget_s=4.0)
            end = next((n for n, r, nm, i in items if i == "end_call"
                        and self._attr(n, "AXEnabled")), None)
            if end is None:
                names = [nm for _n, r, nm, _i in items if r == "AXButton" and nm]
                ctl = classify_controls(names, need="end").get("end")
                end = next((n for n, r, nm, _i in items if r == "AXButton" and nm == ctl), None) if ctl else None
            if end is None:
                return False, "no call is in progress in WhatsApp"
            err = AS.AXUIElementPerformAction(end, "AXPress")
            return (True, "End Call") if err == 0 else (False, "the hang-up control could not be pressed")
        except Exception as e:
            return False, str(e)

    def diagnose(self) -> tuple[bool, str]:
        try:
            _app, win = self._window(launch=True)
        except Exception as e:
            return False, str(e)
        title = self._title(win)
        if not title:
            return False, "no conversation is open — click any normal chat and press CHECK again"
        if self._find(win, "ChatBar_ComposerTextView") is None:
            return False, f"'{title}' is open but its message box could not be found"
        ids = {i for _, i, _ in self._header_buttons(win)}
        if not ({"NavigationBar_CallButton", "NavigationBar_VideoCallButton"} & ids):
            # e.g. "Message yourself" has none — messaging is still fine.
            return True, (f"WhatsApp found and messages work; '{title}' shows no call buttons, "
                          f"so open a contact's chat to check calls")
        return True, f"WhatsApp found; '{title}' is open, messages and call buttons work"


def _mac_app_installed() -> bool:
    try:
        import AppKit
        return AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(
            _MAC_BUNDLE) is not None
    except Exception:
        return Path("/Applications/WhatsApp.app").exists()


# ═════════════════════════════════════════════════════════════════════════════
#  choosing a transport
# ═════════════════════════════════════════════════════════════════════════════

_cache: dict = {}
_cache_lock = threading.Lock()


def _web() -> tuple[Transport | None, str]:
    if not installed("playwright"):
        return None, "playwright is not installed — run: pip install playwright"
    with _cache_lock:
        if "web" not in _cache:
            _cache["web"] = WebTransport()
        return _cache["web"], ""


def _mac_desktop() -> tuple[Transport | None, str]:
    missing = [m for m in ("ApplicationServices", "Quartz", "AppKit", "pyautogui", "pyperclip")
               if not installed(m)]
    if missing:
        return None, ("the macOS desktop transport needs pyobjc — pip install "
                      "pyobjc-framework-ApplicationServices pyautogui pyperclip")
    if not _mac_app_installed():
        return None, "the WhatsApp desktop app is not installed (get it from the Mac App Store)"
    with _cache_lock:
        if "mac" not in _cache:
            try:
                _cache["mac"] = MacDesktop()
            except Exception as e:
                return None, f"could not load that transport: {e}"
        return _cache["mac"], ""


def _desktop() -> tuple[Transport | None, str]:
    if _OS == "Darwin":
        return _mac_desktop()
    if _OS != "Windows":
        return None, ("there is no WhatsApp desktop driver for this system — switch the WhatsApp "
                      "transport to 'web' in settings")
    missing = [m for m in ("pywinauto", "pyautogui", "pyperclip") if not installed(m)]
    if missing:
        return None, "the desktop transport is unavailable — pip install " + " ".join(missing)
    with _cache_lock:
        if "desktop" not in _cache:
            try:
                _cache["desktop"] = WindowsDesktop()
            except Exception as e:
                return None, f"could not load that transport: {e}"
        return _cache["desktop"], ""


def get() -> tuple[Transport | None, str]:
    """The transport to use here, or (None, why nothing can reach WhatsApp)."""
    choice = str(settings().get("transport") or "auto").lower()
    if choice == "web":
        return _web()
    if choice == "desktop":
        return _desktop()
    if _OS in ("Windows", "Darwin"):
        # The desktop app the user already has open beats a second WhatsApp
        # session in a browser (which needs its own QR link and a network
        # round-trip to load before it can do anything).
        t, why = _desktop()
        if t is not None:
            return t, ""
        w, why2 = _web()
        return (w, "") if w is not None else (None, f"{why}; {why2}")
    return _web()
