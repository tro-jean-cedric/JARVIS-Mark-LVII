"""
plugins/shopping_list.py — a shopping / grocery list that survives restarts.

Stored in memory/plugin_data/shopping_list.json. Removing and clearing are
reversible through core/undo.py ("undo that"), so they need no confirmation.
"""
from __future__ import annotations

import difflib
import re
import time

from plugins import _announce

PLUGIN = {
    "name": "shopping_list",
    "description": (
        "The user's persistent shopping / grocery list. Use for 'add milk and eggs "
        "to my shopping list', 'put bread, cheese and apples on the list', 'I bought "
        "the milk' or 'got it' (check an item off), 'take sugar off the list' "
        "(remove), 'what's on my shopping list', 'clear the list' or 'clear what I "
        "bought'. Do not use the reminder tool or memory for this."
    ),
    "brief": "Shopping list: add, check off, remove, read, clear.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'add', 'check' (mark bought), 'uncheck', 'remove', "
                                      "'list' (default) or 'clear'."},
            "items": {"type": "STRING",
                      "description": "The item(s) in the user's words. Several items are "
                                     "separated by COMMAS, e.g. 'milk, eggs, bread'."},
            "quantity": {"type": "STRING",
                         "description": "Optional quantity when adding one item, e.g. '2 kg'."},
            "scope": {"type": "STRING",
                      "description": "For 'clear': 'all' (default) or 'done' for bought items only."},
        },
        "required": [],
    },
}

_DATA = "shopping_list"


def _load() -> list[dict]:
    data = _announce.load(_DATA, {}) or {}
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict) and i.get("name")]


def _save(items: list[dict]) -> None:
    _announce.save(_DATA, {"items": items})


def split_items(text: str) -> list[str]:
    """'milk, eggs; bread' → ['milk', 'eggs', 'bread']. Only explicit separators
    split, so 'salt and pepper' stays one item."""
    parts = re.split(r"[,;\n،、]+", str(text or ""))
    out = []
    for part in parts:
        p = part.strip().strip(".").strip()
        p = re.sub(r"^(and|&|und|et|y|e|ve)\s+", "", p, flags=re.I)
        if p:
            out.append(p[:80])
    return out


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.casefold()).strip()


def find(items: list[dict], query: str) -> int | None:
    """Index of the item best matching `query`: exact, then whole-word containment,
    then a close spelling. None when nothing is close."""
    q = _key(query)
    if not q:
        return None
    keys = [_key(i["name"]) for i in items]
    if q in keys:
        return keys.index(q)
    for idx, k in enumerate(keys):
        if re.search(rf"\b{re.escape(q)}\b", k) or re.search(rf"\b{re.escape(k)}\b", q):
            return idx
    # plural / singular
    for idx, k in enumerate(keys):
        if k.rstrip("s") == q.rstrip("s"):
            return idx
    close = difflib.get_close_matches(q, keys, n=1, cutoff=0.8)
    return keys.index(close[0]) if close else None


def _say_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _push_undo(label: str, before: list[dict]) -> None:
    snapshot = [dict(i) for i in before]
    try:
        from core.undo import push_undo
    except Exception:
        return

    def _restore() -> str:
        _save(snapshot)
        return "Shopping list restored."
    push_undo(label, _restore)


def _add(p: dict) -> str:
    names = split_items(p.get("items"))
    if not names:
        return "What should I add to the shopping list?"
    qty = str(p.get("quantity") or "").strip()[:30]
    items = _load()
    added, existing = [], []
    for n in names:
        idx = find(items, n)
        if idx is not None and _key(items[idx]["name"]) == _key(n):
            it = items[idx]
            if it.get("done"):
                it["done"] = False
                added.append(n)
            else:
                existing.append(n)
            if qty and len(names) == 1:
                it["qty"] = qty
            continue
        items.append({"name": n, "qty": qty if len(names) == 1 else "",
                      "done": False, "added": time.strftime("%Y-%m-%d")})
        added.append(n)
    _save(items)
    open_n = sum(1 for i in items if not i.get("done"))
    msg = []
    if added:
        msg.append(f"Added {_say_list(added)}.")
    if existing:
        msg.append(f"{_say_list(existing)} {'was' if len(existing) == 1 else 'were'} already on the list.")
    msg.append(f"{open_n} item{'s' if open_n != 1 else ''} to buy.")
    return " ".join(msg)


