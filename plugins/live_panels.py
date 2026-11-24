"""
plugins/live_panels.py — live panels on the HUD: the computer's load, a city's weather.

action='system'  a diagnostics board — CPU (total, per core, recent history),
                 memory, disk, GPU and temperature where the system reports
                 them, network speed and the busiest programs — redrawn every
                 few seconds on a background thread for as long as it stays on
                 screen (closed by voice, by the HUD's ✕, or by another
                 picture replacing it), at most half an hour.
action='weather' a weather board for a city — now, the next 24 hours as a
                 line, the next 7 days as a table — from Open-Meteo (free, no
                 key). JARVIS's own weather_report action only opens a search
                 page, so the numbers are fetched here.
action='close'   closes the panel.

Both return the facts behind the panel, so the assistant answers the question
("is my computer busy?", "will it rain tomorrow?") and the user also sees it.
Boards are drawn by plugins/_visual_core.py (PIL, HUD palette) and shown with
player.show_image — nothing here touches Qt.
"""
from __future__ import annotations

import os
import platform
import threading
import time
from collections import deque
from datetime import datetime

from plugins import _announce
from plugins import _visual_core as vc

PLUGIN = {
    "name": "live_panels",
    "description": (
        "Shows a LIVE panel on the HUD and returns the facts behind it. action='system': the "
        "computer's live diagnostics - CPU (total and per core), memory, disk, GPU, "
        "temperature, network and the busiest programs, refreshed every few seconds. "
        "action='weather': the weather for a city - now, the next 24 hours and 7 days (pass "
        "'city'). action='close': close the panel. Prefer this over system_status for any "
        "question about the computer's performance, load, memory, disk or temperature, and "
        "over weather_report for any weather, temperature, rain or forecast question - it "
        "answers AND shows the panel. Use 'close' when the user asks to close or hide the "
        "panel or dashboard."
    ),
    "brief": "Live HUD panel: system load (CPU/RAM/GPU/disk/net/processes) or a city's weather.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "'system', 'weather' or 'close'"},
            "city": {"type": "STRING", "description": "City for action='weather', in any language"},
            "when": {"type": "STRING",
                     "description": "What the user asked about for weather, e.g. 'today', 'tomorrow', 'weekend'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "live_panels",
    "title": "Live panels",
    "fields": [
        {"key": "temperature_unit", "label": "Temperature unit", "type": "choice",
         "options": ["celsius", "fahrenheit"], "default": "celsius"},
        {"key": "refresh_seconds", "label": "System panel refresh (seconds)", "type": "text",
         "default": "3"},
    ],
}

_STEM = "live"
_MAX_LIVE_S = 30 * 60
_state = {"token": 0}
_lock = threading.Lock()


def _setting(key: str, default):
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("live_panels", key, default)
    except Exception:
        return default


def _new_token() -> int:
    with _lock:
        _state["token"] += 1
        return _state["token"]


def _current(token: int) -> bool:
    with _lock:
        return _state["token"] == token


# ═════════════════════════════════════════════════════════════════════════════
#  System
# ═════════════════════════════════════════════════════════════════════════════
def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def _rate(bps: float) -> str:
    for unit, div in (("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


class _Sampler:
    """psutil readings, with the deltas (network, per-process CPU) that need a
    previous sample. One instance per live panel."""

    def __init__(self):
        import psutil
        self.ps = psutil
        self.cpu_hist: deque = deque(maxlen=40)
        self.ncpu = psutil.cpu_count() or 1
        self._procs: dict[int, object] = {}
        self._net = (time.monotonic(), psutil.net_io_counters())
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self._prime_procs()

    def _prime_procs(self) -> None:
        for pr in self.ps.process_iter(["name"]):
            try:
                pr.cpu_percent(None)
                self._procs[pr.pid] = pr
            except Exception:
                continue

    def _top_procs(self, n: int = 6) -> list[tuple[str, float, float]]:
        rows = []
        seen = set()
        for pr in self.ps.process_iter(["name"]):
            try:
                known = self._procs.get(pr.pid)
                if known is None:
                    pr.cpu_percent(None)                  # first look: no reading yet
                    self._procs[pr.pid] = pr
                    continue
                seen.add(pr.pid)
                name = known.info.get("name") if hasattr(known, "info") else known.name()
                if not name or name.lower() in ("system idle process", "idle"):
                    continue
                cpu = known.cpu_percent(None) / self.ncpu
                mem = known.memory_info().rss
                rows.append((name, cpu, mem))
            except Exception:
                continue
        for pid in list(self._procs):
            if pid not in seen and not self.ps.pid_exists(pid):
                self._procs.pop(pid, None)
        rows.sort(key=lambda r: (-r[1], -r[2]))
        return rows[:n]

    def sample(self) -> dict:
        ps = self.ps
        cpu = ps.cpu_percent(None)
        cores = ps.cpu_percent(None, percpu=True)
        self.cpu_hist.append(cpu)
        vm = ps.virtual_memory()
        root = os.environ.get("SystemDrive", "C:") + "\\" if platform.system() == "Windows" else "/"
        try:
            du = ps.disk_usage(root)
        except Exception:
            du = None
        now, net = time.monotonic(), ps.net_io_counters()
        t0, n0 = self._net
        dt = max(0.001, now - t0)
        down = (net.bytes_recv - n0.bytes_recv) / dt
        up = (net.bytes_sent - n0.bytes_sent) / dt
        self._net = (now, net)
        gpu = temp = None
        try:
            from actions.system_monitor import _get_gpu_usage, _get_cpu_temp
            g = _get_gpu_usage()
            gpu = g if g and g >= 0 else None
            t = _get_cpu_temp()
            temp = t if t and t > 0 else None
        except Exception:
            pass
        batt = None
        try:
            b = ps.sensors_battery()
            if b is not None:
                batt = (b.percent, b.power_plugged)
        except Exception:
            pass
        return {"cpu": cpu, "cores": cores, "mem": vm, "disk": du, "root": root,
                "down": down, "up": up, "gpu": gpu, "temp": temp, "batt": batt,
                "procs": self._top_procs(), "boot": ps.boot_time()}


def _system_spec(s: dict, hist: list[float]) -> dict:
    vm, du = s["mem"], s["disk"]
    rings = [{"label": "CPU", "value": s["cpu"], "detail": f"{len(s['cores'])} threads"},
             {"label": "Memory", "value": vm.percent, "detail": f"{_gb(vm.used)} / {_gb(vm.total)}"}]
    if du is not None:
        rings.append({"label": f"Disk {s['root']}", "value": du.percent,
                      "detail": f"{_gb(du.free)} free"})
    if s["gpu"] is not None:
        rings.append({"label": "GPU", "value": s["gpu"]})
    up_h = (time.time() - s["boot"]) / 3600
    stats = [{"label": "Download", "value": _rate(s["down"])},
             {"label": "Upload", "value": _rate(s["up"])},
             {"label": "Uptime", "value": f"{up_h:.0f} h" if up_h >= 2 else f"{up_h * 60:.0f} min"}]
    if s["temp"] is not None:
        stats.insert(0, {"label": "CPU temp", "value": f"{s['temp']:.0f}°C"})
    if s["batt"] is not None:
        stats.append({"label": "Battery", "value": f"{s['batt'][0]:.0f}%",
                      "detail": "charging" if s["batt"][1] else "on battery"})
    blocks = [
        {"type": "rings", "span": "full", "items": rings},
        {"type": "line", "title": "CPU, last two minutes", "points": hist or [s["cpu"]], "unit": "%"},
        {"type": "stats", "span": "full", "items": stats[:5]},
        {"type": "bars", "title": "Per core", "unit": "%",
         "items": [{"label": f"#{i + 1}", "value": round(v)} for i, v in enumerate(s["cores"][:12])]},
        {"type": "table", "span": "half", "title": "Busiest programs", "columns": ["Program", "CPU", "Memory"],
         "rows": [f"{n[:28]} | {c:.0f}% | {m / 1024 ** 2:.0f} MB" for n, c, m in s["procs"]]},
    ]
    return {"title": "System diagnostics", "subtitle": f"{platform.node()} - {platform.system()} "
            f"{platform.release()} - live", "blocks": blocks}


def _system_facts(s: dict) -> str:
    vm, du = s["mem"], s["disk"]
    parts = [f"CPU {s['cpu']:.0f}% (busiest core {max(s['cores'] or [0]):.0f}%)",
             f"memory {vm.percent:.0f}% ({_gb(vm.used)} of {_gb(vm.total)})"]
    if du is not None:
        parts.append(f"disk {s['root']} {du.percent:.0f}% full ({_gb(du.free)} free)")
    if s["gpu"] is not None:
        parts.append(f"GPU {s['gpu']:.0f}%")
    if s["temp"] is not None:
        parts.append(f"CPU temperature {s['temp']:.0f}°C")
    parts.append(f"network down {_rate(s['down'])}, up {_rate(s['up'])}")
    if s["batt"] is not None:
        parts.append(f"battery {s['batt'][0]:.0f}%")
    procs = ", ".join(f"{n} {c:.0f}%" for n, c, _m in s["procs"][:4])
    return "; ".join(parts) + (f". Busiest programs: {procs}." if procs else ".")


def _system_loop(token: int, player, sampler: _Sampler) -> None:
    try:
        period = max(1.0, min(30.0, float(_setting("refresh_seconds", "3") or 3)))
    except (TypeError, ValueError):
        period = 3.0
    started = time.time()
    time.sleep(1.5)
    while _current(token) and time.time() - started < _MAX_LIVE_S:
        if vc.is_showing(player, _STEM) is False:
            return
        try:
            s = sampler.sample()
            vc.show(player, _system_spec(s, list(sampler.cpu_hist)), _STEM, "System diagnostics")
        except Exception as e:
            print(f"[live_panels] refresh failed: {e}")
            return
        time.sleep(period)


def _show_system(player) -> str:
    try:
        sampler = _Sampler()
    except Exception as e:
        return f"status=failed reason=live_panels: the system could not be read ({e})"
    time.sleep(1.0)                                   # one interval for CPU and network deltas
    s = sampler.sample()
    facts = _system_facts(s)
    if player is None:
        return f"Live system readings: {facts}"
    token = _new_token()
    ok, info = vc.show(player, _system_spec(s, list(sampler.cpu_hist)), _STEM, "System diagnostics")
    if not ok:
        return f"The panel could not be shown ({info}). Facts: {facts}"
    threading.Thread(target=_system_loop, args=(token, player, sampler), daemon=True,
                     name="live-system").start()
    return (f"A live diagnostics panel is now on screen (it updates every few seconds). Facts: "
            f"{facts} Answer briefly in the user's language; mention anything near its limit.")


# ═════════════════════════════════════════════════════════════════════════════
#  Weather (Open-Meteo, no key)
# ═════════════════════════════════════════════════════════════════════════════
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _weather(city: str) -> tuple[dict | None, str]:
    import requests
    try:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "format": "json"}, timeout=8)
        g.raise_for_status()
        hits = (g.json() or {}).get("results") or []
    except Exception as e:
        return None, f"the weather service could not be reached ({e})"
    if not hits:
        return None, f"no place called '{city}' was found"
    place = hits[0]
    unit = "fahrenheit" if str(_setting("temperature_unit", "celsius")).lower().startswith("f") else "celsius"
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "temperature_unit": unit, "wind_speed_unit": "kmh", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "wind_speed_10m,weather_code,precipitation",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,sunrise,sunset",
        }, timeout=10)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        return None, f"the forecast could not be fetched ({e})"
    data["_place"] = place
    data["_unit"] = "°F" if unit == "fahrenheit" else "°C"
    return data, ""


