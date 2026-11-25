"""
providers/_speakable.py — a reply rewritten so a local voice says its numbers
right, in the user's language.

WHY
    Piper, the system voices and OpenAI-compatible servers such as Kokoro read
    "21 °C", "49,99 €", "100 km/h", "25.11.2026", "+33 6 12 34 56 78" or
    "10–20 %" badly or not at all: some read the symbols in English whatever
    the language, some skip them, some read a German "3,5" as "three, five".
    Gemini Live and ElevenLabs say these well on their own and are left alone
    (the voice provider says so with SPEAKS_NUMBERS = True).

    So before such a voice reads a sentence, every number in it becomes words:
    the digits through providers/_numbers.py (num2words), the units, currency
    names, month names and the decimal and grouping symbols through Babel's
    CLDR data for that language - "21 Grad Celsius", "neunundvierzig Euro
    neunundneunzig", "yüzde otuz".

THE FEW WORDS NEITHER LIBRARY HAS
    "to" in a range, "plus", "times", "divided by", "equals", and how a clock
    time is said. English is built in. Any other language gets them written
    once by the model (core.gemini, in the background, the first time that
    language is spoken) and kept in memory/speech_words.json; until then a
    range is read as two numbers and a time as hour and minutes.

WHAT IS LEFT ALONE
    Web and e-mail addresses; anything in a language that num2words does not
    speak (Hindi, Chinese...) - digits read by the voice beat wrong words;
    every step that cannot be done cleanly. Nothing here raises: on any error
    the text comes back as it was given.

Results are cached (maxsize 512): the same short replies come back often.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from providers import _numbers as N

# A number as written: grouped thousands (comma, dot, ordinary or narrow
# no-break space) and/or a decimal part.
_NUM = r"\d{1,3}(?:[,.   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"

# What may not touch a number on either side. Latin letters only: Japanese,
# Chinese and Thai write a number straight against the words around it.
_NB = r"A-Za-z\u00c0-\u024f\d_"
# An optional minus: only at the start or after a space or bracket.
_MINUS = r"(?:(?<![^\s(\[\"'«„“])-)?"

_PROTECT = re.compile(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+\.[\w.]+|\b[\w-]+\.[a-z]{2,}/\S*")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d  \-().]{5,}\d(?!\d)")
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_NUM_DATE = re.compile(r"(?<![\d./-])(\d{1,2})([./-])(\d{1,2})\2(\d{4})(?![\d/-]|\.\d)")
# The word written after a time ("14:30 Uhr", "9h05") is dropped when the
# language's own clock wording is known: that wording already has it.
_TIME = re.compile(r"(?<![\d:.,])([01]?\d|2[0-3])[:h]([0-5]\d)(?![\d:])"
                   r"(?P<w>\s?(?:Uhr|uur|heures?|h)\b)?")
_GROUPED_DOTS = re.compile(r"\d{1,3}(?:\.\d{3})+")
_VERSION = re.compile(r"(?<![\d.,])\d+(?:\.\d+){2,}(?![\d.]*\d)")
_RANGE = re.compile(rf"(?<![\w.,:/-])({_NUM})\s?[-–—]\s?({_NUM})(?![\w.,:]*\d)")
_SUM = re.compile(rf"(?<![\w.,:/])({_NUM})\s*([+×*÷=])\s*({_NUM})(?![\w.,]*\d)")
_ORDINAL_EN = re.compile(r"\b(\d+)(st|nd|rd|th)\b")
# A minus belongs to the number only at the start or after a space or bracket
# ("-5 °C", "( -3"), never inside "10-20" or "x-5".
_PLAIN = re.compile(rf"{_MINUS}(?<![{_NB}.,])(?:{_NUM})(?![{_NB}])")

_SYMBOLS = {"€": "EUR", "$": "USD", "US$": "USD", "£": "GBP", "¥": "JPY", "₹": "INR",
            "₽": "RUB", "₺": "TRY", "₩": "KRW", "₦": "NGN", "R$": "BRL", "CHF": "CHF",
            "FCFA": "XOF", "CFA": "XOF"}
_SYM_RX = "|".join(re.escape(s) for s in sorted(_SYMBOLS, key=len, reverse=True))
_MONEY_BEFORE = re.compile(rf"(?<![{_NB}])(?P<s>{_SYM_RX})\s?(?P<n>{_NUM})(?![{_NB}])")
_MONEY_AFTER = re.compile(rf"(?P<n>{_MINUS}(?<![{_NB}.,])(?:{_NUM}))\s?(?P<s>{_SYM_RX}|[A-Z]{{3}})(?![{_NB}])")

# Written symbol -> CLDR unit id. Longest first, so km/h is not "km" + "/h".
_UNITS = [
    ("km/h", "speed-kilometer-per-hour"), ("mph", "speed-mile-per-hour"),
    ("°C", "temperature-celsius"), ("°F", "temperature-fahrenheit"),
    ("kWh", "energy-kilowatt-hour"), ("kW", "power-kilowatt"), ("W", "power-watt"),
    ("GHz", "frequency-gigahertz"), ("MHz", "frequency-megahertz"), ("Hz", "frequency-hertz"),
    ("TB", "digital-terabyte"), ("GB", "digital-gigabyte"), ("MB", "digital-megabyte"),
    ("KB", "digital-kilobyte"), ("kB", "digital-kilobyte"),
    ("km", "length-kilometer"), ("cm", "length-centimeter"), ("mm", "length-millimeter"),
    ("mi", "length-mile"), ("m", "length-meter"),
    ("kg", "mass-kilogram"), ("mg", "mass-milligram"), ("lbs", "mass-pound"), ("lb", "mass-pound"),
    ("g", "mass-gram"), ("ml", "volume-milliliter"), ("mL", "volume-milliliter"),
    ("l", "volume-liter"), ("L", "volume-liter"),
    ("min", "duration-minute"), ("ms", "duration-millisecond"), ("h", "duration-hour"),
    ("s", "duration-second"), ("V", "electric-volt"), ("%", "concentr-percent"),
]
_UNIT_OF = dict(_UNITS)
_UNIT_AFTER = re.compile(rf"(?P<n>{_MINUS}(?<![{_NB}.,])(?:{_NUM}))\s?(?P<u>"
                         + "|".join(re.escape(s) for s, _ in _UNITS) + rf")(?![{_NB}/])")
_PERCENT_BEFORE = re.compile(rf"%\s?(?P<n>{_NUM})(?![{_NB}])")   # Turkish writes %30

ENGLISH = {
    "range": "{a} to {b}", "plus": "{a} plus {b}", "times": "{a} times {b}",
    "divided": "{a} divided by {b}", "equals": "{a} equals {b}",
}
_SIGN_OF = {"+": "plus", "×": "times", "*": "times", "÷": "divided", "=": "equals"}


# ── locale data ──────────────────────────────────────────────────────────────
@lru_cache(maxsize=64)
def _locale(code: str):
    try:
        from babel import Locale
        return Locale.parse(str(code).replace("-", "_"))
    except Exception:
        return None


def _symbols(code: str) -> tuple[str, str]:
    """(decimal symbol, group symbol) of the language; '.' and ',' when unknown."""
    loc = _locale(code)
    if loc is None:
        return ".", ","
    try:
        from babel.numbers import get_decimal_symbol, get_group_symbol
        return get_decimal_symbol(loc), get_group_symbol(loc)
    except Exception:
        return ".", ","


def parse(token: str, code: str) -> Optional[tuple[bool, str, str]]:
    """(negative, whole digits, fraction digits) of a written number, read
    with the language's own decimal symbol; None when it is not one."""
    t = token.strip()
    neg = t.startswith("-")
    t = t.lstrip("-").replace(" ", " ").replace(" ", " ")
    if not t or not t[0].isdigit():
        return None
    dec, group = _symbols(code)
    if " " in t:
        t = t.replace(" ", "")
    if "." in t and "," in t:                      # both: the last one is the decimal point
        point = "." if t.rfind(".") > t.rfind(",") else ","
    elif "." in t or "," in t:
        sep = "." if "." in t else ","
        grouped = re.fullmatch(rf"\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+", t)
        # A separator that is the language's decimal symbol is a decimal point;
        # otherwise it groups thousands - unless the shape says it cannot
        # ("3.5" in a German reply is three point five).
        point = sep if (sep == dec or not grouped) else ""
    else:
        point = ""
    if point:
        whole, _, frac = t.rpartition(point)
        whole = re.sub(r"[.,]", "", whole)
    else:
        whole, frac = re.sub(r"[.,]", "", t), ""
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    return neg, whole, frac