def _mark(p: dict, done: bool) -> str:
    names = split_items(p.get("items"))
    if not names:
        return "Which item?"
    items = _load()
    hit, miss = [], []
    for n in names:
        idx = find(items, n)
        if idx is None:
            miss.append(n)
        else:
            items[idx]["done"] = done
            hit.append(items[idx]["name"])
    if hit:
        _save(items)
    left = sum(1 for i in items if not i.get("done"))
    parts = []
    if hit:
        parts.append(f"{'Checked off' if done else 'Put back'} {_say_list(hit)}.")
    if miss:
        parts.append(f"I couldn't find {_say_list(miss)} on the list.")
    if hit and done:
        parts.append("Everything on the list is checked off." if left == 0
                     else f"{left} still to buy.")
    return " ".join(parts)


def _remove(p: dict) -> str:
    names = split_items(p.get("items"))
    if not names:
        return "Which item should I remove from the list?"
    items = _load()
    before = [dict(i) for i in items]
    gone, miss = [], []
    for n in names:
        idx = find(items, n)
        if idx is None:
            miss.append(n)
        else:
            gone.append(items.pop(idx)["name"])
    if gone:
        _save(items)
        _push_undo(f"removed {_say_list(gone)} from the shopping list", before)
    parts = []
    if gone:
        parts.append(f"Removed {_say_list(gone)}.")
    if miss:
        parts.append(f"{_say_list(miss)} wasn't on the list.")
    return " ".join(parts)


def _list(player) -> str:
    items = _load()
    if not items:
        return "Your shopping list is empty."
    todo = [i for i in items if not i.get("done")]
    done = [i for i in items if i.get("done")]
    if player is not None:
        lines = [f"[ ] {i['name']}" + (f"  ({i['qty']})" if i.get("qty") else "") for i in todo]
        lines += [f"[x] {i['name']}" for i in done]
        try:
            player.show_content("Shopping list", "\n".join(lines))
        except Exception:
            pass
    if not todo:
        return f"Everything on your shopping list is checked off ({len(done)} items)."
    names = [i["name"] + (f" ({i['qty']})" if i.get("qty") else "") for i in todo]
    text = f"{len(todo)} item{'s' if len(todo) != 1 else ''} to buy: {_say_list(names)}."
    if done:
        text += f" {len(done)} already checked off."
    return text


def _clear(p: dict) -> str:
    items = _load()
    if not items:
        return "Your shopping list was already empty."
    scope = str(p.get("scope") or "all").strip().lower()
    if scope in ("done", "bought", "checked", "completed"):
        keep = [i for i in items if not i.get("done")]
        n = len(items) - len(keep)
        if n == 0:
            return "Nothing on the list is checked off yet."
        _save(keep)
        _push_undo("cleared bought items from the shopping list", items)
        return f"Removed {n} bought item{'s' if n != 1 else ''}; {len(keep)} left."
    _save([])
    _push_undo("cleared the shopping list", items)
    return f"Cleared all {len(items)} items. Say 'undo' if that was a mistake."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()
        if action in ("add", "put", "append"):
            return _add(p)
        if action in ("check", "bought", "done", "tick", "got"):
            return _mark(p, True)
        if action in ("uncheck", "untick", "undone"):
            return _mark(p, False)
        if action in ("remove", "delete", "drop"):
            return _remove(p)
        if action in ("list", "show", "read", "what"):
            return _list(player)
        if action in ("clear", "empty", "reset"):
            return _clear(p)
        return "I can add, check off, remove, read or clear the shopping list."
    except Exception as e:
        return f"The shopping list hit an error: {e}"
