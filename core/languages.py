"""
core/languages.py — which languages the assistant may hear and speak.

The user picks ACTIVE languages (⚙ CUSTOMISE ASSISTANT → LANGUAGES, or by
voice). With none picked the assistant is automatic, as before: it answers in
the language of the user's latest message. With some picked it understands and
answers ONLY in those:

  * one language   — everything heard is taken as that language and every reply
                     is in it, whatever the user (or a mishearing) sounds like;
  * several        — each reply is in whichever active language the user used;
                     anything that sounds like another language is treated as a
                     mishearing of an active one.

How it is enforced:
  1. prompt_block() goes at the END of the system instruction (main.py
     _build_config) and overrides the "follow the latest message" rule in
     core/prompt.txt;
  2. after each spoken reply main.py calls check_reply(); if the reply was in a
     language that is not active, the assistant is told to say it again in the
     right one (with a cooldown, so it can never loop);
  3. the local engine (providers/stt_whisper.py) locks Whisper to the one
     active language, or to the most likely active one;
  4. every other place that picks a language for the assistant (greeting,
     briefing, recap, notices, summaries) goes through resolve_name().

Supported = the languages Gemini Live documents for speech. Codes are ISO 639-1
(what Whisper and Piper use); names are English (what memory stores).
"""
from __future__ import annotations

import re
import threading
from typing import Optional

# code, English name, native name, writing system
SUPPORTED: list[tuple[str, str, str, str]] = [
    ("en", "English",    "English",     "latin"),
    ("fr", "French",     "Français",    "latin"),
    ("es", "Spanish",    "Español",     "latin"),
    ("pt", "Portuguese", "Português",   "latin"),
    ("de", "German",     "Deutsch",     "latin"),
    ("it", "Italian",    "Italiano",    "latin"),
    ("nl", "Dutch",      "Nederlands",  "latin"),
    ("pl", "Polish",     "Polski",      "latin"),
    ("ro", "Romanian",   "Română",      "latin"),
    ("tr", "Turkish",    "Türkçe",      "latin"),
    ("id", "Indonesian", "Bahasa Indonesia", "latin"),
    ("vi", "Vietnamese", "Tiếng Việt",  "latin"),
    ("ru", "Russian",    "Русский",     "cyrillic"),
    ("uk", "Ukrainian",  "Українська",  "cyrillic"),
    ("ar", "Arabic",     "العربية",     "arabic"),
    ("hi", "Hindi",      "हिन्दी",       "devanagari"),
    ("mr", "Marathi",    "मराठी",        "devanagari"),
    ("bn", "Bengali",    "বাংলা",        "bengali"),
    ("gu", "Gujarati",   "ગુજરાતી",      "gujarati"),
    ("ta", "Tamil",      "தமிழ்",        "tamil"),
    ("te", "Telugu",     "తెలుగు",       "telugu"),
    ("kn", "Kannada",    "ಕನ್ನಡ",        "kannada"),
    ("ml", "Malayalam",  "മലയാളം",      "malayalam"),
    ("th", "Thai",       "ไทย",          "thai"),
    ("ja", "Japanese",   "日本語",        "japanese"),
    ("ko", "Korean",     "한국어",        "hangul"),
    ("zh", "Chinese (Mandarin)", "中文",  "han"),
]

CODES = [c for c, *_ in SUPPORTED]
_BY_CODE = {c: (n, nat, s) for c, n, nat, s in SUPPORTED}
_BY_NAME = {n.lower(): c for c, n, *_ in SUPPORTED}
_BY_NAME.update({nat.lower(): c for c, _, nat, _ in SUPPORTED})
_BY_NAME.update({"chinese": "zh", "mandarin": "zh", "portugues": "pt", "francais": "fr",
                 "espanol": "es", "brazilian portuguese": "pt", "japanese": "ja",
                 "nihongo": "ja", "english (us)": "en"})


# ── the setting ──────────────────────────────────────────────────────────────

def active() -> list[str]:
    """Active language codes, in the user's order. [] = automatic (any language)."""
    try:
        from memory.config_manager import get_active_languages
        return [c for c in get_active_languages() if c in _BY_CODE]
    except Exception:
        return []


