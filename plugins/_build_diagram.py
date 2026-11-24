"""
plugins/_build_diagram.py — turning a build plan into pictures with Pillow.

Helper for build_guide (leading underscore: the loader skips it).

  normalise_plan(raw)          the model's JSON -> a clean plan (parts, wires...)
  fault_list(plan)             wiring mistakes that can be seen without a datasheet
  render_diagram(plan, path)   a numbered connection diagram: parts as labelled
                               boxes, pins on their edges, coloured wires
  annotate_photo(jpeg, plan, path, marks, statuses)
                               the user's own photo with the parts boxed and the
                               problem spots marked

Plan shape (after normalise_plan):
  parts: [{"id", "name", "kind", "box": [ymin,xmin,ymax,xmax] 0-1000 or []}]
  wires: [{"n", "a": (part_id, pin), "b": (part_id, pin), "kind", "note"}]
  steps, need, warn: [str]; say, answer, mode, view, lang: str
  safety: {"level": ok|caution|danger, "reason"}; legend: [4 words]; ref: [str]

Never raises out of render/annotate: they return None and log why.
"""
from __future__ import annotations

import io
import os
import re
from typing import Optional

KINDS = ("power", "ground", "signal", "mechanical")
_SHARED_PINS = re.compile(r"^(gnd|ground|vcc|vdd|vin|v\+|3v3|3\.3v|5v|vbus|sda|scl|mosi|miso|sck|"
                          r"sclk|clk|cs|ss|rst|reset|en|\+|-|v-|com)$", re.I)

# Dark, HUD-like picture: readable on the avatar surface.
BG = (6, 14, 20)
PANEL = (10, 26, 36)
EDGE = (0, 212, 255)
TEXT = (220, 240, 248)
DIM = (120, 150, 165)
COLORS = {"power": (235, 64, 52), "ground": (150, 150, 150), "mechanical": (200, 170, 90)}
SIGNALS = [(52, 152, 219), (46, 204, 113), (241, 196, 15), (155, 89, 182), (26, 188, 156),
           (230, 126, 34), (236, 112, 160), (127, 140, 255)]
STATUS_COLORS = {"ok": (46, 204, 113), "wrong": (235, 64, 52), "missing": (241, 160, 15),
                 "unclear": (150, 150, 150)}


# ── Plan ─────────────────────────────────────────────────────────────────────

def _box(v) -> list:
    try:
        b = [float(x) for x in v][:4]
        if len(b) == 4 and b[2] > b[0] and b[3] > b[1]:
            return [max(0.0, min(1000.0, x)) for x in b]
    except Exception:
        pass
    return []


def _kind(v) -> str:
    k = str(v or "signal").strip().lower()
    if k in ("vcc", "vdd", "power", "supply", "+", "positive"):
        return "power"
    if k in ("gnd", "ground", "-", "negative"):
        return "ground"
    if k.startswith("mech"):
        return "mechanical"
    return "signal"


