"""
core/installed_apps.py — which programs are really installed, and which one
the user means.

    from core import installed_apps
    app = installed_apps.resolve("le bloc-notes")    # -> App("Notepad", ...) or None
    installed_apps.launch(app)                         # -> True when the OS took the request

WHY
    open_app used to hand the spoken name straight to the launcher. That works
    when the user says the program's name in English; it fails for a
    translation ("bloc-notes", "calculadora"), a nickname ("the music app") or a
    description ("the thing I edit photos with"), and the Start-menu fallback
    then opens whatever happens to match the typed letters. Asking what is
    installed first turns all of those into the right program — or into an
    honest "nothing like that is installed".

HOW
  1. list()      the installed apps, cached for five minutes:
                   macOS    .app bundles in the Applications folders
                   Windows  the Start menu (Get-StartApps), Store apps included
                   Linux    .desktop entries, Flatpak and Snap ones included
  2. resolve()   an exact name wins outright; then a single program whose name
                 contains the spoken words; otherwise the model is shown the
                 list and must answer with one of its names or nothing — never
                 "a similar but different program". Its answer is checked
                 against the list, so it cannot invent one.
  3. launch()    starts exactly that program (open -a <bundle>,
                 shell:AppsFolder\\<id>, gtk-launch <id>). No shell anywhere.

Nothing raises: a failure is None or False, and open_app falls back to the
behaviour it had before this existed.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SYSTEM = platform.system()
_CACHE_SECONDS = 300
_cache: tuple[float, list] = (0.0, [])
_lock = threading.Lock()


@dataclass(frozen=True)
class App:
    name: str          # what the user sees in the menu / Finder
    target: str        # bundle path, AppUserModelID or desktop-file id


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# ── listing ──────────────────────────────────────────────────────────────────
def _mac_apps() -> list:
    roots = [Path("/Applications"), Path("/System/Applications"),
             Path("/System/Applications/Utilities"), Path("/Applications/Utilities"),
             Path.home() / "Applications"]
    out, seen = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in list(root.glob("*.app")) + list(root.glob("*/*.app")):
            name = p.stem
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(App(name, str(p)))
    return out


def _windows_apps() -> list:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(r.stdout or "[]")
    except Exception as e:
        print(f"[installed_apps] Get-StartApps failed: {e}")
        return []
    if isinstance(data, dict):
        data = [data]
    out, seen = [], set()
    for row in data or []:
        name, app_id = str(row.get("Name") or "").strip(), str(row.get("AppID") or "").strip()
        if not name or not app_id or name.lower() in seen:
            continue
        # Start-menu entries that are documents or uninstallers are not programs.
        if re.search(r"\b(uninstall|readme|help|documentation|release notes)\b", name, re.I):
            continue
        seen.add(name.lower())
        out.append(App(name, app_id))
    return out


def _linux_apps() -> list:
    dirs = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications",
            Path("/var/lib/snapd/desktop/applications")]
    out, seen = [], set()
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entry = text.split("[Desktop Entry]", 1)[-1].split("\n[", 1)[0]
            fields = dict(line.split("=", 1) for line in entry.splitlines() if "=" in line)
            if fields.get("NoDisplay", "").lower() == "true" or fields.get("Type") != "Application":
                continue
            name = fields.get("Name", "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(App(name, f.stem))
    return out


def list_apps(refresh: bool = False) -> list:
    global _cache
    with _lock:
        if not refresh and _cache[1] and time.time() - _cache[0] < _CACHE_SECONDS:
            return list(_cache[1])
    try:
        apps = {"Darwin": _mac_apps, "Windows": _windows_apps,
                "Linux": _linux_apps}.get(_SYSTEM, lambda: [])()
    except Exception as e:
        print(f"[installed_apps] listing failed: {e}")
        apps = []
    apps.sort(key=lambda a: a.name.lower())
    with _lock:
        _cache = (time.time(), apps)
    return list(apps)


# ── resolving ────────────────────────────────────────────────────────────────
_ASK = (
    "The user asked this computer to open a program. They said: {spoken!r}\n\n"
    "Which program on this computer's list do they mean? The name may be in any "
    "language, a translation, a nickname or a description of what the program does. "
    "Answer with the program's name EXACTLY as written in the list, or with an empty "
    "string when nothing on the list is what they mean. Never a similar but different "
    "program: if they asked for one that is not installed, answer empty.\n\n"
    'Reply with JSON only: {{"app": "<name from the list or empty>"}}\n\n'
    "Programs on this computer:\n{names}"
)


def _direct(spoken: str, apps: list) -> Optional[App]:
    want = _fold(spoken)
    if not want:
        return None
    for a in apps:
        if _fold(a.name) == want:
            return a
    words = want.split()
    hits = [a for a in apps if all(re.search(rf"\b{re.escape(w)}\b", _fold(a.name))
                                   for w in words)]
    return hits[0] if len(hits) == 1 else None


def resolve(spoken: str, apps: Optional[list] = None, ask=None) -> Optional[App]:
    """The installed program the user means, or None.

    `ask(prompt) -> dict | None` is the model call (core.gemini.as_json by
    default); tests pass their own."""
    apps = list_apps() if apps is None else apps
    if not apps or not (spoken or "").strip():
        return None
    hit = _direct(spoken, apps)
    if hit is not None:
        return hit
    if ask is None:
        def ask(prompt):
            from core import gemini
            return gemini.as_json(prompt, tier=gemini.FAST, timeout_ms=12_000)
    names = "\n".join(a.name for a in apps[:900])
    try:
        reply = ask(_ASK.format(spoken=spoken, names=names))
    except Exception as e:
        print(f"[installed_apps] the model could not be asked: {e}")
        return None
    chosen = str((reply or {}).get("app") or "").strip() if isinstance(reply, dict) else ""
    if not chosen:
        return None
    by_name = {a.name.lower(): a for a in apps}
    found = by_name.get(chosen.lower())
    if found is None:
        print(f"[installed_apps] the model named {chosen!r}, which is not installed - ignored")
    return found


# ── launching ────────────────────────────────────────────────────────────────
def launch(app: App) -> bool:
    """Ask the OS to start exactly this program. True = the request was taken
    (open_app still verifies that a window appeared)."""
    try:
        if _SYSTEM == "Darwin":
            r = subprocess.run(["open", "-a", app.target], capture_output=True, timeout=10)
            return r.returncode == 0
        if _SYSTEM == "Windows":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.target}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if _SYSTEM == "Linux":
            r = subprocess.run(["gtk-launch", app.target], capture_output=True, timeout=8)
            return r.returncode == 0
    except Exception as e:
        print(f"[installed_apps] launching {app.name} failed: {e}")
    return False


if __name__ == "__main__":
    import sys
    apps = list_apps()
    print(f"{len(apps)} programs, e.g.: " + ", ".join(a.name for a in apps[:12]))
    for q in sys.argv[1:]:
        print(f"{q!r} -> {resolve(q, apps)}")
