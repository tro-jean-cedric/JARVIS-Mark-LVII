"""
plugins/disk_map.py — what is filling a drive or folder, shown in the content
panel, with drill-down (focus / up). Read-only: it never deletes or moves.

The scan (plugins/_disk_map_core.py) runs on a worker thread with a time limit
and a folder budget. Small folders usually finish inside run()'s short wait and
the answer comes back directly; a whole drive keeps scanning, the panel shows
progress, and the result is announced when it is done.
"""
from __future__ import annotations

import os
import threading
import time

from plugins import _announce, _disk_map_core as core

PLUGIN = {
    "name": "disk_map",
    "description": (
        "Shows what takes up space on a drive or in a folder — biggest folders and "
        "files, by type, and where space could often be freed — in the panel on "
        "screen, and lets the user drill in. action='scan' with 'path' as a drive "
        "letter ('C', 'D'), a known folder ('downloads', 'desktop', 'documents', "
        "'pictures', 'videos', 'music', 'home', 'appdata', 'temp', 'program files', "
        "'caches') or a full path; empty = the system drive. action='focus' with "
        "'folder' zooms into a folder (or picks out a file) of the current scan; "
        "'up' goes back one level; 'status' re-reads the result; 'close' forgets "
        "it. Use for any question about disk space, storage, the biggest files or "
        "how big a folder is. It never deletes anything: suggest what could be "
        "cleaned and let the user decide."
    ),
    "brief": "Disk space map: biggest folders/files of a drive or folder, drill down. Read-only.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'scan' (default), 'focus', 'up', 'status' or 'close'."},
            "path": {"type": "STRING",
                     "description": "For 'scan': drive letter, known folder name or full path; "
                                    "empty = system drive."},
            "folder": {"type": "STRING",
                       "description": "For 'focus': the folder or file name to show, e.g. "
                                      "'Library', 'node_modules', or a relative path."},
            "refresh": {"type": "BOOLEAN",
                        "description": "For 'scan': true to scan again even if a recent scan exists."},
        },
        "required": [],
    },
}

TIME_LIMIT = 90.0            # seconds per scan
WAIT_IN_RUN = 3.0            # how long run() waits for a quick scan to finish
REUSE_FOR = 15 * 60          # a scan this recent is reused unless refresh=true

_lock = threading.RLock()
_scan: core.Scanner | None = None
_focus: core.Node | None = None
_finished_at = 0.0
_announce_when_done = False


def _show(text: str, title: str = "Disk map") -> None:
    p = _announce.player()
    if p is None:
        return
    try:
        p.show_content(title, text)
    except Exception:
        pass


def _title(sc: core.Scanner, node: core.Node | None = None) -> str:
    n = node or sc.root
    return f"Disk map · {n.name}"[:48]


def _progress(sc: core.Scanner) -> str:
    return (f"Scanning {sc.root_path}…\n{sc.file_count:,} files, {core.human(sc.bytes)} so far "
            f"({int(time.time() - sc.started)} s)\nNow in: {core.rel(sc, sc.current)}")


def _worker(sc: core.Scanner) -> None:
    global _finished_at, _focus
    ticker_stop = threading.Event()

    def _ticker():
        while not ticker_stop.wait(2.0):
            if sc.done:
                return
            _show(_progress(sc), _title(sc))

    threading.Thread(target=_ticker, daemon=True, name="disk-map-progress").start()
    try:
        sc.run()
    except Exception as e:
        sc.done, sc.partial, sc.reason = True, True, f"error: {e}"
    finally:
        ticker_stop.set()
    with _lock:
        if _scan is not sc:
            return            # superseded or closed meanwhile
        _finished_at = time.time()
        _focus = sc.root
        tell = _announce_when_done
    _show(core.report(sc), _title(sc))
    _announce.log(f"Disk map: scanned {sc.root_path} in {sc.elapsed:.1f} s")
    if tell:
        _announce.say(
            "The disk scan has finished. " + core.summary(sc)
            + " Tell the user briefly what takes the most space and where space could be "
              "freed. Never offer to delete files yourself. Do not call any tool for this.",
            prefix="[DISK MAP]")


