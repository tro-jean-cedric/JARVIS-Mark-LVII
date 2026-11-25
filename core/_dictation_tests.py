"""
Self-test for dictation (core/dictation.py) and its microphone hook in main.py.

    python -m core._dictation_tests

Standard library only. Speech is synthesised (math.sin into array('h')), the
clock is a stand-in, and transcription, typing and the focused field are all
replaced: nothing is sent anywhere and nothing is typed.
"""
from __future__ import annotations

import array
import contextlib
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import dictation  # noqa: E402
from core import os_input  # noqa: E402
from core import screenact  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


# ─────────────────────────────────────────────────────────────────────────────
#  Synthetic audio and a fake clock
# ─────────────────────────────────────────────────────────────────────────────
RATE = dictation.RATE
BLOCK = 1024                                  # main.py's CHUNK_SIZE


def tone(ms: float, amp: float = 0.25, freq: float = 220.0) -> array.array:
    n = int(RATE * ms / 1000)
    return array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE))
                             for i in range(n)))


def silence(ms: float) -> array.array:
    return array.array("h", bytes(int(RATE * ms / 1000) * 2))


def speech(ms: float) -> array.array:
    """Syllables: 200 ms of voice, 80 ms of gap, like talking."""
    out = array.array("h")
    while len(out) < RATE * ms / 1000:
        out.extend(tone(200))
        out.extend(silence(80))
    return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def play(clock: Clock, samples: array.array) -> None:
    for i in range(0, len(samples), BLOCK):
        block = samples[i:i + BLOCK]
        clock.t += len(block) / RATE
        dictation.feed(block.tobytes())


def drain() -> list:
    out = []
    while not dictation._jobs.empty():
        out.append(dictation._jobs.get_nowait())
    return out


def fresh(hold: bool = False) -> Clock:
    dictation._stop("test", commit=False)
    drain()
    with dictation._lock:
        dictation._typed.clear()
    clock = Clock()
    dictation._now = clock
    dictation._begin("test", hold, pid=10)
    return clock


# ─────────────────────────────────────────────────────────────────────────────
def test_segments() -> None:
    clock = fresh()
    pcm = array.array("h")
    pcm.extend(silence(500))
    for _ in range(3):
        pcm.extend(speech(1200))
        pcm.extend(silence(1000))
    play(clock, pcm)
    jobs = drain()
    check("vad: three phrases with 1 s pauses -> three segments", len(jobs) == 3, str(len(jobs)))
    check("vad: each segment holds its phrase",
          all(1.0 <= len(j[0]) / 2 / RATE <= 2.2 for j in jobs),
          str([round(len(j[0]) / 2 / RATE, 2) for j in jobs]))
    check("vad: speech_ms is measured", all(j[1] >= 600 for j in jobs))

    clock = fresh()
    pcm = silence(1000)
    pcm.extend(tone(150, amp=0.6))
    pcm.extend(silence(1500))
    play(clock, pcm)
    check("vad: a 150 ms click gives no segment", len(drain()) == 0)

    clock = fresh()
    pcm = speech(20_000)
    pcm.extend(silence(1000))
    play(clock, pcm)
    jobs = drain()
    lens = [round(len(j[0]) / 2 / RATE, 1) for j in jobs]
    check("vad: a 20 s monologue is cut at MAX_SEG_S",
          len(jobs) == 2 and abs(lens[0] - dictation.MAX_SEG_S) < 0.3, str(lens))
    check("vad: still on after a long phrase", dictation.active())


def test_hold() -> None:
    clock = fresh(hold=True)
    pcm = array.array("h")
    for _ in range(3):
        pcm.extend(speech(800))
        pcm.extend(silence(1000))
    play(clock, pcm)
    check("hold: no endpointing while held", len(drain()) == 0)
    dictation.stop(commit=True)
    jobs = drain()
    check("hold: stop flushes one segment with everything held",
          len(jobs) == 1 and len(jobs[0][0]) / 2 / RATE > 5.0,
          str([len(j[0]) for j in jobs]))
    check("hold: off after stop", not dictation.active())

    clock = fresh(hold=True)
    play(clock, speech(800))
    dictation.stop(commit=False)
    check("hold: stop without commit drops it", len(drain()) == 0)


def test_idle_stop() -> None:
    events = []
    unsub = dictation.on_event(lambda k, d: events.append((k, d)))
    try:
        clock = fresh()
        play(clock, silence(dictation.IDLE_STOP_S * 1000 - 1000))
        check("idle: still on before IDLE_STOP_S", dictation.active())
        play(clock, silence(2000))
        check("idle: off after IDLE_STOP_S of silence", not dictation.active())
        check("idle: 'stopped' with why=silence",
              ("stopped", {"why": "silence"}) in events, str(events[-3:]))
        check("idle: level events are rate-limited",
              0 < sum(1 for k, _ in events if k == "level") <= dictation.LEVEL_HZ * 14)
    finally:
        unsub()


