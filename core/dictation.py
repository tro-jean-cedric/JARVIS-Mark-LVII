"""
core/dictation.py — the microphone types into the focused text field.

WHY NOT JUST ASK THE CONVERSATION MODEL TO TYPE IT
    Because a conversation model answers. Asked to "type what I say", Live
    paraphrases, translates, fixes the grammar, drops the "um" and, given a
    question, replies to it — and the reply is what lands in the email. Text
    entry needs the words as spoken, so while dictation is on the microphone
    is taken AWAY from the conversation (main.py's mic callback hands each
    block to `feed()` and sends nothing to the model) and phrases go to a
    speech-to-text engine with one instruction: write down exactly this.

HOW A PHRASE IS CUT
    An energy VAD with the pipeline engine's numbers (providers/
    engine_pipeline.py): the noise floor follows the quietest recent blocks,
    speech is 3.5x above it and never below 0.006, a phrase needs 250 ms of
    speech to count (a click, a cough, a keyboard clack does not), and 700 ms
    of quiet ends it. A phrase is typed when it ends, so text appears in the
    rhythm the user speaks in. A monologue is cut every 15 s so nothing waits
    for ever. In hold mode (walkie-talkie) there is no endpointing: everything
    held is one phrase, typed on release.

    12 s without any speech stops dictation by itself. A forgotten dictation
    otherwise types the next conversation in the room into whatever has focus.

THE ENGINE
    1. The Whisper the user already configured for the pipeline engine;
    2. else faster-whisper on this machine, if installed (model "base");
    3. else Gemini over REST with a verbatim-transcription instruction.
    The setting air_control.dictation_engine narrows this ("whisper" never
    falls back to the cloud: someone who chose local did not choose upload).
    Whisper invents "Thank you." out of short noise; those exact outputs are
    dropped when the phrase was under 0.6 s of speech.

SPOKEN COMMANDS
    "new line", "send it", "scratch that", "stop" (and French equivalents)
    are commands only when they are the WHOLE phrase. "add a new line here"
    is typed as text. Plain Enter is only ever pressed for "send it", because
    Enter in a chat box sends the message: "new line" is Shift+Enter, which
    breaks the line in chat inputs and plain text areas alike. Both check
    first that a (non-password) text field of another app has the keyboard,
    so a dialog's default button or JARVIS's own input is never pressed.
    "stop air control" (and "jarvis stop", "exit air control" ...) would
    otherwise be typed into the document, because the microphone is taken
    away from the conversation while dictating: it stops dictation and emits
    a "command" event named "exit", which the Air Control window turns into
    closing itself.

PRIVACY
    The log gets "typed N characters", never the words. A password field
    (the platform's own secure flag, see os_input.focused_text_field) is
    refused at start and stops a running dictation the moment it has focus.

NOTHING RAISES
    feed() runs inside the audio callback and must never throw or block: it
    measures one block, maybe appends it, maybe queues a phrase. All
    transcription and typing happens on one worker thread.
"""
from __future__ import annotations

import array
import asyncio
import io
import math
import queue
import re
import threading
import time
import unicodedata
import wave
from collections import deque
from typing import Any, Callable, Optional

from core import outcome

RATE = 16_000
SAMPLE_WIDTH = 2

SILENCE_MS = 700
MIN_SPEECH_MS = 250
MAX_SEG_S = 15.0
FLOOR_MARGIN = 3.5
ABS_MIN = 0.006
IDLE_STOP_S = 12.0
HOLD_MAX_S = 60.0          # walkie-talkie: a bound on memory, not an endpoint
PREROLL_S = 0.3            # keeps the first consonant of a phrase
TAIL_KEEP_MS = 200         # quiet kept after a phrase (the last consonant)
LEVEL_HZ = 15.0
MIN_SEG_S = 0.25
HALLUCINATION_MS = 600
_LEVEL_LO, _LEVEL_HI = 0.003, 0.08

_VERBATIM = ("Transcribe this speech exactly as spoken, in the language spoken. "
             "Do not translate, answer, summarise or add anything. Add normal "
             "punctuation. Output only the transcript, or nothing if there is "
             "no speech.")