def normalise_plan(raw) -> dict:
    """Whatever the model returned -> a plan this module can draw. Wires that
    name an unknown part create that part, so nothing the model said is lost."""
    raw = raw if isinstance(raw, dict) else {}
    parts: list[dict] = []
    by_key: dict[str, dict] = {}

    def add_part(pid: str, name: str = "", kind: str = "", box=None) -> dict:
        pid = str(pid or "").strip() or f"p{len(parts) + 1}"
        if pid.lower() in by_key:
            return by_key[pid.lower()]
        part = {"id": pid, "name": str(name or pid).strip(), "kind": str(kind or "").lower(),
                "box": _box(box or [])}
        parts.append(part)
        by_key[pid.lower()] = part
        if part["name"].lower() not in by_key:
            by_key[part["name"].lower()] = part
        return part

    for i, p in enumerate(raw.get("parts") or [], 1):
        if isinstance(p, str):
            add_part(f"p{i}", p)
        elif isinstance(p, (list, tuple)) and p:
            add_part(str(p[0]), str(p[1]) if len(p) > 1 else "", "",
                     p[2] if len(p) > 2 and isinstance(p[2], (list, tuple)) else None)
        elif isinstance(p, dict):
            add_part(p.get("id") or f"p{i}", p.get("name") or p.get("label") or "",
                     p.get("kind") or "", p.get("box"))

    def pin_of(end) -> Optional[tuple[str, str]]:
        part, pin = None, ""
        if isinstance(end, dict):
            part, pin = end.get("part") or end.get("id"), end.get("pin") or ""
        elif isinstance(end, (list, tuple)) and end:
            part, pin = end[0], end[1] if len(end) > 1 else ""
        elif isinstance(end, str) and end.strip():
            s = end.strip()
            if "." in s:
                part, pin = s.split(".", 1)
            elif ":" in s:
                part, pin = s.split(":", 1)
            else:
                part = s
        if not part:
            return None
        key = str(part).strip()
        found = by_key.get(key.lower()) or add_part(key, key)
        return found["id"], str(pin or "").strip()

    wires = []
    for w in raw.get("wires") or []:
        a = b = None
        kind, note = "signal", ""
        if isinstance(w, dict):
            a, b = pin_of(w.get("from") or w.get("a")), pin_of(w.get("to") or w.get("b"))
            kind, note = _kind(w.get("kind")), str(w.get("note") or "")
        elif isinstance(w, (list, tuple)) and len(w) >= 2:
            a, b = pin_of(w[0]), pin_of(w[1])
            kind = _kind(w[2]) if len(w) > 2 else "signal"
            note = str(w[3]) if len(w) > 3 else ""
        if not a or not b or a == b:
            continue
        wires.append({"n": len(wires) + 1, "a": a, "b": b, "kind": kind, "note": note.strip()})

    def strings(key) -> list:
        v = raw.get(key) or []
        if isinstance(v, str):
            v = [v]
        out = []
        for s in v:
            if isinstance(s, dict):
                s = s.get("text") or s.get("say") or " ".join(str(x) for x in s.values())
            s = str(s).strip()
            if s:
                out.append(s)
        return out

    safety = raw.get("safety") or {}
    if isinstance(safety, str):
        safety = {"level": safety, "reason": ""}
    level = str(safety.get("level") or "ok").lower()
    level = level if level in ("ok", "caution", "danger") else "ok"
    legend = strings("legend")
    if len(legend) != 4:
        legend = list(KINDS)
    plan = {
        "lang": str(raw.get("lang") or ""), "mode": str(raw.get("mode") or "wiring").lower(),
        "say": str(raw.get("say") or "").strip(), "answer": str(raw.get("answer") or "").strip(),
        "view": str(raw.get("view") or "").strip(), "parts": parts, "wires": wires,
        "steps": strings("steps"), "need": strings("need"), "warn": strings("warn"),
        "safety": {"level": level, "reason": str(safety.get("reason") or "")},
        "legend": legend, "ref": strings("ref"), "marks": _marks(raw.get("marks")),
    }
    if level == "danger":
        plan["wires"], plan["steps"], plan["marks"] = [], [], []
    return plan


def _marks(v) -> list:
    out = []
    for m in v or []:
        if not isinstance(m, dict):
            continue
        pt = m.get("point") or []
        box = _box(m.get("box") or [])
        try:
            pt = [float(pt[0]), float(pt[1])] if len(pt) >= 2 else []
        except Exception:
            pt = []
        if not pt and not box:
            continue
        out.append({"point": pt, "box": box, "label": str(m.get("label") or ""),
                    "status": str(m.get("status") or ("wrong" if m.get("problem") else "unclear")).lower()})
    return out[:30]


def part_name(plan: dict, pid: str) -> str:
    for p in plan.get("parts") or []:
        if p["id"] == pid:
            return p["name"]
    return pid


