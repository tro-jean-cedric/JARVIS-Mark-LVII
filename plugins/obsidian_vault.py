"""
plugins/obsidian_vault.py — read, search and write Markdown notes in the user's
Obsidian vault.

The vault folder comes from ⚙ → plugin settings (OBSIDIAN VAULT). Left blank,
it is found automatically: first from Obsidian's own obsidian.json (the vault
list the app keeps), then by looking for a `.obsidian` folder in the usual
places. Every path is checked to stay inside the vault, nothing is ever
deleted, and writes push an undo step (core/undo.py).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from datetime import date, datetime
from pathlib import Path

PLUGIN = {
    "name": "obsidian_vault",
    "description": (
        "Reads, searches and writes notes in the user's Obsidian vault. Use whenever "
        "the user mentions notes, the vault, Obsidian, the daily note or journal, or "
        "asks what they wrote about something: 'what did I write about the trip', "
        "'add this to my notes', 'put it in today's note', 'open my Website note', "
        "'what's still unchecked in my notes', 'which notes link to Website'. Not for "
        "ordinary files or PDFs (use file_processor / file_controller). Only touches "
        "Markdown inside the configured vault and never deletes anything."
    ),
    "brief": "Obsidian vault: search, read, list, create, append, daily note, tasks, backlinks.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "search, read, list, create, append, daily, tasks, "
                                      "backlinks or open. Default: search when a query is "
                                      "given, else list."},
            "note": {"type": "STRING",
                     "description": "Note title or vault-relative path, e.g. 'Website' or "
                                    "'Projects/Website'. For read, create, append, backlinks, open."},
            "text": {"type": "STRING", "description": "Markdown to write, for create, append and daily."},
            "query": {"type": "STRING", "description": "What to look for, for search."},
            "folder": {"type": "STRING", "description": "Vault-relative folder to limit list/search to."},
        },
        "required": [],
    },
}

_NS = "obsidian_vault"


def _check_action(values: dict):
    """Settings-panel button: validate the typed path, or find a vault."""
    raw = str((values or {}).get("vault_path") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_dir():
            return False, "That folder doesn't exist."
        n = sum(1 for _ in _iter_notes(p, limit=100_000))
        flag = "" if (p / ".obsidian").is_dir() else " (no .obsidian folder — is this the vault root?)"
        return True, f"OK: {n} notes{flag}."
    found = detect_vaults()
    if not found:
        return False, "No vault path set, and I couldn't find one automatically."
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(_NS, {"vault_path": str(found[0])})
    except Exception as e:
        return True, f"Found a vault at {found[0]} — type it in the box ({e})."
    return True, f"No path set. Found a vault at: {found[0]} — saved; reopen settings to see it."


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "OBSIDIAN VAULT",
    "fields": [
        {"key": "vault_path", "label": "Vault folder (blank = find automatically)",
         "type": "text", "placeholder": "e.g. ~/Documents/MyVault"},
        {"key": "daily_folder", "label": "Daily notes folder (blank = Obsidian's setting / root)",
         "type": "text", "placeholder": "Journal"},
        {"key": "daily_format", "label": "Daily note name format (blank = YYYY-MM-DD)",
         "type": "text", "placeholder": "YYYY-MM-DD"},
        {"key": "new_folder", "label": "Folder for new notes (blank = vault root)",
         "type": "text", "placeholder": "Inbox"},
    ],
    "action": {"label": "CHECK VAULT", "run": _check_action},
}

_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".stfolder"}
_MAX_READ_KB = 200


# ── settings & vault discovery ───────────────────────────────────────────────

def _setting(key: str, default: str = "") -> str:
    try:
        from memory.config_manager import get_plugin_setting
        return str(get_plugin_setting(_NS, key, default) or default).strip()
    except Exception:
        return default


def _obsidian_json_paths() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [base / "obsidian" / "obsidian.json"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [cfg / "obsidian" / "obsidian.json",
            home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
            home / "snap" / "obsidian" / "current" / ".config" / "obsidian" / "obsidian.json"]


def detect_vaults() -> list[Path]:
    """Vault folders on this machine, most likely first."""
    out: list[Path] = []
    for f in _obsidian_json_paths():
        try:
            vaults = json.loads(f.read_text(encoding="utf-8")).get("vaults") or {}
        except Exception:
            continue
        ranked = sorted(vaults.values(),
                        key=lambda v: (bool(v.get("open")), v.get("ts", 0)), reverse=True)
        for v in ranked:
            p = Path(str(v.get("path") or ""))
            if p.is_dir() and p not in out:
                out.append(p)
    if out:
        return out
    home = Path.home()
    roots = [home, home / "Documents", home / "Obsidian", home / "Documents" / "Obsidian",
             home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents",
             home / "OneDrive" / "Documents", home / "Dropbox"]
    for root in roots:
        try:
            if (root / ".obsidian").is_dir():
                out.append(root)
            for child in root.iterdir():
                if child.is_dir() and (child / ".obsidian").is_dir() and child not in out:
                    out.append(child)
        except OSError:
            continue
    return out


def _vault() -> tuple[Path | None, str]:
    raw = _setting("vault_path")
    if raw:
        p = Path(raw).expanduser()
        return (p, "") if p.is_dir() else (None, f"The vault folder {raw} doesn't exist.")
    found = detect_vaults()
    if found:
        return found[0], ""
    return None, ("No Obsidian vault is set up yet. Open the settings, put the vault folder "
                  "into OBSIDIAN VAULT and press CHECK VAULT — leave the box empty and it "
                  "will try to find it for you.")


# ── path helpers ─────────────────────────────────────────────────────────────

def _inside(vault: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(vault.resolve())
        return True
    except (ValueError, OSError):
        return False


def _iter_notes(vault: Path, folder: Path | None = None, limit: int = 50_000):
    base = folder or vault
    n = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                yield Path(root) / f
                n += 1
                if n >= limit:
                    return


def _rel(vault: Path, p: Path) -> str:
    try:
        return p.relative_to(vault).as_posix()[:-3] if p.suffix == ".md" else p.relative_to(vault).as_posix()
    except ValueError:
        return p.stem


def _note_path(vault: Path, name: str) -> tuple[Path | None, str]:
    """Where a note called `name` is (or would be). Returns (path, error)."""
    name = str(name or "").strip().strip("[]").strip()
    if "|" in name:
        name = name.split("|", 1)[0]
    if not name or name in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', name):
        return None, f"'{name}' isn't a usable note name."
    rel = name.replace("\\", "/").strip("/")
    if not rel.lower().endswith(".md"):
        rel += ".md"
    direct = vault / rel
    if not _inside(vault, direct):
        return None, "That note name points outside the vault, so I've left it alone."
    if direct.is_file():
        return direct, ""
    if "/" not in rel:
        stem = rel[:-3].casefold()
        for p in _iter_notes(vault):
            if p.stem.casefold() == stem:
                return p, ""
        new_dir = _setting("new_folder")
        if new_dir:
            cand = vault / new_dir / rel
            if _inside(vault, cand):
                return cand, ""
    return direct, ""


def _folder(vault: Path, name: str) -> tuple[Path | None, str]:
    if not name:
        return vault, ""
    p = vault / name.replace("\\", "/").strip("/")
    if not _inside(vault, p):
        return None, "That folder is outside the vault, so I've left it alone."
    if not p.is_dir():
        return None, f"There's no folder '{name}' in the vault."
    return p, ""


def _push_undo(label: str, path: Path, before: str | None) -> None:
    try:
        from core.undo import push_undo
    except Exception:
        return

    def _undo() -> str:
        try:
            if before is None:
                # we created it: move it to the vault's own trash, never delete
                trash = _vault()[0]
                if trash is not None:
                    tdir = trash / ".trash"
                    tdir.mkdir(exist_ok=True)
                    path.replace(tdir / f"{path.stem}-{int(time.time())}.md")
                return f"Moved {path.stem} to the vault's .trash folder."
            path.write_text(before, encoding="utf-8")
            return f"Restored {path.stem} to how it was."
        except Exception as e:
            return f"Couldn't undo the note change: {e}"
    push_undo(label, _undo)


# ── actions ──────────────────────────────────────────────────────────────────

def _search(vault: Path, query: str, folder: str, player) -> str:
    query = query.strip()
    if not query:
        return "What should I look for in the vault?"
    base, err = _folder(vault, folder)
    if err:
        return err
    terms = [t.casefold() for t in re.findall(r"\w+", query) if len(t) > 1] or [query.casefold()]
    hits = []
    deadline = time.monotonic() + 4.0
    for p in _iter_notes(vault, base):
        if time.monotonic() > deadline:
            break
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low, title = text.casefold(), p.stem.casefold()
        score = sum(low.count(t) for t in terms) + 5 * sum(t in title for t in terms)
        if query.casefold() in low:
            score += 5
        if score == 0 or not all(t in low or t in title for t in terms):
            continue
        i = low.find(terms[0])
        snippet = re.sub(r"\s+", " ", text[max(0, i - 80): i + 160]).strip() if i >= 0 else ""
        hits.append((score, p, snippet))
    if not hits:
        return f"Nothing in the vault mentions '{query}'."
    hits.sort(key=lambda h: h[0], reverse=True)
    top = hits[:8]
    if player is not None:
        try:
            player.show_content(f"Vault: {query}"[:48],
                                "\n\n".join(f"■ {_rel(vault, p)}\n…{s}…" for _, p, s in top))
        except Exception:
            pass
    lines = [f"{_rel(vault, p)}: …{s[:160]}…" for _, p, s in top[:5]]
    return (f"{len(hits)} note{'s mention' if len(hits) != 1 else ' mentions'} '{query}'. "
            f"Top matches — "
            + " | ".join(lines))


def _read(vault: Path, note: str, player) -> str:
    if not note:
        return "Which note should I read?"
    p, err = _note_path(vault, note)
    if err:
        return err
    if p is None or not p.is_file():
        return f"There's no note called '{note}' in the vault."
    size_kb = p.stat().st_size / 1024
    text = p.read_text(encoding="utf-8", errors="ignore")
    if player is not None:
        try:
            player.show_content(p.stem, text[:4000])
        except Exception:
            pass
    if size_kb > _MAX_READ_KB:
        return (f"{p.stem} is {int(size_kb)} KB — too long to read out. Here's the beginning "
                f"(open it in Obsidian for the rest): {text[:2500]}")
    if len(text) > 6000:
        return f"[{p.stem} — the note continues; this is the beginning.]\n{text[:6000]}"
    return f"[{p.stem}]\n{text}" if text.strip() else f"{p.stem} is empty."


def _list(vault: Path, folder: str) -> str:
    base, err = _folder(vault, folder)
    if err:
        return err
    notes = []
    for p in _iter_notes(vault, base):
        try:
            notes.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not notes:
        return "There are no notes there yet."
    notes.sort(key=lambda t: t[0], reverse=True)
    recent = ", ".join(f"{_rel(vault, p)} ({datetime.fromtimestamp(m):%b %d})" for m, p in notes[:10])
    where = f" in {folder}" if folder else ""
    return f"{len(notes)} notes{where}. Most recently changed: {recent}."


def _create(vault: Path, note: str, text: str) -> str:
    if not note:
        return "What should the note be called?"
    p, err = _note_path(vault, note)
    if err:
        return err
    if p.exists():
        return (f"A note called {p.stem} already exists; I haven't touched it. Say 'add it to "
                f"{p.stem}' if you want it appended instead.")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n" if text else "")
    except Exception as e:
        return f"I couldn't create that note: {e}"
    _push_undo(f"created note {p.stem}", p, None)
    return f"Created {_rel(vault, p)}."


def _append_to(p: Path, text: str) -> str | None:
    """Append, returning the previous content (None if the file was new)."""
    before = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not before or before.endswith("\n") else "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(sep + text.rstrip() + "\n")
    return before


def _append(vault: Path, note: str, text: str) -> str:
    if not note:
        return "Which note should I add that to?"
    if not text.strip():
        return "What should I add to it?"
    p, err = _note_path(vault, note)
    if err:
        return err
    try:
        before = _append_to(p, text)
    except Exception as e:
        return f"I couldn't write to that note: {e}"
    _push_undo(f"added to note {p.stem}", p, before)
    if before is None:
        return f"'{p.stem}' didn't exist, so I made it and wrote that in."
    return f"Added to {_rel(vault, p)}."


def _moment_format(fmt: str) -> str:
    """Obsidian (moment.js) date format → strftime."""
    table = [("YYYY", "%Y"), ("YY", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
             ("DD", "%d"), ("dddd", "%A"), ("ddd", "%a"), ("Do", "%d"), ("M", "%m"), ("D", "%d")]
    out, i = "", 0
    while i < len(fmt):
        if fmt[i] == "[":
            j = fmt.find("]", i)
            if j > i:
                out += fmt[i + 1:j].replace("%", "%%")
                i = j + 1
                continue
        for tok, rep in table:
            if fmt.startswith(tok, i):
                out += rep
                i += len(tok)
                break
        else:
            out += fmt[i].replace("%", "%%")
            i += 1
    return out


def _daily_path(vault: Path) -> Path:
    folder, fmt = _setting("daily_folder"), _setting("daily_format")
    try:
        cfg = json.loads((vault / ".obsidian" / "daily-notes.json").read_text(encoding="utf-8"))
        folder = folder or str(cfg.get("folder") or "")
        fmt = fmt or str(cfg.get("format") or "")
    except Exception:
        pass
    name = date.today().strftime(_moment_format(fmt or "YYYY-MM-DD"))
    p = (vault / folder.strip("/") / f"{name}.md") if folder else vault / f"{name}.md"
    return p if _inside(vault, p) else vault / f"{name}.md"


def _daily(vault: Path, text: str, player) -> str:
    p = _daily_path(vault)
    if not text.strip():
        if p.is_file():
            return _read(vault, _rel(vault, p), player)
        return f"Today's daily note ({p.stem}) doesn't exist yet. Tell me what to put in it."
    line = text.strip()
    if "\n" not in line and not line.startswith(("-", "*", "#", ">")):
        line = f"- {datetime.now():%H:%M} {line}"
    try:
        before = _append_to(p, line)
    except Exception as e:
        return f"I couldn't write to the daily note: {e}"
    _push_undo("added to today's daily note", p, before)
    return f"Added to today's note, {p.stem}."


def _tasks(vault: Path, folder: str, player) -> str:
    base, err = _folder(vault, folder)
    if err:
        return err
    found = []
    for p in _iter_notes(vault, base):
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"\s*[-*+]\s+\[ \]\s+(.*)", line)
                if m and m.group(1).strip():
                    found.append((p, m.group(1).strip()))
        except OSError:
            continue
        if len(found) > 500:
            break
    if not found:
        return "There's nothing unchecked in the vault — all clear."
    if player is not None:
        try:
            player.show_content("Open tasks",
                                "\n".join(f"☐ {t}   ({_rel(vault, p)})" for p, t in found[:80]))
        except Exception:
            pass
    head = "; ".join(f"{t} ({p.stem})" for p, t in found[:10])
    more = f" …and {len(found) - 10} more." if len(found) > 10 else ""
    return f"{len(found)} open task{'s' if len(found) != 1 else ''}: {head}.{more}"


def _backlinks(vault: Path, note: str) -> str:
    if not note:
        return "Which note's backlinks do you want?"
    p, err = _note_path(vault, note)
    if err:
        return err
    target = (p.stem if p else note).casefold()
    rel = _rel(vault, p).casefold() if p else target
    pat = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
    links = []
    for q in _iter_notes(vault):
        if p is not None and q == p:
            continue
        try:
            text = q.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pat.finditer(text):
            t = m.group(1).strip().casefold()
            if t.endswith(".md"):
                t = t[:-3]
            if t == target or t == rel or t.split("/")[-1] == target:
                links.append(_rel(vault, q))
                break
    if not links:
        return f"No notes link to {p.stem if p else note}."
    return f"{len(links)} note{'s link' if len(links) != 1 else ' links'} to it: " + ", ".join(links[:15]) + "."


def _open(vault: Path, note: str) -> str:
    target = ""
    if note:
        p, err = _note_path(vault, note)
        if err:
            return err
        if p is None or not p.is_file():
            return f"There's no note called '{note}'."
        target = _rel(vault, p)
    uri = "obsidian://open?vault=" + urllib.parse.quote(vault.name)
    if target:
        uri += "&file=" + urllib.parse.quote(target)
    try:
        if not webbrowser.open(uri):
            return "I couldn't hand that to Obsidian — is it installed?"
    except Exception as e:
        return f"I couldn't open Obsidian: {e}"
    return f"Opening {target or vault.name} in Obsidian."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        p = parameters or {}
        vault, err = _vault()
        if vault is None:
            return err
        note = str(p.get("note") or "").strip()
        text = str(p.get("text") or "")
        query = str(p.get("query") or "").strip()
        folder = str(p.get("folder") or "").strip()
        action = str(p.get("action") or "").strip().lower() or ("search" if query else "list")
        if action == "search":
            return _search(vault, query or note, folder, player)
        if action == "read":
            return _read(vault, note or query, player)
        if action == "list":
            return _list(vault, folder)
        if action == "create":
            return _create(vault, note, text)
        if action == "append":
            return _append(vault, note, text)
        if action in ("daily", "today", "journal"):
            return _daily(vault, text, player)
        if action in ("tasks", "todo", "todos"):
            return _tasks(vault, folder, player)
        if action == "backlinks":
            return _backlinks(vault, note or query)
        if action == "open":
            return _open(vault, note)
        return (f"I don't know the vault action '{action}'. I can: search, read, list, create, "
                f"append, daily, tasks, backlinks, open.")
    except Exception as e:
        return f"The vault operation failed: {e}"
