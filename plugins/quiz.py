"""
plugins/quiz.py — put a quiz on the HUD with player.show_quiz.

Two ways in:
  * the assistant passes `questions` it wrote itself — shown at once;
  * it passes only a topic (or "this" with a file in the upload box) — the
    questions are written by core.gemini on a worker thread, then shown.

If the model gives nothing back, a small built-in fallback is used: gap-fill
questions cut from the uploaded document, or open questions about the topic
that the assistant marks itself. The user's answers come back to the
conversation by themselves (the HUD sends the result), so run() never waits.
"""
from __future__ import annotations

import difflib
import random
import re
import threading
import unicodedata
from pathlib import Path

from plugins import _announce, _doc_text

PLUGIN = {
    "name": "quiz",
    "description": (
        "Puts an interactive quiz on screen for the user to answer — use for 'quiz me "
        "on…', 'test me', 'ask me 10 questions about…', 'let's revise', 'quiz me on "
        "this document'. Say one short sentence about what you'll test first. You may "
        "write the questions yourself in `questions` (in the user's language, mixed "
        "types, with a short note explaining each answer); if you only give `topic`, "
        "the questions are written for you and appear a few seconds later. With a "
        "file uploaded and no topic, the quiz is built from that file. Returns "
        "immediately; the user's results come back to you later as a message — mark "
        "open answers then and tell them how they did. action='stop' clears the quiz. "
        "For memorising over weeks use spaced_repetition instead."
    ),
    "brief": "Interactive on-screen quiz on any topic or the uploaded document.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "'start' (default) or 'stop'."},
            "topic": {"type": "STRING",
                      "description": "What is tested, in the user's language; also the "
                                     "on-screen title. Empty + uploaded file = quiz on that file."},
            "count": {"type": "INTEGER", "description": "Number of questions, 1-20 (default 5)."},
            "difficulty": {"type": "STRING",
                           "description": "Optional: 'easy', 'medium', 'hard' or a level such "
                                          "as 'high school'."},
            "language": {"type": "STRING", "description": "Optional language for the questions."},
            "use_file": {"type": "BOOLEAN",
                         "description": "True to base the quiz on the uploaded document."},
            "questions": {
                "type": "ARRAY",
                "description": "Optional: the questions you wrote, in order (1-20).",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "type": {"type": "STRING",
                                 "description": "'multiple_choice' (3-5 options), 'true_false' "
                                                "(2 options in the user's language), "
                                                "'fill_blank' (sentence with ___) or 'open'."},
                        "question": {"type": "STRING"},
                        "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "answer": {"type": "STRING",
                                   "description": "Correct answer, exactly as in options when "
                                                  "there are options; a model answer for open."},
                        "note": {"type": "STRING",
                                 "description": "One-line explanation shown after answering."},
                    },
                },
            },
        },
        "required": [],
    },
}


# ── normalisation & marking ──────────────────────────────────────────────────

def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"^(the|a|an)\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def normalize_questions(raw) -> list[dict]:
    """Model/assistant output → the dicts show_quiz expects. Bad entries are
    dropped; a choice question whose answer is not among its options becomes an
    open question rather than an unanswerable one."""
    if isinstance(raw, dict):
        raw = raw.get("questions") or []
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or q.get("q") or "").strip()
        answer = str(q.get("answer") or q.get("correct") or "").strip()
        if not text:
            continue
        kind = str(q.get("type") or "").strip().lower()
        options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
        if kind in ("fill_blank", "open"):
            options = []
        if options:
            match = next((o for o in options if _norm(o) == _norm(answer)), None)
            if match is None and len(answer) == 1 and answer.upper() in "ABCDE":
                i = "ABCDE".index(answer.upper())
                match = options[i] if i < len(options) else None
            if match is None:
                options, kind = [], "open"
            else:
                answer = match
        if not kind:
            kind = "multiple_choice" if options else ("fill_blank" if "___" in text else "open")
        out.append({"question": text[:400], "options": options[:6], "answer": answer[:300],
                    "note": str(q.get("note") or q.get("explanation") or "").strip()[:300],
                    "type": kind})
        if len(out) >= 20:
            break
    return out


def grade(question: dict, given) -> bool | None:
    """True / False when the answer is clear-cut, None to let the assistant judge."""
    try:
        answer, g = _norm(question.get("answer")), _norm(given)
        if not g:
            return False
        if question.get("options"):
            return g == answer
        if question.get("type") == "open" or not answer:
            return None
        if g == answer:
            return True
        # near miss on a gap-fill (typo, plural): leave it to the assistant
        if difflib.SequenceMatcher(None, g, answer).ratio() >= 0.75:
            return None
        return False
    except Exception:
        return None


# ── generation ───────────────────────────────────────────────────────────────