def set_active(codes) -> list[str]:
    from memory.config_manager import save_active_languages
    clean = []
    for c in codes or []:
        code = to_code(c)
        if code and code not in clean:
            clean.append(code)
    save_active_languages(clean)
    return clean


def is_automatic() -> bool:
    return not active()


# ── names and codes ──────────────────────────────────────────────────────────

def to_code(value) -> str:
    """'fr', 'fr-FR', 'French', 'Français' → 'fr'. '' when unknown."""
    v = str(value or "").strip()
    if not v:
        return ""
    low = v.lower()
    base = re.split(r"[-_]", low)[0]
    if base in _BY_CODE:
        return base
    if base == "cmn":
        return "zh"
    return _BY_NAME.get(low, "") or _BY_NAME.get(low.split(" (")[0], "")


def name(code: str) -> str:
    return _BY_CODE.get(code, (code,))[0]


def native(code: str) -> str:
    return _BY_CODE.get(code, ("", code))[1]


def names(codes=None) -> list[str]:
    return [name(c) for c in (active() if codes is None else codes)]


def human_list(items: list[str], joiner: str = "and") -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {joiner} " + items[-1]


def resolve_name(lang_name: str = "") -> str:
    """The language the assistant should use when code picks one (greeting,
    briefing, notices…). Automatic: the given one (or English). Otherwise the
    given one if it is active, else the first active language."""
    act = active()
    if not act:
        return lang_name or "English"
    code = to_code(lang_name)
    return name(code if code in act else act[0])


def clause(lang_name: str = "") -> str:
    """A sentence for one-off instructions: which language to speak in."""
    act = active()
    if not act:
        return ""
    if len(act) == 1:
        return f" Speak only {name(act[0])}."
    return (f" Speak {resolve_name(lang_name)} (only {human_list(names(act), 'or')} are allowed).")


# ── the system-instruction block ─────────────────────────────────────────────

def prompt_block() -> str:
    act = active()
    if not act:
        return ""
    ns = names(act)
    if len(act) == 1:
        lang = ns[0]
        return (
            "[ACTIVE LANGUAGES — this overrides every other rule about language above]\n"
            f"The user has set {lang} as the ONLY language. Hear everything the user says as "
            f"{lang}: if some words sound like another language, they are {lang} that was "
            f"misheard — interpret them as the closest {lang} words. ALWAYS reply in {lang}, in "
            f"every sentence, whatever language the user's message, memory, a tool result, a web "
            f"page or these instructions are in. If the user clearly speaks or writes another "
            f"language on purpose, answer in {lang} and say briefly, in {lang}, that only {lang} "
            f"is switched on (they can add languages in Customise assistant → Languages, or ask "
            f"you to). Never mix languages."
        )
    any_of = human_list(ns, "or")
    return (
        "[ACTIVE LANGUAGES — this overrides every other rule about language above]\n"
        f"The user has switched on ONLY these languages: {human_list(ns)}. Reply in whichever of "
        f"them the user's latest message is in. If a message sounds like any other language, it "
        f"is {any_of} that was misheard: interpret it as the closest of these and reply in that "
        f"one. Short words that exist in many languages ('ok', 'ja', 'no', 'si', names) never "
        f"change the language — keep the one you were speaking. Never reply in a language that "
        f"is not {any_of}, even if memory, a tool result, a web page or these instructions are "
        f"in it. If the user clearly speaks another language on purpose, answer in {ns[0]} and "
        f"say briefly that only {human_list(ns)} are switched on. Never mix languages."
    )


# ── detection ────────────────────────────────────────────────────────────────