def wire_line(plan: dict, w: dict) -> str:
    a = f"{part_name(plan, w['a'][0])} {w['a'][1]}".strip()
    b = f"{part_name(plan, w['b'][0])} {w['b'][1]}".strip()
    note = f" ({w['note']})" if w.get("note") else ""
    return f"{w['n']}: {a} -> {b} [{w['kind']}]{note}"


def fault_list(plan: dict) -> list[str]:
    """Mistakes visible from the plan alone: unconnected parts, pins doing two
    jobs, wires from a pin to itself."""
    faults = []
    if plan.get("mode") not in ("wiring", "troubleshoot") or not plan.get("wires"):
        return faults
    count: dict[str, int] = {}
    pin_kinds: dict[tuple, set] = {}
    pin_peers: dict[tuple, set] = {}
    kinds_of = {p["id"]: p.get("kind") or "" for p in plan["parts"]}
    for w in plan["wires"]:
        for end, other in ((w["a"], w["b"]), (w["b"], w["a"])):
            count[end[0]] = count.get(end[0], 0) + 1
            if not end[1]:
                continue
            pin_kinds.setdefault(end, set()).add(w["kind"])
            # A single component may share the line (a pull-up resistor); two
            # pins of modules or boards on one signal pin may not.
            if w["kind"] == "signal" and kinds_of.get(other[0]) != "component":
                pin_peers.setdefault(end, set()).add(other)
    for p in plan["parts"]:
        kind = p.get("kind") or ""
        n = count.get(p["id"], 0)
        if kind in ("board", "power", "tool", "piece"):
            continue
        if n == 0:
            faults.append(f"{p['name']} is not connected, and it must be")
        elif n == 1:
            faults.append(f"{p['name']} has fewer than two wires: it is not connected completely")
    for (pid, pin), kinds in pin_kinds.items():
        if _SHARED_PINS.match(pin):
            continue
        electrical = kinds - {"mechanical"}
        if len(electrical) > 1:
            faults.append(f"{part_name(plan, pid)} {pin} carries more than one signal: wired as "
                          f"{', '.join(sorted(electrical))}")
        elif len(pin_peers.get((pid, pin), ())) > 1:
            faults.append(f"{part_name(plan, pid)} {pin} carries more than one signal: wired to "
                          f"{', '.join(f'{part_name(plan, x)} {y}'.strip() for x, y in sorted(pin_peers[(pid, pin)]))}")
    return faults


# ── Drawing ──────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf"),
]
_font_cache: dict = {}


