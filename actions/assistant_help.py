"""
JARVIS action — the assistant's own manual, and a live check of itself.

Asked "how do I change your voice?" or "why don't you hear me?", a model answers
from general knowledge — which means inventing buttons that do not exist. This
tool answers from manual/en.md, a short hand-written manual of what JARVIS
really is and where its settings really are, and with diagnose=true adds facts
read from this computer right now: which plugins loaded or broke, whether a
microphone and camera are visible, whether an API key is set, which engine is
in use.

FINDING THE RIGHT SECTIONS
    The manual is split on '## ' headings and every section is scored against
    the question by shared words (accents folded, short words ignored), with
    words in the heading counting more. Plain and local: no model call, and
    the manual is small enough that this is instant. A question that matches
    nothing gets the overview, which is the right answer to "what can you do".
"""
from __future__ import annotations

import platform
import re
import threading
import unicodedata
from pathlib import Path

_STOP = {
    "the", "and", "for", "you", "your", "can", "how", "what", "why", "does", "doesnt",
    "dont", "with", "this", "that", "are", "not", "is", "do", "to", "of", "in", "on",
    "it", "my", "me", "i", "a", "an", "be", "or", "where", "when", "which", "there",
    "work", "working", "use", "using", "set", "get", "from", "about", "have", "has",
}


def _manual_path() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base / "manual" / "en.md"


def _fold_words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z0-9]+", t)
    out = []
    for w in words:
        if len(w) < 3 or w in _STOP:
            continue
        # A crude stem: "plugins" ~ "plugin", "subtitles" ~ "subtitle".
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.append(w)
    return out


def sections() -> list[tuple[str, str]]:
    """[(heading, body)] from the manual. [] when it cannot be read."""
    try:
        text = _manual_path().read_text(encoding="utf-8")
    except Exception as e:
        print(f"[AssistantHelp] manual unreadable: {e}")
        return []
    out, head, body = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if head is not None:
                out.append((head, "\n".join(body).strip()))
            head, body = line[3:].strip(), []
        elif head is not None:
            body.append(line)
    if head is not None:
        out.append((head, "\n".join(body).strip()))
    return out


def best_sections(question: str, limit: int = 3) -> list[tuple[str, str]]:
    secs = sections()
    if not secs:
        return []
    q = set(_fold_words(question))
    scored = []
    for i, (head, body) in enumerate(secs):
        hw, bw = set(_fold_words(head)), _fold_words(body)
        bset = set(bw)
        score = 3.0 * len(q & hw) + 1.0 * len(q & bset)
        # Several mentions of a word in the body make it the topic of the section.
        score += 0.15 * sum(min(bw.count(w), 4) for w in q & bset)
        if "troubleshoot" in " ".join(hw) and q & {"not", "doesnt", "broken", "fail", "problem",
                                                    "error", "wrong", "hear", "stuck", "empty"}:
            score += 1.0
        scored.append((score, -i, head, body))
    scored.sort(reverse=True)
    hits = [(h, b) for s, _i, h, b in scored if s > 0][:limit]
    if not hits:
        overview = [s for s in secs if "overview" in s[0].lower() or "what jarvis is" in s[0].lower()]
        hits = overview[:2] or secs[:2]
    return hits


# ── Live facts ───────────────────────────────────────────────────────────────

def _diagnose(player) -> list[str]:
    facts = [f"- Computer: {platform.system()} {platform.release()}"]

    try:
        from memory.config_manager import (get_gemini_key, get_provider_mode, get_pipeline,
                                           get_input_device, load_api_keys)
        key = str(get_gemini_key() or "").strip()
        local_only = bool(load_api_keys().get("local_only"))
        facts.append("- Gemini API key: " + ("set" if key else "NOT set")
                     + (" (but 'run without Gemini' is chosen)" if local_only else ""))
        mode = get_provider_mode()
        if mode == "pipeline":
            pipe = get_pipeline() or {}
            facts.append(f"- Engine: own setup (hearing={pipe.get('stt') or '?'}, "
                         f"thinking={pipe.get('llm') or '?'}, voice={pipe.get('tts') or '?'})")
        else:
            facts.append("- Engine: Gemini Live")
        facts.append(f"- Chosen microphone: {get_input_device() or 'system default'}")
    except Exception as e:
        facts.append(f"- Settings could not be read: {e}")

    try:
        import sounddevice as sd
        devs = sd.query_devices()
        ins = [d for d in devs if d.get("max_input_channels", 0) > 0]
        outs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        facts.append(f"- Audio: {len(ins)} input device(s), {len(outs)} output device(s)"
                     + ("" if ins else " - NO MICROPHONE IS VISIBLE"))
    except Exception as e:
        facts.append(f"- Audio devices could not be listed: {e}")

    try:
        if player is not None and getattr(player, "muted", False):
            facts.append("- The microphone is MUTED in JARVIS (F4 or the mic button unmutes it)")
    except Exception:
        pass

    # The camera is not opened just to check it — only whether one is visible.
    try:
        sysname = platform.system()
        if sysname == "Linux":
            cams = sorted(Path("/dev").glob("video*"))
            facts.append(f"- Camera: {len(cams)} video device(s) in /dev" if cams
                         else "- Camera: none found in /dev")
        else:
            import importlib.util
            facts.append("- Camera support (opencv): " +
                         ("installed" if importlib.util.find_spec("cv2") else "NOT installed")
                         + " (the camera itself is not opened for this check)")
    except Exception:
        pass

    try:
        get_plugins = getattr(player, "get_plugins", None) if player is not None else None
        rows = get_plugins() if callable(get_plugins) else None
        if rows:
            on = [r["name"] for r in rows if r.get("valid") and r.get("enabled")]
            off = [r["name"] for r in rows if r.get("valid") and not r.get("enabled")]
            bad = [f"{r.get('file') or r.get('name')} ({str(r.get('error') or '')[:80]})"
                   for r in rows if not r.get("valid")]
            facts.append(f"- Plugins on ({len(on)}): {', '.join(on[:40]) or 'none'}")
            if off:
                facts.append(f"- Plugins switched off: {', '.join(off)}")
            if bad:
                facts.append(f"- Plugins that FAILED to load: {'; '.join(bad[:8])}")
        else:
            plug_dir = _manual_path().parent.parent / "plugins"
            files = sorted(p.stem for p in plug_dir.glob("*.py") if not p.name.startswith("_"))
            facts.append(f"- Plugin files present ({len(files)}): {', '.join(files[:40])}")
    except Exception as e:
        facts.append(f"- Plugins could not be listed: {e}")

    try:
        from core import screenview
        missing = screenview.missing_packages()
        if missing:
            facts.append(f"- Screen/app control is missing: {', '.join(missing)}")
    except Exception:
        pass

    try:
        from actions.action_history import _read
        recent = [e for e in _read()[-25:] if e.get("outcome") in ("failed", "problem")]
        if recent:
            facts.append("- Recent tool failures: " + ", ".join(
                f"{e.get('tool')}" for e in recent[-6:]))
    except Exception:
        pass
    return facts


