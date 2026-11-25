"""
Check a tool call's arguments against the tool's own schema, and repair what
can be repaired without guessing.

WHY THIS EXISTS
    The tool router (core/tool_router.py) has a second model write the
    arguments for a feature the live model only named. That model reads the
    schema, but what comes back is still text that was parsed as JSON: a
    number arrives as "3", a yes/no as "yes", an allowed value as "Weekly"
    when the list says "weekly", a parameter as "fileName" when it is called
    "file_name". Every one of those is an easy fix and a certain failure if
    passed through as it is — plugins compare strings exactly.

    So this module does the fixes that have exactly one right answer, and
    reports everything else as a plain-language problem. The router hands those
    problems back to the model once; whatever is still wrong after that goes to
    the tool anyway, because every tool already has to cope with bad input and
    say what it needs (AGENTS.md: run() never raises).

WHAT IS FIXED AND WHAT IS ONLY REPORTED
    fixed     a key that differs only in case, spaces, dashes or underscores;
              numbers and booleans written as text; a single value where a
              list is wanted; an allowed value written in another case, or a
              near spelling when only one choice is close enough.
    reported  a key the tool does not have (dropped), a value of the wrong
              kind that cannot be converted, a value outside the allowed list,
              a required parameter that is missing.

    Nothing here invents a value. A missing required parameter is reported,
    never filled with a default — choosing a default is the model's call, with
    the request in front of it.

Pure stdlib, no model, no I/O: the self-test drives it with literal dicts.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any

# How close a misspelt choice must be to an allowed one before it is taken as
# that choice. High on purpose: "weekly" for "weekley" yes, "on" for "off" no.
_CLOSE_ENOUGH = 0.8

_TRUE = {"true", "yes", "y", "on", "1", "enable", "enabled"}
_FALSE = {"false", "no", "n", "off", "0", "disable", "disabled", "none"}
_NUMBER = re.compile(r"\s*([-+]?\d+(?:[.,]\d+)?)\s*")


def _kind(spec: Any) -> str:
    """The schema type in upper case ("STRING", "OBJECT"...), "" if absent."""
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("type") or "").upper()


def _key(text: str) -> str:
    """A parameter name reduced to what matters for matching."""
    return re.sub(r"[\s_\-]+", "", str(text)).casefold()


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)
    return text if len(text) <= 40 else text[:37] + "..."


def _choice(value: Any, choices: list) -> tuple[Any, bool]:
    """(the allowed value meant, True) or (value, False) when none fits."""
    if value in choices:
        return value, True
    text = str(value).strip()
    folded = {str(c).casefold(): c for c in choices}
    if text.casefold() in folded:
        return folded[text.casefold()], True
    near = difflib.get_close_matches(text.casefold(), list(folded), n=2,
                                     cutoff=_CLOSE_ENOUGH)
    # Two near matches means the spelling does not decide it; leave it reported.
    if len(near) == 1:
        return folded[near[0]], True
    return value, False


def _convert(value: Any, spec: dict, where: str, problems: list[str]) -> Any:
    """`value` as the kind `spec` asks for; appends to `problems` when it
    cannot be. Returns the converted value, or the original one unchanged."""
    kind = _kind(spec)
    if kind == "STRING":
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        return value if isinstance(value, str) else str(value)

    if kind in ("INTEGER", "NUMBER"):
        if isinstance(value, bool):
            problems.append(f"'{where}' should be a number, got {_short(value)}")
            return value
        if isinstance(value, (int, float)):
            number = value
        else:
            m = _NUMBER.fullmatch(str(value))
            if not m:
                problems.append(f"'{where}' should be a number, got {_short(value)}")
                return value
            number = float(m.group(1).replace(",", "."))
        if kind == "INTEGER":
            if float(number).is_integer():
                return int(number)
            problems.append(f"'{where}' should be a whole number, got {_short(value)}")
            return value
        return number if isinstance(value, (int, float)) else (
            int(number) if float(number).is_integer() else number)

    if kind == "BOOLEAN":
        if isinstance(value, bool):
            return value
        word = str(value).strip().casefold()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        problems.append(f"'{where}' should be true or false, got {_short(value)}")
        return value

    if kind == "ARRAY":
        if isinstance(value, str) and value.strip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if not isinstance(value, (list, tuple)):
            value = [value]
        inner = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        return [_convert(v, inner, f"{where}[{i}]", problems)
                for i, v in enumerate(value)]

    if kind == "OBJECT":
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if not isinstance(value, dict):
            problems.append(f"'{where}' should be an object with named fields")
            return value
        if isinstance(spec.get("properties"), dict) and spec["properties"]:
            fixed, inner_problems = check(spec, value, prefix=f"{where}.")
            problems.extend(inner_problems)
            return fixed
        return value

    return value


def check(schema: Any, args: Any, *, keep_unknown: bool = False,
          prefix: str = "") -> tuple[dict, list[str]]:
    """(repaired arguments, problems) for one call against one OBJECT schema.

    `problems` is a list of short sentences meant to be shown to a model so it
    can correct itself; empty means the call fits the schema. `keep_unknown`
    keeps keys the schema does not name (reported either way) for tools whose
    schema is deliberately open. Never raises.
    """
    problems: list[str] = []
    if not isinstance(args, dict):
        return {}, ["the arguments must be a JSON object of name: value pairs"]
    if not isinstance(schema, dict):
        return dict(args), []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    by_key = {_key(name): name for name in props}

    out: dict = {}
    for given, value in args.items():
        name = given if given in props else by_key.get(_key(given))
        if name is None:
            problems.append(f"'{prefix}{given}' is unknown here (no such parameter)")
            if keep_unknown:
                out[given] = value
            continue
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        value = _convert(value, spec, prefix + name, problems)
        choices = spec.get("enum")
        if isinstance(choices, list) and choices and value is not None:
            if isinstance(value, list):
                value = [_choice(v, choices)[0] for v in value]
            else:
                value, ok = _choice(value, choices)
                if not ok:
                    problems.append(
                        f"'{prefix}{name}' is {_short(value)}; allowed values are "
                        + ", ".join(str(c) for c in choices))
        out[name] = value

    for name in schema.get("required") or ():
        if name in props and (name not in out or out[name] in (None, "")):
            problems.append(f"'{prefix}{name}' is required but was not given")
    return out, problems