def _prompt(topic: str, count: int, difficulty: str, language: str, doc: str) -> str:
    src = (f"Base every question only on this document:\n<<<\n{doc}\n>>>\n" if doc
           else f"Subject: {topic}\n")
    return (
        f"Write a quiz of {count} questions.\n{src}"
        + (f"Difficulty: {difficulty}\n" if difficulty else "")
        + (f"Write it in {language}.\n" if language else
           "Write it in the language of the subject text.\n")
        + "Mix question types: mostly multiple_choice (4 options), some true_false (the two "
          "options are the words for true and false in that language), a fill_blank or two "
          "(sentence with ___ and the missing word as answer), at most one open question. "
          "For questions with options the answer must equal one option exactly. Add a short "
          "'note' explaining each answer.\n"
          'Reply with JSON only: [{"type": "...", "question": "...", "options": [...], '
          '"answer": "...", "note": "..."}]'
    )


def fallback_questions(topic: str, doc: str, count: int) -> list[dict]:
    """Questions that need no model: gap-fills cut from the document, or open
    questions on the topic that the assistant marks itself."""
    qs: list[dict] = []
    if doc:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", doc) if 40 <= len(s.strip()) <= 220]
        random.shuffle(sentences)
        for s in sentences:
            words = [w for w in re.findall(r"\b[^\W\d_]{6,}\b", s)]
            if not words:
                continue
            w = max(words, key=len)
            qs.append({"question": "Fill the gap: " + s.replace(w, "___", 1), "options": [],
                       "answer": w, "note": s, "type": "fill_blank"})
            if len(qs) >= count:
                break
        if qs:
            return qs
    t = topic or "this subject"
    generic = [
        f"In two or three sentences, explain what {t} is.",
        f"Name one key fact, date or figure connected to {t}.",
        f"What is the most important idea to understand about {t}, and why?",
        f"Give a real-world example related to {t}.",
        f"What is a common misconception about {t}?",
    ]
    return [{"question": g, "options": [], "answer": "", "note": "", "type": "open"}
            for g in generic[:max(1, min(count, len(generic)))]]


def _show(player, topic: str, questions: list[dict]) -> bool:
    if player is None or not hasattr(player, "show_quiz"):
        return False
    try:
        player.show_quiz(topic, questions, grade)
        return True
    except Exception as e:
        _announce.log(f"Quiz could not be shown: {e}")
        return False


def _build_and_show(player, topic: str, count: int, difficulty: str, language: str,
                    doc: str) -> None:
    try:
        from core import gemini
        raw = gemini.as_json(_prompt(topic, count, difficulty, language, doc),
                             tier=gemini.SMART, timeout_ms=60_000)
        questions = normalize_questions(raw)[:count]
        used_fallback = not questions
        if used_fallback:
            questions = fallback_questions(topic, doc, count)
        if not _show(player, topic, questions):
            _announce.say("Tell the user the quiz could not be put on screen.")
            return
        _announce.say(
            f"The {len(questions)}-question quiz on {topic} is now on screen"
            + (" (a simple built-in set, because the model did not answer)" if used_fallback else "")
            + ". Tell them to take their time; you'll go through the results together "
              "when they finish.")
    except Exception as e:
        _announce.say(f"Tell the user building the quiz failed: {e}")


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        action = str(p.get("action") or "start").strip().lower()
        if action in ("stop", "clear", "close", "cancel", "hide"):
            if player is not None and hasattr(player, "hide_quiz"):
                try:
                    player.hide_quiz()
                except Exception:
                    pass
            return "Quiz cleared."

        if player is None or not hasattr(player, "show_quiz"):
            return "I can't show a quiz here — there's no screen attached."

        topic = str(p.get("topic") or "").strip()[:80]
        try:
            count = max(1, min(int(float(p.get("count") or 5)), 20))
        except (TypeError, ValueError):
            count = 5

        given = normalize_questions(p.get("questions"))
        if given:
            if not _show(player, topic or "Quiz", given):
                return "The quiz could not be shown on screen."
            return (f"The quiz is on screen: {len(given)} questions. Tell the user to take "
                    f"their time; you'll go through the results together when they finish.")

        doc = ""
        want_file = bool(p.get("use_file")) or not topic or \
            re.search(r"\b(this|the (file|document|upload)|uploaded)\b", topic, re.I)
        if want_file:
            path = _doc_text.uploaded_file(player)
            if path:
                doc, err = _doc_text.read_text(path, 30_000)
                if err:
                    return f"I couldn't use the uploaded file: {err}."
                if not topic or re.search(r"\b(this|uploaded)\b", topic, re.I):
                    topic = Path(path).stem[:80]
            elif not topic:
                return "What should the quiz be about? Or upload a document and say 'quiz me on this'."

        threading.Thread(target=_build_and_show,
                         args=(player, topic, count, str(p.get("difficulty") or "").strip(),
                               str(p.get("language") or "").strip(), doc),
                         daemon=True, name="quiz-build").start()
        return (f"Writing {count} question{'s' if count != 1 else ''} on {topic}; they'll appear on screen in a "
                f"few seconds.")
    except Exception as e:
        return f"The quiz hit an error: {e}"
