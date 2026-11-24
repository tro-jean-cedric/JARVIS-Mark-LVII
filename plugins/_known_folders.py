"""
plugins/_known_folders.py — where the user's real Desktop, Documents, Downloads
... folders are, on every platform.

Helper module (leading underscore: the plugin loader skips it).

`Path.home() / "Desktop"` is wrong more often than it looks:
  * Windows with OneDrive backup moves Desktop/Documents/Pictures into
    %OneDrive%\\Desktop and friends; the old folder may still exist, empty.
  * Windows lets the user relocate any of them (Properties → Location).
  * Linux names them per locale (~/Bureau, ~/Schreibtisch) via user-dirs.dirs.

The registry / user-dirs answer is used first; the plain home-relative folder is
the last resort. Nothing here raises.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Windows "User Shell Folders" value names.
_WIN_NAMES = {
    "desktop":   ("Desktop",),
    "documents": ("Personal",),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}",),
    "pictures":  ("My Pictures",),
    "videos":    ("My Video",),
    "music":     ("My Music",),
}
# XDG user-dirs keys.
_XDG_NAMES = {
    "desktop": "DESKTOP", "documents": "DOCUMENTS", "downloads": "DOWNLOAD",
    "pictures": "PICTURES", "videos": "VIDEOS", "music": "MUSIC",
}
# Plain folder names under home (macOS / fallback).
_PLAIN = {
    "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
    "pictures": "Pictures", "videos": "Movies" if sys.platform == "darwin" else "Videos",
    "music": "Music",
}

KNOWN = tuple(_PLAIN)


def _windows(name: str) -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    for value in _WIN_NAMES.get(name, ()):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, value)
            p = Path(os.path.expandvars(str(raw)))
            if p.is_dir():
                return p
        except OSError:
            continue
    # OneDrive folder backup, when the registry did not answer.
    if name in ("desktop", "documents", "pictures"):
        sub = {"desktop": "Desktop", "documents": "Documents", "pictures": "Pictures"}[name]
        for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            root = os.environ.get(env)
            if root and (Path(root) / sub).is_dir():
                return Path(root) / sub
    return None


def _linux(name: str) -> Path | None:
    key = _XDG_NAMES.get(name)
    if not key:
        return None
    env = os.environ.get(f"XDG_{key}_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(rf'\s*XDG_{key}_DIR\s*=\s*"(.*)"', line)
            if m:
                p = Path(m.group(1).replace("$HOME", str(Path.home())))
                if p.is_dir():
                    return p
    except OSError:
        pass
    return None


def folder(name: str) -> Path:
    """The real folder for 'desktop', 'documents', 'downloads', 'pictures',
    'videos', 'music' or 'home'. Falls back to ~/<Name>, then to home."""
    name = (name or "").strip().lower()
    if name in ("home", "~", "user", "profile"):
        return Path.home()
    found = None
    try:
        if sys.platform == "win32":
            found = _windows(name)
        elif sys.platform.startswith("linux"):
            found = _linux(name)
    except Exception:
        found = None
    if found is not None:
        return found
    plain = Path.home() / _PLAIN.get(name, name.capitalize())
    return plain if plain.is_dir() else Path.home()


def desktop() -> Path:
    return folder("desktop")


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """directory/stem.suffix, or 'stem (2).suffix' etc. when that is taken."""
    p = directory / f"{stem}{suffix}"
    n = 2
    while p.exists():
        p = directory / f"{stem} ({n}){suffix}"
        n += 1
    return p