_check_running = threading.Event()


def _full_check(player) -> str:
    """Run core/selftest.py on a thread (it takes several seconds); the
    report is shown on screen and a short summary spoken when it is done."""
    from core import outcome
    if _check_running.is_set():
        return outcome.started("assistant_help", what="the full check is already running")
    _check_running.set()

    def work():
        try:
            from core import selftest
            results = selftest.run(network=True)
            report = "\n".join(r.line() for r in results)
            print("[SelfTest]\n" + report)
            if player is not None:
                try:
                    player.show_content("SELF-CHECK", report)
                except Exception:
                    pass
                say = getattr(player, "request_say", None)
                if callable(say):
                    say("The full check of this computer has finished. Tell the user the "
                        "result briefly, in their language, naming each problem and its fix "
                        "in plain words (the full list is on screen): "
                        + selftest.summary(results))
        except Exception as e:
            print(f"[SelfTest] could not run: {e}")
        finally:
            _check_running.clear()

    threading.Thread(target=work, daemon=True, name="selftest").start()
    return outcome.started("assistant_help",
                           what="a full check of this computer (about ten seconds)",
                           next="let the user know briefly that the check is running; "
                                "its result appears on screen and is announced by itself")


def assistant_help(parameters: dict, player=None) -> str:
    p = parameters or {}
    if p.get("full_check"):
        return _full_check(player)
    question = str(p.get("question") or p.get("topic") or "").strip()
    diagnose = bool(p.get("diagnose"))

    hits = best_sections(question or "what can you do overview")
    if not hits:
        body = "The manual (manual/en.md) could not be read."
    else:
        body = "\n\n".join(f"## {h}\n{b}" for h, b in hits)
    out = ["FROM YOUR OWN MANUAL (this is about you, JARVIS):", body]
    if diagnose:
        try:
            out.append("LIVE CHECK OF THIS COMPUTER:\n" + "\n".join(_diagnose(player)))
        except Exception as e:
            out.append(f"LIVE CHECK failed: {e}")
    out.append("Answer the user from this, briefly and in their language; name the exact "
               "button or setting. Do not read this text out or paste it.")
    return "\n\n".join(out)


TOOL = {
    "name": "assistant_help",
    "description": (
        "Your own manual and a live check of yourself. Call it whenever the user asks about "
        "YOU - what you are, who made you, what you can do, how a feature or plugin is set "
        "up, where a setting is, privacy, the phone dashboard - and whenever they say "
        "something of yours does not work (then with diagnose=true; full_check=true when they "
        "ask for a full self-check or the quick one does not explain it). Never answer such "
        "questions from general knowledge or say you are 'just an AI'."
    ),
    "brief": "Your own manual (features, settings, privacy, troubleshooting); diagnose=true adds a live check.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING",
                         "description": "What the user asks about, in a few English words "
                                        "(e.g. 'microphone not hearing', 'privacy', 'plugins')"},
            "diagnose": {"type": "BOOLEAN",
                         "description": "true when something is not working: adds a live check "
                                        "(API key, engine, audio devices, camera, plugins, errors)"},
            "full_check": {"type": "BOOLEAN",
                           "description": "true when the user asks for a full self-check / "
                                          "self-test of the computer, or diagnose did not explain "
                                          "a problem: tests that libraries, HTTPS, the key, "
                                          "browser automation, video tools, audio, speaker "
                                          "capture, wake word and screen access really work, "
                                          "and says what to fix. Runs in the background."},
        },
    },
    "handler": assistant_help,
}
