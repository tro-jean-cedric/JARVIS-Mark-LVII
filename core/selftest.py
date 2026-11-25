"""
core/selftest.py — check, on THIS computer, that what the features rely on
really works, and say plainly what is missing and how to fix it.

    python -m core.selftest              # everything, including two tiny network calls
    python -m core.selftest --offline    # nothing leaves the computer
    python -m core.selftest playwright tls   # only the checks named

Also run by voice: assistant_help with full_check=true runs it in the
background and reports the result on screen and out loud.

WHY, NEXT TO assistant_help's diagnose
    diagnose reads SETTINGS and lists DEVICES: is a key set, which engine,
    how many microphones. That answers "is it configured". It cannot answer
    "does it work": a library that is installed but will not import (a
    missing system library under it), a browser automation package without
    its browser, a wake-word model that loads but never scores, an HTTPS
    stack that cannot verify a certificate. Those fail later, inside some
    feature, with an error that names the symptom and not the cause. Every
    check here exercises the real thing the feature uses, once, quickly.

THE RESULTS
    pass   it works
    fail   it is needed and it does not work; `fix` says what to do
    off    an optional part that is not set up (wake word not downloaded,
           no loopback device): not an error, but the feature that needs it
           will not work until it is; `fix` says how
    Nothing here opens the camera or the microphone stream, changes a
    setting, or installs anything. The network checks send one HTTPS request
    and one tiny model call, and are skipped with --offline.

Each check runs on its own thread with its own deadline, so one that hangs
(a browser driver that never starts) costs its deadline, not the run.
"""
from __future__ import annotations

import importlib
import platform
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL, OFF = "pass", "fail", "off"
CHECK_TIMEOUT_S = 20.0


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    seconds: float = 0.0

    def line(self) -> str:
        mark = {PASS: "PASS", FAIL: "FAIL", OFF: "OFF "}.get(self.status, "????")
        out = f"{mark}  {self.name}: {self.detail}"
        if self.fix and self.status != PASS:
            out += f"\n      fix: {self.fix}"
        return out


# ── the checks ───────────────────────────────────────────────────────────────
# Each returns (status, detail, fix). They may raise; the runner turns that
# into a FAIL with the error as the detail.

def check_python():
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}"
    if (3, 11) <= (v.major, v.minor) <= (3, 13):
        return PASS, text, ""
    return FAIL, f"{text} (this app supports 3.11 to 3.13)", "install Python 3.11, 3.12 or 3.13 and run setup.py again"


_PLATFORM_OF = {"win32": "Windows", "darwin": "Darwin", "linux": "Linux"}


def requirements(path: Optional[Path] = None, system: Optional[str] = None) -> list[tuple[str, str]]:
    """(distribution name, version rule) for every line of requirements.txt
    that applies to `system` (default: this computer)."""
    system = system or platform.system()
    out = []
    for raw in (path or ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, _, marker = line.partition(";")
        m = re.search(r'sys_platform\s*==\s*["\']([\w]+)["\']', marker)
        if m and _PLATFORM_OF.get(m.group(1)) != system:
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)", spec.strip())
        if m:
            out.append((m.group(1), m.group(3).strip()))
    return out


def check_packages():
    from importlib import metadata
    try:
        from packaging.specifiers import SpecifierSet
    except Exception:
        SpecifierSet = None
    missing, wrong = [], []
    reqs = requirements()
    for name, rule in reqs:
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        if SpecifierSet is not None and rule:
            try:
                if have not in SpecifierSet(rule):
                    wrong.append(f"{name} {have} (wants {rule})")
            except Exception:
                pass
    if not missing and not wrong:
        return PASS, f"all {len(reqs)} packages this computer needs are installed", ""
    bits = []
    if missing:
        bits.append("missing: " + ", ".join(missing))
    if wrong:
        bits.append("wrong version: " + ", ".join(wrong))
    return FAIL, "; ".join(bits), "pip install -r requirements.txt (or run setup.py again)"


