"""
Self-test for numbers said as words for local voices (providers/_numbers.py,
providers/_speakable.py) and where the pipeline engine uses them.

    python -m providers._speakable_tests            # no network
    python -m providers._speakable_tests --live     # also learn the connecting
                                                    # words for German, French and
                                                    # Spanish with the Gemini key

Needs babel and num2words (requirements.txt); says SKIP without them.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# Templates as the model wrote them in the live run (2026-11-25), so these
# tests do not depend on the network.
LEARNT = {
    "de": {"range": "von {a} bis {b}", "plus": "{a} plus {b}", "times": "{a} mal {b}",
           "divided": "{a} geteilt durch {b}", "equals": "{a} ist gleich {b}",
           "clock": "{h} Uhr {m}", "clock_hour": "{h} Uhr"},
    "fr": {"range": "de {a} à {b}", "plus": "{a} plus {b}", "times": "{a} fois {b}",
           "divided": "{a} divisé par {b}", "equals": "{a} égale {b}",
           "clock": "{h} heures {m}", "clock_hour": "{h} heures"},
    "es": {"range": "de {a} a {b}", "plus": "{a} más {b}", "times": "{a} por {b}",
           "divided": "{a} dividido entre {b}", "equals": "{a} es igual a {b}",
           "clock": "{h} y {m}", "clock_hour": "{h} en punto"},
}

CASES = [
    # (language, written, must contain..., must not contain...)
    ("en", "It is 21 °C.", ["twenty-one degrees Celsius"], ["21"]),
    ("en", "That is $49.99.", ["forty-nine US dollars ninety-nine"], ["$", "49"]),
    ("en", "Call +33 6 12 34 56 78.", ["three three six one two"], ["+"]),
    ("en", "On 2026-11-25 at 14:30.", ["November twenty-fifth, twenty twenty-six", "two thirty p.m."], ["2026"]),
    ("en", "At 9:05 and at 17:00.", ["nine oh five", "five p.m."], []),
    ("en", "Version 1.2.3 is out.", ["one point two point three"], []),
    ("en", "The 3rd time, 10-20 km.", ["third time", "ten to twenty kilometers"], []),
    ("en", "2 + 3 = 5, -5 degrees.", ["two plus three equals five", "minus five degrees"], []),
    ("en", "1,234,567 people, 75% done.", ["one million", "seventy-five percent"], []),
    ("en", "See https://example.com/a1 or a1@b.com, x-5.", ["https://example.com/a1", "a1@b.com", "x-five"], []),
    ("en", "Room 3.5 GB of 100 km/h, 2 kg.", ["three point five gigabytes", "one hundred kilometers per hour", "two kilograms"], []),
    ("de", "Es sind 21 °C, das kostet 49,99 € und wiegt 3,5 kg.",
     ["einundzwanzig Grad Celsius", "neunundvierzig Euro neunundneunzig", "drei Komma fünf Kilogramm"], ["€"]),
    ("de", "Am 25.11.2026 um 14:30 Uhr.", ["Am fünfundzwanzigsten November zweitausendsechsundzwanzig",
                                           "vierzehn Uhr dreißig."], ["Uhr dreißig Uhr"]),
    ("de", "1.234.567 Menschen, 3.5 GB, von 10-20 km, 2 + 3 = 5.",
     ["eine Million zweihundertvierunddreißigtausend", "drei Komma fünf Gigabyte", "von zehn bis zwanzig Kilometer",
      "zwei plus drei ist gleich fünf"], ["von von"]),
    ("fr", "Il fait -3 °C, ça coûte 32 500 FCFA, le 25/11/2026 à 9h05.",
     ["moins trois degrés Celsius", "trente-deux mille cinq cents francs CFA", "vingt-cinq novembre deux mille vingt-six",
      "neuf heures cinq"], ["BCEAO", "heures h"]),
    ("es", "Son 15 €, a 120 km/h, el 25/11/2026. De 10-20 km.",
     ["quince euros", "ciento veinte kilómetros por hora", "veinticinco de noviembre de dos mil veintiséis",
      "De diez a veinte kilómetros"], ["de de", "De de"]),
    ("tr", "Fiyat %30 indirimli, 3,5 kg.", ["yüzde otuz", "üç virgül beş kilogram"], []),
    ("ru", "21 °C и 3,5", ["двадцать один градус Цельсия", "три целых пять десятых"], []),
    ("ja", "気温は21 °Cです。", ["二十一"], ["21"]),
]


def test_cases() -> None:
    import providers._speakable as S
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "speech_words.json"
        path.write_text(json.dumps(LEARNT), encoding="utf-8")
        saved, saved_learn = S._words_file, S.learn_later
        S._words_file = lambda: path
        S.learn_later = lambda *a, **k: None      # no model calls from these cases
        S.speakable.cache_clear()
        try:
            for code, written, must, mustnt in CASES:
                said = S.speakable(written, code)
                ok = all(m in said for m in must) and not any(x in said for x in mustnt)
                check(f"{code}: {written[:48]}", ok, said)
        finally:
            S._words_file, S.learn_later = saved, saved_learn
            S.speakable.cache_clear()


def test_left_alone() -> None:
    import providers._speakable as S
    check("no language: unchanged", S.speakable("It is 21 °C.", "") == "It is 21 °C.")
    check("a language num2words does not speak (Hindi): unchanged",
          S.speakable("तापमान 21 °C है", "hi") == "तापमान 21 °C है")
    check("no digits: unchanged and cheap", S.speakable("Hello there.", "en") == "Hello there.")
    check("three capitals that are not a currency stay", "ABC" in S.speakable("Order 12 ABC now", "en"))


def test_learning() -> None:
    import providers._speakable as S
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w.json"
        saved = S._words_file
        S._words_file = lambda: path
        try:
            asked = []
            S.learn_later("it", ask=lambda p: asked.append(p) or {
                "range": "da {a} a {b}", "plus": "{a} più {b}", "times": "{a} per {b}",
                "divided": "{a} diviso {b}", "equals": "{a} uguale {b}",
                "clock": "le {h} e {m}", "clock_hour": "le {h}", "extra": "ignored 5"})
            S.learn_later("it", ask=lambda p: asked.append(p))
            deadline = time.monotonic() + 3
            while not S.words_for("it") and time.monotonic() < deadline:
                time.sleep(0.05)
            got = S.words_for("it")
            check("learn: written once, kept on disk, unknown keys dropped",
                  got.get("range") == "da {a} a {b}" and "extra" not in got and len(asked) == 1
                  and "Italian" in asked[0], f"{got} asked={len(asked)}")
            S.learn_later("pt", ask=lambda p: {"range": "de 1 a 2", "plus": "{a}+", "clock": "{h}"})
            time.sleep(0.4)
            check("learn: templates with digits or missing placeholders are refused", not S.words_for("pt"))
            S.learn_later("nl", ask=lambda p: 1 / 0)
            time.sleep(0.3)
            check("learn: a failure is not raised and nothing is kept", not S.words_for("nl"))
            check("english needs no learning", S.words_for("en") == S.ENGLISH)
        finally:
            S._words_file = saved


def test_pipeline() -> None:
    """The engine's own _speak: local voices get words, ElevenLabs does not."""
    try:
        from providers.engine_pipeline import PipelineEngine
        from providers._tests import Cfg
    except Exception as e:
        print(f"SKIP  pipeline ({type(e).__name__}: {e})")
        return

    class Voice:
        SPEAKS_NUMBERS = False

        def __init__(self):
            self.got = []

        async def speak(self, text, language=""):
            self.got.append((text, language))
            yield b"\x00\x00" * 160

    class Eleven(Voice):
        SPEAKS_NUMBERS = True

    class STT:
        caps = {}

        def __init__(self, lang):
            self.detected_language = lang

    class LLM:
        caps = {"tools": False, "tool_format": None}
        model = "m"

    import providers._speakable as S
    saved_learn, saved_file = S.learn_later, S._words_file
    S.learn_later = lambda *a, **k: None          # no model calls, no file written
    tmp = Path(tempfile.mkdtemp()) / "w.json"
    S._words_file = lambda: tmp
    S.speakable.cache_clear()

    async def say(tts, stt, text):
        eng = PipelineEngine(LLM(), stt=stt, tts=tts, config=Cfg(), log=lambda m: None)
        await eng._speak(text)
        return tts.got[-1][0] if tts.got else ""

    v = Voice()
    got = asyncio.run(say(v, STT("de"), "Es sind 21 °C."))
    check("pipeline: a local voice gets the numbers as words in the heard language",
          got == "Es sind einundzwanzig Grad Celsius.", got)
    e = Eleven()
    got = asyncio.run(say(e, STT("de"), "Es sind 21 °C."))
    check("pipeline: ElevenLabs reads numbers itself and gets the text as written", got == "Es sind 21 °C.", got)
    from core import languages
    saved = languages.active
    try:
        languages.active = lambda: []
        got = asyncio.run(say(Voice(), STT(""), "Il fait vingt degrés aujourd'hui, c'est 21 °C exactement."))
        check("pipeline: no language from speech -> the text's own language", "vingt et un degrés" in got, got)
        got = asyncio.run(say(Voice(), STT(""), "21 °C"))
        check("pipeline: too short to tell and nothing known -> left alone", got == "21 °C", got)
        languages.active = lambda: ["es"]
        got = asyncio.run(say(Voice(), STT(""), "21 °C"))
        check("pipeline: one active language is used", "veintiún" in got or "veintiuno" in got, got)
    finally:
        languages.active = saved
        S.learn_later, S._words_file = saved_learn, saved_file
        S.speakable.cache_clear()


def live() -> None:
    import providers._speakable as S
    from core import gemini
    if not gemini.api_key():
        print("SKIP  live: no Gemini key")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w.json"
        saved = S._words_file
        S._words_file = lambda: path
        try:
            for code in ("de", "fr", "es"):
                S.learn_later(code)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and not all(S.words_for(c) for c in ("de", "fr", "es")):
                time.sleep(0.5)
            for code in ("de", "fr", "es"):
                got = S.words_for(code)
                print(f"INFO  {code}: {got}")
                check(f"live: {code} words learnt", len(got) >= 5)
        finally:
            S._words_file = saved


def main() -> int:
    try:
        import babel  # noqa: F401
        import num2words  # noqa: F401
    except Exception as e:
        print(f"SKIP  everything: {e} (pip install babel num2words)")
        return 0
    test_cases()
    test_left_alone()
    test_learning()
    test_pipeline()
    if "--live" in sys.argv:
        live()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