def number_words(token: str, code: str) -> Optional[str]:
    p = parse(token, code)
    if p is None:
        return None
    neg, whole, frac = p
    if frac:
        return N.decimal(whole, frac, code, negative=neg)
    return N.integer(-int(whole) if neg else int(whole), code)


def _value(token: str, code: str):
    p = parse(token, code)
    if p is None:
        return None
    neg, whole, frac = p
    try:
        v = Decimal(f"{'-' if neg else ''}{whole}.{frac or '0'}")
    except InvalidOperation:
        return None
    return int(v) if not frac else v


# ── the words neither library has ────────────────────────────────────────────
_WORDS_LOCK = threading.Lock()
_learning: set[str] = set()


def _words_file() -> Path:
    try:
        from memory.config_manager import get_base_dir
        return Path(get_base_dir()) / "memory" / "speech_words.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "memory" / "speech_words.json"


def _valid(templates) -> dict:
    out = {}
    if not isinstance(templates, dict):
        return out
    for key, eng in ENGLISH.items():
        t = " ".join(str(templates.get(key) or "").split())
        if t and "{a}" in t and "{b}" in t and not re.search(r"\d", t) and len(t) <= 40 \
                and t.count("{") == 2:
            out[key] = t
    clock = " ".join(str(templates.get("clock") or "").split())
    if clock and "{h}" in clock and "{m}" in clock and not re.search(r"\d", clock) and len(clock) <= 40:
        out["clock"] = clock
    hour = " ".join(str(templates.get("clock_hour") or "").split())
    if hour and "{h}" in hour and "{m}" not in hour and not re.search(r"\d", hour) and len(hour) <= 30:
        out["clock_hour"] = hour
    return out


