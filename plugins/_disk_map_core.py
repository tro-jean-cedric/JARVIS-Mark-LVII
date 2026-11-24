"""
plugins/_disk_map_core.py — a bounded, read-only folder-size scanner for the
disk_map plugin.

Helper module (leading underscore: the plugin loader skips it). Nothing here
touches Qt and nothing here deletes, moves or writes a file.

`Scanner(root).run()` walks the tree with os.scandir on the calling thread
(disk_map runs it on its own worker thread). It is bounded by a time limit and
a folder budget; when either runs out the scan stops cleanly and the result is
marked partial (sizes are then lower bounds). Symlinks, junctions and other
reparse points are never followed; on Linux other mounted file systems are not
entered; pseudo file systems (/proc, /sys, /dev) are skipped.
"""
from __future__ import annotations

import heapq
import os
import shutil
import sys
import threading
import time
from pathlib import Path

WIN = sys.platform == "win32"
_REPARSE = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT
_OFFLINE = 0x1000         # FILE_ATTRIBUTE_OFFLINE
_RECALL = 0x400000        # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (OneDrive online-only)
_RECALL_OPEN = 0x40000    # FILE_ATTRIBUTE_RECALL_ON_OPEN

_POSIX_SKIP = {"/proc", "/sys", "/dev", "/run", "/System/Volumes", "/Volumes",
               "/private/var/vm/sleepimage", "/snap"}

CATEGORIES = ("videos", "images", "audio", "archives", "programs", "documents", "code", "other")
_EXT: dict[str, int] = {}
for _i, _names in enumerate((
    "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts m2ts mts 3gp vob",
    "jpg jpeg png gif bmp tif tiff webp heic heif raw cr2 cr3 nef arw dng psd svg ico avif exr",
    "mp3 wav flac aac ogg m4a wma opus aiff aif mid midi",
    "zip rar 7z tar gz tgz bz2 xz zst iso img cab dmg wim esd vhd vhdx vmdk qcow2",
    "exe dll sys msi msp msix appx so dylib lib jar apk app pak bin dat",
    "pdf doc docx xls xlsx ppt pptx txt rtf odt ods odp csv md epub pages numbers key",
    "py pyc js ts tsx jsx json xml html htm css c cpp h hpp cs java class go rs rb php "
    "lua sh ps1 bat ipynb whl pdb o a map",
)):
    for _e in _names.split():
        _EXT[_e] = _i
_OTHER = len(CATEGORIES) - 1

# Folder names whose contents are often safe for the USER to clean up.
RECLAIMABLE = {
    "downloads", "cache", "caches", ".cache", "temp", "tmp", ".trash", "$recycle.bin",
    "node_modules", "__pycache__", ".gradle", ".npm", ".yarn", "deriveddata",
    "windows.old", "softwaredistribution", "crashdumps", "crashreporter", "logs",
    ".nuget", "pip", "huggingface", "ios devicesupport", "coresimulator",
}


class Node:
    __slots__ = ("name", "path", "parent", "own", "size", "files", "children",
                 "top_files", "partial", "error")

    def __init__(self, name: str, path: str, parent: "Node | None"):
        self.name, self.path, self.parent = name, path, parent
        self.own = 0            # bytes of files directly inside
        self.size = 0           # own + all descendants (filled by finish())
        self.files = 0          # file count, recursive after finish()
        self.children: list[Node] = []
        self.top_files: list[tuple[int, str]] = []   # biggest files directly inside
        self.partial = False    # not (fully) read: budget ran out or unreadable
        self.error = False

    def child(self, name: str) -> "Node | None":
        low = name.casefold()
        return next((c for c in self.children if c.name.casefold() == low), None)


