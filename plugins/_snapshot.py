"""
plugins/_snapshot.py — getting "the thing the user is showing" into a plugin.

Helper module (leading underscore: the plugin loader skips it).

The camera plugins (calorie_counter, fridge_chef, paper_reminders, build_guide)
and the document ones (document_review, video_summarizer) all need the same
few things, so they live here once:

  * a JPEG from the webcam, a photo file, a dropped file or the screen,
    returned as (bytes, error) — never raised;
  * the text of a PDF / Word / text file;
  * the file the user dropped onto the window (`player.current_file`);
  * running slow work on a worker thread and announcing the result.

Nothing here touches Qt; `player` methods used are the thread-safe ones.
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Callable, Optional

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
DOC_EXT = {".docx"}
TEXT_EXT = {".txt", ".md", ".rtf", ".html", ".htm", ".csv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".amr"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg", ".ts"}


# ── Files ─────────────────────────────────────────────────────────────────────

def dropped_file(player) -> Optional[str]:
    """The file the user dropped on the window, if any (player may be None)."""
    if player is None:
        return None
    try:
        f = player.current_file
    except Exception:
        return None
    return str(f) if f else None


def resolve_file(path: str, player=None, kinds: Optional[set] = None) -> Optional[Path]:
    """`path` if given and it exists, else the dropped file. With `kinds` (a set
    of extensions) the dropped file is only used when its type matches."""
    raw = str(path or "").strip().strip('"').strip("'")
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_file() else None
    dropped = dropped_file(player)
    if dropped:
        p = Path(dropped)
        if p.is_file() and (kinds is None or p.suffix.lower() in kinds):
            return p
    return None


def image_file_jpeg(path, max_side: int = 1600) -> tuple[Optional[bytes], str]:
    """Any picture file as JPEG bytes (resized so the long side is <= max_side)."""
    p = Path(path)
    if not p.is_file():
        return None, "that picture file does not exist"
    try:
        from PIL import Image, ImageOps
    except Exception:
        return None, "Pillow is not installed, so picture files cannot be read"
    try:
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            return buf.getvalue(), ""
    except Exception as e:
        return None, f"that file is not a picture this can read ({type(e).__name__})"


def pdf_first_page_jpeg(path, max_side: int = 1600) -> tuple[Optional[bytes], str]:
    """The first page of a PDF as a picture, for scanned PDFs without text."""
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None, "pdfplumber is not installed"
    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return None, "the PDF has no pages"
            img = pdf.pages[0].to_image(resolution=150).original.convert("RGB")
            img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            return buf.getvalue(), ""
    except Exception as e:
        return None, f"the PDF page could not be drawn ({type(e).__name__})"


def pdf_text(path, max_chars: int = 120_000) -> str:
    out: list[str] = []
    total = 0
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                out.append(t)
                total += len(t)
                if total >= max_chars:
                    break
        return "\n".join(out)[:max_chars]
    except Exception as e:
        print(f"[snapshot] pdfplumber could not read {Path(path).name}: {e}")
    try:
        import PyPDF2  # type: ignore
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                t = page.extract_text() or ""
                out.append(t)
                total += len(t)
                if total >= max_chars:
                    break
        return "\n".join(out)[:max_chars]
    except Exception as e:
        print(f"[snapshot] PyPDF2 could not read {Path(path).name}: {e}")
    return ""


def document_text(path, max_chars: int = 120_000) -> tuple[str, str]:
    """(text, error) for a PDF, Word or text file."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in PDF_EXT:
            t = pdf_text(p, max_chars)
            return (t, "") if t.strip() else ("", "the PDF has no text layer (it may be a scan)")
        if ext in DOC_EXT:
            try:
                from docx import Document  # type: ignore
            except Exception:
                return "", "python-docx is not installed, so Word files cannot be read"
            doc = Document(str(p))
            parts = [para.text for para in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts)[:max_chars], ""
        if ext in TEXT_EXT or ext == "":
            raw = p.read_text(encoding="utf-8", errors="ignore")
            if ext in (".html", ".htm"):
                try:
                    from bs4 import BeautifulSoup  # type: ignore
                    raw = BeautifulSoup(raw, "html.parser").get_text("\n")
                except Exception:
                    pass
            return raw[:max_chars], ""
    except Exception as e:
        return "", f"the file could not be read ({type(e).__name__}: {e})"
    return "", f"{ext or 'that'} files cannot be read as a document"


# ── Pictures ──────────────────────────────────────────────────────────────────

