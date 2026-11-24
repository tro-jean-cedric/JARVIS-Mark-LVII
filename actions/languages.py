"""
actions/languages.py — "only speak French", "add English", "which languages?"

Changes the ACTIVE languages (core/languages.py) by voice; the same setting as
⚙ CUSTOMISE ASSISTANT → LANGUAGES. A change rebuilds the voice session a few
seconds later, so the confirmation is heard first.
"""
from __future__ import annotations

import threading

from core import languages


def _reconnect_soon(player, delay: float = 6.0) -> None:
    cb = getattr(player, "on_language_change", None) if player is not None else None
    if not callable(cb):
        return

    def _later():
        try:
            cb()
        except Exception as e:
            print(f"[Languages] reconnect failed: {e}")

    t = threading.Timer(delay, _later)
    t.daemon = True
    t.start()


def _describe(codes: list[str]) -> str:
    if not codes:
        return "automatic (any language — it follows the user)"
    return languages.human_list(languages.names(codes))


def set_languages(parameters: dict, player=None) -> str:
    try:
        action = str((parameters or {}).get("action") or "list").strip().lower()
        raw = (parameters or {}).get("languages") or []
        if isinstance(raw, str):
            raw = [p for p in raw.replace(" and ", ",").split(",")]
        wanted = [languages.to_code(x) for x in raw]
        unknown = [str(x).strip() for x, c in zip(raw, wanted) if str(x).strip() and not c]
        wanted = [c for c in wanted if c]
        current = languages.active()

        if action == "list":
            supported = ", ".join(n for _c, n, *_ in languages.SUPPORTED)
            return (f"status=done active=\"{_describe(current)}\" supported=\"{supported}\" -- "
                    "Tell the user briefly which languages are on.")

        if action == "automatic":
            new = []
        elif action == "set":
            new = wanted
        elif action == "add":
            new = current + [c for c in wanted if c not in current]
        elif action == "remove":
            if not current:
                return ("status=failed reason=\"languages are automatic, nothing to remove — say "
                        "which ones to keep instead\"")
            new = [c for c in current if c not in wanted]
            if not new:
                return ("status=failed reason=\"that would switch off every language — say which "
                        "one to keep, or ask for automatic\"")
        else:
            return f"status=failed reason=\"unknown action {action}\""

        if action in ("set", "add") and not wanted:
            msg = f"none of these is supported: {', '.join(unknown)}" if unknown else "no language named"
            return f"status=failed reason=\"{msg}\" supported=\"{', '.join(languages.names(languages.CODES))}\""

        if new == current:
            return f"status=unchanged active=\"{_describe(current)}\""

        saved = languages.set_active(new)
        _reconnect_soon(player)
        extra = f" not_supported=\"{', '.join(unknown)}\"" if unknown else ""
        confirm_in = languages.name(saved[0]) if saved else "the user's language"
        return (f"status=done active=\"{_describe(saved)}\"{extra} -- Confirm in one short sentence, "
                f"in {confirm_in}. The voice session restarts in a few seconds to apply it.")
    except Exception as e:
        return f"status=failed reason=\"{e}\""


TOOL = {
    "name": "languages",
    "description": (
        "Sets which languages the assistant listens to and speaks (its ACTIVE languages). Use when "
        "the user says to speak / answer only in some language(s), to add or remove a language, "
        "to go back to any language (automatic), or asks which languages are on. Examples: 'only "
        "speak French', 'speak French and English only', 'add Japanese', 'stop using Portuguese', "
        "'answer in any language again', 'which languages are you using?'. action: set (replace "
        "with exactly these) | add | remove | automatic | list. Not for a one-off translation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["set", "add", "remove", "automatic", "list"]},
            "languages": {"type": "ARRAY", "items": {"type": "STRING"},
                          "description": "Language names in English or ISO codes, e.g. ['French', 'English']"},
        },
        "required": ["action"],
    },
    "handler": set_languages,
}