def test_commands() -> None:
    c = dictation._command
    check("cmd: 'New line.'", c("New line.") == "newline")
    check("cmd: 'add a new line here' is text", c("add a new line here") is None)
    check("cmd: 'New paragraph'", c("New paragraph") == "newline")
    check("cmd: 'À la ligne !'", c("À la ligne !") == "newline")
    check("cmd: 'Nouveau paragraphe.'", c("Nouveau paragraphe.") == "newline")
    check("cmd: 'Send it.'", c("Send it.") == "send")
    check("cmd: 'Press enter'", c("Press enter") == "send")
    check("cmd: 'Envoie.'", c("Envoie.") == "send")
    check("cmd: 'Scratch that.'", c("Scratch that.") == "undo")
    check("cmd: 'Efface ça'", c("Efface ça") == "undo")
    check("cmd: 'Annule.'", c("Annule.") == "undo")
    check("cmd: 'Stop.'", c("Stop.") == "stop")
    check("cmd: 'Arrête la dictée.'", c("Arrête la dictée.") == "stop")
    check("cmd: 'Terminé.'", c("Terminé.") == "stop")
    check("cmd: 'Stop the car.' is text", c("Stop the car.") is None)
    check("cmd: 'please send it' is text", c("please send it") is None)
    check("cmd: 'Stop air control.' is exit", c("Stop air control.") == "exit")
    check("cmd: 'Jarvis stop' is exit", c("Jarvis stop") == "exit")
    check("cmd: 'stop air control now please' is text",
          c("stop air control now please") is None)


def test_join() -> None:
    j = dictation._join
    table = [
        ("", "Hello world.", "Hello world."),
        ("Hello world.", "How are you?", " How are you?"),
        ("Hello world.", "how are you?", " How are you?"),
        ("I think", "That is fine.", " that is fine."),
        ("I think", "I agree.", " I agree."),
        ("I think", "NASA is great.", " NASA is great."),
        ("Hello\n", "next line.", "Next line."),
        ("Hello ", "world", "world"),
        ("Hello", ", world", ", world"),
        ("Wait", "...", "..."),
        ("Done!", "ok", " Ok"),
        ("x", "   ", ""),
    ]
    for before, text, want in table:
        got = j(before, text)
        check(f"join: {before!r} + {text!r}", got == want, f"got {got!r}, want {want!r}")


class Field(SimpleNamespace):
    pass


class Env:
    """Stand-ins for os_input, screenact.type_text and _transcribe."""

    def __init__(self, text="Hello world.", field=None, value=None, type_ok=True,
                 type_error="", own=False):
        self.text = text
        self.own = own
        self.field = field if field is not None else Field(pid=10, secure=False)
        self.value = value
        self.type_ok, self.type_error = type_ok, type_error
        self.typed: list[str] = []
        self.keys: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def focused(self):
        return self.field

    def type_text(self, text, **kw):
        if self.type_ok:
            self.typed.append(text)
        return SimpleNamespace(ok=self.type_ok, error=self.type_error, detail="")

    def key(self, name, mods=(), count=1):
        self.keys.append((name, tuple(mods), count))
        return True, ""

    @contextlib.contextmanager
    def on(self):
        with patched(os_input, focused_text_field=self.focused,
                     focused_field_value=lambda: self.value, key=self.key,
                     own_app_focused=lambda: self.own), \
                patched(screenact, type_text=self.type_text), \
                patched(dictation, _transcribe=lambda pcm: self.text):
            yield self


PCM = speech(900).tobytes()


def test_start_refusals() -> None:
    dictation._stop("test", commit=False)
    with Env(field=Field(pid=10, secure=True)).on():
        r = dictation.start()
        check("start: a password field is refused",
              r.startswith("status=failed") and "password" in r, r)
        check("start: still off", not dictation.active())
    with patched(os_input, focused_text_field=lambda: None):
        r = dictation.start()
        check("start: no field -> click into a text field first",
              r.startswith("status=failed") and "text field" in r, r)


