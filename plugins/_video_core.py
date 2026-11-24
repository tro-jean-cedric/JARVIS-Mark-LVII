"""
plugins/_video_core.py — the words of a video or recording, from wherever it is.

Helper module for video_summarizer (leading underscore: the loader skips it).

Order of attempts:
  YouTube link  -> youtube-transcript-api (no download, no model allowance)
                -> yt-dlp subtitles / automatic captions
                -> yt-dlp audio + speech-to-text through core.gemini
  other links   -> yt-dlp subtitles -> yt-dlp audio + speech-to-text
  local file    -> speech-to-text through core.gemini (ffmpeg cuts long or
                   video files into small audio pieces; without ffmpeg a small
                   audio file is sent as it is)

Everything raises VideoError with a sentence a person can act on.
"""
from __future__ import annotations

import concurrent.futures as cf
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from plugins._snapshot import AUDIO_EXT, VIDEO_EXT

MEDIA_EXT = AUDIO_EXT | VIDEO_EXT
_INLINE_LIMIT = 18 * 1024 * 1024     # stay under the request size for inline audio
_CHUNK_SECONDS = 600


class VideoError(Exception):
    pass


@dataclass
class Transcript:
    source: str
    title: str = ""
    duration: float = 0.0
    segments: list = field(default_factory=list)   # [(start_seconds, text)]
    how: str = ""                                    # captions | youtube | speech

    def text(self, times: bool = True) -> str:
        if not times:
            return " ".join(t for _, t in self.segments)
        return "\n".join(f"[{fmt_time(s)}] {t}" for s, t in self.segments)

    def window(self, start: float, span: float = 90.0) -> str:
        """What was said from start-span/3 to start+span (for 'what was said at 12:30')."""
        lo, hi = start - span / 3, start + span
        return "\n".join(f"[{fmt_time(s)}] {t}" for s, t in self.segments if lo <= s <= hi)


def fmt_time(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> Optional[float]:
    """'12:30', '1:02:03', '90s', '5 min' -> seconds, else None."""
    t = str(text or "").strip().lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", t)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hour|hours|m|min|minute|minutes|s|sec|second|seconds)\b", t)
    if m:
        v, unit = float(m.group(1)), m.group(2)
        return v * 3600 if unit.startswith("h") else v * 60 if unit.startswith("m") else v
    return None


# ── Cache of recent transcripts ──────────────────────────────────────────────

_cache: "OrderedDict[str, Transcript]" = OrderedDict()
_cache_lock = threading.Lock()
_last: Optional[str] = None


def remember(t: Transcript) -> None:
    global _last
    with _cache_lock:
        _cache[t.source] = t
        _cache.move_to_end(t.source)
        while len(_cache) > 6:
            _cache.popitem(last=False)
        _last = t.source


def cached(source: str) -> Optional[Transcript]:
    with _cache_lock:
        return _cache.get(source)


def last() -> Optional[Transcript]:
    with _cache_lock:
        return _cache.get(_last) if _last else None


# ── Sources ──────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+", re.I)


def is_link(text: str) -> bool:
    return bool(_URL_RE.match(str(text or "").strip()))


def youtube_id(url: str) -> Optional[str]:
    m = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def parse_captions(raw: str, fmt: str = "") -> list:
    """WebVTT, SRT or YouTube json3 -> [(start, text)], with rolling-caption
    repeats removed."""
    raw = raw or ""
    out: list = []
    if fmt == "json3" or raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
            for ev in data.get("events") or []:
                segs = ev.get("segs")
                if not segs:
                    continue
                txt = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
                if txt:
                    out.append((ev.get("tStartMs", 0) / 1000.0, txt))
            return _dedupe(out)
        except Exception:
            pass
    ts = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
    start = None
    buf: list[str] = []

    def flush():
        if start is not None and buf:
            txt = re.sub(r"<[^>]+>", "", " ".join(buf))
            txt = html.unescape(re.sub(r"\s+", " ", txt)).strip()
            if txt:
                out.append((start, txt))

    for line in raw.splitlines():
        m = ts.search(line)
        if m:
            flush()
            buf = []
            h = int(m.group(1) or 0)
            start = h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000
            continue
        s = line.strip()
        if not s:                      # a blank line ends the cue
            flush()
            buf, start = [], None
            continue
        if s.isdigit() and not buf:   # SRT counter
            continue
        if s.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        if start is not None:
            buf.append(s)
    flush()
    return _dedupe(out)


def _dedupe(segs: list) -> list:
    out: list = []
    for s, t in segs:
        if out and (t == out[-1][1] or out[-1][1].endswith(t)):
            continue
        if out and t.startswith(out[-1][1]):
            out[-1] = (out[-1][0], t)
            continue
        out.append((s, t))
    return out