_HALLUCINATIONS = {"thank you", "thanks for watching", "merci",
                   "thank you for watching"}

# Whole-phrase commands, folded (lowercase, no accents, no punctuation).
_COMMANDS = {
    "newline": ("new line", "newline", "new paragraph", "a la ligne",
                "nouveau paragraphe", "nouvelle ligne"),
    "send": ("send it", "press enter", "envoie", "envoyer"),
    "undo": ("scratch that", "delete that", "efface ca", "annule"),
    "stop": ("stop dictation", "stop", "arrete la dictee", "termine"),
    "exit": ("stop air control", "exit air control", "close air control",
             "quit air control", "end air control", "turn off air control",
             "jarvis stop", "stop jarvis", "jarvis stop air control",
             "arrete air control", "quitte air control", "ferme air control",
             "jarvis arrete", "arrete jarvis"),
}

# Tests replace this with a fake clock.
_now = time.monotonic

_lock = threading.RLock()
_state: dict[str, Any] = {"on": False, "hold": False, "source": "", "started": 0.0,
                          "engine": "", "pid": 0}
_vad: dict[str, Any] = {}
_buf = bytearray()
_jobs: "queue.Queue" = queue.Queue()
_typed: deque = deque(maxlen=20)       # (text as typed, pid)
_typed_total = 0
_gen = 0                               # bumped to drop queued phrases
_listeners: list[Callable[[str, dict], None]] = []
_worker: Optional[threading.Thread] = None
_hooks: dict[str, Any] = {"write_log": None, "is_speaking": None}
_whisper_local: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Small pure helpers
# ─────────────────────────────────────────────────────────────────────────────
def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _command(text: str) -> Optional[str]:
    """The command a whole phrase names, or None."""
    f = _fold(text)
    if not f:
        return None
    for name, phrases in _COMMANDS.items():
        if f in phrases:
            return name
    return None


_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_NO_SPACE_BEFORE = (",", ".", "!", "?", ";", ":", ")", "]", "}", "…", "'", "’",
                    "%", "。", "，", "！", "？")


def _keeps_capital(word: str) -> bool:
    w = re.sub(r"[^\w']", "", word)
    if not w:
        return True
    if w == "I" or w.startswith(("I'", "I’")):
        return True
    return sum(1 for c in w if c.isupper()) >= 2       # NASA, iOS, McDonald


def _join(before: str, text: str) -> str:
    """`text` ready to type right after `before` (what precedes the caret):
    a space where one is needed, a capital after a sentence end, and the
    capital Whisper puts on every phrase removed mid-sentence."""
    text = str(text or "").strip()
    if not text:
        return ""
    before = str(before or "")
    if not before:
        return text
    tail = before.rstrip(" \t")
    at_line_start = before.endswith(("\n", "\r"))
    ended = (not tail) or at_line_start or tail.endswith(_SENTENCE_END)
    first = text.split()[0]
    if ended:
        if text[0].islower():
            text = text[0].upper() + text[1:]
    elif text[0].isupper() and not _keeps_capital(first):
        text = text[0].lower() + text[1:]
    if before[-1].isspace() or text.startswith(_NO_SPACE_BEFORE):
        return text
    return " " + text


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split()).lower()


def _wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


def _measure(block) -> tuple[bytes, float, float]:
    """(int16 bytes, RMS in 0..1, milliseconds) of one microphone block."""
    if hasattr(block, "dtype") and hasattr(block, "reshape"):
        import numpy as np
        a = np.asarray(block).reshape(-1)
        if a.dtype != np.int16:
            a = a.astype(np.int16)
        data = a.tobytes()
        if a.size == 0:
            return b"", 0.0, 0.0
        x = a.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        return data, rms, a.size / RATE * 1000.0
    data = bytes(block or b"")
    if len(data) % 2:
        data = data[:-1]
    s = array.array("h")
    s.frombytes(data)
    if not s:
        return b"", 0.0, 0.0
    total = 0
    for v in s:
        total += v * v
    rms = math.sqrt(total / len(s)) / 32768.0
    return data, rms, len(s) / RATE * 1000.0