def _weather_spec_and_facts(d: dict, when: str) -> tuple[dict, str]:
    place, u = d["_place"], d["_unit"]
    name = ", ".join(x for x in (place.get("name"), place.get("admin1"), place.get("country")) if x)
    cur = d.get("current") or {}
    desc = _WMO.get(int(cur.get("weather_code") or 0), "")
    hourly = d.get("hourly") or {}
    times = hourly.get("time") or []
    now_key = str(cur.get("time") or "")[:13]
    start = next((i for i, t in enumerate(times) if str(t)[:13] >= now_key), 0)
    temps = (hourly.get("temperature_2m") or [])[start:start + 24]
    rain = (hourly.get("precipitation_probability") or [])[start:start + 24]
    labels = [str(t)[11:16] for t in times[start:start + 24]]
    daily = d.get("daily") or {}
    days = []
    for i, day in enumerate(daily.get("time") or []):
        try:
            dt = datetime.fromisoformat(day)
            label = "Today" if i == 0 else ("Tomorrow" if i == 1 else dt.strftime("%a %d"))
            days.append({
                "label": label,
                "desc": _WMO.get(int((daily.get("weather_code") or [0])[i] or 0), ""),
                "hi": (daily.get("temperature_2m_max") or [None])[i],
                "lo": (daily.get("temperature_2m_min") or [None])[i],
                "rain": (daily.get("precipitation_probability_max") or [None])[i],
            })
        except Exception:
            continue
    stats = [{"label": "Now", "value": f"{cur.get('temperature_2m', '?')}{u}", "detail": desc},
             {"label": "Feels like", "value": f"{cur.get('apparent_temperature', '?')}{u}"},
             {"label": "Humidity", "value": f"{cur.get('relative_humidity_2m', '?')}%"},
             {"label": "Wind", "value": f"{cur.get('wind_speed_10m', '?')} km/h"}]
    blocks = [{"type": "stats", "span": "full", "items": stats}]
    if temps:
        blocks.append({"type": "line", "title": "Next 24 hours", "points": temps, "labels": labels,
                       "unit": u})
    if rain and any(r for r in rain if r):
        blocks.append({"type": "bars", "title": "Chance of rain, next hours", "unit": "%",
                       "items": [{"label": labels[i], "value": rain[i] or 0}
                                 for i in range(0, min(24, len(rain)), 3)]})
    if days:
        blocks.append({"type": "table", "title": "Next 7 days", "span": "full",
                       "columns": ["Day", "Sky", "High", "Low", "Rain"],
                       "rows": [f"{x['label']} | {x['desc']} | {x['hi']}{u} | {x['lo']}{u} | "
                                f"{x['rain'] if x['rain'] is not None else '?'}%" for x in days]})
    sun = ""
    try:
        sun = (f"sunrise {str(daily['sunrise'][0])[11:16]}, sunset {str(daily['sunset'][0])[11:16]}")
    except Exception:
        pass
    spec = {"title": f"Weather - {place.get('name') or name}", "subtitle":
            f"{name} - local time {str(cur.get('time') or '')[11:16]} - Open-Meteo", "blocks": blocks}
    day_txt = "; ".join(f"{x['label']}: {x['desc']}, {x['lo']}-{x['hi']}{u}, rain {x['rain']}%"
                        for x in days)
    facts = (f"{name}: now {cur.get('temperature_2m')}{u} ({desc}), feels like "
             f"{cur.get('apparent_temperature')}{u}, humidity {cur.get('relative_humidity_2m')}%, "
             f"wind {cur.get('wind_speed_10m')} km/h" + (f", {sun}" if sun else "")
             + f". Days: {day_txt}." + (f" The user asked about: {when}." if when else ""))
    return spec, facts


