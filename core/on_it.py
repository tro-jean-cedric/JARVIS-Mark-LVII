"""
core/on_it.py — a short "on it" line, said the moment a slow tool starts.

WHY
    While the live model waits for a tool it says nothing, and some tools take
    several seconds (a web search, a calendar read, opening a program). From
    the user's side that silence looks exactly like not having been heard, so
    they repeat themselves — and the repeat lands on top of the answer.

    The live session cannot speak during that wait: it is the thing waiting.
    So the lines are recorded AHEAD of time, in the assistant's own Live voice
    (core.gemini.say_audio), and played straight into the speaker queue when a
    tool has been running for DELAY_S. Nothing is generated at the moment it is
    needed, which is what makes it instant.

THE LINES
    Three per language, written once by a model in that language (never
    translated by hand here, never hard-coded). They are kept short (six
    words at most), never ask anything, never promise an outcome, never
    mention the request and never name the user — the same three are reused
    for every request and every user. A line that breaks one of those rules is dropped; fewer than two
    usable lines means none are kept and nothing is played, which is better
    than a wrong one. Each line is recorded once, its transcript checked
    against the text, and kept in memory/on_it/ with the voice it was
    recorded in: change the voice and they are recorded again.

    Played in rotation, never the same line twice in a row.

WHEN IT STAYS SILENT
    The language is not known yet; the lines for it are not ready (they are
    then prepared in the background for next time); the assistant is already
    speaking; a local voice engine is in use (it would not be the same voice);
    or the user switched it off ("on_it_lines": false).
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable, Optional

DELAY_S = 1.2            # a tool quicker than this needs no "on it"
MAX_WORDS = 6
WANT_LINES = 3


def enabled() -> bool:
    try:
        from memory.config_manager import get_on_it_enabled
        return get_on_it_enabled()
    except Exception:
        return False


def _default_folder() -> Path:
    try:
        from memory.config_manager import get_base_dir
        return Path(get_base_dir()) / "memory" / "on_it"
    except Exception:
        return Path(__file__).resolve().parent.parent / "memory" / "on_it"


def language_of(text: str, previous: str = "") -> str:
    """The language code of what the user said, or `previous` when that
    cannot be told (too short, mixed). '' when neither is known."""
    try:
        from core import languages
        act = languages.active()
        if len(act) == 1:
            return act[0]
        code, confidence = languages.detect(text or "")
        if code and confidence >= 0.5 and (not act or code in act):
            return code
    except Exception:
        pass
    return previous


def _language_name(code: str) -> str:
    try:
        from core import languages
        return languages.name(code) or code
    except Exception:
        return code


def line_prompt(code: str) -> str:
    return (
        "Someone has just asked a voice assistant for something, and it has started "
        "working on it; the answer will take a few seconds. Write three different, "
        "very short things the assistant could say at once so the person knows it "
        f"is busy with their request. Write them in {_language_name(code)}, spelt "
        "correctly. Keep each to six words or fewer. None may ask anything, promise "
        "how it will turn out, mention what was requested, or call the person by "
        "any name or title, since the same lines get reused for every request and "
        "every user. Put each on its own line with nothing else: no numbering, no "
        "quotation marks."
    )


def clean_lines(raw: str, code: str = "") -> list[str]:
    """The usable lines out of a model's reply (see the module docstring)."""
    out: list[str] = []
    expected = ""
    try:
        from core import languages
        expected = languages._BY_CODE.get(code, ("", "", ""))[2] if code else ""
        script_of = languages.script_of
    except Exception:
        script_of = None
    for line in str(raw or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("\"'“”«»").strip()
        if not line or "?" in line or "？" in line:
            continue
        words = re.findall(r"\w+", line)
        # Scripts written without spaces count characters instead of words.
        too_long = (len(line) > 18) if expected in ("han", "japanese", "thai") else len(words) > MAX_WORDS
        if not words or too_long:
            continue
        # Japanese written only in kanji reads as Chinese script; still Japanese.
        allowed = {expected, ""} | ({"han"} if expected == "japanese" else set())
        if expected and script_of is not None and script_of(line) not in allowed:
            continue
        if line.casefold() not in (l.casefold() for l in out):
            out.append(line)
    return out[:WANT_LINES]


class Lines:
    """The recorded lines, per language, on disk. `write(prompt) -> str` and
    `record(text, voice) -> bytes|None` are injectable (the self-test's
    stand-ins); they default to core.gemini."""

    def __init__(self, folder: Optional[Path] = None,
                 write: Optional[Callable[[str], str]] = None,
                 record: Optional[Callable[[str, str], Optional[bytes]]] = None):
        self.folder = Path(folder) if folder else _default_folder()
        self._write = write or _write_with_gemini
        self._record = record or _record_with_gemini
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._last: dict[str, int] = {}

    # -- disk -----------------------------------------------------------------
    def _index_path(self) -> Path:
        return self.folder / "index.json"

    def _index(self) -> dict:
        try:
            data = json.loads(self._index_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_index(self, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._index_path())

    def entry(self, code: str, voice: str) -> Optional[dict]:
        e = self._index().get(code)
        if not isinstance(e, dict) or e.get("voice") != voice:
            return None
        files = [self.folder / f for f in e.get("files", [])]
        if len(files) < 2 or not all(f.is_file() for f in files):
            return None
        return e

    def ready(self, code: str, voice: str) -> bool:
        return bool(code) and self.entry(code, voice) is not None

    # -- making them ------------------------------------------------------------
    def prepare(self, code: str, voice: str) -> bool:
        """Write and record the lines for `code` in `voice`. Blocking (a few
        seconds of model calls). True when they are ready. Never raises."""
        if not code:
            return False
        if self.ready(code, voice):
            return True
        try:
            lines = clean_lines(self._write(line_prompt(code)) or "", code)
            if len(lines) < 2:
                print(f"[OnIt] no usable lines for {code}: {lines}")
                return False
            self.folder.mkdir(parents=True, exist_ok=True)
            files, kept = [], []
            for i, text in enumerate(lines):
                pcm = self._record(text, voice)
                if not pcm:
                    continue
                name = f"{code}_{re.sub(r'[^A-Za-z0-9]', '', voice) or 'voice'}_{i}.pcm"
                (self.folder / name).write_bytes(pcm)
                files.append(name)
                kept.append(text)
            if len(files) < 2:
                print(f"[OnIt] lines for {code} could not be recorded")
                return False
            with self._lock:
                data = self._index()
                data[code] = {"voice": voice, "lines": kept, "files": files}
                self._save_index(data)
            print(f"[OnIt] ready for {code}: {kept}")
            return True
        except Exception as e:
            print(f"[OnIt] could not prepare {code}: {e}")
            return False

    def prepare_later(self, code: str, voice: str) -> None:
        """`prepare` on a background thread, at most one per language at a time."""
        key = f"{code}/{voice}"
        with self._lock:
            if not code or key in self._busy:
                return
            self._busy.add(key)

        def run():
            try:
                self.prepare(code, voice)
            finally:
                with self._lock:
                    self._busy.discard(key)

        threading.Thread(target=run, daemon=True, name="on-it-lines").start()

    # -- using them ---------------------------------------------------------------
    def pick(self, code: str, voice: str) -> Optional[tuple[str, bytes]]:
        """(text, PCM) of the next line for `code`, never the one used last
        time; None when none are ready."""
        e = self.entry(code, voice)
        if e is None:
            return None
        n = len(e["files"])
        i = (self._last.get(code, -1) + 1) % n
        self._last[code] = i
        try:
            return e["lines"][i], (self.folder / e["files"][i]).read_bytes()
        except Exception:
            return None


def _write_with_gemini(prompt: str) -> str:
    from core import gemini
    return gemini.text(prompt, tier=gemini.FAST)


def _record_with_gemini(text: str, voice: str) -> Optional[bytes]:
    from core import gemini
    return gemini.say_audio(text, voice)