# ─────────────────────────────────────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────────────────────────────────────
def on_event(cb: Callable[[str, dict], None]) -> Callable[[], None]:
    """Listen for dictation events; returns the function that stops it.
    `cb(kind, data)` runs on the audio or worker thread: hand off to the GUI
    thread with a signal, never touch widgets from it."""
    with _lock:
        _listeners.append(cb)

    def unsubscribe() -> None:
        with _lock:
            try:
                _listeners.remove(cb)
            except ValueError:
                pass
    return unsubscribe


def _emit(kind: str, data: Optional[dict] = None) -> None:
    with _lock:
        targets = list(_listeners)
    for cb in targets:
        try:
            cb(kind, dict(data or {}))
        except Exception as e:
            print(f"[dictation] listener failed on {kind}: {e}")


def _log(text: str) -> None:
    fn = _hooks.get("write_log")
    if fn is None:
        return
    try:
        fn(text)
    except Exception:
        pass


def attach(write_log=None, is_speaking=None) -> None:
    """main.py hands over its log and its "is the assistant talking" flag."""
    _hooks["write_log"] = write_log
    _hooks["is_speaking"] = is_speaking


# ─────────────────────────────────────────────────────────────────────────────
#  The VAD (runs inside the audio callback)
# ─────────────────────────────────────────────────────────────────────────────
def _vad_reset(now: float) -> None:
    _buf.clear()
    _vad.update(floor=ABS_MIN, speech_ms=0.0, quiet_ms=0.0, started=False,
                last_speech=now, last_level=0.0)


def active() -> bool:
    return bool(_state["on"])


def feed(pcm_block) -> None:
    """One int16 mono 16 kHz block from the microphone. O(1) when off."""
    if not _state["on"]:
        return
    try:
        speaking = _hooks.get("is_speaking")
        if speaking is not None and speaking():
            return
        data, rms, ms = _measure(pcm_block)
        if not data:
            return
        now = _now()
        events: list[tuple[str, dict]] = []
        with _lock:
            if not _state["on"]:
                return
            _vad_step(data, rms, ms, now, events)
        for kind, payload in events:
            _emit(kind, payload)
        for kind, payload in events:
            if kind == "stopped":
                _log("SYS: dictation stopped after a long silence")
    except Exception as e:
        print(f"[dictation] feed: {e}")


def _queue_segment(events: list) -> None:
    if len(_buf) >= int(MIN_SEG_S * RATE) * SAMPLE_WIDTH:
        _jobs.put((bytes(_buf), float(_vad["speech_ms"]), _gen))
        events.append(("heard", {}))
    _buf.clear()
    _vad.update(speech_ms=0.0, quiet_ms=0.0, started=False)