def _show_weather(player, city: str, when: str) -> str:
    if not city:
        return "status=ask question=which city the user wants the weather for"
    data, err = _weather(city)
    if data is None:
        return f"status=failed reason=the weather for {city} could not be read ({err}); say so - do not guess the weather"
    try:
        spec, facts = _weather_spec_and_facts(data, when)
    except Exception as e:
        return f"status=failed reason=the weather for {city} could not be read (the facts could not be written: {e})"
    if player is None:
        return f"Weather facts: {facts}"
    _new_token()                       # stops a running system panel
    ok, info = vc.show(player, spec, _STEM, spec["title"])
    if not ok:
        return f"The panel could not be shown ({info}). Facts: {facts}"
    return (f"A live weather panel is now on screen. Facts: {facts} Answer the user's question "
            "briefly in their language; do not read the panel out.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "system").strip().lower()
    _announce.remember(player)
    try:
        if action in ("close", "hide", "stop"):
            _new_token()
            if player is not None:
                vc.close(player, _STEM)
            return "status=done closed=live panel"
        if action in ("system", "diagnostics", "status"):
            return _show_system(player)
        if action == "weather":
            return _show_weather(player, str(p.get("city") or "").strip(), str(p.get("when") or "").strip())
        return f"status=failed reason=live_panels: unknown action '{action}' (use system, weather or close)"
    except Exception as e:
        return f"status=failed reason=live_panels: {e}"
