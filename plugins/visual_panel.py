"""
plugins/visual_panel.py — show what the assistant is talking about, on screen.

"Compare these three phones", "make me a packing checklist", "count down to
six" — answers with something to SEE. The model describes a board of up to
about six blocks (table, stats, chart, list, checklist, countdown, pictures);
plugins/_visual_core.py draws it with PIL in the HUD's own colours and it is
shown where the face is, through player.show_image. "Close it" gives the face
back (the HUD's own close button works too).

A board with a countdown is redrawn every second while it is on screen, and
the assistant says so when it reaches zero. A video block is not drawn: the
model is told to play it with the video tool instead.

The board's text is also returned to the model, so it can talk about it
without having to read it out.
"""
from __future__ import annotations

import copy
import json
import threading
import time

from plugins import _announce
from plugins import _visual_core as vc

_STEM = "board"
_state = {"spec": None, "token": 0}
_lock = threading.Lock()

_ITEM = {
    "type": "OBJECT",
    "properties": {
        "label": {"type": "STRING"},
        "value": {"type": "STRING", "description": "May carry a unit: '21°C', '$1,299', '45'"},
        "detail": {"type": "STRING"},
        "max": {"type": "NUMBER", "description": "progress: the full amount (default 100)"},
        "done": {"type": "BOOLEAN", "description": "checklist: ticked"},
        "image": {"type": "STRING", "description": "list/gallery: picture URL"},
        "url": {"type": "STRING"},
    },
}

_BLOCK = {
    "type": "OBJECT",
    "properties": {
        "type": {"type": "STRING",
                 "description": "text | callout | stats | list | table | bars | line | pie | "
                                "progress | checklist | countdown | image | gallery | video"},
        "title": {"type": "STRING"},
        "span": {"type": "STRING", "description": "'full' = whole width, 'half' = half"},
        "text": {"type": "STRING", "description": "text / callout / countdown caption"},
        "items": {"type": "ARRAY", "items": _ITEM,
                  "description": "stats, list, bars, pie, progress, checklist, gallery"},
        "columns": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "table headers"},
        "rows": {"type": "ARRAY", "items": {"type": "STRING"},
                 "description": "table: one row per entry, cells separated by ' | '"},
        "points": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "line: the values"},
        "labels": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "line: x labels"},
        "unit": {"type": "STRING"},
        "seconds": {"type": "NUMBER", "description": "countdown: seconds from now"},
        "until": {"type": "STRING", "description": "countdown: until HH:MM"},
        "query": {"type": "STRING", "description": "image / gallery / video: what to search for"},
        "url": {"type": "STRING", "description": "image / video: a direct link"},
        "caption": {"type": "STRING"},
    },
}

PLUGIN = {
    "name": "visual_panel",
    "description": (
        "Puts what you are telling the user ON SCREEN, where your face is - use it whenever "
        "the answer has something to see: a comparison or timetable (table), key numbers "
        "(stats), a trend (line), shares (pie), a ranking (bars), results with pictures "
        "(list), photos of something (image or gallery with a search query), a plan or to-do "
        "list (checklist), progress toward goals (progress), time left (countdown). Get the "
        "facts with your other tools first when you need them, then show them here, then say "
        "one or two sentences - never read the board aloud. action 'show' opens a board "
        "(replacing any other), 'update' changes the open one (send the full new blocks), "
        "'close' closes it. Keep to about 6 blocks. Each block is {type, title?, span?, ...}: "
        "text{text}; callout{text}; stats{items:[{label,value,detail?}]}; list{items:[{label,"
        "detail?,value?,image?}]}; table{columns, rows:['a | b | c']}; bars{items:[{label,"
        "value}],unit?}; line{points,labels?,unit?}; pie{items:[{label,value}]}; progress{items:"
        "[{label,value,max?}]}; checklist{items:[{label,done?}]}; countdown{seconds | until "
        "'HH:MM', text?}; image{url | query, caption?}; gallery{query | items:[{image,label?}]}. "
        "For the computer's live load or a live weather board use live_panels instead."
    ),
    "brief": "Show tables, charts, numbers, lists, checklists, countdowns, pictures on the HUD.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "show (default) | update | close"},
            "title": {"type": "STRING", "description": "The board's heading"},
            "subtitle": {"type": "STRING", "description": "One line under it: the source, the date, the city"},
            "blocks": {"type": "ARRAY", "items": _BLOCK, "description": "The blocks, in reading order"},
        },
        "required": ["action"],
    },
}