def _start(path: str, refresh: bool) -> str:
    global _scan, _focus, _announce_when_done, _finished_at
    with _lock:
        old = _scan
        if old is not None and os.path.normcase(old.root_path) == os.path.normcase(path):
            if not old.done:
                return (f"status=running A scan of {path} is already underway — "
                        f"{old.file_count:,} files so far.")
            if not refresh and time.time() - _finished_at < REUSE_FOR:
                _focus = old.root
                _show(core.report(old), _title(old))
                return ("status=done (recent scan reused) " + core.summary(old)
                        + " Answer briefly in the user's language: what takes the most space "
                          "and, if useful, where space could be freed.")
        if old is not None and not old.done:
            old.stop.set()
        sc = core.Scanner(path, time_limit=TIME_LIMIT)
        _scan, _focus, _announce_when_done, _finished_at = sc, None, False, 0.0
    threading.Thread(target=_worker, args=(sc,), daemon=True, name="disk-map-scan").start()

    deadline = time.time() + WAIT_IN_RUN
    while time.time() < deadline and not sc.done:
        time.sleep(0.05)
    with _lock:
        if sc.done and _focus is not None:
            return ("status=done " + core.summary(sc)
                    + " Answer briefly in the user's language: what takes the most space and, "
                      "if useful, where space could be freed. Never offer to delete files yourself.")
        _announce_when_done = True
    _show(_progress(sc), _title(sc))
    return (f"status=running Scanning {path} — {sc.file_count:,} files read so far. Progress "
            f"is on screen and the result will be reported when it finishes (at most about "
            f"{int(TIME_LIMIT)} seconds). Tell the user briefly that the scan is underway.")


def _focus_on(name: str) -> str:
    global _focus
    rescan = None
    with _lock:
        sc = _scan
        if sc is None:
            return "status=failed reason=no disk map is open; scan first (action='scan')"
        if not sc.done:
            return "status=running the scan is still going; try again when it has finished"
        if not name:
            return "status=ask question=which folder the user wants to see"
        start = _focus or sc.root
        node = sc.find(name, start) or (sc.find(name) if start is not sc.root else None)
        if node is None:
            f = sc.find_file(name, start) or sc.find_file(name)
            if f is not None:
                size, path = f
                return (f"status=done Selected the file {core.rel(sc, path)}: {core.human(size)}. "
                        f"Tell the user exactly this.")
            return (f"status=failed reason=no folder or file called '{name}' in this scan. "
                    f"Ask the user which one they mean.")
        if node.partial and not node.error and not node.children and node.size == 0:
            # the budget ran out before this folder was read: scan it on its own
            rescan = node.path
    if rescan:
        msg = _start(rescan, refresh=True)
        return msg.replace("status=running", f"status=running {name} was not read in the first "
                                             f"pass; its contents are being scanned now.", 1)
    with _lock:
        _focus = node
    _show(core.report(sc, node), _title(sc, node))
    return ("status=done " + core.summary(sc, node)
            + " Answer briefly in the user's language.")


def _up() -> str:
    global _focus
    pass_path = None
    with _lock:
        sc = _scan
        if sc is None or not sc.done:
            return "status=failed reason=no disk map is open; scan first (action='scan')"
        node = _focus or sc.root
        if node.parent is None:
            parent = os.path.dirname(sc.root_path.rstrip("\\/"))
            if (sc.is_mount or not parent
                    or os.path.normcase(parent) == os.path.normcase(sc.root_path)):
                return "status=done already at the top of the drive"
            pass_path = parent
        else:
            _focus = node.parent
            _show(core.report(sc, _focus), _title(sc, _focus))
            return ("status=done went one folder up. " + core.summary(sc, _focus)
                    + " Answer briefly in the user's language.")
    return _start(pass_path, refresh=False)


def _status() -> str:
    with _lock:
        sc = _scan
        if sc is None:
            return "No disk map is open. Ask me what's taking up space to start a scan."
        if not sc.done:
            return "status=running " + _progress(sc).replace("\n", " · ")
        node = _focus or sc.root
    _show(core.report(sc, node), _title(sc, node))
    return "status=done " + core.summary(sc, node)


def _close() -> str:
    global _scan, _focus
    with _lock:
        sc, _scan, _focus = _scan, None, None
    if sc is not None and not sc.done:
        sc.stop.set()
    return "status=done Disk map closed."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        _announce.remember(player)
        p = parameters or {}
        action = str(p.get("action") or "scan").strip().lower()
        if action in ("scan", "map", "show", "analyze", "analyse"):
            path, err = core.resolve_target(str(p.get("path") or ""))
            if err:
                return f"status=failed reason={err}. Ask the user which drive or folder they mean."
            refresh = str(p.get("refresh") or "").lower() in ("true", "1", "yes")
            return _start(path, refresh)
        if action in ("focus", "zoom", "open", "drill", "into"):
            return _focus_on(str(p.get("folder") or p.get("path") or "").strip())
        if action in ("up", "back", "parent"):
            return _up()
        if action in ("status", "result", "progress"):
            return _status()
        if action in ("close", "stop", "hide", "cancel"):
            return _close()
        return "status=failed reason=unknown action; use scan, focus, up, status or close"
    except Exception as e:
        return f"status=failed reason=disk_map: {e}"