class Scanner:
    def __init__(self, root: str, time_limit: float = 60.0, max_dirs: int = 250_000,
                 min_file: int = 1 << 20):
        self.root_path = os.path.abspath(root)
        self.time_limit = time_limit
        self.max_dirs = max_dirs
        self.min_file = min_file
        self.stop = threading.Event()
        self.root = Node(os.path.basename(self.root_path.rstrip("\\/")) or self.root_path,
                         self.root_path, None)
        self.nodes: list[Node] = [self.root]
        self.file_count = 0
        self.bytes = 0
        self.errors = 0
        self.cloud = 0
        self.cats = [0] * len(CATEGORIES)
        self.big: list[tuple[int, str]] = []          # min-heap of the largest files
        self.current = ""
        self.started = 0.0
        self.elapsed = 0.0
        self.done = False
        self.partial = False
        self.reason = ""
        self.usage = None                              # (total, used, free) of the volume
        self.is_mount = False
        self._seen_links: set[tuple[int, int]] = set()

    # -- the walk --------------------------------------------------------------
    def run(self) -> "Scanner":
        self.started = time.time()
        try:
            self.is_mount = os.path.ismount(self.root_path)
            du = shutil.disk_usage(self.root_path)
            self.usage = (du.total, du.used, du.free)
        except OSError:
            pass
        dev = None
        if sys.platform.startswith("linux"):
            try:
                dev = os.stat(self.root_path).st_dev
            except OSError:
                pass
        deadline = self.started + self.time_limit
        # One depth-first stack per top-level folder, served in turn, so a scan
        # cut short by the time limit has looked into every top-level folder
        # instead of spending all its time in the first one.
        top: list[Node] = []
        self._read(self.root, top, dev)
        stacks = [[c] for c in top]
        try:
            while stacks:
                if self._should_stop(deadline):
                    break
                for stack in list(stacks):
                    for _ in range(64):
                        if not stack or self._should_stop(deadline):
                            break
                        node = stack.pop()
                        self.current = node.path
                        self._read(node, stack, dev)
                    if not stack:
                        stacks.remove(stack)
                    if self.partial:
                        break
        finally:
            for stack in stacks:
                for n in stack:
                    n.partial = True
            self._finish()
            self.elapsed = time.time() - self.started
            self.done = True
        return self

    def _should_stop(self, deadline: float) -> bool:
        if self.partial:
            return True
        if self.stop.is_set():
            self.partial, self.reason = True, "stopped"
        elif time.time() > deadline:
            self.partial, self.reason = True, f"time limit of {int(self.time_limit)} s reached"
        elif len(self.nodes) > self.max_dirs:
            self.partial, self.reason = True, f"folder budget of {self.max_dirs:,} reached"
        return self.partial

    def _read(self, node: Node, stack: list, dev) -> None:
        try:
            it = os.scandir(node.path)
        except OSError:
            node.partial = node.error = True
            self.errors += 1
            return
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    self.errors += 1
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if WIN:
                            st = e.stat(follow_symlinks=False)
                            if getattr(st, "st_file_attributes", 0) & _REPARSE:
                                continue          # junction / mount point / symlink
                        else:
                            if e.path in _POSIX_SKIP:
                                continue
                            if dev is not None and e.stat(follow_symlinks=False).st_dev != dev:
                                continue
                        child = Node(e.name, e.path, node)
                        node.children.append(child)
                        self.nodes.append(child)
                        stack.append(child)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        self.file_count += 1
                        node.files += 1
                        attrs = getattr(st, "st_file_attributes", 0)
                        if WIN and attrs & (_RECALL | _RECALL_OPEN | _OFFLINE):
                            self.cloud += st.st_size
                            continue
                        if not WIN and st.st_nlink > 1:
                            ident = (st.st_dev, st.st_ino)
                            if ident in self._seen_links:
                                continue          # a hard link to bytes already counted
                            self._seen_links.add(ident)
                        # real allocation where the platform reports it (sparse files)
                        size = st.st_size
                        blocks = getattr(st, "st_blocks", None)
                        if blocks is not None and not WIN:
                            # compressed / sparse / cloud-only (dataless) files take
                            # less than their length on disk
                            size = min(size, blocks * 512)
                        node.own += size
                        self.bytes += size
                        name = e.name
                        dot = name.rfind(".")
                        cat = _EXT.get(name[dot + 1:].lower(), _OTHER) if dot > 0 else _OTHER
                        self.cats[cat] += size
                        if size >= self.min_file:
                            node.top_files.append((size, name))
                            item = (size, e.path)
                            if len(self.big) < 30:
                                heapq.heappush(self.big, item)
                            elif item > self.big[0]:
                                heapq.heapreplace(self.big, item)
                except OSError:
                    self.errors += 1
        if len(node.top_files) > 15:
            node.top_files = heapq.nlargest(15, node.top_files)

    def _finish(self) -> None:
        for n in self.nodes:
            n.size = n.own
        for n in reversed(self.nodes):           # children always come after their parent
            if n.parent is not None:
                n.parent.size += n.size
                n.parent.files += n.files
                if n.partial:
                    n.parent.partial = n.parent.partial or not n.error
        for n in self.nodes:
            n.children.sort(key=lambda c: c.size, reverse=True)
            n.top_files.sort(reverse=True)

    # -- reading results -------------------------------------------------------
    def find(self, name: str, start: Node | None = None) -> Node | None:
        """A folder by name or relative path: first among `start`'s children,
        then anywhere below it (largest match wins)."""
        start = start or self.root
        name = name.strip().strip("\\/")
        if not name:
            return None
        if "/" in name or "\\" in name:
            node = start
            for part in name.replace("\\", "/").split("/"):
                node = node.child(part) if node else None
            if node is not None:
                return node
            name = name.replace("\\", "/").split("/")[-1]
        direct = start.child(name)
        if direct is not None:
            return direct
        low = name.casefold()
        best = None
        todo = [start]
        while todo:
            n = todo.pop()
            for c in n.children:
                if c.name.casefold() == low and (best is None or c.size > best.size):
                    best = c
                todo.append(c)
        if best is None:
            todo = [start]
            while todo:
                n = todo.pop()
                for c in n.children:
                    if low in c.name.casefold() and (best is None or c.size > best.size):
                        best = c
                    todo.append(c)
        return best

    def find_file(self, name: str, start: Node | None = None) -> tuple[int, str] | None:
        low = name.strip().casefold()
        if not low:
            return None
        for size, path in sorted(self.big, reverse=True):
            if os.path.basename(path).casefold() == low or low in os.path.basename(path).casefold():
                if start is None or path.startswith(start.path):
                    return size, path
        todo = [start or self.root]
        while todo:
            n = todo.pop()
            for size, fname in n.top_files:
                if fname.casefold() == low:
                    return size, os.path.join(n.path, fname)
            todo.extend(n.children)
        return None

    def reclaimable(self, limit: int = 6) -> list[Node]:
        out = []
        todo = [self.root]
        while todo:
            n = todo.pop()
            for c in n.children:
                if c.name.casefold() in RECLAIMABLE and c.size > 50 << 20:
                    out.append(c)
                else:
                    todo.append(c)
        out.sort(key=lambda c: c.size, reverse=True)
        return out[:limit]