def _vad_step(data: bytes, rms: float, ms: float, now: float, events: list) -> None:
    if now - _vad["last_level"] >= 1.0 / LEVEL_HZ:
        _vad["last_level"] = now
        level = min(1.0, max(0.0, (rms - _LEVEL_LO) / (_LEVEL_HI - _LEVEL_LO)))
        events.append(("level", {"level": level}))

    if rms < _vad["floor"]:
        _vad["floor"] = rms
    else:
        _vad["floor"] += (rms - _vad["floor"]) * 0.002
    threshold = max(_vad["floor"] * FLOOR_MARGIN, ABS_MIN)
    loud = rms >= threshold
    _buf.extend(data)

    if _state["hold"]:
        if loud:
            _vad["speech_ms"] += ms
            _vad["last_speech"] = now
        if len(_buf) >= int(HOLD_MAX_S * RATE) * SAMPLE_WIDTH:
            _queue_segment(events)
        return

    if loud:
        _vad["speech_ms"] += ms
        _vad["quiet_ms"] = 0.0
        if _vad["speech_ms"] >= MIN_SPEECH_MS:
            _vad["started"] = True
            _vad["last_speech"] = now
    elif not _vad["started"]:
        keep = int(RATE * PREROLL_S) * SAMPLE_WIDTH
        if len(_buf) > keep:
            del _buf[:len(_buf) - keep]
        _vad["speech_ms"] = 0.0
    else:
        _vad["quiet_ms"] += ms
        if _vad["quiet_ms"] >= SILENCE_MS:
            # The 700 ms of quiet that ended the phrase is not sent: it is
            # upload and recognition time for nothing, and silence is what
            # Whisper invents sentences from. 200 ms of it stays.
            extra = int((_vad["quiet_ms"] - TAIL_KEEP_MS) / 1000 * RATE) * SAMPLE_WIDTH
            if 0 < extra < len(_buf):
                del _buf[len(_buf) - extra:]
            _queue_segment(events)
            _vad["last_speech"] = now

    if _vad["started"] and len(_buf) >= int(MAX_SEG_S * RATE) * SAMPLE_WIDTH:
        _queue_segment(events)
        _vad["last_speech"] = now

    if not _vad["started"] and now - _vad["last_speech"] >= IDLE_STOP_S:
        # commit=True: nothing is in progress (not started), and phrases
        # already queued must still be typed.
        _stop_locked("silence", commit=True, events=events)


# ─────────────────────────────────────────────────────────────────────────────
#  Start / stop
# ─────────────────────────────────────────────────────────────────────────────
def _setting() -> str:
    try:
        from memory.config_manager import get_plugin_setting
        v = str(get_plugin_setting("air_control", "dictation_engine", "auto") or "auto")
        return v.strip().lower() if v.strip().lower() in ("auto", "whisper", "gemini") else "auto"
    except Exception:
        return "auto"


def _pipeline_whisper_id() -> str:
    try:
        from memory.config_manager import get_pipeline
        sid = str(get_pipeline().get("stt") or "")
        return sid if sid.partition("@")[0] == "whisper" else ""
    except Exception:
        return ""


def _engine_name() -> str:
    choice = _setting()
    if choice == "gemini":
        return "gemini"
    if _pipeline_whisper_id():
        return "whisper"
    try:
        from core.lazyimport import installed
        if installed("faster_whisper"):
            return "whisper"
    except Exception:
        pass
    return "whisper" if choice == "whisper" else "gemini"


def _begin(source: str, hold: bool, pid: int = 0) -> None:
    """Switch on (no checks); start() is the public way in."""
    global _typed_total
    with _lock:
        now = _now()
        _state.update(on=True, hold=bool(hold), source=str(source or "gesture"),
                      started=now, engine=_engine_name(), pid=int(pid or 0))
        _typed_total = 0
        _vad_reset(now)


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, name="dictation", daemon=True)
        _worker.start()


def start(source: str = "gesture", hold: bool = False) -> str:
    """Start dictating into the focused field. An outcome line."""
    try:
        if _state["on"]:
            return outcome.failed("dictation", "dictation is already on")
        from core import os_input
        field = os_input.focused_text_field()
        if field is None:
            return outcome.failed("dictation", "click into a text field first")
        if getattr(field, "secure", False):
            return outcome.failed("dictation",
                                  "that is a password field; type it on the keyboard")
        _begin(source, hold, getattr(field, "pid", 0))
        _ensure_worker()
        _emit("listening", {})
        _log("SYS: dictation on")
        return outcome.started(
            "dictation",
            note=("what the user says next is typed into the focused field and "
                  "is not for you; say nothing until they stop"))
    except Exception as e:
        return outcome.failed("dictation", f"dictation could not start: {e}")


def _stop_locked(why: str, commit: bool, events: list) -> None:
    global _gen
    if not _state["on"]:
        return
    if commit and _buf and (_state["hold"] or _vad.get("started")):
        _queue_segment(events)
    elif not commit:
        _gen += 1
    _buf.clear()
    _state.update(on=False, hold=False)
    events.append(("stopped", {"why": why}))


def _stop(why: str, commit: bool = True) -> None:
    events: list = []
    with _lock:
        _stop_locked(why, commit, events)
    for kind, payload in events:
        _emit(kind, payload)