def _font(size: int):
    from PIL import ImageFont
    if size in _font_cache:
        return _font_cache[size]
    f = None
    for path in _FONT_CANDIDATES:
        if path and os.path.isfile(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def _tw(draw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return len(text) * 8


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if _tw(draw, test, font) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _wire_color(w: dict, signal_index: dict) -> tuple:
    if w["kind"] in COLORS:
        return COLORS[w["kind"]]
    key = w["n"]
    if key not in signal_index:
        signal_index[key] = SIGNALS[len(signal_index) % len(SIGNALS)]
    return signal_index[key]


def _columns(plan: dict) -> dict:
    """part id -> column 0 (left), 1 (middle), 2 (right)."""
    parts = plan["parts"]
    degree = {p["id"]: 0 for p in parts}
    for w in plan["wires"]:
        for end in (w["a"], w["b"]):
            degree[end[0]] = degree.get(end[0], 0) + 1
    boards = [p for p in parts if p.get("kind") == "board"]
    hub = max(boards or parts, key=lambda p: degree.get(p["id"], 0)) if parts else None
    cols, left, right = {}, 0, 0
    if hub is None:
        return cols
    cols[hub["id"]] = 1
    others = sorted((p for p in parts if p is not hub), key=lambda p: -degree.get(p["id"], 0))
    for p in others:
        if left <= right:
            cols[p["id"]] = 0
            left += 1
        else:
            cols[p["id"]] = 2
            right += 1
    return cols


def render_diagram(plan: dict, path: str, highlight: Optional[list] = None,
                   statuses: Optional[dict] = None) -> Optional[str]:
    try:
        return _render_now(plan, path, highlight or [], statuses or {})
    except Exception as e:
        print(f"[build_guide] diagram could not be drawn: {type(e).__name__}: {e}")
        return None


def _render_now(plan: dict, path: str, highlight: list, statuses: dict) -> str:
    from PIL import Image, ImageDraw

    W = 1500
    f_title, f_part, f_pin, f_txt, f_num = _font(26), _font(19), _font(15), _font(17), _font(14)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    parts, wires = plan["parts"], plan["wires"]
    cols = _columns(plan)

    # Which edge each pin sits on: the side facing the part at the other end.
    sides: dict[str, dict[str, list]] = {p["id"]: {"L": [], "R": []} for p in parts}
    wire_sides = []
    for w in wires:
        ca, cb = cols.get(w["a"][0], 1), cols.get(w["b"][0], 1)
        if ca < cb:
            sa, sb = "R", "L"
        elif ca > cb:
            sa, sb = "L", "R"
        else:
            s = "R" if ca == 0 else "L"
            if ca == 1:
                s = "R"
            sa = sb = s
        wire_sides.append((sa, sb))
        for (pid, pin), s in ((w["a"], sa), (w["b"], sb)):
            label = pin or "?"
            if label not in sides[pid][s]:
                sides[pid][s].append(label)

    PIN_H, HEAD, BOX_W = 30, 46, 300
    col_x = {0: 70, 1: (W - BOX_W) // 2, 2: W - 70 - BOX_W}
    if not any(c == 2 for c in cols.values()) and not any(c == 0 for c in cols.values()):
        col_x[1] = (W - BOX_W) // 2
    y_next = {0: 110, 1: 110, 2: 110}
    rects: dict[str, tuple] = {}
    for p in sorted(parts, key=lambda p: (cols.get(p["id"], 1), parts.index(p))):
        c = cols.get(p["id"], 1)
        name_lines = _wrap(probe, p["name"], f_part, BOX_W - 20)[:3]
        n_pins = max(len(sides[p["id"]]["L"]), len(sides[p["id"]]["R"]), 1)
        h = HEAD + 22 * (len(name_lines) - 1) + n_pins * PIN_H + 10
        x, y = col_x[c], y_next[c]
        rects[p["id"]] = (x, y, x + BOX_W, y + h, name_lines)
        y_next[c] = y + h + 50
    diagram_bottom = max(y_next.values()) + 20

    # Wire list and steps under the diagram.
    text_w = W - 140
    lines_wires = [wire_line(plan, w) for w in wires]
    lines_steps = [f"{i}. {s}" for i, s in enumerate(plan["steps"], 1)]
    extra = [f"! {s}" for s in plan["warn"]] + [f"+ {s}" for s in plan["need"]]
    wrapped = []
    for group, color in ((lines_wires, TEXT), (lines_steps, TEXT), (extra, (241, 196, 15))):
        for line in group:
            for i, l in enumerate(_wrap(probe, line, f_txt, text_w)):
                wrapped.append(("    " + l if i else l, color))
        if group:
            wrapped.append(("", TEXT))
    bus_room = 14 * len(wires)
    H = diagram_bottom + bus_room + 24 * len(wrapped) + 80
    H = max(H, 420)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    head = {"wiring": "Connection diagram", "assembly": "Assembly", "identify": "Parts",
            "troubleshoot": "Check", "explain": "Overview"}.get(plan.get("mode"), "Build guide")
    d.text((40, 30), head, font=f_title, fill=EDGE)
    # legend
    lx = W - 40
    for kind, word in reversed(list(zip(KINDS, plan["legend"]))):
        if kind == "mechanical" and not any(w["kind"] == "mechanical" for w in wires):
            continue
        color = COLORS.get(kind, SIGNALS[0])
        tw = _tw(d, word, f_pin)
        lx -= tw + 40
        d.line((lx, 46, lx + 24, 46), fill=color, width=5)
        d.text((lx + 30, 37), word, font=f_pin, fill=DIM)

    # pin coordinates
    pin_xy: dict[tuple, tuple] = {}
    for p in parts:
        x0, y0, x1, y1, name_lines = rects[p["id"]]
        top = y0 + HEAD + 22 * (len(name_lines) - 1)
        for side in ("L", "R"):
            for i, label in enumerate(sides[p["id"]][side]):
                py = top + i * PIN_H + PIN_H // 2
                pin_xy[(p["id"], side, label)] = ((x0 if side == "L" else x1), py)

    # boxes
    for p in parts:
        x0, y0, x1, y1, name_lines = rects[p["id"]]
        d.rounded_rectangle((x0, y0, x1, y1), radius=10, fill=PANEL, outline=EDGE, width=2)
        for i, line in enumerate(name_lines):
            d.text((x0 + 12, y0 + 10 + 22 * i), line, font=f_part, fill=TEXT)
        for side in ("L", "R"):
            for label in sides[p["id"]][side]:
                px, py = pin_xy[(p["id"], side, label)]
                d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=EDGE)
                tw = _tw(d, label, f_pin)
                tx = px + 10 if side == "L" else px - 10 - tw
                d.text((tx, py - 9), label, font=f_pin, fill=TEXT)

    # wires: out of the pin to a channel, along it, into the other pin
    signal_index: dict = {}
    channel_used = {"0-1": 0, "1-2": 0, "0": 0, "2": 0, "1": 0}
    gap_left = (col_x[0] + BOX_W, col_x[1])
    gap_right = (col_x[1] + BOX_W, col_x[2])
    bus_y = diagram_bottom - 30
    for w, (sa, sb) in zip(wires, wire_sides):
        color = _wire_color(w, signal_index)
        st = statuses.get(w["n"])
        if st:
            color = STATUS_COLORS.get(st, color)
        width = 5 if (not highlight or w["n"] in highlight) else 2
        if highlight and w["n"] not in highlight:
            color = tuple(int(c * 0.35 + b * 0.65) for c, b in zip(color, BG))
        a = pin_xy[(w["a"][0], sa, w["a"][1] or "?")]
        b = pin_xy[(w["b"][0], sb, w["b"][1] or "?")]
        ca, cb = cols.get(w["a"][0], 1), cols.get(w["b"][0], 1)
        lo, hi = min(ca, cb), max(ca, cb)
        if hi - lo == 1:
            key = f"{lo}-{hi}"
            g0, g1 = gap_left if key == "0-1" else gap_right
            k = channel_used[key]
            channel_used[key] += 1
            cx = g0 + 24 + (k * 16) % max(20, (g1 - g0 - 48))
            pts = [a, (cx, a[1]), (cx, b[1]), b]
        elif hi - lo == 2:
            k = channel_used["0-1"]
            channel_used["0-1"] += 1
            cx1 = gap_left[0] + 24 + (k * 16) % max(20, (gap_left[1] - gap_left[0] - 48))
            k2 = channel_used["1-2"]
            channel_used["1-2"] += 1
            cx2 = gap_right[0] + 24 + (k2 * 16) % max(20, (gap_right[1] - gap_right[0] - 48))
            left_pt, right_pt = (a, b) if ca < cb else (b, a)
            by = bus_y + 14 * channel_used["1"]
            channel_used["1"] += 1
            pts = [left_pt, (cx1, left_pt[1]), (cx1, by), (cx2, by), (cx2, right_pt[1]), right_pt]
        else:
            key = str(ca)
            k = channel_used[key]
            channel_used[key] += 1
            out = 30 + k * 14
            cx = a[0] + out if sa == "R" else a[0] - out
            pts = [a, (cx, a[1]), (cx, b[1]), b]
        dashed = w["kind"] == "mechanical"
        for p0, p1 in zip(pts, pts[1:]):
            if dashed:
                _dash(d, p0, p1, color, width)
            else:
                d.line((p0, p1), fill=color, width=width)
        # number badge on the longest vertical run
        # number badge on the run leaving the first pin (each pin has its own row)
        mid = (pts[0], pts[1])
        if abs(pts[1][0] - pts[0][0]) < 36:
            mid = (pts[-2], pts[-1])
        mx, my = (mid[0][0] + mid[1][0]) // 2, (mid[0][1] + mid[1][1]) // 2
        label = str(w["n"])
        d.ellipse((mx - 13, my - 13, mx + 13, my + 13), fill=BG, outline=color, width=3)
        tw = _tw(d, label, f_num)
        d.text((mx - tw // 2, my - 9), label, font=f_num, fill=TEXT)

    if not wires and plan.get("view"):
        d.text((40, diagram_bottom - 20), plan["view"], font=f_txt, fill=(241, 196, 15))
    y = bus_y + bus_room + 30
    if plan["safety"]["level"] == "danger":
        for l in _wrap(d, "DANGER: " + plan["safety"]["reason"], f_txt, text_w):
            d.text((70, y), l, font=f_txt, fill=COLORS["power"])
            y += 24
    for line, color in wrapped:
        d.text((70, y), line, font=f_txt, fill=color)
        y += 24
    img = img.crop((0, 0, W, min(H, y + 30)))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    img.save(path, "PNG")
    return path


def _dash(d, p0, p1, color, width, on=10, off=7):
    (x0, y0), (x1, y1) = p0, p1
    length = max(abs(x1 - x0), abs(y1 - y0))
    if length == 0:
        return
    steps = int(length // (on + off)) + 1
    for i in range(steps):
        s = i * (on + off) / length
        e = min(1.0, (i * (on + off) + on) / length)
        d.line(((x0 + (x1 - x0) * s, y0 + (y1 - y0) * s), (x0 + (x1 - x0) * e, y0 + (y1 - y0) * e)),
               fill=color, width=width)


def annotate_photo(jpeg: bytes, plan: dict, path: str, marks: Optional[list] = None) -> Optional[str]:
    try:
        return _annotate_now(jpeg, plan, path, marks if marks is not None else plan.get("marks") or [])
    except Exception as e:
        print(f"[build_guide] photo could not be marked: {type(e).__name__}: {e}")
        return None


def _annotate_now(jpeg: bytes, plan: dict, path: str, marks: list) -> str:
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = img.size
    d = ImageDraw.Draw(img)
    fs = max(14, W // 60)
    font = _font(fs)

    def box_rect(b):
        return (b[1] * W / 1000, b[0] * H / 1000, b[3] * W / 1000, b[2] * H / 1000)

    for p in plan.get("parts") or []:
        if not p.get("box"):
            continue
        r = box_rect(p["box"])
        d.rectangle(r, outline=EDGE, width=max(2, W // 400))
        label = p["name"][:40]
        tw = _tw(d, label, font)
        ty = r[1] - fs - 8 if r[1] > fs + 8 else r[1] + 4
        d.rectangle((r[0], ty, r[0] + tw + 10, ty + fs + 6), fill=BG)
        d.text((r[0] + 5, ty + 2), label, font=font, fill=EDGE)
    for m in marks:
        color = STATUS_COLORS.get(m.get("status"), STATUS_COLORS["wrong"])
        if m.get("box"):
            r = box_rect(m["box"])
            d.rectangle(r, outline=color, width=max(3, W // 300))
            cx, cy = r[0], r[1]
        else:
            cy, cx = m["point"][0] * H / 1000, m["point"][1] * W / 1000
            rad = max(12, W // 50)
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline=color, width=max(3, W // 300))
        if m.get("label"):
            label = m["label"][:50]
            tw = _tw(d, label, font)
            tx = min(max(0, cx + 14), W - tw - 12)
            ty = min(max(0, cy - fs - 14), H - fs - 8)
            d.rectangle((tx, ty, tx + tw + 10, ty + fs + 6), fill=BG)
            d.text((tx + 5, ty + 2), label, font=font, fill=color)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    img.save(path, "PNG")
    return path
