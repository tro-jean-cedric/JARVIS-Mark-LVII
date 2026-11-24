"""
pushup_counter — counts push-ups live through the shared webcam.

Two ways to see a rep, best first:
  * pose  — MediaPipe Pose (optional: pip install mediapipe). The elbow angle
            goes below ~100 degrees at the bottom and above ~150 at the top.
  * face  — no extra package. With the camera on the floor in front of the
            user, the face moves down and back up once per rep; a rep is a dip
            of more than half a face height followed by a return to the top.
Both feed the same hysteresis counter, so jitter around one threshold never
counts twice.

run() returns at once; the counting runs on its own thread. Counts are spoken at
milestones, the session ends on 'stop' (which returns the total) or by itself
when the user has gone, and every session is kept in memory/plugin_data.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from typing import Optional

from plugins import _announce

PLUGIN = {
    "name": "pushup_counter",
    "description": (
        "Counts the user's push-ups live through the WEBCAM. Use when the user is "
        "about to do push-ups and wants them counted: 'count my pushups', 'pushup "
        "challenge', 'I'll do 20 pushups'. action='start' (pass the goal as target "
        "if they said one), 'stop' when they say they are done (returns the total "
        "- then congratulate them with light, playful teasing about the number), "
        "'status' for the count so far, 'history' for past sessions. Counts are "
        "announced by themselves during the set. Never use the screen or camera "
        "snapshot tools to count push-ups."
    ),
    "brief": "Live webcam push-up counter: start / stop / status / history.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, stop, status or history"},
            "target": {"type": "INTEGER", "description": "rep goal if the user named one"},
        },
        "required": ["action"],
    },
}

MAX_SESSION = 20 * 60.0        # a forgotten session ends after 20 minutes
IDLE_END = 45.0                # no rep and no user in view for this long ends it
FPS = 10.0
HISTORY_KEY = "pushup_counter"


# ── Pure logic (unit-tested without a camera) ────────────────────────────────

class HysteresisCounter:
    """Counts one rep each time `value` goes below `low` and then back above
    `high`. min_gap stops a single bounce counting twice."""

    def __init__(self, low: float, high: float, min_gap: float = 0.45):
        assert low < high
        self.low, self.high, self.min_gap = low, high, min_gap
        self.down = False
        self.count = 0
        self._last = -1e18

    def feed(self, value: float, t: float) -> bool:
        if value is None:
            return False
        if not self.down and value <= self.low:
            self.down = True
        elif self.down and value >= self.high:
            self.down = False
            if t - self._last >= self.min_gap:
                self.count += 1
                self._last = t
                return True
        return False


class FaceDipSignal:
    """Turns the face's vertical position into 'how far below the top', in face
    heights (0 = at the top, -1 = one face height lower).

    The top is the highest recent position: it follows the face up at once and
    drifts down only slowly, so a camera nudge or a new stance is learnt within
    a few seconds while a dip is not mistaken for a new top."""

    def __init__(self, drift: float = 0.02, smooth: float = 0.5):
        self.top: Optional[float] = None
        self.size: Optional[float] = None
        self.value: Optional[float] = None
        self.drift, self.smooth = drift, smooth

    def feed(self, cy: float, face_h: float) -> float:
        self.size = face_h if self.size is None else 0.9 * self.size + 0.1 * face_h
        self.value = cy if self.value is None else (
            self.smooth * self.value + (1 - self.smooth) * cy)
        if self.top is None or self.value < self.top:
            self.top = self.value
        else:
            self.top += (self.value - self.top) * self.drift
        return -(self.value - self.top) / max(self.size, 1e-6)


def angle(a, b, c) -> float:
    """Angle ABC in degrees for 2-D points."""
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n == 0:
        return 180.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cos))


def milestone(count: int, target: Optional[int]) -> bool:
    if target and count == target:
        return True
    return count > 0 and count % 5 == 0


# ── Detectors ────────────────────────────────────────────────────────────────

def _make_pose():
    """(signal_fn, counter, mode) using MediaPipe Pose, or None."""
    try:
        import mediapipe as mp  # optional
        pose = mp.solutions.pose.Pose(model_complexity=0, min_detection_confidence=0.5,
                                      min_tracking_confidence=0.5)
    except Exception:
        return None
    import cv2

    def signal(frame):
        res = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        lm = getattr(res, "pose_landmarks", None)
        if lm is None:
            return None
        p = lm.landmark
        best = None
        for s, e, w in ((11, 13, 15), (12, 14, 16)):       # left / right arm
            vis = min(p[s].visibility, p[e].visibility, p[w].visibility)
            if vis < 0.5:
                continue
            ang = angle((p[s].x, p[s].y), (p[e].x, p[e].y), (p[w].x, p[w].y))
            if best is None or vis > best[0]:
                best = (vis, ang)
        return None if best is None else best[1]

    return signal, HysteresisCounter(100.0, 150.0), "pose (elbow angle)"


def _make_face():
    from plugins import _faces
    det = _faces.FaceDetector(allow_download=True)
    dip = FaceDipSignal()

    def signal(frame):
        small, s = _faces.shrink(frame, 360)
        f = _faces.largest(det.detect(small))
        if f is None:
            return None
        return dip.feed(f.cy, f.h)

    return signal, HysteresisCounter(-0.55, -0.2), "face tracking (camera on the floor in front of you)"


# ── Session ──────────────────────────────────────────────────────────────────

class _Session:
    def __init__(self, target: Optional[int]):
        self.target = target
        self.count = 0
        self.mode = ""
        self.started = time.time()
        self.stop_ev = threading.Event()
        self.done = threading.Event()
        self.error = ""
        self.thread: Optional[threading.Thread] = None
        self.ended_by = ""

    def run(self) -> None:
        from plugins import _camera
        try:
            made = _make_pose()
            if made is None:
                made = _make_face()
            signal, counter, self.mode = made
            if not _camera.subscribe("pushup_counter"):
                self.error = "OpenCV is not installed"
                return
            if _camera.wait_frame(8.0) is None:
                self.error = ("I couldn't get a picture from the camera; it may be in use "
                              "by another app or covered")
                _announce.say(f"Push-up counting could not start: {self.error}. Tell the user.")
                return
            _announce.log(f"Push-up counter: counting with {self.mode}.")
            last_seen = last_rep = time.time()
            while not self.stop_ev.wait(1.0 / FPS):
                now = time.time()
                if now - self.started > MAX_SESSION:
                    self.ended_by = "time limit"
                    break
                frame = _camera.latest(1.0)
                val = signal(frame) if frame is not None else None
                if val is not None:
                    last_seen = now
                if counter.feed(val, now):
                    self.count = counter.count
                    last_rep = now
                    p = _announce.player()
                    if p is not None:
                        try:
                            p.write_log(f"Push-ups: {self.count}")
                        except Exception:
                            pass
                    if milestone(self.count, self.target):
                        if self.target and self.count == self.target:
                            _announce.say(f"The user just reached their goal of {self.target} "
                                          "push-ups. Cheer in a few words; they may keep going.")
                        else:
                            _announce.say(f"Push-up count: {self.count}. Say just the number "
                                          "with two or three words of encouragement.")
                if now - max(last_seen, last_rep) > IDLE_END and now - last_rep > IDLE_END:
                    self.ended_by = "nobody in view"
                    break
            if not self.stop_ev.is_set():
                # Ended by itself: the user is not waiting on a tool result.
                _announce.say(_summary_instruction(self))
        except Exception as e:
            self.error = str(e)
            print(f"[pushup_counter] session crashed: {e}")
            _announce.say(f"Push-up counting stopped after an error ({e}). Tell the user briefly.")
        finally:
            _camera.unsubscribe("pushup_counter")
            if self.count or not self.error:
                _record(self)
            self.done.set()


def _summary_instruction(s: _Session) -> str:
    best = _best()
    if s.count == 0:
        return ("The push-up session ended with zero push-ups counted. Tell the user, "
                "and remind them the camera must clearly see them (or their face, in "
                "face-tracking mode).")
    goal = f" Their goal was {s.target}." if s.target else ""
    return (f"The push-up session is over: {s.count} push-ups.{goal} Best ever: {best}. "
            "Tell the user in their language with playful, lightly teasing encouragement "
            "about the number.")


def _history() -> dict:
    h = _announce.load(HISTORY_KEY, {}) or {}
    if not isinstance(h, dict):
        h = {}
    h.setdefault("sessions", [])
    return h


def _best() -> int:
    return max([int(x.get("count", 0)) for x in _history()["sessions"]] or [0])


def _record(s: _Session) -> None:
    if s.count <= 0:
        return
    try:
        h = _history()
        h["sessions"].append({
            "date": datetime.now().isoformat(timespec="seconds"),
            "count": s.count,
            "target": s.target,
            "seconds": int(time.time() - s.started),
            "mode": s.mode,
        })
        h["sessions"] = h["sessions"][-500:]
        _announce.save(HISTORY_KEY, h)
    except Exception as e:
        print(f"[pushup_counter] could not save history: {e}")


_session: Optional[_Session] = None
_lock = threading.Lock()


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _session
    try:
        _announce.remember(player)
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        with _lock:
            s = _session
            alive = s is not None and s.thread is not None and s.thread.is_alive()
            if action == "start":
                from plugins import _camera
                if not _camera.available():
                    return ("Push-up counting needs OpenCV, which is not installed "
                            "(pip install opencv-python).")
                if alive:
                    return f"Already counting: {s.count} so far."
                target = None
                try:
                    target = int(params.get("target")) if params.get("target") else None
                except (TypeError, ValueError):
                    target = None
                _session = _Session(target)
                _session.thread = threading.Thread(target=_session.run, name="pushup-counter",
                                                   daemon=True)
                _session.thread.start()
                goal = f" Goal: {target}." if target else ""
                return ("Counting push-ups now - get in front of the camera (with no pose "
                        "model installed it follows your face, so put the camera low in "
                        f"front of you). I'll call out the count.{goal} Say stop when done.")
            if action == "stop":
                if not alive:
                    if s is not None:
                        return f"No session is running. The last one counted {s.count}."
                    return "No push-up session is running."
                s.stop_ev.set()
                s.done.wait(3.0)
                best = _best()
                goal = f" (goal {s.target})" if s.target else ""
                if s.count == 0:
                    return ("Session over: zero push-ups counted. The camera must clearly "
                            "see the user (or their face in face-tracking mode).")
                return (f"Session over: {s.count} push-ups{goal}. Best ever: {best}. "
                        "Congratulate the user with playful, light teasing about the number.")
            if action == "status":
                if not alive:
                    return "No push-up session is running."
                return f"{s.count} push-ups so far, counting with {s.mode or 'the camera'}."
            if action == "history":
                sess = _history()["sessions"]
                if not sess:
                    return "No push-up sessions are saved yet."
                last = sess[-5:]
                total = sum(int(x.get("count", 0)) for x in sess)
                lines = ", ".join(f"{x['date'][:10]}: {x['count']}" for x in last)
                return (f"{len(sess)} sessions, {total} push-ups in all, best {_best()}. "
                        f"Latest: {lines}.")
        return "Unknown action; use start, stop, status or history."
    except Exception as e:
        return f"Push-up counter failed: {e}"