# ── formatting ───────────────────────────────────────────────────────────────

def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _bar(frac: float, width: int = 10) -> str:
    full = max(0, min(width, int(round(frac * width))))
    return "█" * full + "░" * (width - full)


def rel(sc: Scanner, path: str) -> str:
    try:
        r = os.path.relpath(path, sc.root_path)
        return path if r.startswith("..") else r
    except ValueError:
        return path


def report(sc: Scanner, focus: Node | None = None) -> str:
    """The text for the content panel."""
    node = focus or sc.root
    lines = [f"{node.path}", f"{human(node.size)} in {node.files:,} files"
             + (" (partial)" if node.partial else "")]
    if node is sc.root:
        if sc.usage:
            total, used, free = sc.usage
            lines.append(f"Volume: {human(used)} used of {human(total)}, {human(free)} free")
            gap = used - sc.root.size
            if sc.is_mount and not sc.partial and gap > used * 0.02:
                lines.append(f"{human(gap)} is system/hidden space the scan could not read.")
        if sc.cloud:
            lines.append(f"{human(sc.cloud)} of OneDrive files are online-only and use no space.")
        if sc.errors:
            lines.append(f"{sc.errors:,} items could not be read (normal for system folders).")
        if sc.partial:
            lines.append(f"Scan incomplete ({sc.reason}) — sizes are lower bounds.")
    lines.append("")
    if node.children:
        lines.append("LARGEST FOLDERS")
        for c in node.children[:12]:
            frac = c.size / node.size if node.size else 0
            lines.append(f"{human(c.size):>9}  {frac * 100:4.0f}%  {_bar(frac)}  {c.name}"
                         + (" …" if c.partial and not c.error else ""))
        rest = node.children[12:]
        if rest:
            lines.append(f"{human(sum(c.size for c in rest)):>9}  in {len(rest)} smaller folders")
        lines.append("")
    if node.own:
        lines.append(f"{human(node.own):>9}  loose files directly in this folder")
    files = (sorted(sc.big, reverse=True) if node is sc.root
             else [(s, os.path.join(node.path, n)) for s, n in node.top_files])
    if node is not sc.root:
        # include big files deeper down too
        deep = [(s, p) for s, p in sc.big if p.startswith(node.path + os.sep)]
        files = sorted(set(files) | set(deep), reverse=True)
    if files:
        lines.append("LARGEST FILES")
        for s, p in files[:10]:
            lines.append(f"{human(s):>9}  {rel(sc, p)}")
        lines.append("")
    if node is sc.root and sc.bytes:
        cats = sorted(((b, CATEGORIES[i]) for i, b in enumerate(sc.cats) if b), reverse=True)
        lines.append("BY TYPE  " + " · ".join(f"{n} {human(b)}" for b, n in cats[:6]))
        rec = sc.reclaimable()
        if rec:
            lines.append("")
            lines.append("Space that is often safe to reclaim (you decide):")
            for c in rec:
                lines.append(f"{human(c.size):>9}  {rel(sc, c.path)}")
    return "\n".join(lines)


