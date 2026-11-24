"""
plugins/excel_writer.py — turn a spoken description into a real .xlsx file on
the user's Desktop (the real one: OneDrive / relocated / localized folders are
resolved by plugins/_known_folders.py).

The model (core.gemini) only produces a JSON table spec; openpyxl writes the
file. The work runs on a worker thread and the result is announced, so run()
returns at once.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from plugins import _announce, _known_folders

PLUGIN = {
    "name": "excel_writer",
    "description": (
        "Creates a real Excel .xlsx spreadsheet file from a spoken description and "
        "saves it on the user's Desktop: column headers, data rows (example data "
        "when none is given), an optional total row and an optional chart. Use for "
        "'make me a spreadsheet of…', 'create an Excel table of my monthly expenses "
        "with a total', 'xlsx of sales by month with a bar chart', 'put this data in "
        "Excel'. Pass the user's whole request in 'request', including any numbers "
        "they gave. Only for when they want an actual file, not to answer a question "
        "about numbers."
    ),
    "brief": "Builds a real .xlsx (table, totals, chart) on the Desktop from a description.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {"type": "STRING",
                        "description": "The user's full spreadsheet request, verbatim, with any data."},
            "filename": {"type": "STRING",
                         "description": "Optional file name without extension."},
            "open": {"type": "BOOLEAN",
                     "description": "Open the file when done (default true)."},
        },
        "required": ["request"],
    },
}

_PROMPT = """You design spreadsheets. Turn the request below into a table specification.
Request: {request}

Reply with JSON only (no markdown) using exactly these keys:
{{"title": "sheet title in the user's language (max 31 chars)",
  "filename": "short_ascii_file_name_without_extension",
  "headers": ["column", ...],
  "rows": [[cell, ...], ...],
  "total_columns": [0-based indexes of numeric columns that need a SUM total row, or []],
  "number_formats": {{"<column index>": "excel number format such as '#,##0.00' or '0%'"}},
  "chart": {{"type": "bar" | "line" | "pie" | "none", "title": "...",
            "category_column": 0, "value_columns": [1, ...]}}}}