# Installed is not the same as working: these carry native libraries that
# can be present and still fail to load (PortAudio, Qt's platform plugins).
_MUST_IMPORT = ("PyQt6.QtWidgets", "sounddevice", "numpy", "cv2", "google.genai", "httpx", "PIL.Image")


def check_imports():
    broken = []
    for mod in _MUST_IMPORT:
        try:
            importlib.import_module(mod)
        except Exception as e:
            broken.append(f"{mod} ({type(e).__name__}: {str(e)[:80]})")
    if broken:
        return FAIL, "installed but will not load: " + "; ".join(broken), \
            "reinstall those packages; on Linux the system library they name may be missing"
    return PASS, "the libraries with native parts load (" + ", ".join(m.split('.')[0] for m in _MUST_IMPORT) + ")", ""


def check_languages():
    from core import languages
    code, conf = languages.detect("Bonjour, est-ce que tu peux m'aider avec mes courriels ce matin ?")
    code_de, _ = languages.detect("Kannst du mir bitte heute Nachmittag mit meinen E-Mails helfen?")
    if code == "fr" and code_de == "de":
        return PASS, f"telling languages apart works (French {conf:.2f}, German)", ""
    if languages._langid() is None:
        return FAIL, "only the writing system can be told, so French, German, Spanish… look the same", \
            "pip install py3langid"
    return FAIL, f"French read as {code or 'unknown'}, German as {code_de or 'unknown'}", "pip install --upgrade py3langid"


def check_tls(network: bool):
    import ssl
    from core import tls
    ctx = tls.context()
    roots = len(ctx.get_ca_certs())
    bundled = tls.add_bundled_roots(ssl.create_default_context())
    if roots < 20:
        return FAIL, f"only {roots} trusted root certificates", "pip install --upgrade certifi"
    detail = f"{roots} trusted roots" + (" (system + bundled)" if bundled else " (system only; certifi missing)")
    if not network:
        return PASS, detail + "; connection not tried (offline)", ""
    import httpx
    t = time.monotonic()
    r = httpx.get("https://generativelanguage.googleapis.com/", verify=ctx, timeout=8)
    return PASS, f"{detail}; HTTPS to Google's API verified ({r.status_code}, {time.monotonic() - t:.1f} s)", ""


def check_gemini(network: bool):
    from core import gemini
    key = gemini.api_key(refresh=True)
    if not key:
        return FAIL, "no Gemini API key is set", "open the app and enter your key (aistudio.google.com/apikey is free)"
    if not network:
        return PASS, "a key is set; not tried (offline)", ""
    t = time.monotonic()
    got = gemini.text("Reply with the single word: ready", tier="gemini-3.5-flash-lite")
    if got:
        return PASS, f"the key works (a model answered in {time.monotonic() - t:.1f} s)", ""
    return FAIL, "the key is set but no model answered", \
        "check the key and its quota at aistudio.google.com; the console says which error came back"


def check_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return FAIL, "browser automation is not installed", "pip install playwright && python -m playwright install chromium"
    with sync_playwright() as p:
        exe = Path(p.chromium.executable_path)
    if exe.exists():
        build = next((p.name for p in exe.parents if p.name.startswith(("chromium", "chrome"))), "Chromium")
        return PASS, f"browser automation ready ({build})", ""
    return FAIL, "browser automation is installed but its browser is not", "python -m playwright install chromium"


def check_video():
    try:
        import yt_dlp
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        return FAIL, "video download/reading (yt-dlp) is not installed", "pip install yt-dlp"
    n = sum(1 for _ in gen_extractor_classes())
    version = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        hint = {"Darwin": "brew install ffmpeg", "Windows": "winget install ffmpeg"}.get(
            platform.system(), "install ffmpeg with your package manager")
        return FAIL, f"yt-dlp {version} reads {n} sites, but ffmpeg is missing (needed for local video and audio files)", hint
    return PASS, f"yt-dlp {version} reads {n} sites; ffmpeg found", ""