def words_for(code: str) -> dict:
    """The templates for `code`: built in for English, learned for others
    ({} until they have been)."""
    base = (code or "").split("-")[0].split("_")[0].lower()
    if base == "en":
        return ENGLISH
    try:
        data = json.loads(_words_file().read_text(encoding="utf-8"))
        return _valid(data.get(base))
    except Exception:
        return {}


def learn_prompt(code: str) -> str:
    try:
        from core import languages
        name = languages.name(code) or code
    except Exception:
        name = code
    return (
        f"Give the everyday spoken {name} for these patterns, as a text-to-speech voice "
        "should read them aloud. {a}, {b}, {h} and {m} stand for numbers already written "
        "as words; keep those placeholders exactly as they are and write everything else "
        f"in {name}, with no digits. Reply with JSON only:\n"
        '{"range": "<from {a} to {b}, as in a range of numbers>", '
        '"plus": "<{a} plus {b}>", "times": "<{a} multiplied by {b}>", '
        '"divided": "<{a} divided by {b}>", "equals": "<{a} equals {b}>", '
        '"clock": "<the time {h} hours and {m} minutes, as people say a clock time>", '
        '"clock_hour": "<the time {h} o\'clock, a plain whole hour, with no word for exactly>"}'
    )


def learn_later(code: str, ask: Optional[Callable[[str], object]] = None) -> None:
    """Write the templates for `code` on a background thread, once."""
    base = (code or "").split("-")[0].split("_")[0].lower()
    if not base or base == "en" or words_for(base):
        return
    with _WORDS_LOCK:
        # Asked again under the lock: a learning thread that finished between
        # the check above and here has already written them (measured: the
        # model was asked twice for the same language).
        if base in _learning or words_for(base):
            return
        _learning.add(base)

    def run():
        try:
            answer = (ask or _ask_gemini)(learn_prompt(base))
            got = _valid(answer)
            if len(got) < 3:
                print(f"[Speech] the words for numbers in {base} were not learnt: {answer!r:.120}")
                return
            with _WORDS_LOCK:
                path = _words_file()
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                data[base] = got
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(path)
            speakable.cache_clear()
            print(f"[Speech] learnt the words for numbers in {base}: {got}")
        except Exception as e:
            print(f"[Speech] the words for numbers in {base} were not learnt: {e}")
        finally:
            with _WORDS_LOCK:
                _learning.discard(base)

    threading.Thread(target=run, daemon=True, name=f"speech-words-{base}").start()


