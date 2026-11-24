"""
plugins/spaced_repetition.py — flashcards on an SM-2 style schedule.

Decks persist in memory/plugin_data/spaced_repetition.json. A review shows one
due card at a time in the content panel (player.show_content); the user asks
for the answer, then grades it again / hard / good / easy, and the card's next
due time is computed by `schedule()` below.

Generating a batch of cards asks the model through core.gemini on a worker
thread and announces the result, so run() always returns quickly.
"""
from __future__ import annotations

import re
import threading
import time
import uuid

from plugins import _announce, _doc_text

PLUGIN = {
    "name": "spaced_repetition",
    "description": (
        "Flashcard trainer with spaced repetition (like Anki) for memorising "
        "vocabulary, definitions, facts or formulas over days and weeks. It keeps "
        "the deck between sessions and brings each card back just before it would "
        "be forgotten. Use for 'add a flashcard', 'make 15 flashcards about the "
        "solar system', 'make flashcards from this document', 'start my review', "
        "'show the answer', grading a card ('got it', 'that was hard', 'no idea'), "
        "'how many cards are due'. For a one-off test use the quiz tool instead."
    ),
    "brief": "Flashcards: add, generate, review, reveal, grade, stats.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'add' one card, 'generate' a batch, 'review' next due "
                                      "card, 'reveal' its answer, 'grade' the shown card, "
                                      "'stats' deck summary (default)."},
            "front": {"type": "STRING", "description": "For 'add': the question / prompt side."},
            "back": {"type": "STRING", "description": "For 'add': the answer side."},
            "deck": {"type": "STRING", "description": "Optional deck name (default 'default')."},
            "topic": {"type": "STRING",
                      "description": "For 'generate': what the cards cover. Leave empty to "
                                     "use the document the user uploaded."},
            "count": {"type": "INTEGER", "description": "For 'generate': number of cards (default 10)."},
            "language": {"type": "STRING",
                         "description": "For 'generate': language or format, e.g. 'English → "
                                        "Spanish' or 'answers in German'."},
            "grade": {"type": "STRING",
                      "description": "For 'grade': 'again' (forgot), 'hard', 'good' or 'easy'."},
        },
        "required": [],
    },
}

_DATA = "spaced_repetition"
_lock = threading.RLock()
_session: dict = {"deck": None, "card": None, "revealed": False, "done": 0}

DAY = 86400.0
AGAIN_DELAY = 10 * 60          # a failed card comes back in this session
MIN_EASE, START_EASE = 1.3, 2.5


# ── scheduler ────────────────────────────────────────────────────────────────

def schedule(card: dict, grade: str, now: float | None = None) -> dict:
    """Update `card` in place for a grade and return it.

    Card fields: ease (≥1.3), interval (days), reps (successful in a row),
    lapses, due (epoch seconds)."""
    now = time.time() if now is None else now
    ease = float(card.get("ease", START_EASE))
    interval = float(card.get("interval", 0))
    reps = int(card.get("reps", 0))

    if grade == "again":
        card["lapses"] = int(card.get("lapses", 0)) + 1
        card["reps"] = 0
        card["ease"] = max(MIN_EASE, ease - 0.2)
        card["interval"] = 0
        card["due"] = now + AGAIN_DELAY
        return card
    if grade == "hard":
        ease = max(MIN_EASE, ease - 0.15)
        interval = 1 if reps == 0 else max(interval + 1, interval * 1.2)
    elif grade == "good":
        interval = 1 if reps == 0 else 3 if reps == 1 else interval * ease
    elif grade == "easy":
        ease += 0.15
        interval = 4 if reps == 0 else max(interval + 1, interval * ease * 1.3)
    else:
        raise ValueError(grade)
    interval = min(interval, 3650)
    card["ease"] = round(ease, 3)
    card["interval"] = round(interval, 2)
    card["reps"] = reps + 1
    card["due"] = now + interval * DAY
    return card


_GRADE_WORDS = [
    ("again", ("again", "fail", "forgot", "wrong", "no idea", "didn't", "did not",
               "couldn't", "could not", "don't know", "dont know", "1", "missed")),
    ("easy", ("easy", "trivial", "too easy", "4", "instantly")),
    ("hard", ("hard", "difficult", "barely", "struggled", "almost", "2", "tough")),
    ("good", ("good", "got it", "correct", "right", "knew", "ok", "okay", "fine",
              "3", "yes", "remembered")),
]


def parse_grade(text: str) -> str | None:
    t = str(text or "").strip().lower()
    if not t:
        return None
    for grade, words in _GRADE_WORDS:
        if t == grade or any(re.search(rf"(^|\b){re.escape(w)}(\b|$)", t) for w in words):
            return grade
    return None