Rules: every row has as many cells as there are headers; numbers are JSON numbers, not
strings; use the data the user gave, and only invent realistic example data when they gave
none; a chart only when the user asked for one or it clearly helps (otherwise "none")."""


# ── spec handling ────────────────────────────────────────────────────────────

def _num(v):
    """'1,234.50' / '€12' / '15%' → number when it clearly is one, else unchanged."""
    if isinstance(v, (int, float)) or v is None:
        return v
    s = str(v).strip()
    m = re.fullmatch(r"[-+]?[$€£¥₺]?\s?[-+]?\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?\s?[$€£¥₺]?|[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return s
    t = re.sub(r"[^\d.\-+]", "", s)
    try:
        f = float(t)
        return int(f) if f.is_integer() and "." not in t else f
    except ValueError:
        return s


def clean_spec(spec) -> dict | None:
    if not isinstance(spec, dict):
        return None
    headers = [str(h).strip() or f"Column {i + 1}"
               for i, h in enumerate(spec.get("headers") or [])][:50]
    rows_in = spec.get("rows") or []
    if not headers and rows_in and isinstance(rows_in[0], list):
        headers = [f"Column {i + 1}" for i in range(len(rows_in[0]))]
    if not headers:
        return None
    n = len(headers)
    rows = []
    for r in rows_in[:5000]:
        if isinstance(r, dict):
            r = [r.get(h) for h in headers]
        if not isinstance(r, list):
            continue
        r = [_num(c) for c in r[:n]] + [None] * max(0, n - len(r))
        rows.append(r)
    numeric = [all(isinstance(r[i], (int, float)) for r in rows if r[i] not in (None, ""))
               and any(isinstance(r[i], (int, float)) for r in rows) for i in range(n)]
    totals = sorted({int(i) for i in (spec.get("total_columns") or [])
                     if str(i).lstrip("-").isdigit() and 0 <= int(i) < n and numeric[int(i)]})
    fmts = {}
    for k, v in (spec.get("number_formats") or {}).items():
        if str(k).isdigit() and int(k) < n and isinstance(v, str) and len(v) < 40:
            fmts[int(k)] = v
    chart = spec.get("chart") if isinstance(spec.get("chart"), dict) else {}
    ctype = str(chart.get("type") or "none").lower()
    cat = chart.get("category_column", 0)
    cat = int(cat) if str(cat).isdigit() and int(cat) < n else 0
    vals = [int(v) for v in (chart.get("value_columns") or [])
            if str(v).isdigit() and int(v) < n and numeric[int(v)] and int(v) != cat]
    if ctype not in ("bar", "line", "pie") or not vals or not rows:
        ctype = "none"
    if ctype == "pie":
        vals = vals[:1]
    title = re.sub(r"[\[\]:*?/\\]", " ", str(spec.get("title") or "Sheet1")).strip()[:31] or "Sheet1"
    return {"title": title, "filename": str(spec.get("filename") or ""), "headers": headers,
            "rows": rows, "totals": totals, "formats": fmts,
            "chart": {"type": ctype, "title": str(chart.get("title") or title)[:80],
                      "cat": cat, "vals": vals}}


def safe_filename(name: str, fallback: str = "spreadsheet") -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().strip(".")
    s = re.sub(r"\s+", "_", s)[:60]
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} or not s:
        s = fallback
    return re.sub(r"\.xlsx$", "", s, flags=re.I)


# ── writing ──────────────────────────────────────────────────────────────────

def write_xlsx(spec: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = spec["title"]
    headers, rows, n = spec["headers"], spec["rows"], len(spec["headers"])
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F4E78")
    for c in range(1, n + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    last = len(rows) + 1
    for idx, fmt in spec["formats"].items():
        for r in range(2, last + 2):
            ws.cell(row=r, column=idx + 1).number_format = fmt
    if spec["totals"] and rows:
        tr = last + 1
        label_col = next((i for i in range(n) if i not in spec["totals"]), None)
        if label_col is not None:
            ws.cell(row=tr, column=label_col + 1, value="Total")
        top = Side(style="thin")
        for i in spec["totals"]:
            col = get_column_letter(i + 1)
            ws.cell(row=tr, column=i + 1, value=f"=SUM({col}2:{col}{last})")
        for c in range(1, n + 1):
            cell = ws.cell(row=tr, column=c)
            cell.font = Font(bold=True)
            cell.border = Border(top=top)
    for i in range(n):
        width = max([len(str(headers[i]))] + [len(str(r[i])) for r in rows[:500] if r[i] is not None])
        ws.column_dimensions[get_column_letter(i + 1)].width = min(max(10, width + 2), 60)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(n)}{last}"

    ch = spec["chart"]
    if ch["type"] != "none":
        chart = {"bar": BarChart, "line": LineChart, "pie": PieChart}[ch["type"]]()
        chart.title = ch["title"]
        cats = Reference(ws, min_col=ch["cat"] + 1, min_row=2, max_row=last)
        for v in ch["vals"]:
            data = Reference(ws, min_col=v + 1, min_row=1, max_row=last)
            chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        if ch["type"] != "pie":
            chart.y_axis.title = headers[ch["vals"][0]] if len(ch["vals"]) == 1 else ""
            chart.x_axis.title = headers[ch["cat"]]
        chart.width, chart.height = 18, 9
        ws.add_chart(chart, f"{get_column_letter(n + 2)}2")
    wb.save(path)


def _open(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        cmd = ["open" if sys.platform == "darwin" else "xdg-open", str(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            _announce.log(f"Excel writer: saved, but opening it failed: "
                          f"{(res.stderr or res.stdout).strip()[:200]}")
    except subprocess.TimeoutExpired:
        pass  # the opener is still running, which is fine
    except Exception as e:
        _announce.log(f"Excel writer: saved, but could not open the file ({e})")


def build(request: str, filename: str = "", open_after: bool = True,
          directory: Path | None = None) -> str:
    """Do the whole job synchronously; returns the sentence to report."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return "Building Excel files needs the openpyxl library: pip install openpyxl"
    from core import gemini
    raw = gemini.as_json(_PROMPT.format(request=request), tier=gemini.SMART, timeout_ms=60_000)
    if raw is None:
        return "I couldn't work out the spreadsheet layout — the model didn't answer."
    spec = clean_spec(raw)
    if spec is None:
        return "I couldn't turn that into a table — try describing the columns and the data."
    folder = directory or _known_folders.desktop()
    stem = safe_filename(filename or spec["filename"] or spec["title"])
    path = _known_folders.unique_path(folder, stem, ".xlsx")
    try:
        write_xlsx(spec, path)
    except Exception as e:
        return f"I built the layout but couldn't write the file: {e}"
    if not path.is_file() or path.stat().st_size == 0:
        return "Writing the spreadsheet seemed to work, but the file isn't there."
    if open_after:
        _open(path)
    extras = []
    if spec["totals"]:
        extras.append("a total row")
    if spec["chart"]["type"] != "none":
        extras.append(f"a {spec['chart']['type']} chart")
    return (f"Created {path.name} with {len(spec['rows'])} rows and {len(spec['headers'])} "
            f"columns" + (" plus " + " and ".join(extras) if extras else "")
            + f", saved in {folder}.")


def _worker(request: str, filename: str, open_after: bool) -> None:
    try:
        msg = build(request, filename, open_after)
    except Exception as e:
        msg = f"The spreadsheet failed: {e}"
    _announce.log(f"Excel writer: {msg}")
    _announce.say(f"Report this to the user briefly: {msg}")


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        request = str(p.get("request") or "").strip()
        if not request:
            return "Tell me what the spreadsheet should contain."
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return "Building Excel files needs the openpyxl library: pip install openpyxl"
        open_after = p.get("open")
        open_after = True if open_after is None else str(open_after).lower() not in ("false", "0", "no")
        threading.Thread(target=_worker,
                         args=(request[:4000], str(p.get("filename") or ""), open_after),
                         daemon=True, name="excel-writer").start()
        return "Building the spreadsheet now; I'll tell you when it's on your Desktop."
    except Exception as e:
        return f"The spreadsheet tool hit an error: {e}"