def stop(commit: bool = True) -> str:
    """Stop dictating. With commit, a phrase in progress (or everything held
    in walkie-talkie mode) is still typed; without, queued phrases are
    dropped."""
    try:
        was_on = bool(_state["on"])
        _stop("user", commit)
        if was_on:
            _log("SYS: dictation off")
        return outcome.done("dictation", stopped=True)
    except Exception as e:
        return outcome.failed("dictation", f"dictation could not stop: {e}")


def state() -> dict:
    with _lock:
        return {"on": bool(_state["on"]), "hold": bool(_state["hold"]),
                "source": _state["source"], "engine": _state["engine"],
                "typed": int(_typed_total)}


# ─────────────────────────────────────────────────────────────────────────────
#  Editing
# ─────────────────────────────────────────────────────────────────────────────
def _reason(line: str) -> str:
    """The reason="..." of an outcome line, for an event the window shows."""
    m = re.search(r'reason="((?:[^"\\]|\\.)*)"', str(line or ""))
    return m.group(1) if m else ""


def _enter_target(os_input) -> tuple:
    """(field, why_not): Enter may only go to a text field of another app
    that is not a password field. Focus can move while dictation runs (a
    confirm dialog, a Finder selection, JARVIS's own input), and Enter there
    presses a default button, renames a file or submits the wrong thing."""
    field = os_input.focused_text_field()
    if field is None:
        return None, "no text field has the keyboard"
    if getattr(field, "secure", False):
        return None, "that is a password field; type it on the keyboard"
    try:
        if os_input.own_app_focused():
            return None, "JARVIS has the keyboard - click into your app first"
    except Exception:
        pass
    return field, ""


def send() -> str:
    """Press Enter in the focused field (asked for by name, never implied)."""
    try:
        from core import os_input
        field, why_not = _enter_target(os_input)
        if field is None:
            return outcome.failed("dictation", why_not)
        ok, why = os_input.key("enter")
        if not ok:
            return outcome.failed("dictation", why or "Enter could not be pressed")
        with _lock:
            _typed.clear()      # a sent message cannot be scratched
        return outcome.unverified("dictation", "Enter was pressed",
                                  note="whether the app sent it was not checked")
    except Exception as e:
        return outcome.failed("dictation", f"Enter could not be pressed: {e}")