_SCRIPT_RANGES = [
    ("japanese", r"[぀-ヿ]"),
    ("hangul", r"[가-힯ᄀ-ᇿ]"),
    ("han", r"[一-鿿]"),
    ("cyrillic", r"[Ѐ-ӿ]"),
    ("arabic", r"[؀-ۿ]"),
    ("devanagari", r"[ऀ-ॿ]"),
    ("bengali", r"[ঀ-৿]"),
    ("gujarati", r"[઀-૿]"),
    ("tamil", r"[஀-௿]"),
    ("telugu", r"[ఀ-౿]"),
    ("kannada", r"[ಀ-೿]"),
    ("malayalam", r"[ഀ-ൿ]"),
    ("thai", r"[฀-๿]"),
    ("latin", r"[A-Za-zÀ-ɏ]"),
]


def script_of(text: str) -> str:
    counts = {s: len(re.findall(rx, text or "")) for s, rx in _SCRIPT_RANGES}
    if counts["japanese"]:
        return "japanese"          # kana means Japanese even with kanji around it
    best = max(counts, key=counts.get)
    return best if counts[best] else ""


_identifier = None
_id_lock = threading.Lock()


def _langid():
    """py3langid's identifier with normalised probabilities, or None."""
    global _identifier
    if _identifier is None:
        with _id_lock:
            if _identifier is None:
                try:
                    from py3langid import langid as _lid
                    LI = _lid.LanguageIdentifier
                    if hasattr(LI, "from_modelpath"):          # py3langid 0.4+
                        _identifier = LI.from_modelpath(_lid.MODEL_DIR / _lid.MODEL_FILE,
                                                        norm_probs=True)
                    else:                                      # py3langid 0.3
                        _identifier = LI.from_pickled_model(_lid.MODEL_FILE, norm_probs=True)
                except Exception as e:
                    print(f"[Languages] py3langid unavailable ({e}); using the script check only")
                    _identifier = False
    return _identifier or None


def detect(text: str) -> tuple[str, float]:
    """(code, confidence) for a piece of text, '' when it cannot tell."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    letters = len(re.findall(r"\w", t))
    if letters < 12:
        return "", 0.0
    ident = _langid()
    if ident is not None:
        try:
            code, prob = ident.classify(t)
            code = "zh" if code in ("zh", "cmn") else code
            return code, float(prob)
        except Exception:
            pass
    # Fallback: the writing system alone (good for non-Latin languages).
    script = script_of(t)
    matches = [c for c, (_, _, s) in _BY_CODE.items() if s == script]
    if len(matches) == 1:
        return matches[0], 0.9
    return "", 0.0


def allowed_by_script(text: str, act: list[str]) -> bool:
    script = script_of(text)
    if not script:
        return True
    ok = {_BY_CODE[c][2] for c in act}
    if "han" in ok or "japanese" in ok:
        ok |= {"han", "japanese"}
    return script in ok or (script == "latin" and len(re.findall(r"[A-Za-z]", text)) < 8)


def check_reply(text: str, user_text: str = "") -> Optional[str]:
    """None when the reply is fine. Otherwise the instruction to send so the
    assistant repeats it in an allowed language."""
    act = active()
    if not act or len(re.findall(r"\w", text or "")) < 20:
        return None
    code, prob = detect(text)
    wrong = False
    if not allowed_by_script(text, act):
        wrong = True
    elif code and code not in act and prob >= 0.95:
        wrong = True
    if not wrong:
        return None
    target = ""
    if len(act) > 1 and user_text:
        ucode, uprob = detect(user_text)
        if ucode in act and uprob >= 0.6:
            target = name(ucode)
    target = target or name(act[0])
    said = name(code) if code in _BY_CODE else (code or "another language")
    return (
        f"[ACTIVE LANGUAGES] Your last reply was in {said}, which is not switched on. Only "
        f"{human_list(names(act))} {'is' if len(act) == 1 else 'are'} allowed. Say that same "
        f"answer again now, in {target}, without apologising at length and without calling any "
        f"tools, and from now on use only {human_list(names(act), 'or')}."
    )


def best_active_from_probs(probs) -> str:
    """From Whisper's [(code, prob), …] pick the most likely ACTIVE language."""
    act = active()
    if not act:
        return ""
    best, best_p = act[0], -1.0
    for code, p in probs or []:
        c = to_code(code)
        if c in act and float(p) > best_p:
            best, best_p = c, float(p)
    return best
