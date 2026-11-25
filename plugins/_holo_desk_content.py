"""
plugins/_holo_desk_content.py — what the Holo Desk's panels show.

Helper module (leading underscore: the plugin loader skips it). Every loader
runs on its own worker thread, never on the Qt thread, and writes its result
into the panel under the desk's lock; the window simply draws what is there.
A loader never raises: a failure becomes the panel's `status` line.

Sources are the app's own:
  weather   plugins/live_panels._weather (Open-Meteo, no key); the city from
            the panel, else the one in the assistant's memory
  news      actions/web_search._ddg_news (DuckDuckGo news, no key)
  notes     plugins/obsidian_vault (the user's vault, local files)
  routines  core/routines (what is coming up, reminders included)
  guide     the gesture list in plugins/_holo_desk_core.GUIDE
Text from outside (headlines, notes) is data to show, never instructions.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from plugins import _holo_desk_core as core

REFRESH = {"weather": 900, "news": 900, "routines": 60, "notes": 300}


def load(panel, desk) -> None:
    """Fill a panel in the background. Safe to call from any thread."""
    if panel.kind in ("clock", "system", "text"):
        return
    threading.Thread(target=_load, args=(panel, desk), daemon=True,
                     name=f"holo-{panel.kind}").start()


def _set(desk, panel, **fields) -> None:
    with desk.lock:
        for k, v in fields.items():
            setattr(panel, k, v)
        panel.loaded_at = time.time()


def _load(panel, desk) -> None:
    try:
        fn = {"weather": _weather, "news": _news, "notes": _notes, "routines": _routines,
              "guide": _guide, "image": _image, "video": _video, "model": _model}.get(panel.kind)
        if fn is None:
            return
        if panel.kind in ("weather", "news", "notes", "routines") and not panel.rows \
                and not panel.text:
            _set(desk, panel, status="Loading...")
        fn(panel, desk)
    except Exception as e:
        _set(desk, panel, status=f"Could not load this panel: {e}")


def refresher(desk, stop: threading.Event) -> None:
    """Keeps weather, news, notes and routines fresh while the desk is open."""
    while not stop.wait(20):
        now = time.time()
        for p in desk.live():
            every = REFRESH.get(p.kind)
            if every and now - getattr(p, "loaded_at", now) >= every:
                p.loaded_at = now
                load(p, desk)


# ── weather ──────────────────────────────────────────────────────────────────
def _memory_city() -> str:
    try:
        from core.briefing import _memory_city
        return _memory_city()
    except Exception:
        return ""


def _weather(panel, desk) -> None:
    city = (panel.arg or "").strip() or _memory_city()
    if not city:
        _set(desk, panel, status='No place known yet - ask e.g. "put the weather for Lisbon on '
                                 'the desk".')
        return
    from plugins.live_panels import _WMO, _weather as fetch
    data, err = fetch(city)
    if data is None:
        _set(desk, panel, status=f"No weather: {err}.")
        return
    cur, daily, u = data.get("current") or {}, data.get("daily") or {}, data.get("_unit", "°C")
    place = data.get("_place") or {}
    name = ", ".join(x for x in (place.get("name"), place.get("country")) if x)
    lines = [f"{cur.get('temperature_2m', '?'):.0f}{u}  {_WMO.get(cur.get('weather_code'), '')}"
             if isinstance(cur.get("temperature_2m"), (int, float)) else "?",
             f"Feels like {cur.get('apparent_temperature', '?')}{u}  ·  wind "
             f"{cur.get('wind_speed_10m', '?')} km/h  ·  humidity {cur.get('relative_humidity_2m', '?')}%"]
    days = daily.get("time") or []
    for i, day in enumerate(days[:4]):
        try:
            label = "Today" if i == 0 else datetime.fromisoformat(day).strftime("%a")
            lines.append(f"{label}:  {daily['temperature_2m_min'][i]:.0f} - "
                         f"{daily['temperature_2m_max'][i]:.0f}{u}  "
                         f"{_WMO.get(daily['weather_code'][i], '')}  "
                         f"rain {daily['precipitation_probability_max'][i]}%")
        except Exception:
            continue
    _set(desk, panel, text="\n".join(lines), status="", arg=panel.arg or city,
         title=panel.title or f"Weather - {name or city}")


# ── news ─────────────────────────────────────────────────────────────────────
def _news(panel, desk) -> None:
    topic = (panel.arg or "").strip() or "top news today"
    from actions.web_search import _ddg_news
    box: list = []
    t = threading.Thread(target=lambda: box.append(_ddg_news(topic, 20)), daemon=True)
    t.start()
    t.join(15)
    items = box[0] if box else []
    rows = []
    for r in items:
        title = " ".join(str(r.get("title") or "").split())
        if not title:
            continue
        src = str(r.get("source") or "").strip()
        rows.append((title + (f"  ({src})" if src else ""), str(r.get("url") or "")))
    if not rows:
        _set(desk, panel, status=f"No headlines came back for '{topic}'.", rows=[])
        return
    _set(desk, panel, rows=rows, status="")


# ── notes ────────────────────────────────────────────────────────────────────
def _notes(panel, desk) -> None:
    try:
        from plugins import obsidian_vault as ov
        vault, why = ov._vault()
    except Exception as e:
        vault, why = None, str(e)
    if vault is None:
        _set(desk, panel, status="No Obsidian vault found - set its folder in the Obsidian "
                                 "plugin's settings.")
        return
    found = []
    for p in ov._iter_notes(vault, limit=20_000):
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            continue
    found.sort(reverse=True)
    rows = [(ov._rel(vault, p), "note:" + str(p)) for _m, p in found[:40]]
    if not rows:
        _set(desk, panel, status="The vault has no notes yet.")
        return
    _set(desk, panel, rows=rows, status="")


def read_note(path: str, limit: int = 6000) -> str:
    try:
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:limit] + ("\n..." if len(text) > limit else "")
    except Exception as e:
        return f"The note could not be read: {e}"


# ── routines ─────────────────────────────────────────────────────────────────
def _routines(panel, desk) -> None:
    try:
        from core.routines import desktop
        eng = desktop.engine()
    except Exception:
        eng = None
    if eng is None:
        _set(desk, panel, status="Routines are not running.")
        return
    now = time.time()
    items = []
    for r in eng.list():
        if not r.get("enabled") or (r.get("trigger") or {}).get("type") != "time":
            continue
        ts = float(r.get("next_ts") or 0)
        if ts and ts >= now - 300:
            items.append((ts, (r.get("text") or r.get("name") or "")[:120]))
    items.sort()
    if not items:
        _set(desk, panel, rows=[], status='Nothing coming up. Try "remind me at 7 to water the '
                                          'plants".')
        return
    today = datetime.now().date()
    rows = []
    for ts, text in items[:30]:
        at = datetime.fromtimestamp(ts)
        when = at.strftime("%H:%M") if at.date() == today else at.strftime("%a %d %b %H:%M")
        rows.append((f"{when}   {text}", ""))
    _set(desk, panel, rows=rows, status="")


# ── the rest ─────────────────────────────────────────────────────────────────
def _guide(panel, desk) -> None:
    _set(desk, panel, rows=[(line, "") for line in core.GUIDE], status="")


def _image(panel, desk) -> None:
    path = os.path.expanduser(panel.arg or "")
    if not path or not os.path.isfile(path):
        _set(desk, panel, status="This picture could not be opened.")
        return
    _set(desk, panel, arg=path, status="", title=panel.title or Path(path).name)


def _model(panel, desk) -> None:
    if panel.arg:
        path = os.path.expanduser(panel.arg)
        if not os.path.isfile(path) or not path.lower().endswith(".obj"):
            _set(desk, panel, status="A 3D model panel needs a .obj file.")
            return
        _set(desk, panel, arg=path, title=panel.title or Path(path).stem)
    else:
        _set(desk, panel, title=panel.title or "The assistant's head")


def _video(panel, desk) -> None:
    src = (panel.arg or "").strip()
    if not src:
        _set(desk, panel, status="A video card needs a file path or a web link.")
        return
    low = src.lower()
    if low.startswith(("http://", "https://")):
        if any(h in low for h in ("youtube.com", "youtu.be")):
            import webbrowser
            webbrowser.open(src)
            _set(desk, panel, status="Opened in the browser (YouTube plays there).")
            return
        _set(desk, panel, status="", title=panel.title or src.rsplit("/", 1)[-1][:40])
        return
    path = os.path.expanduser(src)
    if not os.path.isfile(path):
        _set(desk, panel, status="The video could not be found.")
        return
    _set(desk, panel, arg=path, status="", title=panel.title or Path(path).name)
