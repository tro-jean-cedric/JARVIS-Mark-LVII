"""
plugins/_telegram_ops.py — the things the Telegram remote can DO, kept apart
from the bridge itself.

Helper module (leading underscore: the plugin loader skips it).

telegram_remote.py is the security boundary: who may talk to this computer,
pairing, the poll loop, the reply relay. This file is the toolbox it reaches
into once a message has been let in — capture the screen, read the hardware,
take a webcam picture, list and fetch files from the shared folder, turn a
voice note into text and an answer into a voice note.

Rules every function here follows:
  * it runs on a worker thread and never touches Qt;
  * it raises OpsError with a sentence a person can act on, never a traceback;
  * it is importable on the cloud server too (core/cloud.py ships this file):
    every optional dependency is imported inside the function that needs it.
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Telegram's own limits for a bot: 50 MB for documents it sends, 10 MB for
# photos, 20 MB for a file it lets a bot download.
MAX_SEND_BYTES = 49 * 1024 * 1024
MAX_PHOTO_BYTES = 9 * 1024 * 1024
MAX_FETCH_BYTES = 20 * 1024 * 1024


class OpsError(RuntimeError):
    """A failure with a sentence meant for the phone."""


# ── screen ───────────────────────────────────────────────────────────────────

def _jpeg(img, max_side: int = 2560, quality: int = 80) -> bytes:
    """A PIL image as JPEG bytes that Telegram accepts as a photo."""
    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    if img.mode != "RGB":
        img = img.convert("RGB")
    for q in (quality, 65, 50, 35):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= MAX_PHOTO_BYTES:
            return buf.getvalue()
    return buf.getvalue()


def screenshot(which: str = "") -> list[tuple[bytes, str]]:
    """One JPEG per requested monitor, each with a caption.

    which: "" = the main screen, "2" = the second monitor, "all" = every one."""
    try:
        import mss
    except Exception:
        raise OpsError("Screen capture needs the 'mss' package (pip install mss).") from None
    try:
        from PIL import Image
    except Exception:
        raise OpsError("Screen capture needs the 'pillow' package (pip install pillow).") from None
    which = str(which or "").strip().lower()
    try:
        with mss.mss() as sct:
            monitors = sct.monitors[1:]          # [0] is the union of all of them
            if not monitors:
                raise OpsError("The graphics driver reported no screen at all.")
            if which in ("all", "every", "*"):
                chosen = list(enumerate(monitors, 1))
            elif which.isdigit():
                n = int(which)
                if not 1 <= n <= len(monitors):
                    raise OpsError(f"There is no monitor {n}; this computer has {len(monitors)}.")
                chosen = [(n, monitors[n - 1])]
            else:
                chosen = [(1, monitors[0])]
            out = []
            for n, mon in chosen:
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                cap = f"Screen {n} · {time.strftime('%H:%M:%S')}" if len(monitors) > 1 \
                    else f"Screen · {time.strftime('%H:%M:%S')}"
                out.append((_jpeg(img), cap))
            return out
    except OpsError:
        raise
    except Exception as e:
        raise OpsError(f"The screen could not be captured ({type(e).__name__}: {e}).") from None


# ── webcam ───────────────────────────────────────────────────────────────────

def webcam() -> bytes:
    try:
        from plugins import _camera
    except Exception as e:
        raise OpsError(f"The camera helper is missing ({e}).") from None
    try:
        return _camera.snapshot_jpeg(1280)
    except Exception as e:
        raise OpsError(f"The camera could not be used ({e}).") from None


# ── hardware ─────────────────────────────────────────────────────────────────

def system_status() -> str:
    """A few short lines: CPU, memory, disk, GPU, temperature, uptime, battery."""
    lines = [f"🖥 {platform.node() or 'this computer'} · {platform.system()} {platform.release()}"]
    facts: dict = {}
    try:
        from actions.system_monitor import get_system_status
        facts = get_system_status() or {}
    except Exception:
        try:
            import psutil
            vm = psutil.virtual_memory()
            up = time.time() - psutil.boot_time()
            facts = {"cpu_percent": psutil.cpu_percent(interval=0.3), "ram_percent": vm.percent,
                     "ram_used_gb": round(vm.used / 2 ** 30, 1),
                     "ram_total_gb": round(vm.total / 2 ** 30, 1),
                     "uptime": f"{int(up // 3600)}h {int(up % 3600 // 60)}m"}
        except Exception as e:
            raise OpsError(f"The hardware readout is unavailable ({type(e).__name__}).") from None
    if facts.get("cpu_percent") is not None:
        lines.append(f"CPU {facts['cpu_percent']}%"
                     + (f" · {facts['cpu_temp_c']}°C" if facts.get("cpu_temp_c") else ""))
    if facts.get("ram_percent") is not None:
        lines.append(f"Memory {facts['ram_percent']}%"
                     + (f" ({facts.get('ram_used_gb')}/{facts.get('ram_total_gb')} GB)"
                        if facts.get("ram_total_gb") else ""))
    if facts.get("gpu_percent") is not None:
        lines.append(f"GPU {facts['gpu_percent']}%")
    try:
        du = shutil.disk_usage(str(Path.home()))
        lines.append(f"Disk free {du.free / 2 ** 30:.0f} of {du.total / 2 ** 30:.0f} GB")
    except Exception:
        pass
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is not None:
            lines.append(f"Battery {bat.percent:.0f}%" + (" (charging)" if bat.power_plugged else ""))
    except Exception:
        pass
    if facts.get("uptime"):
        lines.append(f"Up {facts['uptime']}")
    return "\n".join(lines)


# ── files ────────────────────────────────────────────────────────────────────

def default_inbox() -> Path:
    """Downloads/<assistant name> — where files from the phone land by default."""
    try:
        from memory.config_manager import get_assistant_name
        name = get_assistant_name() or "JARVIS"
    except Exception:
        name = "JARVIS"
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "JARVIS"
    return Path.home() / "Downloads" / safe


def safe_filename(name: str, fallback: str = "file") -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join(c for c in base if c not in '<>:"|?*\x00').strip(". ")
    return base[:120] or fallback


def unique_path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / safe_filename(name)
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def list_folder(folder: str, limit: int = 40) -> str:
    root = _folder(folder)
    rows = []
    for p in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and not p.name.startswith("."):
            rows.append(f"• {p.name} ({_size(p.stat().st_size)})")
        if len(rows) >= limit:
            break
    return "\n".join(rows) or "The shared folder is empty."


def file_in_folder(folder: str, name: str) -> Path:
    """A file INSIDE the shared folder — never outside it, whatever the name says."""
    root = _folder(folder).resolve()
    wanted = str(name or "").strip()
    target = (root / wanted).resolve()
    if root not in target.parents and target != root:
        raise OpsError("Only files inside the shared folder can be sent.")
    if not target.is_file():
        # Loose match: the phone types "report" for "Report 2026.pdf".
        low = wanted.lower()
        hits = [p for p in root.iterdir() if p.is_file() and low and low in p.name.lower()]
        if len(hits) == 1:
            target = hits[0]
        elif len(hits) > 1:
            raise OpsError("Several files match: " + ", ".join(p.name for p in hits[:6]))
        else:
            raise OpsError(f"There is no '{wanted}' in the shared folder.")
    check_sendable(target)
    return target


def check_sendable(path: Path) -> None:
    if not path.is_file():
        raise OpsError(f"'{path}' is not a file on this computer.")
    size = path.stat().st_size
    if size > MAX_SEND_BYTES:
        raise OpsError(f"{path.name} is {_size(size)}; Telegram will not carry more than "
                       f"{_size(MAX_SEND_BYTES)} from a bot.")


def _folder(folder: str) -> Path:
    if not str(folder or "").strip():
        raise OpsError("No folder is set for that at the computer.")
    p = Path(os.path.expandvars(os.path.expanduser(str(folder).strip())))
    if not p.is_dir():
        raise OpsError(f"'{p}' is not a folder on this computer.")
    return p


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


# ── voice ────────────────────────────────────────────────────────────────────

def transcribe(audio: bytes, mime: str = "audio/ogg") -> str:
    """A voice note as text, in the language it was spoken in. "" = nothing heard."""
    if not audio:
        raise OpsError("That voice note was empty.")
    try:
        from google.genai import types

        from core import gemini
    except Exception as e:
        raise OpsError(f"No model is available to listen to voice notes ({e}).") from None
    prompt = ("Write down exactly what is said in this recording, in the language it is "
              "spoken in. Output only the words themselves: no quotes, no translation, no "
              "notes. If nothing can be understood, output nothing.")
    part = types.Part.from_bytes(data=audio, mime_type=mime or "audio/ogg")
    text = gemini.text([part, prompt], tier=gemini.FAST, timeout_ms=60_000)
    return (text or "").strip().strip('"').strip()


def synthesize(text: str) -> tuple[bytes, str]:
    """An answer as audio: (bytes, kind) with kind "voice" (OGG/Opus, when ffmpeg
    is installed) or "audio" (WAV, sent as a file). Raises OpsError."""
    text = str(text or "").strip()
    if not text:
        raise OpsError("There was nothing to say.")
    try:
        from providers.tts_system import SystemTTS, available
    except Exception as e:
        raise OpsError(f"No voice is available on this computer ({e}).") from None
    if not available():
        raise OpsError("No speech engine was found on this computer.")
    try:
        wav = SystemTTS({})._synth_blocking(text[:1500])
    except Exception as e:
        raise OpsError(f"The voice failed ({type(e).__name__}: {e}).") from None
    if not wav or len(wav) < 200:
        raise OpsError("The voice produced no sound.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return wav, "audio"
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "a.wav", Path(tmp) / "a.ogg"
        src.write_bytes(wav)
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-c:a", "libopus",
                            "-b:a", "32k", str(dst)], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return dst.read_bytes(), "voice"
        print(f"[TelegramRemote] ffmpeg could not make a voice note: {r.stderr.strip()[:200]}")
    return wav, "audio"


# ── the confirmation gate and undo ───────────────────────────────────────────

def pending_confirmation() -> str:
    try:
        from core import confirm
        return confirm.pending_title()
    except Exception:
        return ""


def undo_last() -> str:
    try:
        from core import undo
    except Exception:
        raise OpsError("This version of the app has no undo.") from None
    if not undo.can_undo():
        return ("There is nothing to undo. Only what the assistant changed itself can be undone — "
                "files it moved or wrote, settings it adjusted.")
    return undo.undo_last() or "Undone."
