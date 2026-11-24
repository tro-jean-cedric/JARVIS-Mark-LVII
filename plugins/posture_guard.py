"""
posture_guard — a gentle eye on how the user sits, through the shared webcam.

start  : learns how the user sits for a few seconds (the baseline), then keeps
         watching about once a second on a background thread.
stop   : stops watching and frees the camera (if nobody else uses it).
status : what it is doing.

It speaks (through _announce.say, with cooldowns) when, for about a minute:
  * the face is much bigger than at the start  -> leaning in to the screen
  * the face sits much lower than at the start -> slumping
  * the eye line is tilted                     -> head tilted to one side
and every 20 minutes of screen time it suggests the 20-20-20 eye break.

No picture is stored; only the numbers of the baseline live in memory.
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Optional

from plugins import _announce

PLUGIN = {
    "name": "posture_guard",
    "description": (
        "Watches the user's posture through the webcam while they work and kindly "
        "says so when they lean in close to the screen, slump, or keep their head "
        "tilted for a while; also reminds them every 20 minutes to rest their eyes "
        "(the 20-20-20 rule). Use for 'watch my posture', 'tell me when I slouch', "
        "'remind me to rest my eyes', 'stop watching my posture'. action='start' "
        "first learns how the user sits for a few seconds, so tell them to sit "
        "comfortably upright; 'stop'; 'status'. Set eye_breaks or posture to false "
        "to switch one half off. It keeps running by itself until stopped."
    ),
    "brief": "Webcam posture watcher with eye-break reminders: start / stop / status.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, stop or status"},
            "eye_breaks": {"type": "BOOLEAN",
                           "description": "20-20-20 eye-break reminders (default true)"},
            "posture": {"type": "BOOLEAN",
                        "description": "posture reminders (default true)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "posture_guard",
    "title": "Posture guard",
    "fields": [
        {"key": "eye_break_minutes", "label": "Minutes between eye breaks", "type": "text",
         "default": "20"},
        {"key": "sensitivity", "label": "Sensitivity", "type": "choice",
         "options": ["gentle", "normal", "strict"], "default": "normal"},
    ],
}

SAMPLE_EVERY = 1.0          # seconds between looks
CALIBRATE_SECONDS = 6.0
SUSTAIN_SECONDS = 60.0      # a problem must last this long before we say anything
ISSUE_COOLDOWN = 15 * 60.0  # the same remark at most every 15 minutes
ANY_COOLDOWN = 4 * 60.0     # and any posture remark at most every 4 minutes
AWAY_RESETS_EYES = 5 * 60.0  # away from the screen this long counts as an eye rest

ISSUE_TEXT = {
    "close": "leaning much closer to the screen",
    "slump": "slumping lower in the chair",
    "tilt": "keeping the head tilted to one side",
}

_THRESHOLDS = {
    #            closer (width ratio), lower (face heights), tilt (degrees)
    "gentle": (1.40, 0.75, 20.0),
    "normal": (1.28, 0.55, 15.0),
    "strict": (1.18, 0.40, 11.0),
}


# ── Pure logic (unit-tested without a camera) ────────────────────────────────

def make_baseline(samples: list) -> Optional[dict]:
    """samples: dicts with w, cy, roll (roll may be None), frame_h.
    Median of each, so one bad frame does not spoil it."""
    if len(samples) < 4:
        return None
    rolls = [s["roll"] for s in samples if s.get("roll") is not None]
    return {
        "w": statistics.median(s["w"] for s in samples),
        "cy": statistics.median(s["cy"] for s in samples),
        "roll": statistics.median(rolls) if len(rolls) >= 3 else None,
    }


def assess(baseline: dict, sample: dict, sensitivity: str = "normal") -> set:
    """The problems visible in one sample compared with the baseline."""
    closer, lower, tilt = _THRESHOLDS.get(sensitivity, _THRESHOLDS["normal"])
    issues = set()
    bw = baseline["w"] or 1.0
    if sample["w"] / bw >= closer:
        issues.add("close")
    # slumping: face lower in the picture by a good part of a face height, while
    # not simply leaning in (leaning in also moves the face, so judge separately)
    elif (sample["cy"] - baseline["cy"]) / bw >= lower:
        issues.add("slump")
    if baseline.get("roll") is not None and sample.get("roll") is not None:
        if abs(sample["roll"] - baseline["roll"]) >= tilt:
            issues.add("tilt")
    return issues


class PostureJudge:
    """Turns per-sample issues into rare, sustained announcements."""

    def __init__(self, sustain: float = SUSTAIN_SECONDS, share: float = 0.7,
                 issue_cooldown: float = ISSUE_COOLDOWN, any_cooldown: float = ANY_COOLDOWN):
        self.sustain, self.share = sustain, share
        self.issue_cooldown, self.any_cooldown = issue_cooldown, any_cooldown
        self._hist: deque = deque()        # (t, issues)
        self._said: dict = {}
        self._last_any = -1e18

    def feed(self, t: float, issues: set) -> Optional[str]:
        self._hist.append((t, set(issues)))
        while self._hist and t - self._hist[0][0] > self.sustain:
            self._hist.popleft()
        if not self._hist or t - self._hist[0][0] < self.sustain * 0.9:
            return None                     # not enough history yet
        if t - self._last_any < self.any_cooldown:
            return None
        n = len(self._hist)
        for issue in ("close", "slump", "tilt"):
            hits = sum(1 for _, s in self._hist if issue in s)
            if hits / n >= self.share and t - self._said.get(issue, -1e18) >= self.issue_cooldown:
                self._said[issue] = t
                self._last_any = t
                self._hist.clear()
                return issue
        return None


class EyeBreakTimer:
    """Counts screen time while a face is seen; a long absence is a rest."""

    def __init__(self, every: float = 20 * 60.0, away_resets: float = AWAY_RESETS_EYES):
        self.every, self.away_resets = every, away_resets
        self.screen = 0.0
        self._last_t: Optional[float] = None
        self._last_seen: Optional[float] = None

    def feed(self, t: float, face_seen: bool) -> bool:
        if self._last_t is not None and face_seen:
            self.screen += min(t - self._last_t, 5.0)
        if face_seen:
            if self._last_seen is not None and t - self._last_seen >= self.away_resets:
                self.screen = 0.0
            self._last_seen = t
        self._last_t = t
        if self.screen >= self.every:
            self.screen = 0.0
            return True
        return False


# ── The watcher thread ───────────────────────────────────────────────────────

class _Guard:
    def __init__(self, eye_breaks: bool, posture: bool):
        self.eye_breaks = eye_breaks
        self.posture = posture
        self.stop_ev = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.state = "starting"
        self.baseline: Optional[dict] = None
        self.started = time.time()
        self.detector_kind = ""
        self.reminders = 0

    def _sample(self, detector, frame) -> Optional[dict]:
        from plugins import _faces
        small, s = _faces.shrink(frame, 480)
        f = _faces.largest(detector.detect(small))
        if f is None:
            return None
        return {"w": f.w / s, "cy": f.cy / s, "roll": f.roll_degrees(),
                "frame_h": frame.shape[0] / 1.0}

    def calibrate(self, detector) -> bool:
        from plugins import _camera
        samples = []
        end = time.time() + CALIBRATE_SECONDS
        while time.time() < end and not self.stop_ev.is_set():
            frame = _camera.latest(1.0)
            if frame is not None:
                smp = self._sample(detector, frame)
                if smp:
                    samples.append(smp)
            self.stop_ev.wait(0.3)
        self.baseline = make_baseline(samples)
        return self.baseline is not None

    def run(self) -> None:
        from plugins import _camera, _faces
        try:
            try:
                detector = _faces.FaceDetector(allow_download=True)
            except Exception as e:
                self.state = "failed"
                _announce.say(f"Posture watching could not start: the face detector "
                              f"could not start ({e}). Tell the user briefly.")
                return
            self.detector_kind = detector.kind
            if not _camera.subscribe("posture_guard"):
                self.state = "failed"
                _announce.say("Posture watching could not start because OpenCV is "
                              "missing. Tell the user briefly.")
                return
            if _camera.wait_frame(8.0) is None:
                self.state = "failed"
                _announce.say("Posture watching could not start: no picture came from "
                              f"the webcam ({_camera.last_error() or 'it may be in use or covered'}). "
                              "Tell the user briefly.")
                return
            self.state = "calibrating"
            if self.posture and not self.calibrate(detector):
                if self.stop_ev.is_set():
                    return
                self.state = "failed"
                _announce.say("Posture watching stopped: no face was seen while learning "
                              "how the user sits. The webcam has to see their face. Tell "
                              "them briefly.")
                return
            self.state = "watching"
            _announce.log("Posture guard: baseline learnt, watching.")
            sens = _setting("sensitivity", "normal")
            try:
                minutes = float(_setting("eye_break_minutes", "20") or 20)
            except (TypeError, ValueError):
                minutes = 20.0
            judge = PostureJudge()
            eyes = EyeBreakTimer(every=max(1.0, minutes) * 60.0)
            while not self.stop_ev.wait(SAMPLE_EVERY):
                frame = _camera.latest(2.0)
                if frame is None:
                    if _camera.last_error():
                        self.state = "failed"
                        _announce.say(f"Posture watching stopped: {_camera.last_error()}. "
                                      "Tell the user briefly.")
                        return
                    continue
                smp = self._sample(detector, frame)
                now = time.time()
                if self.eye_breaks and eyes.feed(now, smp is not None):
                    self.reminders += 1
                    _announce.say(
                        "The user has been at the screen for a while without resting their "
                        "eyes. In one short, friendly sentence suggest the 20-20-20 break: "
                        "look at something about six metres away for twenty seconds.")
                if self.posture and smp is not None and self.baseline:
                    issue = judge.feed(now, assess(self.baseline, smp, sens))
                    if issue:
                        self.reminders += 1
                        _announce.say(
                            f"For about a minute the user has been {ISSUE_TEXT[issue]}, "
                            "compared with how they sat when posture watching started "
                            "(seen through the webcam). In one short, warm, natural sentence "
                            "suggest they sit back and straighten up. No lecture.")
        except Exception as e:
            self.state = "failed"
            print(f"[posture_guard] watcher crashed: {e}")
            _announce.log(f"Posture guard stopped after an error: {e}")
        finally:
            from plugins import _camera as cam
            cam.unsubscribe("posture_guard")
            if self.state != "failed":
                self.state = "stopped"


_guard: Optional[_Guard] = None
_glock = threading.Lock()


def _setting(key, default):
    try:
        from memory.config_manager import get_plugin_setting
        return get_plugin_setting("posture_guard", key, default)
    except Exception:
        return default


def _bool(v, default=True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "off")
    return bool(v)


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _guard
    try:
        _announce.remember(player)
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        with _glock:
            g = _guard
            alive = g is not None and g.thread is not None and g.thread.is_alive()
            if action == "start":
                from plugins import _camera
                if not _camera.available():
                    return ("Posture watching needs OpenCV, which is not installed "
                            "(pip install opencv-python).")
                eye = _bool(params.get("eye_breaks"), True)
                pos = _bool(params.get("posture"), True)
                if not eye and not pos:
                    return "Both posture and eye-break reminders were switched off, so there is nothing to watch."
                if alive:
                    g.eye_breaks, g.posture = eye, pos
                    return "Posture watching was already on; I updated what it reminds about."
                _guard = _Guard(eye, pos)
                _guard.thread = threading.Thread(target=_guard.run, name="posture-guard",
                                                 daemon=True)
                _guard.thread.start()
                if pos:
                    return ("Posture watching is starting. Ask the user to sit comfortably "
                            "upright for a few seconds while I learn how they sit; then it "
                            "runs by itself until they say stop.")
                return "Eye-break reminders are on; I'll suggest a short rest every twenty minutes."
            if action == "stop":
                if not alive:
                    return "Posture watching was not running."
                g.stop_ev.set()
                return f"Posture watching is off. I gave {g.reminders} reminder(s) this time."
            if action == "status":
                if not alive:
                    if g is not None and g.state == "failed":
                        return "Posture watching is off; the last attempt failed."
                    return "Posture watching is off."
                mins = int((time.time() - g.started) / 60)
                parts = [p for p, on in (("posture", g.posture), ("eye breaks", g.eye_breaks)) if on]
                return (f"Posture watching is {g.state} ({' and '.join(parts)}), running for "
                        f"{mins} minute(s), {g.reminders} reminder(s) so far.")
        return "Unknown action for posture watching; use start, stop or status."
    except Exception as e:
        return f"Posture watching failed: {e}"