def check_audio():
    import sounddevice as sd
    devs = sd.query_devices()
    ins = [d for d in devs if d.get("max_input_channels", 0) > 0]
    outs = [d for d in devs if d.get("max_output_channels", 0) > 0]
    if not ins:
        return FAIL, f"no microphone is visible ({len(outs)} output device(s))", \
            "connect a microphone; on macOS allow it under System Settings (Privacy & Security, then Microphone)"
    if not outs:
        return FAIL, f"no speaker or headphones are visible ({len(ins)} input device(s))", "connect speakers or headphones"
    try:
        di, do = sd.default.device
        names = f"; default in: {sd.query_devices(di)['name']}, out: {sd.query_devices(do)['name']}"
    except Exception:
        names = ""
    return PASS, f"{len(ins)} input and {len(outs)} output device(s){names}", ""


def check_speaker_capture():
    from plugins import live_subtitles
    kind, _handle, label = live_subtitles.find_source()
    if kind:
        return PASS, f"the computer's own sound can be captured ({kind}: {label})", ""
    return OFF, "the computer's own sound cannot be captured, so live subtitles will not work", label


def check_speech_detector():
    """The local engine's own end-of-turn detector (providers/engine_pipeline.py,
    loudness against a measured noise floor - no model to download), driven
    with one second of room hiss, a voice-like sound, then silence again."""
    import asyncio
    import numpy as np
    from providers import audio as pa
    from providers.engine_pipeline import _VAD_ABSOLUTE_MIN, PipelineEngine

    eng = PipelineEngine.__new__(PipelineEngine)
    eng._vad_floor, eng._vad_speech_ms, eng._vad_quiet_ms = _VAD_ABSOLUTE_MIN, 0.0, 0.0
    eng._vad_started, eng._mic, eng._mic_bytes = False, [], 0
    ended = []

    async def flush():
        ended.append(True)
    eng.flush_microphone = flush
    frame = int(pa.MIC_RATE * 0.02)
    rng = np.random.default_rng(7)
    t = np.arange(frame) / pa.MIC_RATE

    def pcm(x):
        return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()

    async def drive():
        for _ in range(50):
            await eng._watch_for_turn_end(pcm(rng.normal(0, 0.001, frame)))
        hiss_is_speech = eng._vad_started
        for i in range(40):
            voice = 0.2 * np.sin(2 * np.pi * 220 * (t + i * 0.02)) + rng.normal(0, 0.02, frame)
            await eng._watch_for_turn_end(pcm(voice))
        heard = eng._vad_started
        for _ in range(50):
            await eng._watch_for_turn_end(pcm(rng.normal(0, 0.001, frame)))
        return hiss_is_speech, heard, bool(ended)

    hiss_is_speech, heard, turn_ended = asyncio.run(drive())
    if hiss_is_speech:
        return FAIL, "the local engine's speech detector takes room hiss for speech", \
            "providers/engine_pipeline.py: _VAD_ABSOLUTE_MIN may be too low"
    if not heard or not turn_ended:
        return FAIL, (f"the local engine's speech detector did not {'hear the voice' if not heard else 'end the turn'}"),\
            "providers/engine_pipeline.py: check the _VAD_ settings"
    return PASS, ("the local engine's speech detector ignores hiss, hears a voice and ends the "
                  "turn after the pause (loudness-based; no model needed)"), ""


def check_numbers():
    """What a local voice is given to read: numbers and units as words."""
    try:
        import babel  # noqa: F401
        import num2words  # noqa: F401
    except Exception as e:
        return FAIL, f"numbers cannot be said as words for local voices ({e.name} missing)", \
            "pip install babel num2words"
    from providers._speakable import speakable
    de = speakable("Es sind 21 °C und es kostet 49,99 €.", "de")
    fr = speakable("Il fait -3 °C.", "fr")
    if "einundzwanzig Grad Celsius" in de and "neunundvierzig Euro" in de and "moins trois" in fr:
        return PASS, f"local voices get numbers as words (\"{de}\")", ""
    return FAIL, f"numbers came out as: {de!r} / {fr!r}", "pip install --upgrade babel num2words"


