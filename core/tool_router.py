"""
The tool router: rarely used features behind one "use_feature" tool.

WHY
    Every tool declaration is sent to the live model at the start of every
    session and sits in its context for every turn. The big, everyday tools
    (open an app, change a setting, search the web) earn that place. Thirty
    plugins the user reaches for once a week do not — and their long, careful
    descriptions are most of the payload. A model choosing between fifty tools
    also picks the wrong one more often than a model choosing between twenty.

    So when the router is on, every declaration marked `"tier": "extra"` is
    taken out of the live list and replaced by ONE declaration: `use_feature`,
    with a feature name (an enum of the hidden ones, each with a one-line
    summary in the description) and the user's request in their own words.
    When it is called, a second, text-only model call reads that feature's
    full schema and writes the real arguments; core/tool_args.py checks them,
    one repair round fixes what it found, and the feature runs exactly as if
    the live model had called it directly.

WHAT STAYS DIRECT
    - the inline tools in main.py and anything marked `"tier": "core"`;
    - anything with no tier at all (user-added plugins, older files): the
      router only moves what a file has itself said is an extra;
    - declarations with a Live `behavior` (NON_BLOCKING): that only works on
      a tool the live model calls by its own name;
    - everything, when fewer than MIN_DEFERRED features would move — one
      router call in front of two tools saves nothing.

SWITCHED-OFF FEATURES
    A plugin the user switched off is still listed, marked "(off)". Without
    it the model, asked for that feature, improvises with whatever tool is
    nearest — or says it has no such ability, which is not true. Listed, it
    calls the router, and the router answers with how to switch it on.

COST AND FAILURE
    Off by default (`"tool_router": true` in config turns it on), so a user
    who never asks for it gets exactly the tool list they always had. When on,
    a feature whose schema has no parameters skips the model call entirely;
    otherwise the call is one FAST-tier request through core.gemini. A model
    that does not answer is reported as a failed outcome, never as a guess.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from core import outcome, tool_args

ROUTER_NAME = "use_feature"

# Fewer features than this and the router stays out of the way.
MIN_DEFERRED = 3

# How much of each piece of context goes into the argument-writing prompt.
_REQUEST_CHARS = 4000
_CONVERSATION_CHARS = 1500
_LINE_CHARS = 110

# Keys that only this project reads (providers/schema.py). Not shown to the
# argument writer: they say nothing about the arguments.
_META = ("tier", "brief", "behavior", "scheduling")


def enabled() -> bool:
    """Whether the user turned the router on. Never raises."""
    try:
        from memory.config_manager import get_tool_router_enabled
        return get_tool_router_enabled()
    except Exception:
        return False


def _name(decl: Any) -> str:
    if isinstance(decl, dict):
        return str(decl.get("name") or "")
    return str(getattr(decl, "name", "") or "")


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "")).casefold()


def summary(decl: dict, limit: int = _LINE_CHARS) -> str:
    """One short line about what a feature does: its `brief` when it has one,
    else the first sentence of its description, cut to `limit` characters."""
    text = " ".join(str(decl.get("brief") or "").split())
    if not text:
        text = " ".join(str(decl.get("description") or "").split())
        m = re.search(r"(?<=[.!?])\s", text)
        if m and m.start() >= 20:
            text = text[:m.start()]
    if len(text) > limit:
        text = text[:limit - 1].rstrip(" ,;:-") + "…"
    return text


def can_defer(decl: Any) -> bool:
    """True for a declaration the router may take out of the live list."""
    return (isinstance(decl, dict)
            and bool(_name(decl))
            and str(decl.get("tier") or "").strip().lower() == "extra"
            and not decl.get("behavior"))


class Menu:
    """The features behind the router for one session, and how to run one.

    `features` maps name -> full declaration (the one the live model no longer
    sees); `off` maps name -> one-line summary for switched-off plugins.
    """

    def __init__(self, features: dict[str, dict], off: dict[str, str]):
        self.features = features
        self.off = off

    # -- the declaration the live model sees --------------------------------
    def declaration(self) -> dict:
        lines = [f"- {n}: {summary(d)}" for n, d in sorted(self.features.items())]
        lines += [f"- {n} (off): {s}" for n, s in sorted(self.off.items())]
        description = (
            "Runs one of the extra features in the list below. Call it whenever "
            "the user wants something one of them does. Give the feature's name, "
            "and put the user's wish in `request`: in their language, keeping "
            "every name, number, date, place and choice they mentioned, because "
            "that text is all the feature has to choose its settings from. Only "
            "the names below exist. When the feature needs text that you compose "
            "(a message body, questions for a quiz, a document, some code), write "
            "that text out completely inside `request`; nothing else will write "
            "it. Call a feature marked (off) all the same when its job is wanted: "
            "the reply explains how to turn it on. Its job is never to be done "
            "with a different tool.\nFeatures:\n" + "\n".join(lines))
        return {
            "name": ROUTER_NAME,
            "description": description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "feature": {
                        "type": "STRING",
                        "description": "Name of the feature to run, from the list.",
                        "enum": sorted(self.features) + sorted(self.off),
                    },
                    "request": {
                        "type": "STRING",
                        "description": ("What the user asked for, as they said it "
                                        "and in the language they used, details "
                                        "included."),
                    },
                },
                "required": ["feature", "request"],
            },
        }

    # -- running one --------------------------------------------------------
    def find(self, feature: str) -> Optional[str]:
        """The listed name `feature` means, tolerating case, spacing and a
        near spelling; None when it names nothing on the list."""
        names = list(self.features) + list(self.off)
        if feature in names:
            return feature
        by_key = {_key(n): n for n in names}
        if _key(feature) in by_key:
            return by_key[_key(feature)]
        near = difflib.get_close_matches(_key(feature), list(by_key), n=1, cutoff=0.8)
        return by_key[near[0]] if near else None

    def resolve(self, feature: str, request: str, conversation: str = "",
                ask: Optional[Callable[[str], Any]] = None,
                log: Callable[[str], None] = print
                ) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """(tool name, arguments, None) to run, or (None, None, answer) when
        the call cannot go ahead — the answer is an outcome line for the live
        model. `ask(prompt)` returns the model's parsed JSON or None; it
        defaults to core.gemini. Never raises."""
        try:
            return self._resolve(str(feature or ""), str(request or ""),
                                 str(conversation or ""), ask or _ask_gemini, log)
        except Exception as e:
            log(f"[Router] {feature}: {e}")
            return None, None, outcome.failed(
                ROUTER_NAME, f"the feature could not be prepared ({e})",
                feature=feature or None)

    def _resolve(self, feature, request, conversation, ask, log):
        name = self.find(feature)
        if name is None:
            return None, None, outcome.failed(
                ROUTER_NAME, f"there is no feature called '{feature}'",
                available=", ".join(sorted(self.features)))
        if name in self.off:
            return None, None, outcome.failed(
                ROUTER_NAME, f"the {name} feature is switched off",
                how=("open the Plugin Manager (the PLUGINS button in the side "
                     "panel) and switch it on; it is offered from the next "
                     "session"))

        decl = self.features[name]
        schema = decl.get("parameters") if isinstance(decl.get("parameters"), dict) else {}
        if not (schema.get("properties") or {}):
            return name, {}, None

        prompt = _prompt(decl, request, conversation)
        answer = ask(prompt)
        args = _unwrap(answer)
        if args is None:
            return None, None, outcome.failed(
                ROUTER_NAME, "the settings for this feature could not be worked "
                "out just now (the helper model did not answer)", feature=name)

        args, problems = tool_args.check(schema, args)
        if problems:
            log(f"[Router] {name}: repairing {len(problems)} problem(s)")
            again = _unwrap(ask(_repair_prompt(prompt, args, problems)))
            if again is not None:
                args, problems = tool_args.check(schema, again)
            if problems:
                # The feature has to cope with bad input anyway (it never
                # raises, and says what it is missing); it is the better judge.
                log(f"[Router] {name}: {len(problems)} problem(s) left: "
                    + "; ".join(problems))
        return name, args, None


def build(decls: list, switched_off: Iterable[tuple[str, str]] = ()
          ) -> tuple[list, Optional[Menu]]:
    """(declarations to send, menu) for one session.

    The declarations keep their original order with the deferred ones
    removed and the router appended. When too few features would move, the
    input comes back unchanged with menu None. `switched_off` is (name,
    one-line summary) for plugins the user turned off. Never raises.
    """
    try:
        features = {_name(d): d for d in decls if can_defer(d)}
        if len(features) < MIN_DEFERRED:
            return decls, None
        live = {_name(d) for d in decls}
        off = {n: " ".join(str(s or "").split())[:_LINE_CHARS]
               for n, s in switched_off if n and n not in live}
        menu = Menu(features, off)
        kept = [d for d in decls if _name(d) not in features]
        return kept + [menu.declaration()], menu
    except Exception as e:
        print(f"[Router] left off for this session: {e}")
        return decls, None


# ── the argument-writing prompt ─────────────────────────────────────────────
def _schema_for_prompt(decl: dict) -> dict:
    return {k: v for k, v in decl.items() if k not in _META}


def _prompt(decl: dict, request: str, conversation: str) -> str:
    now = datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M (UTC%z)")
    parts = [
        "You fill in the arguments for one tool call.",
        "TOOL (name, what it does, its parameters as JSON schema):",
        json.dumps(_schema_for_prompt(decl), ensure_ascii=False),
        f"CURRENT LOCAL TIME: {now}",
        "WHAT THE USER WANTS: " + request[:_REQUEST_CHARS],
    ]
    if conversation.strip():
        parts.append("RECENT CONVERSATION, for words like 'it', 'that one' or "
                     "'no, the other': " + conversation[-_CONVERSATION_CHARS:])
    parts.append(
        'Answer with JSON only, shaped {"args": {...}}, using the parameter '
        "names from the schema exactly.\n"
        "- Every required parameter gets a value. When the request does not "
        "settle one, use the plainest choice that fits what was asked.\n"
        "- Optional parameters the request says nothing about are left out.\n"
        "- A parameter with an enum takes one of its listed values, spelt "
        "exactly as listed.\n"
        "- Keep every specific the request carries (people, places, rooms, "
        "devices, amounts, dates, file names, titles). A specific left out "
        "here never reaches the tool.\n"
        "- Turn relative dates and times into real ones using the current "
        "local time above.\n"
        "- If the request already holds the text to use (a message, a "
        "document, questions, code), copy it in unchanged instead of writing "
        "new text. Words meant for the user's eyes or ears stay in their language "
        "unless a parameter's description asks for something else.")
    return "\n\n".join(parts)


def _repair_prompt(prompt: str, args: dict, problems: list[str]) -> str:
    return (prompt
            + "\n\nYOUR PREVIOUS ANSWER:\n"
            + json.dumps({"args": args}, ensure_ascii=False, default=str)
            + "\n\nWHAT IS WRONG WITH IT:\n- " + "\n- ".join(problems)
            + '\n\nAnswer again with the corrected JSON only, shaped {"args": {...}}.')


def _unwrap(answer: Any) -> Optional[dict]:
    """The argument dict out of the model's reply, or None."""
    if not isinstance(answer, dict):
        return None
    inner = answer.get("args")
    return inner if isinstance(inner, dict) else answer


def _ask_gemini(prompt: str) -> Any:
    from core import gemini
    return gemini.as_json(prompt, tier=gemini.FAST)
