"""
OpenAI-compatible speech — any server that serves POST /v1/audio/speech.

WHO SPEAKS THIS
    OpenAI itself (tts-1, gpt-4o-mini-tts), and the local servers that copied
    its shape: Kokoro-FastAPI (a small, very natural local voice, Apache-2.0,
    `docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu`), and the
    many TTS front-ends that put an OpenAI-style endpoint in front of their own
    model. One file covers all of them, the way providers/llm_openai.py covers
    every /v1/chat/completions server.

FORMAT
    `response_format: "pcm"` is raw 16-bit mono at 24 kHz on OpenAI and Kokoro
    — exactly the rate the speaker is open at, so it goes straight through, in
    pieces, while it is still arriving. A server that refuses "pcm" is asked
    for "wav" instead (learned once), which is decoded and resampled. A server
    whose raw PCM is at another rate can say so under MORE.
"""
from __future__ import annotations

from importlib.util import find_spec
from typing import AsyncIterator

from . import audio
from .base import DEFAULT_CAPS, Field

OPENAI_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage",
                 "shimmer", "verse")
KOKORO_VOICES = ("af_heart", "af_bella", "af_nicole", "af_sky", "am_adam", "am_michael",
                 "bf_emma", "bm_george", "ff_siwis", "if_sara", "jf_alpha", "zf_xiaobei")

CATALOG = (
    {"id": "kokoro", "label": "Kokoro (on this computer)",
     "note": "local · free · very natural (Kokoro-FastAPI on port 8880)", "local": True,
     "values": {"base_url": "http://localhost:8880/v1", "model": "kokoro", "voice": "af_heart"}},
    {"id": "openai", "label": "OpenAI voices",
     "note": "cloud · needs an OpenAI key", "local": False,
     "values": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini-tts",
                "voice": "coral"}},
    {"id": "custom", "label": "Another OpenAI-compatible voice server",
     "note": "any address serving /v1/audio/speech", "local": None, "values": {}},
)