def test_process() -> None:
    events = []
    unsub = dictation.on_event(lambda k, d: events.append((k, d)))
    try:
        # A password field takes focus mid-dictation.
        fresh()
        with Env(field=Field(pid=10, secure=True)) as env, env.on():
            dictation._process(PCM, 900, dictation._gen)
            check("secure: dictation stops", not dictation.active())
            check("secure: nothing typed", env.typed == [])
            check("secure: 'failed' emitted", any(k == "failed" for k, _ in events))

        # The assistant has the keyboard.
        events.clear()
        fresh()
        with Env(type_ok=False, type_error=screenact.SELF_FOCUS) as env, env.on():
            dictation._process(PCM, 900, dictation._gen)
            fails = [d for k, d in events if k == "failed"]
            check("self focus: 'failed' with screenact's reason",
                  fails and fails[-1]["why"] == screenact.SELF_FOCUS, str(fails))

        # Typed and verified.
        events.clear()
        fresh()
        with Env(value="Dear Sam, hello world.") as env, env.on():
            dictation._process(PCM, 900, dictation._gen)
            typed = [d for k, d in events if k == "typed"]
            check("typed: text goes to the field", env.typed == ["Hello world."], str(env.typed))
            check("typed: event with chars, verified, preview",
                  typed and typed[-1] == {"chars": 12, "verified": True,
                                          "preview": "Hello world."}, str(typed))
            check("typed: state counts characters", dictation.state()["typed"] == 12)
            env.text = "How are you?"
            dictation._process(PCM, 900, dictation._gen)
            check("typed: the next phrase is joined with a space",
                  env.typed[-1] == " How are you?", str(env.typed))

        # Hallucinations from short noise.
        fresh()
        with Env(text="Thank you.") as env, env.on():
            dictation._process(PCM, 400, dictation._gen)
            check("hallucination: 'Thank you.' from 400 ms is dropped", env.typed == [])
            dictation._process(PCM, 900, dictation._gen)
            check("hallucination: 'Thank you.' from 900 ms is typed",
                  env.typed == ["Thank you."])

        # Commands only as whole phrases.
        events.clear()
        fresh()
        with Env(text="New line.") as env, env.on():
            dictation._process(PCM, 900, dictation._gen)
            check("command: 'New line.' presses Shift+Enter (never a bare Enter that "
                  "sends a chat message) and types nothing",
                  env.keys == [("enter", ("shift",), 1)] and env.typed == [], str(env.keys))
            check("command: event emitted", ("command", {"name": "newline"}) in events)
            env.text = "add a new line here"
            dictation._process(PCM, 900, dictation._gen)
            check("command: 'add a new line here' is typed",
                  env.typed == ["Add a new line here"], str(env.typed))
            env.text = "Stop."
            dictation._process(PCM, 900, dictation._gen)
            check("command: 'Stop.' stops dictation", not dictation.active())

        # Enter goes only to a text field of another app.
        for label, kw in (("no field", {"field": None}),
                          ("a password field", {"field": Field(pid=10, secure=True)}),
                          ("JARVIS focused", {"own": True})):
            for phrase in ("New line.", "Send it."):
                events.clear()
                fresh()
                env = Env(text=phrase, **kw)
                if kw.get("field", 1) is None:
                    env.field = None
                with env, env.on():
                    dictation._process(PCM, 900, dictation._gen)
                    check(f"enter guard: '{phrase}' with {label} presses nothing",
                          env.keys == [] and any(k == "failed" for k, _ in events),
                          (env.keys, events))

        # "stop air control" while dictating: an exit, never typed.
        for phrase in ("Stop air control.", "Jarvis, stop!", "Exit Air Control",
                       "Arrête Air Control."):
            events.clear()
            fresh()
            with Env(text=phrase) as env, env.on():
                dictation._process(PCM, 900, dictation._gen)
                check(f"exit: {phrase!r} stops dictation, asks to exit, types nothing",
                      env.typed == [] and not dictation.active()
                      and ("command", {"name": "exit"}) in events, (env.typed, events))

        # A phrase queued before stop(commit=False) is dropped.
        fresh()
        with Env() as env, env.on():
            gen = dictation._gen
            dictation.stop(commit=False)
            dictation._process(PCM, 900, gen)
            check("cancel: phrases from before stop(commit=False) are dropped", env.typed == [])

        # Transcription failed.
        events.clear()
        fresh()
        with Env(text=None) as env, env.on():
            dictation._process(PCM, 900, dictation._gen)
            check("transcribe: None -> failed 'didn't catch that'",
                  ("failed", {"why": "didn't catch that"}) in events)
    finally:
        unsub()
        dictation._stop("test", commit=False)