def _from_youtube_api(vid: str) -> Optional[list]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except Exception:
        return None
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "list"):           # 1.x
            listing = api.list(vid)
        else:                              # 0.x
            listing = YouTubeTranscriptApi.list_transcripts(vid)
        chosen = None
        for t in listing:
            if not getattr(t, "is_generated", False):
                chosen = t
                break
        if chosen is None:
            for t in listing:
                chosen = t
                break
        if chosen is None:
            return None
        fetched = chosen.fetch()
        segs = []
        for e in fetched:
            if isinstance(e, dict):
                segs.append((float(e.get("start", 0)), str(e.get("text", "")).replace("\n", " ")))
            else:
                segs.append((float(getattr(e, "start", 0)), str(getattr(e, "text", "")).replace("\n", " ")))
        return [(s, t.strip()) for s, t in segs if t.strip()]
    except Exception as e:
        print(f"[video] youtube transcript not available: {type(e).__name__}: {str(e)[:120]}")
        return None


def _ydl():
    try:
        import yt_dlp  # type: ignore
        return yt_dlp
    except Exception:
        raise VideoError("reading videos from links needs yt-dlp (pip install yt-dlp)")


def link_info(url: str) -> dict:
    yt_dlp = _ydl()
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise VideoError(f"the link could not be opened ({str(e)[:160]})")
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise VideoError("that link is a list with no video in it")
        info = entries[0]
    if not info:
        raise VideoError("nothing could be read from that link")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise VideoError("that is a live stream or has not started yet; it can be summarized once "
                         "it is a recording")
    return info


def _pick_caption(info: dict) -> Optional[tuple[str, str]]:
    """(url, ext) of the best caption track: the video's own language first."""
    lang = (info.get("language") or "").split("-")[0]
    for pool in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not pool:
            continue
        keys = list(pool)
        order = [k for k in keys if lang and k.split("-")[0] == lang and "orig" in k]
        order += [k for k in keys if lang and k.split("-")[0] == lang]
        if pool is info.get("subtitles"):
            order += keys
        else:
            order += [k for k in keys if k.endswith("-orig")] + [k for k in keys if k.startswith("en")]
        for k in order:
            tracks = pool.get(k) or []
            for ext in ("json3", "vtt", "srt"):
                for tr in tracks:
                    if tr.get("ext") == ext and tr.get("url"):
                        return tr["url"], ext
    return None


def _download_text(url: str) -> str:
    import requests
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def _download_audio(url: str, folder: str) -> Path:
    yt_dlp = _ydl()
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio[filesize<60M]/bestaudio/worstaudio/worst",
            "outtmpl": os.path.join(folder, "audio.%(ext)s"), "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        raise VideoError(f"the sound could not be fetched ({str(e)[:140]})")
    files = sorted(Path(folder).glob("audio.*"))
    if not files:
        raise VideoError("the sound could not be fetched")
    return files[0]


def read_link(url: str, use_subtitles: bool = True, max_minutes: int = 0,
              progress: Callable[[str], None] = print) -> Transcript:
    hit = cached(url)
    if hit:
        return hit
    vid = youtube_id(url)
    title, duration = "", 0.0
    if vid and use_subtitles:
        segs = _from_youtube_api(vid)
        if segs:
            t = Transcript(source=url, title="", segments=segs, how="youtube",
                           duration=segs[-1][0] if segs else 0)
            try:
                info = link_info(url)
                t.title, t.duration = info.get("title") or "", float(info.get("duration") or t.duration)
            except Exception:
                pass
            _check_length(t.duration, max_minutes)
            remember(t)
            return t
    info = link_info(url)
    title, duration = info.get("title") or "", float(info.get("duration") or 0)
    _check_length(duration, max_minutes)
    if use_subtitles:
        cap = _pick_caption(info)
        if cap:
            try:
                segs = parse_captions(_download_text(cap[0]), cap[1])
                if segs:
                    t = Transcript(url, title, duration, segs, "captions")
                    remember(t)
                    return t
            except Exception as e:
                print(f"[video] captions could not be fetched: {e}")
    progress("Video: no subtitles, listening to the sound")
    with tempfile.TemporaryDirectory(prefix="jarvis_video_") as folder:
        audio = _download_audio(info.get("webpage_url") or url, folder)
        segs = transcribe(audio, progress)
    if not segs:
        raise VideoError("nothing was said in that video that could be transcribed")
    t = Transcript(url, title, duration, segs, "speech")
    remember(t)
    return t


def _check_length(duration: float, max_minutes: int) -> None:
    if max_minutes and duration and duration > max_minutes * 60:
        raise VideoError(f"that video is {int(duration // 60)} minutes long; the limit in this "
                         f"plugin's settings is {max_minutes} minutes")