def _ask_gemini(prompt: str):
    from core import gemini
    return gemini.as_json(prompt, tier=gemini.FAST)


# ── the steps ────────────────────────────────────────────────────────────────
def _say_money(num: str, sym: str, code: str) -> Optional[str]:
    cur = _SYMBOLS.get(sym, sym)
    p = parse(num, code)
    loc = _locale(code)
    if p is None or loc is None:
        return None
    neg, whole, frac = p
    try:
        from babel.numbers import get_currency_name
        n = int(whole)
        name = get_currency_name(cur, count=n, locale=loc)
    except Exception:
        return None
    if not name or name == cur and len(cur) == 3 and cur not in _SYMBOLS.values():
        return None                                  # three capitals that are not a currency
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()   # "francs CFA (BCEAO)" -> "francs CFA"
    w = N.integer(-n if neg else n, code)
    if w is None:
        return None
    out = f"{w} {name}"
    cents = (frac + "0")[:2] if frac else ""
    if cents and int(cents):
        c = N.integer(int(cents), code)
        if c is None:
            return None
        out += f" {c}"
    return out


def _say_unit(num: str, unit: str, code: str) -> Optional[str]:
    loc = _locale(code)
    value = _value(num, code)
    spoken = number_words(num, code)
    if loc is None or value is None or spoken is None:
        return None
    try:
        from babel.units import format_unit
        text = format_unit(value, unit, length="long", locale=loc)
    except Exception:
        return None
    # The pattern puts the number where the language puts it ("yüzde 30",
    # "摂氏 21 度"); swap the written number in it for the words.
    out, n = re.subn(r"[-−]?\d[\d.,   ]*", spoken + " ", text, count=1)
    return " ".join(out.split()) if n else None


def _say_date(d: _dt.date, code: str, before: str = "") -> Optional[str]:
    loc = _locale(code)
    if loc is None:
        return None
    try:
        from babel.dates import format_date
        text = format_date(d, "long", locale=loc)
    except Exception:
        return None
    base = code.split("_")[0].split("-")[0]
    day_ordinal = base in ("en", "de")

    def num(m):
        n = int(m.group(1))
        if n == d.year:
            return N.year(n, code) or m.group(0)
        if n == d.day:
            if day_ordinal:
                w = N.ordinal(n, code)
                if w:
                    # German: "am fünfundzwanzigsten", not "am fünfundzwanzigste".
                    if base == "de" and w.endswith("e") and re.search(
                            r"(?i)\b(?:am|vom|zum|seit dem|bis zum)\s*$", before):
                        w += "n"
                    return w + (" " if m.group(2) == "." else "")
            return N.integer(n, code) or m.group(0)
        return N.integer(n, code) or m.group(0)
    return " ".join(re.sub(r"(\d+)(\.?)", num, text).split())


def _say_time(h: int, m: int, code: str, words: dict) -> Optional[str]:
    base = code.split("_")[0].split("-")[0]
    if base == "en":
        h12 = h % 12 or 12
        suffix = " p.m." if h >= 13 else (" a.m." if h == 0 else "")
        hw = N.integer(h12, code)
        if hw is None:
            return None
        if m == 0:
            return hw + (suffix or " o'clock")
        mw = N.integer(m, code)
        return f"{hw} {'oh ' if m < 10 else ''}{mw}{suffix}" if mw else None
    hw, mw = N.integer(h, code), N.integer(m, code)
    if hw is None or mw is None:
        return None
    if m == 0:
        return words.get("clock_hour", "{h}").format(h=hw)
    return words.get("clock", "{h} {m}").format(h=hw, m=mw)


def _sub(rx, fn, text):
    def repl(m):
        try:
            out = fn(m)
        except Exception:
            out = None
        return m.group(0) if out is None else out
    return rx.sub(repl, text)


