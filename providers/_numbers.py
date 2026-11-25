"""
providers/_numbers.py — a number, said as words, in a given language.

A thin, careful layer over num2words (optional; without it every function
returns None and the caller leaves the digits alone). Used by
providers/_speakable.py before a local voice reads a reply.

WHAT num2words GETS WRONG, AND WHAT IS DONE INSTEAD (measured, 0.5.14)
    decimals   its float path says 3.5 in Turkish as "üçvirgülelli" (three
               comma FIFTY) and breaks in Arabic. So a decimal is built here:
               the whole part, the language's own decimal word, then each
               digit after it - the way decimals are read aloud nearly
               everywhere ("three point one four", "drei Komma eins vier").
               The decimal word itself is taken from num2words' own "1.5"
               with the words for one and five removed, and only used when
               that leaves a short, clean word.
    years      "to=year" gives English "twenty twenty-six" (right) but a
               Japanese imperial era name for 2026 and nothing at all in
               Vietnamese. So the year form is used only where it is how
               people say years; everywhere else a year is a plain number.
    coverage   Hindi, Chinese and a few others are not supported: None, and
               the digits stay as they are - a local voice reading digits is
               better than one reading the wrong words.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Optional

# Languages where a year is said in its own pattern ("nineteen eighty-four")
# rather than as a plain number. Everywhere else num2words' cardinal is the
# spoken form ("deux mille vingt-six", "zweitausendsechsundzwanzig").
_YEAR_FORM = frozenset({"en"})


@lru_cache(maxsize=64)
def lang(code: str) -> Optional[str]:
    """num2words' name for a language code ("pt-BR" -> "pt_BR", "de" -> "de"),
    or None when it does not speak that language or is not installed."""
    try:
        from num2words import CONVERTER_CLASSES
    except Exception:
        return None
    code = str(code or "").strip().replace("-", "_")
    if not code:
        return None
    if code in CONVERTER_CLASSES:
        return code
    base = code.split("_")[0].lower()
    return base if base in CONVERTER_CLASSES else None


def _n2w(value, lg: str, **kw) -> Optional[str]:
    try:
        from num2words import num2words
        out = num2words(value, lang=lg, **kw)
    except Exception:
        return None
    out = " ".join(str(out).split())
    return out if out and not re.search(r"\d", out) else None


@lru_cache(maxsize=4096)
def integer(n: int, code: str) -> Optional[str]:
    """`n` as words, negative numbers included; None when not possible."""
    lg = lang(code)
    return _n2w(int(n), lg) if lg else None


@lru_cache(maxsize=256)
def year(n: int, code: str) -> Optional[str]:
    lg = lang(code)
    if not lg:
        return None
    if lg.split("_")[0] in _YEAR_FORM:
        return _n2w(int(n), lg, to="year") or integer(n, code)
    return integer(n, code)


@lru_cache(maxsize=512)
def ordinal(n: int, code: str) -> Optional[str]:
    lg = lang(code)
    return _n2w(int(n), lg, to="ordinal") if lg else None


@lru_cache(maxsize=64)
def decimal_word(code: str) -> str:
    """The word said for the decimal point ("point", "Komma", "virgule"),
    or '' when it cannot be told cleanly."""
    lg = lang(code)
    if not lg:
        return ""
    whole = _n2w(Decimal("1.5"), lg)
    one = _n2w(1, lg)
    if not (whole and one and whole.startswith(one)):
        return ""
    rest = whole[len(one):]
    # Its fraction is "five" in most languages and, by a num2words bug,
    # "fifty" in Turkish and Vietnamese: either way, what is between is the word.
    for tail in (_n2w(5, lg), _n2w(50, lg)):
        if tail and rest.endswith(tail):
            word = rest[:len(rest) - len(tail)].strip(" ,")
            if word and len(word) <= 20 and not re.search(r"[\d,]", word):
                return word
    return ""


def digits(text: str, code: str) -> Optional[str]:
    """Each digit of `text` as a word ("07" -> "zero seven"), for phone
    numbers and the part after a decimal point."""
    words = []
    for ch in text:
        if ch.isdigit():
            w = integer(int(ch), code)
            if w is None:
                return None
            words.append(w)
    return " ".join(words) if words else None


# Languages whose num2words decimals are wrong (measured): built here instead.
_OWN_DECIMAL = frozenset({"tr", "vi", "ar"})


def decimal(whole: str, fraction: str, code: str, negative: bool = False) -> Optional[str]:
    """`whole`.`fraction` (digit strings) as words; None when not possible.
    num2words' own reading where it is right (Russian "три целых пять
    десятых" is grammar, not a digit list); built from the decimal word and
    the digits where it is not."""
    lg = lang(code)
    if lg is None:
        return None
    if lg.split("_")[0] not in _OWN_DECIMAL:
        try:
            value = Decimal(f"{'-' if negative else ''}{int(whole or '0')}.{fraction}")
        except (ValueError, InvalidOperation):
            return None
        return _n2w(value, lg)
    try:
        w = integer(int(whole or "0") * (-1 if negative else 1), code)
    except (ValueError, InvalidOperation):
        return None
    point = decimal_word(code)
    tail = digits(fraction, code)
    if w is None or not point or tail is None:
        return None
    if negative and int(whole or "0") == 0:
        minus = integer(-1, code)
        one = integer(1, code)
        if minus and one and minus.endswith(one):
            w = minus[:-len(one)].strip() + " " + w
    return f"{w} {point} {tail}"