class OpenAISpeech:
    def __init__(self, cfg: dict):
        self.base = (cfg.get("base_url") or "http://localhost:8880/v1").strip().rstrip("/")
        if not self.base.endswith("/v1") and "/v1/" not in self.base + "/":
            self.base += "/v1"
        self.model = (cfg.get("model") or "").strip() or "tts-1"
        self.voice = (cfg.get("voice") or "").strip() or "alloy"
        self.key = (cfg.get("api_key") or "").strip()
        try:
            self.speed = float(cfg.get("speed") or 1.0)
        except (TypeError, ValueError):
            self.speed = 1.0
        try:
            self.pcm_rate = int(float(cfg.get("pcm_rate") or audio.SPEAKER_RATE))
        except (TypeError, ValueError):
            self.pcm_rate = audio.SPEAKER_RATE
        self._format: str | None = None          # learned: "pcm" or "wav"
        self.caps = dict(DEFAULT_CAPS)
        self.caps["audio_out"] = True
        self._local = any(h in self.base for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _body(self, text: str, fmt: str, voice: str) -> dict:
        body = {"model": self.model, "input": text, "voice": voice or self.voice,
                "response_format": fmt}
        if self.speed and abs(self.speed - 1.0) > 1e-3:
            body["speed"] = self.speed
        return body

    async def speak(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        import httpx
        text = (text or "").strip()
        if not text:
            return
        async with httpx.AsyncClient(timeout=60.0, trust_env=not self._local) as cl:
            fmt = self._format or "pcm"
            if fmt == "pcm":
                try:
                    async for piece in self._pcm(cl, text, voice):
                        yield piece
                    self._format = "pcm"
                    return
                except _Refused:
                    self._format = "wav"            # this server has no raw PCM
            async for piece in self._wav(cl, text, voice):
                yield piece

    async def _post(self, cl, text: str, fmt: str, voice: str):
        return cl.stream("POST", f"{self.base}/audio/speech", headers=self._headers(),
                         json=self._body(text, fmt, voice))

    async def _pcm(self, cl, text: str, voice: str) -> AsyncIterator[bytes]:
        async with await self._post(cl, text, "pcm", voice) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "replace")
                if r.status_code in (400, 415, 422) and "format" in body.lower():
                    raise _Refused(body)
                raise RuntimeError(f"{r.status_code} {_terse(body)} (at {self.base})")
            buf = b""
            async for piece in r.aiter_bytes():
                buf += piece
                # Even byte counts only: a sample is two bytes.
                step = 2400 if self.pcm_rate == audio.SPEAKER_RATE else 9600
                while len(buf) >= step:
                    yield self._rate(buf[:step])
                    buf = buf[step:]
            if len(buf) >= 2:
                yield self._rate(buf[: len(buf) - len(buf) % 2])

    def _rate(self, pcm: bytes) -> bytes:
        if self.pcm_rate == audio.SPEAKER_RATE:
            return pcm
        return audio.resample(pcm, self.pcm_rate, audio.SPEAKER_RATE)

    async def _wav(self, cl, text: str, voice: str) -> AsyncIterator[bytes]:
        async with await self._post(cl, text, "wav", voice) as r:
            data = await r.aread()
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {_terse(data.decode('utf-8', 'replace'))}"
                                   f" (at {self.base})")
        pcm = audio.wav_to_pcm(data, audio.SPEAKER_RATE)
        for piece in audio.chunk(pcm):
            yield piece

    # ── for the panel ────────────────────────────────────────────────────────
    def voices(self) -> list[tuple[str, str]]:
        """The server's own list when it has one (Kokoro: /audio/voices),
        else OpenAI's voices."""
        import httpx
        try:
            with httpx.Client(timeout=6.0, trust_env=not self._local) as cl:
                r = cl.get(f"{self.base}/audio/voices", headers=self._headers())
                if r.status_code < 400:
                    data = r.json()
                    items = data.get("voices") if isinstance(data, dict) else data
                    names = [v if isinstance(v, str) else (v.get("id") or v.get("name") or "")
                             for v in items or []]
                    names = [n for n in names if n]
                    if names:
                        return [(n, n) for n in names]
        except Exception:
            pass
        return [(v, v) for v in (KOKORO_VOICES if self._local else OPENAI_VOICES)]

    def probe(self) -> tuple[bool, str]:
        import asyncio
        import time
        t0 = time.monotonic()

        async def one():
            n = 0
            async for piece in self.speak("Ready."):
                n += len(piece)
            return n
        # On a thread of its own: TEST may be pressed from code that already
        # runs an event loop, where asyncio.run() is not allowed.
        import threading
        box: dict = {}

        def work():
            try:
                box["n"] = asyncio.run(one())
            except Exception as e:      # handed back below
                box["err"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(70)
        try:
            if "err" in box:
                raise box["err"]
            if "n" not in box:
                raise RuntimeError("timed out")
            n = box["n"]
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg:
                return False, "The server refused the key."
            if "connect" in msg.lower() or "timed out" in msg.lower():
                return False, f"Nothing answered at {self.base} - is the voice server running?"
            return False, msg[:150]
        if not n:
            return False, "The server answered with no sound."
        secs = n / 2 / audio.SPEAKER_RATE
        return True, (f"Spoke {secs:.1f}s of audio in {time.monotonic() - t0:.1f}s "
                      f"({self._format or 'pcm'}, voice {self.voice})")


class _Refused(Exception):
    pass


def _terse(text: str, limit: int = 160) -> str:
    import json
    text = " ".join((text or "").split())
    try:
        obj = json.loads(text)
        err = obj.get("error", obj) if isinstance(obj, dict) else obj
        text = str(err.get("message") if isinstance(err, dict) else err)
    except Exception:
        pass
    return text[:limit]


def _make(cfg: dict) -> OpenAISpeech:
    return OpenAISpeech(cfg)


PROVIDER = {
    "id": "openai_tts",
    "kind": "tts",
    "label": "OpenAI-compatible voice",
    "hint": "Kokoro on this computer · OpenAI · any /v1/audio/speech server",
    "requires": ("httpx",),
    "catalog": CATALOG,
    "needs": (
        Field("base_url", "Address", default="http://localhost:8880/v1",
              hint="Where /v1/audio/speech is served"),
        Field("voice", "Voice", kind="choice", choices=KOKORO_VOICES + OPENAI_VOICES,
              default="af_heart", hint="Press LIST for the server's own voices"),
        Field("model", "Model", kind="choice", choices=("kokoro", "gpt-4o-mini-tts", "tts-1",
                                                        "tts-1-hd"),
              default="kokoro", required=False),
        Field("api_key", "API key", kind="secret", required=False,
              hint="Local servers need none"),
        Field("speed", "Speed", kind="number", default=1.0, required=False, advanced=True,
              hint="0.5 to 2"),
        Field("pcm_rate", "Raw PCM sample rate", kind="number", default=24000,
              required=False, advanced=True,
              hint="Only for a server whose raw PCM is not 24000 Hz"),
    ),
    "caps": {"audio_out": True},
    "factory": _make,
}


def available() -> bool:
    return find_spec("httpx") is not None