def _when(seconds: float) -> str:
    if seconds < 3600:
        return f"in about {max(1, int(round(seconds / 60)))} minutes"
    days = seconds / DAY
    if days < 1.5:
        return "tomorrow"
    if days < 30:
        return f"in {int(round(days))} days"
    if days < 365:
        return f"in about {int(round(days / 30))} months"
    return f"in about {days / 365:.1f} years"


# ── storage ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    data = _announce.load(_DATA, {}) or {}
    if not isinstance(data.get("decks"), dict):
        data["decks"] = {}
    return data


def _deck_name(p: dict) -> str:
    return (str(p.get("deck") or "").strip() or "default")[:40]


def _new_card(front: str, back: str, source: str = "") -> dict:
    return {"id": uuid.uuid4().hex[:10], "front": front.strip()[:500],
            "back": back.strip()[:1000], "ease": START_EASE, "interval": 0,
            "reps": 0, "lapses": 0, "due": time.time(), "created": time.strftime("%Y-%m-%d"),
            "source": source[:80]}


def _due(cards: list[dict], now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return sorted((c for c in cards if float(c.get("due", 0)) <= now),
                  key=lambda c: float(c.get("due", 0)))


# ── actions ──────────────────────────────────────────────────────────────────

def _add(p: dict) -> str:
    front, back = str(p.get("front") or "").strip(), str(p.get("back") or "").strip()
    if not front or not back:
        return "To add a card I need both a front (the prompt) and a back (the answer)."
    deck = _deck_name(p)
    with _lock:
        data = _load()
        cards = data["decks"].setdefault(deck, [])
        if any(c["front"].casefold() == front.casefold() for c in cards):
            return f"There's already a card '{front}' in the {deck} deck."
        cards.append(_new_card(front, back))
        _announce.save(_DATA, data)
    return (f"Card added to the {deck} deck ({len(cards)} card{'s' if len(cards) != 1 else ''}). "
            f"It's due for review now.")


def _gen_prompt(topic: str, count: int, language: str, doc: str) -> str:
    src = (f"Base every card only on this document:\n<<<\n{doc}\n>>>\n" if doc else
           f"Subject: {topic}\n")
    return (
        f"Write {count} flashcards for long-term memorisation.\n{src}"
        + (f"Language / format: {language}\n" if language else
           "Use the language the subject is written in.\n")
        + "Each card tests one atomic fact. The front is a short question or cue; the "
          "back is the shortest complete answer. No duplicates, no trick questions.\n"
          'Reply with JSON only: [{"front": "...", "back": "..."}, ...]'
    )


def _clean_cards(raw) -> list[tuple[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("cards") or raw.get("flashcards") or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        f = str(item.get("front") or item.get("question") or "").strip()
        b = str(item.get("back") or item.get("answer") or "").strip()
        if f and b:
            out.append((f, b))
    return out


def _generate_worker(deck: str, topic: str, count: int, language: str,
                     doc: str, label: str) -> None:
    try:
        from core import gemini
        raw = gemini.as_json(_gen_prompt(topic, count, language, doc),
                             tier=gemini.SMART, timeout_ms=60_000)
        pairs = _clean_cards(raw)
        if not pairs:
            _announce.say(f"Tell the user you couldn't produce flashcards about {label}: the "
                          f"model returned nothing usable. Suggest trying again shortly.")
            return
        with _lock:
            data = _load()
            cards = data["decks"].setdefault(deck, [])
            seen = {c["front"].casefold() for c in cards}
            added = 0
            for f, b in pairs[:count]:
                if f.casefold() in seen:
                    continue
                seen.add(f.casefold())
                cards.append(_new_card(f, b, source=label))
                added += 1
            _announce.save(_DATA, data)
        p = _announce.player()
        if p is not None:
            try:
                p.show_content(f"New cards: {label}"[:48],
                               "\n\n".join(f"Q: {f}\nA: {b}" for f, b in pairs[:count]))
            except Exception:
                pass
        _announce.say(f"{added} new flashcards about {label} are in the '{deck}' deck and "
                      f"due now. Tell the user briefly and that they can say 'start my "
                      f"review' whenever they're ready.")
    except Exception as e:
        _announce.say(f"Tell the user generating flashcards failed: {e}")


def _generate(p: dict, player) -> str:
    topic = str(p.get("topic") or "").strip()
    try:
        count = max(1, min(int(float(p.get("count") or 10)), 50))
    except (TypeError, ValueError):
        count = 10
    language = str(p.get("language") or "").strip()
    doc, label = "", topic
    if not topic or re.search(r"\b(this|the (file|document|upload)|uploaded)\b", topic, re.I):
        path = _doc_text.uploaded_file(player)
        if not path:
            return "Tell me what topic the flashcards should cover, or upload a document first."
        doc, err = _doc_text.read_text(path, 30_000)
        if err:
            return f"I couldn't use the uploaded file: {err}."
        from pathlib import Path
        label = Path(path).stem
    deck = _deck_name(p)
    threading.Thread(target=_generate_worker,
                     args=(deck, topic, count, language, doc, label),
                     daemon=True, name="flashcards-gen").start()
    return (f"Writing {count} flashcards about {label} for the '{deck}' deck. "
            f"I'll tell you when they're ready.")


def _show(player, title: str, text: str) -> None:
    if player is None:
        return
    try:
        player.show_content(title, text)
    except Exception:
        pass


def _review(p: dict, player) -> str:
    with _lock:
        data = _load()
        deck = str(p.get("deck") or "").strip() or _session.get("deck") or None
        pools = ([(deck, data["decks"].get(deck, []))] if deck
                 else list(data["decks"].items()))
        if deck and deck not in data["decks"]:
            return f"There's no deck called {deck}."
        if not any(cards for _, cards in pools):
            return "Your flashcard deck is empty. Add a card or ask me to generate some."
        due = [(name, c) for name, cards in pools for c in _due(cards)]
        due.sort(key=lambda nc: float(nc[1].get("due", 0)))
        if not due:
            upcoming = min(float(c.get("due", 0)) for _, cards in pools for c in cards)
            _session.update(card=None, revealed=False)
            done = _session.get("done", 0)
            lead = f"That's all your due cards — {done} reviewed, nicely done! " if done else ""
            return f"{lead}No cards are due right now; the next one is due {_when(upcoming - time.time())}."
        name, card = due[0]
        _session.update(deck=deck, card=(name, card["id"]), revealed=False)
    _show(player, f"Flashcard · {name}", f"{card['front']}\n\n(say 'show the answer')")
    return (f"Card 1 of {len(due)} due: {card['front']} — "
            f"say 'show the answer' when you're ready.")


def _current():
    ref = _session.get("card")
    if not ref:
        return None, None, None
    data = _load()
    deck, cid = ref
    for c in data["decks"].get(deck, []):
        if c.get("id") == cid:
            return data, deck, c
    return None, None, None


def _reveal(player) -> str:
    with _lock:
        data, deck, card = _current()
        if card is None:
            return "There's no card in review right now. Say 'start my review' first."
        _session["revealed"] = True
    _show(player, f"Flashcard · {deck}",
          f"{card['front']}\n\n— — —\n\n{card['back']}\n\n(rate it: again / hard / good / easy)")
    return f"Answer: {card['back']}. How did you do — again, hard, good or easy?"


def _grade(p: dict, player) -> str:
    grade = parse_grade(p.get("grade"))
    if grade is None:
        return "Tell me how you did: again, hard, good or easy."
    with _lock:
        data, deck, card = _current()
        if card is None:
            return "There's no card to grade yet. Say 'start my review' first."
        schedule(card, grade)
        _announce.save(_DATA, data)
        _session["done"] = _session.get("done", 0) + 1
        _session["card"] = None
        wait = float(card["due"]) - time.time()
    head = f"Marked {grade}; you'll see that card again {_when(wait)}."
    nxt = _review({"deck": _session.get("deck") or ""}, player)
    return f"{head} {nxt}"


def _stats(p: dict) -> str:
    data = _load()
    decks = data["decks"]
    want = str(p.get("deck") or "").strip()
    if want:
        decks = {want: decks.get(want, [])}
    total = sum(len(c) for c in decks.values())
    if total == 0:
        return ("Your flashcard deck is empty. Add a card or ask me to generate some on any "
                "topic.")
    now = time.time()
    parts = []
    for name, cards in decks.items():
        if not cards:
            continue
        due = len(_due(cards, now))
        new = sum(1 for c in cards if int(c.get("reps", 0)) == 0 and int(c.get("lapses", 0)) == 0)
        mature = sum(1 for c in cards if float(c.get("interval", 0)) >= 21)
        parts.append(f"{name}: {len(cards)} cards, {due} due, {new} new, {mature} mature")
    due_all = sum(len(_due(c, now)) for c in decks.values())
    tail = (f" {due_all} due now — say 'start my review' whenever you're ready."
            if due_all else " Nothing is due right now.")
    return "; ".join(parts) + "." + tail


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        action = str(p.get("action") or "stats").strip().lower()
        if action in ("add", "create", "new"):
            return _add(p)
        if action in ("generate", "make", "write", "batch"):
            return _generate(p, player)
        if action in ("review", "start", "next", "study"):
            return _review(p, player)
        if action in ("reveal", "show", "answer", "flip"):
            return _reveal(player)
        if action in ("grade", "rate", "score"):
            return _grade(p, player)
        if action in ("stats", "status", "due", "count", "summary"):
            return _stats(p)
        return "I can add cards, generate a batch, run a review, or show your deck stats."
    except Exception as e:
        return f"The flashcards hit an error: {e}"