def check_notifications():
    """Whether the system's notifications can be used (nothing is shown)."""
    from core import notify
    how, why = notify.method()
    if not notify.enabled():
        return OFF, "desktop notifications are switched off", 'set "desktop_notifications": true to use them'
    if how:
        return PASS, f"finished tasks and reminders can show a system notification ({how})", ""
    return OFF, f"no system notifications: {why}", why


def check_wake_word():
    from core import wake_word
    if not wake_word.is_installed():
        return OFF, "the wake word is not installed (the assistant listens all the time instead)", \
            "Settings > wake word > INSTALL, or pip install openwakeword"
    if not wake_word.is_ready():
        return OFF, "the wake word is installed but its model is not downloaded", \
            "Settings > wake word > INSTALL downloads it"
    import numpy as np
    from openwakeword.model import Model
    model = Model(wakeword_models=[wake_word.WAKE_MODEL], inference_framework="onnx")
    frame = np.zeros(1280, dtype=np.int16)
    peak = 0.0
    for _ in range(25):                       # two seconds of silence
        scores = model.predict(frame)
        peak = max(peak, max((float(v) for v in scores.values()), default=0.0))
    if peak < wake_word.DEFAULT_THRESHOLD:
        return PASS, f"'{wake_word.WAKE_MODEL}' loads and stays quiet on silence (peak {peak:.3f})", ""
    return FAIL, f"'{wake_word.WAKE_MODEL}' fires on silence (peak {peak:.2f})", "Settings > wake word > INSTALL again"


def check_screen():
    from core import screenview
    caps = screenview.capabilities()
    problems, fixes = [], []
    if not caps.get("capture_available"):
        problems.append("screenshots are not possible")
        fixes.append("pip install mss pillow")
    if not caps.get("accessibility_available"):
        problems.append("programs' controls cannot be read")
        fixes.append(f"pip install {caps.get('accessibility_package')}")
    if platform.system() == "Darwin":
        try:
            import ApplicationServices as AS
            if not AS.AXIsProcessTrusted():
                problems.append("macOS has not allowed this app to control the computer")
                fixes.append("allow the app running JARVIS under System Settings (Privacy & Security, then Accessibility)")
        except Exception:
            pass
        try:
            import Quartz
            if not Quartz.CGPreflightScreenCaptureAccess():
                problems.append("macOS has not allowed this app to see the screen")
                fixes.append("allow the app running JARVIS under System Settings (Privacy & Security, then Screen Recording)")
        except Exception:
            pass
    if problems:
        return FAIL, "; ".join(problems), "; ".join(fixes)
    return PASS, f"screenshots and reading programs' controls ({caps.get('accessibility_backend')}) are allowed", ""


def check_app_files():
    problems = []
    prompt = ROOT / "core" / "prompt.txt"
    if not prompt.is_file() or len(prompt.read_text(encoding="utf-8")) < 200:
        problems.append("core/prompt.txt is missing or empty")
    from actions import assistant_help
    sections = assistant_help.sections()
    if len(sections) < 3:
        problems.append("the manual (manual/en.md) could not be read")
    agents = sorted((ROOT / "agents").glob("*.md"))
    if not agents:
        problems.append("no background-task agent profiles in agents/")
    if problems:
        return FAIL, "; ".join(problems), "restore those files from the project (git checkout -- <file>)"
    return PASS, f"prompt, manual ({len(sections)} sections) and {len(agents)} agent profile(s) present", ""


