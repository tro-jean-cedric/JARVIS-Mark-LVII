"""
Self-test for the "on it" lines (core/on_it.py) and the see-or-do check
(core/see_or_do.py), including how main.py uses both.

    python -m core._on_it_tests            # stand-in model and voice; no network
    python -m core._on_it_tests --live     # also write and record real lines with
                                           # the Gemini key (English and French)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import on_it, see_or_do  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def test_clean_lines() -> None:
    raw = ('1. On it.\n- "Working on that now."\nShall I start?\n'
           "This line is far too long to be said quickly\nOn it.\n• One moment.\n")
    check("clean: numbering, bullets and quotes stripped; questions, long and repeated lines dropped",
          on_it.clean_lines(raw, "en") == ["On it.", "Working on that now.", "One moment."],
          str(on_it.clean_lines(raw, "en")))
    check("clean: a line in another script is dropped",
          on_it.clean_lines("Je m'en occupe.\nСейчас сделаю.\nUn instant.", "fr")
          == ["Je m'en occupe.", "Un instant."])
    check("clean: Japanese in kanji only is still Japanese",
          on_it.clean_lines("少々お待ちください。\n確認中。", "ja") == ["少々お待ちください。", "確認中。"],
          str(on_it.clean_lines("少々お待ちください。\n確認中。", "ja")))
    check("clean: at most three", len(on_it.clean_lines("a b\nc d\ne f\ng h", "en")) == 3)
    check("prompt names the language", "French" in on_it.line_prompt("fr"))


class Model:
    def __init__(self, reply="On it.\nOne moment.\nWorking on it."):
        self.reply, self.prompts = reply, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply


class Voice:
    def __init__(self, fail_on=()):
        self.fail_on, self.recorded = set(fail_on), []

    def __call__(self, text, voice):
        self.recorded.append((text, voice))
        return None if text in self.fail_on else (text + voice).encode() * 2000


def test_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        model, voice = Model(), Voice()
        lines = on_it.Lines(Path(tmp), write=model, record=voice)
        check("lines: not ready before prepare", not lines.ready("en", "Charon"))
        check("lines: prepare writes and records three", lines.prepare("en", "Charon")
              and len(voice.recorded) == 3 and lines.ready("en", "Charon"))
        check("lines: prepared once, not again", lines.prepare("en", "Charon")
              and len(model.prompts) == 1)
        again = on_it.Lines(Path(tmp), write=Model(""), record=Voice())
        check("lines: kept on disk for the next run", again.ready("en", "Charon"))
        picks = [lines.pick("en", "Charon")[0] for _ in range(4)]
        check("lines: rotate, never the same twice in a row",
              picks == ["On it.", "One moment.", "Working on it.", "On it."], str(picks))
        text, pcm = lines.pick("en", "Charon")
        check("lines: the audio is the recording of that line", pcm == (text + "Charon").encode() * 2000)
        check("lines: another voice needs recording again", not lines.ready("en", "Puck"))
        check("lines: no language, nothing", not lines.ready("", "Charon")
              and lines.pick("de", "Charon") is None)

        bad = on_it.Lines(Path(tmp) / "b", write=Model("Can I help?\nShall I?\nOk"), record=Voice())
        check("lines: fewer than two usable lines -> none kept", not bad.prepare("en", "Charon"))
        half = on_it.Lines(Path(tmp) / "c", write=Model(),
                           record=Voice(fail_on={"On it.", "One moment."}))
        check("lines: fewer than two recordings -> none kept", not half.prepare("en", "Charon"))
        crash = on_it.Lines(Path(tmp) / "d", write=lambda p: 1 / 0, record=Voice())
        check("lines: a crash is not raised", crash.prepare("en", "Charon") is False)

        slow_model = Model()
        later = on_it.Lines(Path(tmp) / "e", write=slow_model, record=Voice())
        later.prepare_later("fr", "Charon")
        later.prepare_later("fr", "Charon")
        deadline = time.monotonic() + 3
        while not later.ready("fr", "Charon") and time.monotonic() < deadline:
            time.sleep(0.05)
        check("lines: prepared in the background, once", later.ready("fr", "Charon")
              and len(slow_model.prompts) == 1)


def test_language() -> None:
    from core import languages
    saved = languages.active
    try:
        languages.active = lambda: []
        check("language: detected from a sentence",
              on_it.language_of("Est-ce que tu peux ouvrir mes courriels de ce matin", "") == "fr")
        check("language: too short keeps the previous one", on_it.language_of("ok", "de") == "de")
        languages.active = lambda: ["es"]
        check("language: one active language wins", on_it.language_of("open my mail please now", "") == "es")
    finally:
        languages.active = saved


FEATURES = [("video_summarizer", "Summary of a video"), ("screen_agent", "Tasks on screen"),
            ("browser_control", "Web pages")]


def test_see_or_do() -> None:
    calls = []

    def ask(reply):
        def f(p):
            calls.append(p)
            return reply
        return f

    v = see_or_do.judge("what does this error say", FEATURES, ask({"kind": "see"}))
    check("judge: see", v["kind"] == "see" and "what does this error say" in calls[-1]
          and "video_summarizer" in calls[-1])
    v = see_or_do.judge("what is this video about", FEATURES,
                        ask({"kind": "do", "wants": "a summary of the video", "by": "Video_Summarizer"}))
    check("judge: do, with the feature named (case-insensitive)",
          v == {"kind": "do", "wants": "a summary of the video", "by": "video_summarizer"}, str(v))
    v = see_or_do.judge("x", FEATURES, ask({"kind": "do", "wants": "y", "by": "teleporter"}))
    check("judge: 'do' with no real feature to go to -> look anyway", v["kind"] == "see")
    import memory.config_manager as cm
    saved = cm.load_api_keys
    try:
        cm.load_api_keys = lambda: {}
        check("settings: see-or-do off, 'on it' on, with an untouched config",
              cm.get_see_or_do_enabled() is False and cm.get_on_it_enabled() is True)
        cm.load_api_keys = lambda: {"see_or_do": True, "on_it_lines": False}
        check("settings: both can be switched", cm.get_see_or_do_enabled() is True
              and cm.get_on_it_enabled() is False)
    finally:
        cm.load_api_keys = saved
    check("judge: garbage is see", see_or_do.judge("x", FEATURES, ask("nonsense"))["kind"] == "see")
    check("judge: a crash is see", see_or_do.judge("x", FEATURES, lambda p: 1 / 0)["kind"] == "see")
    n = len(calls)
    check("judge: nothing said, no model call", see_or_do.judge("  ", FEATURES, ask({}))["kind"] == "see"
          and len(calls) == n)
    r = see_or_do.redirect({"kind": "do", "wants": "a summary of the video", "by": "video_summarizer"})
    check("redirect: names the tool", "status=failed" in r and "'video_summarizer' tool" in r, r)
    r = see_or_do.redirect({"kind": "do", "wants": "z", "by": "video_summarizer"}, {"video_summarizer"})
    check("redirect: through use_feature when it is behind the router", "use_feature" in r, r)
    r = see_or_do.redirect({"kind": "do", "wants": "z", "by": ""})
    check("redirect: no feature -> say so, do not describe", "do not describe the screen" in r, r)


def test_main() -> None:
    try:
        import main
        from google.genai import types
    except Exception as e:
        print(f"SKIP  main.py ({type(e).__name__}: {e})")
        return
    import threading

    class UI:
        muted = False
        states: list = []

        def set_state(self, s):
            self.states.append(s)

        def write_log(self, *_a):
            pass

        def show_content(self, *_a):
            pass

    class Plugins:
        def __init__(self, delay):
            self.delay = delay

        def has(self, n):
            return n == "slow_tool"

        def run(self, n, a, player=None, session_memory=None):
            time.sleep(self.delay)
            return "status=done action=slow_tool"

        def scheduling(self, n):
            return None

        def get_tool_declarations(self):
            return [{"name": "video_summarizer", "description": "Summarises a video."}]

    class Actions:
        def has(self, n):
            return False

        def scheduling(self, n):
            return None

        def get_tool_declarations(self):
            return []

    def jarvis(delay=1.6):
        j = main.JarvisLive.__new__(main.JarvisLive)
        j.ui, j._action_registry, j._plugin_registry = UI(), Actions(), Plugins(delay)
        j._briefing_until = j._briefing_started = 0.0
        j._last_user_input_at = time.monotonic()
        j._last_user_text = "please find the cheapest flight to Lisbon for me"
        j._router_menu = None
        j._engine = None
        j._speaking_lock = threading.Lock()
        j._is_speaking = False
        j._on_it_lang, j._on_it_turn, j._on_it_live = "", -1.0, False
        j._out_latency = 0.1
        j._tail_until = 0.0
        j._out_level = 0.0
        j._vision_busy, j._vision_last_time = False, 0.0
        j._pending_vision = None
        return j

    import actions.action_history as ah
    saved_record, ah.record = ah.record, lambda *a, **k: None
    saved_enabled = on_it.enabled
    on_it.enabled = lambda: True
    saved_voice = main.get_voice
    main.get_voice = lambda: "Charon"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            lines = on_it.Lines(Path(tmp), write=Model(), record=Voice())
            lines.prepare("en", "Charon")

            async def run(j, name="slow_tool"):
                j.audio_in_queue = asyncio.Queue()
                fr = await j._execute_tool(types.FunctionCall(id="1", name=name, args={}))
                await asyncio.sleep(0.05)
                return fr

            j = jarvis()
            j._on_it = lines
            asyncio.run(run(j))
            check("main: a slow tool gets an 'on it' line in the speaker queue",
                  j.audio_in_queue.qsize() > 0 and j._on_it_turn == j._last_user_input_at)

            async def twice(j):
                await run(j)
                first = j.audio_in_queue.qsize()
                j.audio_in_queue = asyncio.Queue()
                await j._execute_tool(types.FunctionCall(id="2", name="slow_tool", args={}))
                return first, j.audio_in_queue.qsize()
            j = jarvis()
            j._on_it = lines
            first, second = asyncio.run(twice(j))
            check("main: once per user turn, not for every tool in it", first > 0 and second == 0)

            j = jarvis(delay=0.2)
            j._on_it = lines
            asyncio.run(run(j))
            check("main: a quick tool gets no line", j.audio_in_queue.qsize() == 0)

            j = jarvis()
            j._on_it = lines
            j._is_speaking = True
            asyncio.run(run(j))
            check("main: never over the assistant's own speech", j.audio_in_queue.qsize() == 0)

            j = jarvis()
            j._on_it = lines
            j._engine = object()
            asyncio.run(run(j))
            check("main: not with a local voice engine", j.audio_in_queue.qsize() == 0)

            fresh = on_it.Lines(Path(tmp) / "fr", write=Model("Je m'en occupe.\nUn instant.\nC'est parti."),
                                record=Voice())
            j = jarvis()
            j._on_it = fresh
            j._last_user_text = "(typed) peux-tu trouver le vol le moins cher pour Lisbonne"
            asyncio.run(run(j))
            deadline = time.monotonic() + 3
            while not fresh.ready("fr", "Charon") and time.monotonic() < deadline:
                time.sleep(0.05)
            check("main: a new language is prepared for next time, silently now",
                  j.audio_in_queue.qsize() == 0 and fresh.ready("fr", "Charon"))

            async def released(j):
                j.audio_in_queue = asyncio.Queue()
                j._say_on_it()
                while not j.audio_in_queue.empty():
                    j.audio_in_queue.get_nowait()
                j.set_speaking(True)
                await asyncio.sleep(2.0)     # longer than any stand-in line + 0.4 s
                return j._is_speaking
            j = jarvis()
            j._on_it = lines
            j._on_it_lang = "en"
            check("main: the microphone is released once the line has played",
                  asyncio.run(released(j)) is False)

        # see or do, through the real screen_process branch
        saved_cap, saved_judge = main._capture_screen, see_or_do.judge
        saved_sod = see_or_do.enabled
        see_or_do.enabled = lambda: True
        main._capture_screen = lambda: (b"jpeg", "image/jpeg")
        try:
            see_or_do.judge = lambda said, feats: {"kind": "do", "wants": "the video's content",
                                                   "by": "video_summarizer"}
            j = jarvis()
            j._last_user_text = "what is this video about"
            fr = asyncio.run(run(j, "screen_process"))
            r = fr.response["result"]
            check("main: 'do' -> no picture, the model is sent to the right tool",
                  j._pending_vision is None and "video_summarizer" in r and not j._vision_busy, r)
            see_or_do.judge = lambda said, feats: {"kind": "see", "wants": "", "by": ""}
            j = jarvis()
            fr = asyncio.run(run(j, "screen_process"))
            check("main: 'see' -> the picture is attached as before",
                  j._pending_vision is not None and "VISION_ACTIVE" in fr.response["result"])

            def slow(said, feats):
                time.sleep(main.JarvisLive._SEE_OR_DO_WAIT_S + 1)
                return {"kind": "do", "wants": "x", "by": ""}
            see_or_do.judge = slow
            j = jarvis()

            async def timed(j):
                t = time.monotonic()
                await run(j, "screen_process")
                return time.monotonic() - t
            took = asyncio.run(timed(j))   # (asyncio.run then waits for the thread; the app's loop never ends)
            check("main: a slow verdict never holds the picture past the wait",
                  j._pending_vision is not None and took < main.JarvisLive._SEE_OR_DO_WAIT_S + 0.8,
                  f"{took:.2f}s")
            j = jarvis()
            j._last_user_input_at = time.monotonic() - 120
            see_or_do.judge = lambda *a: (_ for _ in ()).throw(AssertionError("called"))
            fr = asyncio.run(run(j, "screen_process"))
            check("main: nothing said lately -> no judging, just look",
                  j._pending_vision is not None)
        finally:
            main._capture_screen, see_or_do.judge = saved_cap, saved_judge
            see_or_do.enabled = saved_sod
    finally:
        ah.record = saved_record
        on_it.enabled = saved_enabled
        main.get_voice = saved_voice


def live() -> None:
    """Real lines from the real model and voice (needs the Gemini key)."""
    from core import gemini
    if not gemini.api_key():
        print("SKIP  live: no Gemini key in config/api_keys.json")
        return
    with tempfile.TemporaryDirectory() as tmp:
        lines = on_it.Lines(Path(tmp))
        for code in ("en", "fr"):
            t = time.monotonic()
            ok = lines.prepare(code, "Charon")
            e = lines.entry(code, "Charon") or {}
            secs = [len((Path(tmp) / f).read_bytes()) / 48000 for f in e.get("files", [])]
            print(f"INFO  {code}: {e.get('lines')} audio {[round(s, 2) for s in secs]} s "
                  f"in {time.monotonic() - t:.1f} s")
            check(f"live: {code} lines written and recorded", ok and all(0.3 < s < 4 for s in secs))
        feats = [("video_summarizer", "Summary of a video or audio link or file"),
                 ("screen_agent", "Multi-step tasks on screen by looking, acting and checking"),
                 ("web_search", "Search the web")]
        for said, want in (("what does the error on my screen say", "see"),
                           ("what is the video I'm watching about", "do"),
                           ("click the blue send button", "do"),
                           ("qu'est-ce qui est écrit dans cette fenêtre", "see")):
            v = see_or_do.judge(said, feats)
            print(f"INFO  {said!r} -> {v}")
            check(f"live: judge {said!r} is {want}", v["kind"] == want)


def main_() -> int:
    test_clean_lines()
    test_lines()
    test_language()
    test_see_or_do()
    test_main()
    if "--live" in sys.argv:
        live()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
