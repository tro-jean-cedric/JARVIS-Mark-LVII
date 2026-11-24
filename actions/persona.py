"""
JARVIS action — the assistant's character, as the user wants it, kept for good.

People shape an assistant by talking to it: "call me boss", "fewer jokes",
"keep it short". Said once, that should hold tomorrow too. save_memory is the
wrong home for it — memory is facts about the USER, and a fact is recalled when
relevant; a wish about how the assistant BEHAVES has to be in force on every
single answer. So wishes live here, one per aspect, and main.py puts them into
the system instruction as a [PERSONA] block at the start of every session.

STORAGE
    A small dict {aspect: wish} under the `persona` key of the settings file,
    written through memory.config_manager.update_config (never by hand), so it
    is backed up and moved with the rest of the user's settings.

THE CHANGE IS IN FORCE AT ONCE
    The system instruction is only read when a session starts, so the tool's
    answer itself tells the model to follow the new wish from this reply on.
    Every change is pushed onto core.undo, so "undo that" puts it back.
"""
from __future__ import annotations

import re

from core import outcome

_KEY = "persona"
_MAX_ASPECTS = 16
_MAX_WISH = 240

# What a well-known aspect is about, for the line in the prompt. Anything else
# the model invents ("accent", "emoji") is kept under its own name.
_ASPECT_HINTS = {
    "address": "how to address the user",
    "tone": "tone and formality",
    "humour": "humour",
    "humor": "humour",
    "length": "answer length",
    "proactivity": "how proactive to be",
    "catchphrase": "catchphrase",
    "style": "way of speaking",
    "avoid": "things to avoid",
}


def _load() -> dict:
    try:
        from memory.config_manager import load_api_keys
        data = load_api_keys().get(_KEY)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}


def _save(wishes: dict) -> None:
    from memory.config_manager import update_config
    update_config(**{_KEY: dict(wishes)})


def _norm_aspect(text: str) -> str:
    t = re.sub(r"[^a-z0-9_ ]", "", str(text or "").strip().lower())
    t = "_".join(t.split())[:32]
    return "humour" if t == "humor" else t


def get_persona() -> dict:
    """The stored wishes, {aspect: wish}. Never raises."""
    return _load()


def prompt_block() -> str:
    """The [PERSONA] block main.py puts into the system instruction, or ''.

    Phrased so that the user's wishes win over the default character described
    further down in prompt.txt, and so that a name or catchphrase is used
    exactly as the user gave it.
    """
    wishes = _load()
    if not wishes:
        return ""
    lines = ["[PERSONA]",
             "The user has told you how they want you to be. These wishes override "
             "the default character and voice described elsewhere in your "
             "instructions (but never the rules about honesty, tools or safety). "
             "Keep names, titles and catchphrases exactly as written:"]
    for aspect, wish in wishes.items():
        hint = _ASPECT_HINTS.get(aspect)
        label = f"{aspect} ({hint})" if hint and hint != aspect else aspect
        lines.append(f"- {label}: {wish}")
    return "\n".join(lines) + "\n\n"


def _push_undo(before: dict, label: str) -> None:
    try:
        from core.undo import push_undo

        def _restore() -> str:
            _save(before)
            return ("The persona is back as it was. From this answer on, behave as "
                    "before that change.")
        push_undo(label, _restore)
    except Exception as e:
        print(f"[Persona] undo not recorded: {e}")


def _listing(wishes: dict) -> str:
    if not wishes:
        return ("No persona wishes are stored: you are using your default character. "
                "Tell the user they can shape it simply by saying how they want you to be.")
    return "Persona wishes in force:\n" + "\n".join(f"- {k}: {v}" for k, v in wishes.items())


def persona(parameters: dict, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").strip().lower()
    aspect = _norm_aspect(params.get("aspect", ""))
    wish = " ".join(str(params.get("wish") or "").split())[:_MAX_WISH]

    try:
        wishes = _load()
        before = dict(wishes)

        if action == "list":
            return _listing(wishes)

        if action == "set":
            if not wish:
                return outcome.ask("persona", "how exactly the user wants you to be")
            if not aspect:
                aspect = "style"
            if aspect not in wishes and len(wishes) >= _MAX_ASPECTS:
                return outcome.failed("persona", f"there are already {_MAX_ASPECTS} wishes; "
                                                 "remove one first")
            wishes[aspect] = wish
            _save(wishes)
            _push_undo(before, f"persona: {aspect}")
            if player is not None:
                try:
                    player.write_log(f"SYS: Persona — {aspect}: {wish[:60]}")
                except Exception:
                    pass
            return (outcome.done("persona", aspect=aspect, saved=True)
                    + f" Kept for every conversation from now on: {aspect} = {wish}. "
                      "Follow it from THIS answer on; confirm in one short sentence, "
                      "already speaking that way.")

        if action == "remove":
            if not aspect:
                return outcome.ask("persona", "which part of your character to put back")
            if aspect not in wishes:
                return (f"There is no stored wish called '{aspect}'. " + _listing(wishes))
            removed = wishes.pop(aspect)
            _save(wishes)
            _push_undo(before, f"persona: remove {aspect}")
            return (outcome.done("persona", removed=aspect)
                    + f" '{removed}' no longer applies: go back to your default for "
                      f"{aspect} from this answer on, even if earlier answers in this "
                      "conversation did otherwise.")

        if action == "clear":
            if not wishes:
                return "There were no persona wishes to clear; you are already your default self."
            _save({})
            _push_undo(before, "persona: clear")
            return (outcome.done("persona", cleared=len(before))
                    + " All persona wishes are gone: from this answer on you are your "
                      "default self again - stop doing them now, even if your earlier "
                      "answers in this conversation did.")

        return outcome.failed("persona", f"unknown action '{action}'; use set, remove, clear or list")
    except Exception as e:
        return outcome.failed("persona", f"the persona could not be changed: {e}")


TOOL = {
    "name": "persona",
    "description": (
        "Your own character as the user wants it, kept across every conversation. Call it "
        "when the user tells you how you should BE or SPEAK from now on: what to call them "
        "(a name, title or nickname), tone or formality, humour, answer length, how "
        "proactive to be, a catchphrase, a speaking style, something to stop doing - in "
        "any language ('call me captain', 'be more relaxed', 'no more jokes', 'shorter "
        "answers'). Not for a one-off request ('explain this one in detail' is simply "
        "answered) and not save_memory - wishes about your behaviour belong here. "
        "set adds or replaces the wish for one aspect; remove restores one aspect to "
        "default; clear removes them all ('be yourself again'); list shows them. The "
        "wishes in force are under [PERSONA] in your instructions."
    ),
    "brief": "How you should be (address, tone, humour, length, style): set/remove/clear/list.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "set | remove | clear | list"},
            "aspect": {
                "type": "STRING",
                "description": ("Short English name of what the wish is about: address, tone, "
                                "humour, length, proactivity, catchphrase, style, avoid - or "
                                "another. Reuse the existing name to change a wish under [PERSONA]."),
            },
            "wish": {
                "type": "STRING",
                "description": ("For set: the wish as a short instruction in English describing the "
                                "style (e.g. 'warm and casual, like an old friend'), not sample "
                                "phrases. Names, titles and catchphrases stay exactly as the user "
                                "said them, in their language."),
            },
        },
        "required": ["action"],
    },
    "handler": persona,
}