def check_tools_load():
    from core.action_loader import discover_actions
    from core.plugin_loader import discover_plugins
    errors: list[str] = []
    actions = discover_actions(ROOT / "actions", logger=lambda m: errors.append(str(m)))
    names = {d["name"] for d in actions.get_tool_declarations()}
    plugins = discover_plugins(ROOT / "plugins", names, logger=lambda m: None)
    rows = plugins.list_for_ui()
    broken = [f"{r['file']} ({str(r['error'])[:70]})" for r in rows
              if not r["valid"] and not r.get("review")]
    failed_actions = [e for e in errors if re.search(r"(?i)fail|error|could not", e)]
    good = sum(1 for r in rows if r["valid"])
    if broken or failed_actions:
        return FAIL, (f"{len(names)} actions and {good} plugins load; broken: "
                      + "; ".join(broken[:6] + failed_actions[:3])), \
            "the error after each name says what it needs (usually a missing package)"
    return PASS, f"{len(names)} actions and {good} plugins load, none broken", ""


# name, function, takes the network flag
CHECKS: list[tuple[str, Callable, bool]] = [
    ("python", check_python, False),
    ("packages", check_packages, False),
    ("imports", check_imports, False),
    ("languages", check_languages, False),
    ("tls", check_tls, True),
    ("gemini", check_gemini, True),
    ("playwright", check_playwright, False),
    ("video", check_video, False),
    ("audio", check_audio, False),
    ("speaker_capture", check_speaker_capture, False),
    ("speech_detector", check_speech_detector, False),
    ("numbers", check_numbers, False),
    ("notifications", check_notifications, False),
    ("wake_word", check_wake_word, False),
    ("screen", check_screen, False),
    ("app_files", check_app_files, False),
    ("tools", check_tools_load, False),
]


def run_one(name: str, fn: Callable, takes_network: bool, network: bool,
            timeout_s: float = CHECK_TIMEOUT_S) -> Result:
    box: dict = {}

    def target():
        try:
            box["r"] = fn(network) if takes_network else fn()
        except BaseException as e:                     # noqa: BLE001
            box["e"] = e

    t = time.monotonic()
    th = threading.Thread(target=target, daemon=True, name=f"selftest-{name}")
    th.start()
    th.join(timeout_s)
    took = time.monotonic() - t
    if th.is_alive():
        return Result(name, FAIL, f"did not finish within {timeout_s:.0f} s", "run it again; if it repeats, "
                      "this part of the computer is not answering", took)
    if "e" in box:
        e = box["e"]
        return Result(name, FAIL, f"{type(e).__name__}: {str(e)[:160]}", "", took)
    status, detail, fix = box["r"]
    return Result(name, status, detail, fix, took)


def run(network: bool = True, only: Optional[list[str]] = None,
        on_result: Optional[Callable[[Result], None]] = None) -> list[Result]:
    """Every check (or those named in `only`), in order. Never raises."""
    results = []
    for name, fn, takes_network in CHECKS:
        if only and name not in only:
            continue
        r = run_one(name, fn, takes_network, network)
        results.append(r)
        if on_result is not None:
            try:
                on_result(r)
            except Exception:
                pass
    return results


def summary(results: list[Result]) -> str:
    """One short paragraph for the user: counts, then each problem and its fix."""
    failed = [r for r in results if r.status == FAIL]
    off = [r for r in results if r.status == OFF]
    passed = sum(1 for r in results if r.status == PASS)
    head = f"{passed} of {len(results)} checks passed"
    if not failed and not off:
        return head + "; everything this computer needs works."
    parts = [head + "."]
    for r in failed:
        parts.append(f"Not working - {r.name}: {r.detail}. Fix: {r.fix or 'see the detail'}.")
    for r in off:
        parts.append(f"Not set up - {r.name}: {r.detail}. To set it up: {r.fix}.")
    return " ".join(parts)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    network = "--offline" not in argv
    only = [a for a in argv if not a.startswith("-")] or None
    unknown = [n for n in (only or []) if n not in {c[0] for c in CHECKS}]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(c[0] for c in CHECKS)}")
        return 2
    sys.path.insert(0, str(ROOT))
    t = time.monotonic()
    results = run(network, only, on_result=lambda r: print(r.line(), flush=True))
    failed = sum(1 for r in results if r.status == FAIL)
    off = sum(1 for r in results if r.status == OFF)
    print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}"
          + (f", {off} not set up" if off else "") + f" ({time.monotonic() - t:.1f} s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