def camera_jpeg(hold_seconds: float = 0.0, max_side: int = 1280) -> tuple[Optional[bytes], str]:
    """One webcam picture. `hold_seconds` gives the user time to hold things up."""
    try:
        from plugins import _camera
    except Exception as e:
        return None, f"the camera helper is missing ({e})"
    if not _camera.available():
        return None, "OpenCV is not installed, so the camera cannot be read (pip install opencv-python)"
    if hold_seconds > 0:
        time.sleep(min(10.0, hold_seconds))
    try:
        return _camera.snapshot_jpeg(max_side=max_side), ""
    except Exception as e:
        msg = str(e) or "no picture came from the camera"
        if "open" in msg.lower():
            msg = "the camera could not be opened (none connected, or another app is using it)"
        return None, msg


def screen_jpeg(max_side: int = 1600) -> tuple[Optional[bytes], str]:
    try:
        import mss  # type: ignore
        from PIL import Image
    except Exception:
        return None, "screen capture needs mss and Pillow"
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            im = Image.frombytes("RGB", shot.size, shot.rgb)
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), ""
    except Exception as e:
        return None, f"the screen could not be captured ({e})"


def picture(source: str, file: str = "", player=None, hold_seconds: float = 0.0,
            max_side: int = 1280) -> tuple[Optional[bytes], str]:
    """A JPEG from source 'camera' | 'file' | 'screen'.

    A file path given with source 'camera' still wins — the user named a file.
    A dropped picture is used for source 'file' when no path is given."""
    src = (source or "camera").strip().lower()
    if file or src == "file":
        p = resolve_file(file, player, IMAGE_EXT | PDF_EXT)
        if p is None:
            return None, ("that picture file does not exist" if file
                          else "no picture file was given or dropped")
        if p.suffix.lower() in PDF_EXT:
            return pdf_first_page_jpeg(p, max_side)
        return image_file_jpeg(p, max_side)
    if src == "screen":
        return screen_jpeg(max_side)
    return camera_jpeg(hold_seconds, max_side)


def frame_problem(jpeg: bytes) -> str:
    """A sentence when the picture is unusable (dark, blank, glare), else ''."""
    try:
        from PIL import Image, ImageStat
        with Image.open(io.BytesIO(jpeg)) as im:
            g = im.convert("L")
            g.thumbnail((320, 320))
            stat = ImageStat.Stat(g)
            mean, std = stat.mean[0], stat.stddev[0]
            hist = g.histogram()
            n = max(1, sum(hist))
            bright = sum(hist[245:]) / n
    except Exception:
        return ""
    if mean < 28:
        return "the camera picture is too dark to read - ask the user to turn on a light, then try again"
    if std < 6:
        return "the camera sees nothing (covered lens or empty view) - ask the user to uncover it and hold the thing in front of it"
    if bright > 0.55:
        return "the camera picture is washed out by glare - ask the user to move away from the bright light"
    return ""


def show_picture(player, jpeg: Optional[bytes]) -> None:
    """Flash the picture that was taken in the small camera overlay."""
    if player is None or not jpeg:
        return
    try:
        player.show_camera_frame(jpeg)
    except Exception:
        pass


def image_part(jpeg: bytes, mime: str = "image/jpeg"):
    from plugins import _camera
    return _camera.image_part(jpeg, mime)


# ── Background work ──────────────────────────────────────────────────────────

def background(name: str, work: Callable[[], str], player=None) -> None:
    """Run `work` on a worker thread; whatever sentence it returns is announced.

    `work` returns an instruction for the assistant (or '' for silence). An
    exception is caught and announced as a failure, so a background job can
    never vanish without the user hearing about it."""
    from plugins import _announce
    _announce.remember(player)

    def runner():
        try:
            msg = work()
        except Exception as e:  # never die silently
            import traceback
            traceback.print_exc()
            msg = f"{name} failed: {type(e).__name__}: {e}. Tell the user in one sentence."
        if msg:
            _announce.say(msg, prefix=f"[{name.upper()} RESULT]")

    threading.Thread(target=runner, name=f"{name}-work", daemon=True).start()


def run_or_background(name: str, work: Callable[[], str], player=None,
                      started: str = "") -> str:
    """With a window (player), run `work` in the background and answer at once;
    without one (tests, the phone dashboard) run it here and return its text."""
    if player is None:
        try:
            return work() or "Done."
        except Exception as e:
            return f"{name} failed: {type(e).__name__}: {e}"
    background(name, work, player)
    from core import outcome
    return outcome.started(name, note=started or "working on it; the result will be announced")