@lru_cache(maxsize=512)
def speakable(text: str, code: str) -> str:
    """`text` with its numbers, money, units, dates, times, ranges, sums and
    phone numbers as words in language `code`. Unchanged when `code` is
    empty or not spoken by num2words. Never raises."""
    if not text or not code or not re.search(r"\d", text) or N.lang(code) is None:
        return text
    try:
        return _speakable(text, code)
    except Exception as e:
        print(f"[Speech] numbers left as written: {e}")
        return text


def _speakable(text: str, code: str) -> str:
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return chr(0xF0000 + len(kept) - 1)      # a private-use character: no digits in it
    text = _PROTECT.sub(keep, text)
    words = words_for(code)
    if not words:
        learn_later(code)

    text = _sub(_PHONE, lambda m: N.digits(m.group(0), code), text)

    def iso(m):
        return _say_date(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), code,
                         m.string[:m.start()])
    text = _sub(_ISO_DATE, iso, text)

    def numeric_date(m):
        loc = _locale(code)
        first = (loc.date_formats["short"].pattern.lstrip("'")[:1].lower() if loc else "m")
        a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
        day, month = (b, a) if first == "m" else (a, b)
        return _say_date(_dt.date(y, month, day), code, m.string[:m.start()])
    text = _sub(_NUM_DATE, numeric_date, text)

    def clock(m):
        said = _say_time(int(m.group(1)), int(m.group(2)), code, words)
        if said is None:
            return None
        own = "clock" in words or code.split("_")[0].split("-")[0] == "en"
        return said if own or not m.group("w") else said + m.group("w")
    text = _sub(_TIME, clock, text)

    if "range" in words:
        # Only the first number is spoken here; the second keeps its digits so
        # a unit or currency after it is still read with it ("10-20 km").
        lead = words["range"].split("{a}")[0].strip()

        def span(m):
            template = words["range"]
            # "de 10-20 km" in Spanish already has the "de" the wording starts with.
            if lead and re.search(rf"(?i)\b{re.escape(lead)}\s*$", m.string[:m.start()]):
                template = "{a}" + template.split("{a}", 1)[1]
            return template.format(a=number_words(m.group(1), code) or m.group(1), b=m.group(2))
        text = _sub(_RANGE, span, text)

    def total(m):
        key = _SIGN_OF.get(m.group(2))
        if key not in words:
            return None
        a, b = number_words(m.group(1), code), number_words(m.group(3), code)
        return words[key].format(a=a, b=b) if a and b else None
    text = _sub(_SUM, total, text)

    for sign, key in _SIGN_OF.items():
        if key in words:
            middle = words[key].replace("{a}", "").replace("{b}", "").strip()
            text = re.sub(rf"(?<=[^\W\d_])\s*{re.escape(sign)}\s*(?=[^\W_])", f" {middle} ", text)

    text = _sub(_MONEY_BEFORE, lambda m: _say_money(m.group("n"), m.group("s"), code), text)
    text = _sub(_MONEY_AFTER, lambda m: _say_money(m.group("n"), m.group("s"), code), text)
    text = _sub(_UNIT_AFTER, lambda m: _say_unit(m.group("n"), _UNIT_OF[m.group("u")], code), text)
    text = _sub(_PERCENT_BEFORE, lambda m: _say_unit(m.group("n"), "concentr-percent", code), text)

    point = N.decimal_word(code)
    def version(m):
        s = m.group(0)
        if _GROUPED_DOTS.fullmatch(s) and _symbols(code)[1] == ".":
            return None                              # 1.234.567 in German is a number
        return f" {point} ".join(N.integer(int(p), code) or p for p in s.split("."))
    if point:
        text = _sub(_VERSION, version, text)
    if code.split("_")[0].split("-")[0] == "en":
        text = _sub(_ORDINAL_EN, lambda m: N.ordinal(int(m.group(1)), code), text)

    text = _sub(_PLAIN, lambda m: number_words(m.group(0), code), text)

    text = re.sub("[\U000F0000-\U000FFFFD]", lambda m: kept[ord(m.group(0)) - 0xF0000], text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