def summary(sc: Scanner, focus: Node | None = None) -> str:
    """A compact, factual sentence block for the model to speak from."""
    node = focus or sc.root
    parts = [f"{node.path}: {human(node.size)} in {node.files:,} files"
             + (" (scan incomplete, lower bound)" if node.partial or sc.partial else "") + "."]
    if node is sc.root and sc.usage:
        total, used, free = sc.usage
        parts.append(f"Volume {human(used)} used of {human(total)}, {human(free)} free.")
    if node.children:
        parts.append("Largest folders: " + ", ".join(
            f"{c.name} {human(c.size)}" for c in node.children[:5]) + ".")
    big = [(s, p) for s, p in sorted(sc.big, reverse=True)
           if node is sc.root or p.startswith(node.path + os.sep)]
    if big:
        parts.append("Largest files: " + ", ".join(
            f"{os.path.basename(p)} {human(s)}" for s, p in big[:3]) + ".")
    if node is sc.root:
        rec = sc.reclaimable(4)
        if rec:
            parts.append("Often reclaimable: " + ", ".join(
                f"{rel(sc, c.path)} {human(c.size)}" for c in rec) + ".")
    return " ".join(parts)


# ── resolving what the user named ────────────────────────────────────────────

def resolve_target(raw: str) -> tuple[str | None, str]:
    """(path, error). Drive letters, known folders, full paths; '' = system drive."""
    from plugins import _known_folders
    s = str(raw or "").strip().strip('"').strip("'")
    low = s.casefold().rstrip(":\\/ ").removesuffix(" drive").removeprefix("drive ").strip()
    system_words = {"", "system", "system drive", "computer", "my computer", "disk", "pc",
                    "main drive", "hard drive", "ssd", "root"}
    if not WIN:
        system_words.add("c")     # "the C drive" on a Mac / Linux box means the main disk
    if not s or low in system_words:
        if WIN:
            return (os.environ.get("SystemDrive", "C:") + "\\"), ""
        if sys.platform == "darwin" and os.path.isdir("/System/Volumes/Data"):
            return "/System/Volumes/Data", ""
        return "/", ""
    if WIN and len(low) == 1 and low.isalpha():
        p = f"{low.upper()}:\\"
        return (p, "") if os.path.isdir(p) else (None, f"no drive {low.upper()}: was found")
    known = {
        "downloads": "downloads", "download": "downloads", "desktop": "desktop",
        "documents": "documents", "docs": "documents", "my documents": "documents",
        "pictures": "pictures", "photos": "pictures", "videos": "videos", "movies": "videos",
        "music": "music", "home": "home", "user": "home", "profile": "home",
    }
    if low in known:
        return str(_known_folders.folder(known[low])), ""
    if low in ("appdata", "app data", "application support", "library"):
        if WIN:
            return str(Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")).parent), ""
        if sys.platform == "darwin":
            return str(Path.home() / "Library"), ""
        return str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")), ""
    if low in ("temp", "tmp", "temporary files"):
        import tempfile
        return tempfile.gettempdir(), ""
    if low in ("program files", "programs", "applications", "apps"):
        if WIN:
            return os.environ.get("ProgramFiles", "C:\\Program Files"), ""
        if sys.platform == "darwin":
            return "/Applications", ""
        return "/usr", ""
    if low in ("cache", "caches"):
        if sys.platform == "darwin":
            return str(Path.home() / "Library" / "Caches"), ""
        if WIN:
            return str(Path(os.environ.get("LOCALAPPDATA", Path.home()))), ""
        return str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")), ""
    p = Path(os.path.expandvars(s)).expanduser()
    if p.is_dir():
        return str(p.resolve()), ""
    # a folder name directly under home ("Projects")
    cand = Path.home() / s
    if cand.is_dir():
        return str(cand), ""
    return None, f"no drive or folder called '{s}' was found"