def _coerce_blocks(raw) -> tuple[list | None, str]:
    if raw in (None, "", []):
        return [], ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, ("blocks must be a list of objects, not text - and each table row is ONE "
                          "string, cells separated by ' | '")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None, "blocks must be a list of objects"
    blocks = []
    for b in raw:
        if isinstance(b, str):
            try:
                b = json.loads(b)
            except Exception:
                b = {"type": "text", "text": b}
        if isinstance(b, dict):
            b = dict(b)
            b["type"] = str(b.get("type") or "text").strip().lower()
            blocks.append(b)
    return blocks, ""


def _countdown_watch(token: int, player) -> None:
    """Redraw the board every second while it has a running countdown and is
    still the picture on the HUD; announce each countdown reaching zero."""
    time.sleep(1.5)                       # the first picture is still on its way
    announced: set[int] = set()
    while True:
        with _lock:
            if _state["token"] != token or _state["spec"] is None:
                return
            spec = _state["spec"]
        showing = vc.is_showing(player, _STEM)
        if showing is False:
            return
        now = time.time()
        running = False
        for i, b in enumerate(spec.get("blocks") or []):
            if b.get("type") != "countdown":
                continue
            left = vc.countdown_left(b, now)
            if left is None:
                continue
            if left > -2:
                running = True
            if left <= 0 and i not in announced:
                announced.add(i)
                what = str(b.get("text") or b.get("title") or "the countdown")
                _announce.say(f"The countdown on screen has reached zero ({what}). Tell the user.")
        try:
            vc.show(player, spec, _STEM, str(spec.get("title") or ""))
        except Exception as e:
            print(f"[visual_panel] refresh failed: {e}")
            return
        if not running:
            return
        time.sleep(1.0)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "show").strip().lower()
    _announce.remember(player)

    try:
        if action in ("close", "hide", "clear"):
            with _lock:
                _state["spec"] = None
                _state["token"] += 1
            closed = vc.close(player, _STEM) if player is not None else False
            return "status=done action=show_visual closed=true" if closed or player is None else \
                "status=done action=show_visual closed=true (nothing of mine was on screen)"

        blocks, err = _coerce_blocks(p.get("blocks"))
        if blocks is None:
            return f'status=failed action=show_visual reason="{err}"'

        with _lock:
            current = copy.deepcopy(_state["spec"]) if _state["spec"] else None
        if action == "update" and current:
            spec = current
            if p.get("title"):
                spec["title"] = str(p["title"])
            if p.get("subtitle"):
                spec["subtitle"] = str(p["subtitle"])
            if blocks:
                # Keep the end time of a countdown that is already running.
                old_ends = [b.get("_end") for b in spec.get("blocks") or [] if b.get("type") == "countdown"]
                spec["blocks"] = blocks
                for b in blocks:
                    if b.get("type") == "countdown" and old_ends and not (b.get("seconds") or b.get("until")):
                        b["_end"] = old_ends.pop(0)
        else:
            if not blocks:
                return ('status=failed action=show_visual reason="nothing to show: give blocks, e.g. '
                        "{type:'table', columns:[...], rows:['a | b']}\"")
            spec = {"title": str(p.get("title") or ""), "subtitle": str(p.get("subtitle") or ""),
                    "blocks": blocks}

        note = ""
        videos = [b for b in spec["blocks"] if b.get("type") == "video"]
        if videos:
            spec["blocks"] = [b for b in spec["blocks"] if b.get("type") != "video"]
            note = " A video was left out: play it on its own with the video tool."
            if not spec["blocks"]:
                return ('status=failed action=show_visual reason="a video is not drawn on a board - '
                        'play it with the video tool instead"')
        if len(spec["blocks"]) > vc.MAX_BLOCKS:
            spec["blocks"] = spec["blocks"][:vc.MAX_BLOCKS]
            note += f" Only the first {vc.MAX_BLOCKS} blocks fit."
        for b in spec["blocks"]:
            if b.get("type") == "countdown":
                vc.prepare_countdown(b)

        text = vc.as_text(spec)
        if player is None:
            return f'status=done action=show_visual shown="{spec.get("title") or "board"}" (text only)\n{text}'

        ok, info = vc.show(player, spec, _STEM, spec.get("title") or "")
        with _lock:
            _state["spec"] = spec
            _state["token"] += 1
            token = _state["token"]
        if not ok:
            return (f'status=failed action=show_visual reason="{info}" - it was put in the text '
                    f'panel instead.{note}\n{text[:1500]}')
        if any(b.get("type") == "countdown" for b in spec["blocks"]):
            threading.Thread(target=_countdown_watch, args=(token, player), daemon=True,
                             name="visual-countdown").start()
        return (f'status=done action=show_visual shown="{(spec.get("title") or "board")[:60]}". The user '
                f'can see it now: say one or two sentences about what matters - do not read it out.'
                f'{note}\nOn the board (data):\n{text[:1500]}')
    except Exception as e:
        return f'status=failed action=show_visual reason="the board could not be shown: {e}"'