def undo_last() -> str:
    """Delete the last dictated phrase, only in the field it went into."""
    try:
        from core import os_input
        with _lock:
            if not _typed:
                return outcome.failed("dictation", "nothing dictated to take back")
            chunk, pid = _typed[-1]
        field = os_input.focused_text_field()
        if field is None or int(getattr(field, "pid", 0) or 0) != int(pid or 0) \
                or getattr(field, "secure", False):
            return outcome.unverified("dictation", "focus moved; nothing deleted")
        n = len(unicodedata.normalize("NFC", chunk))
        before = os_input.focused_field_value()
        left = n
        while left > 0:
            k = min(50, left)
            ok, why = os_input.key("backspace", (), k)
            if not ok:
                return outcome.failed("dictation", why or "the keys could not be sent",
                                      deleted=n - left)
            left -= k
        with _lock:
            if _typed and _typed[-1] == (chunk, pid):
                _typed.pop()
        after = os_input.focused_field_value()
        if isinstance(before, str) and isinstance(after, str) \
                and len(after) == max(0, len(before) - n):
            return outcome.done("dictation", deleted=n)
        return outcome.unverified("dictation", "the field could not be re-read",
                                  deleted=n)
    except Exception as e:
        return outcome.failed("dictation", f"the phrase could not be deleted: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Transcription
# ─────────────────────────────────────────────────────────────────────────────
def _run_coro(coro):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


def _transcribe(pcm: bytes) -> Optional[str]:
    """The words in `pcm`, verbatim; "" for no speech; None on failure."""
    global _whisper_local
    choice = _setting()
    if choice in ("auto", "whisper"):
        sid = _pipeline_whisper_id()
        if sid:
            try:
                from memory.config_manager import get_provider_config
                from providers import registry
                stt = registry.build(sid, get_provider_config(sid))
                return str(_run_coro(stt.transcribe(pcm, RATE)) or "").strip()
            except Exception as e:
                print(f"[dictation] whisper ({sid}) failed: {e}")
        try:
            from core.lazyimport import installed
            if installed("faster_whisper"):
                if _whisper_local is None:
                    from providers.stt_whisper import WhisperSTT
                    _whisper_local = WhisperSTT({"backend": "local", "model": "base"})
                return str(_run_coro(_whisper_local.transcribe(pcm, RATE)) or "").strip()
        except Exception as e:
            print(f"[dictation] local whisper failed: {e}")
        if choice == "whisper":
            return None
    try:
        from core import gemini
        from google.genai import types
        resp = gemini.call(
            [types.Part.from_bytes(data=_wav(pcm), mime_type="audio/wav"), _VERBATIM],
            tier=gemini.FAST, timeout_ms=10_000)
        if resp is None:
            return None
        text = getattr(resp, "text", None)
        return str(text or "").strip()
    except Exception as e:
        print(f"[dictation] gemini transcription failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  The worker
# ─────────────────────────────────────────────────────────────────────────────
def _work() -> None:
    while True:
        try:
            job = _jobs.get()
        except Exception:
            continue
        try:
            _process(*job)
        except Exception as e:
            print(f"[dictation] phrase failed: {e}")
            _emit("failed", {"why": "the phrase could not be typed"})


def _before_caret(pid: int) -> str:
    with _lock:
        if _typed and _typed[-1][1] == pid:
            return _typed[-1][0]
    return ""


def _process(pcm: bytes, speech_ms: float, gen: int) -> None:
    global _typed_total
    if len(pcm) < int(MIN_SEG_S * RATE) * SAMPLE_WIDTH or gen != _gen:
        return
    text = _transcribe(pcm)
    if gen != _gen:
        return
    if text is None:
        _emit("failed", {"why": "didn't catch that"})
        return
    text = text.strip()
    if not text:
        return
    if _fold(text) in _HALLUCINATIONS and speech_ms < HALLUCINATION_MS:
        return

    from core import os_input
    cmd = _command(text)
    if cmd is not None:
        _emit("command", {"name": cmd})
        if cmd == "newline":
            field, why_not = _enter_target(os_input)
            if field is None:
                _emit("failed", {"why": why_not})
                return
            # Shift+Enter: a line break in chat inputs too, where plain Enter
            # would send the half-written message.
            ok, why = os_input.key("enter", ("shift",))
            if ok:
                with _lock:
                    _typed.append(("\n", int(getattr(field, "pid", 0) or 0)))
            else:
                _emit("failed", {"why": why or "the line break could not be typed"})
        elif cmd == "send":
            r = send()
            if outcome.is_problem(r):
                _emit("failed", {"why": _reason(r) or "Enter could not be pressed"})
        elif cmd == "undo":
            undo_last()
        elif cmd in ("stop", "exit"):
            _stop("command", commit=False)
        return

    field = os_input.focused_text_field()
    if field is None:
        _emit("failed", {"why": "no text field has the keyboard"})
        return
    if getattr(field, "secure", False):
        _stop("secure", commit=False)
        _emit("failed", {"why": "that is a password field; type it on the keyboard"})
        return
    pid = int(getattr(field, "pid", 0) or 0)
    out = _join(_before_caret(pid), text)
    if not out:
        return

    from core import screenact
    act = screenact.type_text(out)
    if not getattr(act, "ok", False):
        _emit("failed", {"why": str(getattr(act, "error", "") or "the text could not be typed")})
        return
    with _lock:
        _typed.append((out, pid))
        _typed_total += len(out)
    value = os_input.focused_field_value()
    verified = None if value is None else (_norm(text) in _norm(value))
    _emit("typed", {"chars": len(out), "verified": verified, "preview": text[:60]})
    _log(f"SYS: typed {len(out)} characters")