def read_file(path: str, max_minutes: int = 0, progress: Callable[[str], None] = print) -> Transcript:
    p = Path(path)
    key = str(p.resolve())
    hit = cached(key)
    if hit:
        return hit
    if p.suffix.lower() not in MEDIA_EXT:
        raise VideoError("that is neither a link nor a video or audio file")
    duration = probe_seconds(p) or 0.0
    _check_length(duration, max_minutes)
    segs = transcribe(p, progress)
    if not segs:
        raise VideoError("nothing was said in that recording that could be transcribed")
    t = Transcript(key, p.name, duration or (segs[-1][0] if segs else 0), segs, "speech")
    remember(t)
    return t


# ── Speech to text through core.gemini ───────────────────────────────────────

def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def probe_seconds(path: Path) -> Optional[float]:
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
         ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
         ".webm": "audio/webm", ".mp4": "video/mp4", ".mov": "video/quicktime",
         ".amr": "audio/amr", ".wma": "audio/x-ms-wma"}


def _pieces(path: Path, folder: str) -> list[tuple[float, Path]]:
    """Small mono MP3 pieces of the sound, with their start times."""
    ff = _ffmpeg()
    if not ff:
        if path.suffix.lower() in AUDIO_EXT and path.stat().st_size <= _INLINE_LIMIT:
            return [(0.0, path)]
        raise VideoError("there is no speech recognition for this file: ffmpeg is needed to take "
                         "the sound out of it (install ffmpeg), or use a small audio file")
    pattern = os.path.join(folder, "piece_%04d.mp3")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-ac", "1",
           "-ar", "16000", "-b:a", "32k", "-f", "segment", "-segment_time", str(_CHUNK_SECONDS),
           pattern]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        print(f"[video] ffmpeg failed: {err[-400:]}")
        if "does not contain any stream" in err or "matches no streams" in err:
            raise VideoError("that video has no sound to transcribe")
        raise VideoError(f"that file could not be opened as audio or video ({err[-120:]})")
    files = sorted(Path(folder).glob("piece_*.mp3"))
    if not files:
        raise VideoError("that video has no sound to transcribe")
    return [(i * float(_CHUNK_SECONDS), f) for i, f in enumerate(files)]


_LINE_RE = re.compile(r"^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-:]?\s*(.+)$")


def _parse_lines(text: str, offset: float) -> list:
    segs = []
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line)
        if m:
            t = parse_time(m.group(1)) or 0.0
            segs.append((offset + t, m.group(2).strip()))
        elif line.strip() and segs:
            segs[-1] = (segs[-1][0], segs[-1][1] + " " + line.strip())
        elif line.strip():
            segs.append((offset, line.strip()))
    return segs


def transcribe(path: Path, progress: Callable[[str], None] = print) -> list:
    from core import gemini
    from google.genai import types

    prompt = ("Transcribe everything said in this recording, in the language it is spoken. "
              "One line per sentence or short passage, each starting with its time in the "
              "recording as [m:ss]. Words only, no description of sounds. If nothing is said, "
              "reply with exactly NO_SPEECH.")

    with tempfile.TemporaryDirectory(prefix="jarvis_stt_") as folder:
        pieces = _pieces(Path(path), folder)
        if len(pieces) > 1:
            progress(f"Video: transcribing {len(pieces)} parts")

        def one(item):
            offset, f = item
            data = f.read_bytes()
            if len(data) > _INLINE_LIMIT:
                raise VideoError("a piece of the sound is too large to send")
            mime = "audio/mpeg" if f.suffix == ".mp3" else _MIME.get(f.suffix.lower(), "audio/mpeg")
            part = types.Part.from_bytes(data=data, mime_type=mime)
            reply = gemini.text([part, prompt], tier=gemini.SMART, timeout_ms=240_000)
            if not reply:
                raise VideoError("the speech model did not answer (the allowance may be used up)")
            if reply.strip().upper().startswith("NO_SPEECH"):
                return []
            return _parse_lines(reply, offset)

        segs: list = []
        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            for result in pool.map(one, pieces):
                segs.extend(result)
    return segs


# ── Finding the video on the screen ──────────────────────────────────────────

def link_on_screen() -> Optional[str]:
    """The address in the browser on screen, read by the vision model, if any."""
    try:
        from plugins import _snapshot
        from core import gemini
        jpeg, _ = _snapshot.screen_jpeg(1600)
        if not jpeg:
            return None
        reply = gemini.text([_snapshot.image_part(jpeg),
                             "If a web browser with a video page is visible, reply with ONLY the "
                             "full address shown in its address bar (add https:// if missing). "
                             "Otherwise reply NONE."], tier=gemini.FAST, timeout_ms=15_000)
        m = re.search(r"(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?", reply or "", re.I)
        if not m or "NONE" == (reply or "").strip().upper():
            return None
        url = m.group(0)
        return url if url.lower().startswith("http") else "https://" + url
    except Exception as e:
        print(f"[video] screen not read: {e}")
        return None