def test_undo_and_send() -> None:
    fresh()
    with Env(field=Field(pid=11, secure=False)) as env, env.on():
        with dictation._lock:
            dictation._typed.append(("Hello world", 10))
        r = dictation.undo_last()
        check("undo: focus moved -> nothing deleted",
              r.startswith("status=unverified") and "focus moved" in r and env.keys == [], r)
        check("undo: the phrase stays undoable", len(dictation._typed) == 1)

    with Env(field=Field(pid=10, secure=False)) as env, env.on():
        r = dictation.undo_last()
        check("undo: same field -> n backspaces",
              env.keys == [("backspace", (), 11)], str(env.keys))
        check("undo: the phrase is forgotten", len(dictation._typed) == 0)
        with dictation._lock:
            dictation._typed.append(("x" * 120, 10))
        env.keys.clear()
        dictation.undo_last()
        check("undo: long phrases go in chunks of 50",
              [k[2] for k in env.keys] == [50, 50, 20], str(env.keys))
        with dictation._lock:
            dictation._typed.append(("Café", 10))      # e + combining accent
        env.keys.clear()
        dictation.undo_last()
        check("undo: counted after NFC", env.keys == [("backspace", (), 4)], str(env.keys))
        r = dictation.undo_last()
        check("undo: nothing left -> failed", r.startswith("status=failed"), r)

        env.keys.clear()
        r = dictation.send()
        check("send: Enter, reported as unverified",
              env.keys == [("enter", (), 1)] and r.startswith("status=unverified")
              and "Enter was pressed" in r, r)
    dictation._stop("test", commit=False)


def test_listeners_and_cost() -> None:
    got = []
    unsub = dictation.on_event(lambda k, d: got.append(k))
    dictation._emit("heard", {})
    unsub()
    dictation._emit("heard", {})
    check("events: listeners get events and unsubscribe works", got == ["heard"], str(got))
    unsub()
    check("events: unsubscribing twice is harmless", True)

    def bad(k, d):
        raise RuntimeError("listener bug")
    unsub = dictation.on_event(bad)
    dictation._emit("heard", {})
    unsub()
    check("events: a failing listener does not break emit", True)

    dictation._stop("test", commit=False)
    calls = []
    with patched(dictation, _measure=lambda b: calls.append(1) or (b"", 0.0, 0.0)):
        for _ in range(1000):
            dictation.feed(object())
    check("feed: nothing is measured while dictation is off", calls == [])
    dictation.feed(None)
    check("feed: garbage never raises", True)


def test_worker_end_to_end() -> None:
    dictation._stop("test", commit=False)
    drain()
    clock = Clock()
    dictation._now = clock
    done = threading.Event()
    events = []

    def listen(kind, data):
        events.append((kind, data))
        if kind == "typed":
            done.set()
    unsub = dictation.on_event(listen)
    try:
        with Env(text="Ship it today.") as env, env.on():
            r = dictation.start(source="test")
            check("e2e: start says started", r.startswith("status=started"), r)
            check("e2e: listening emitted", events and events[0][0] == "listening")
            pcm = silence(300)
            pcm.extend(speech(1000))
            pcm.extend(silence(900))
            play(clock, pcm)
            check("e2e: the worker types the phrase", done.wait(3.0)
                  and env.typed == ["Ship it today."], str(env.typed))
            check("e2e: heard came before typed",
                  [k for k, _ in events if k in ("heard", "typed")] == ["heard", "typed"])
            s = dictation.state()
            check("e2e: state", s["on"] and s["source"] == "test" and s["typed"] == 14, str(s))
            r = dictation.start()
            check("e2e: starting twice is refused", r.startswith("status=failed"), r)
            r = dictation.stop()
            check("e2e: stop -> done stopped=true",
                  r.startswith("status=done") and "stopped=true" in r, r)
    finally:
        unsub()
        dictation._stop("test", commit=False)


def test_main_hook() -> None:
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    start = src.find("def callback(indata, frames, time_info, status):")
    check("main: mic callback found", start >= 0)
    body = src[start:start + 6000]
    i_dict = body.find("_dictation.active()")
    i_wake = body.find("if self._wake_enabled and not self._awake")
    check("main: dictation is checked before the wake-word gate",
          0 <= i_dict < i_wake, f"{i_dict} vs {i_wake}")
    i_feed = body.find("_dictation.feed(indata)")
    check("main: blocks are fed to dictation", i_dict < i_feed < i_wake)
    check("main: the import is guarded",
          "from core import dictation as _dictation" in src and "_dictation = None" in src)
    check("main: attach is called", "_dictation.attach(" in src)


def main_() -> int:
    saved_now = dictation._now
    try:
        test_segments()
        test_hold()
        test_idle_stop()
        test_commands()
        test_join()
        test_start_refusals()
        test_process()
        test_undo_and_send()
        test_listeners_and_cost()
        test_worker_end_to_end()
        test_main_hook()
    finally:
        dictation._now = saved_now
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
