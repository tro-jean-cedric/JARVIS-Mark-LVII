"""core/tasks/claude_task.py — a background task done by Claude Code.

An agent profile with `mode: claude_code` is not run round by round through
core/tasks/brain.py. The whole task goes to Claude Code (core/claude_code.py),
working in the task's own folder with the built-in tools the profile lists —
WebSearch, WebFetch, Read, Write… — and its final message is the result.

What the engine promises for every task still holds:

  budget      the time budget is Claude Code's deadline; the spending cap from
              the Claude Code settings applies too
  finish      "write it up now" stops the run and resumes the same session
              with one instruction: write the result from what you have
  cancel      stops it, no result
  steer       a note stops the current step and resumes the same session with
              the note as the next message — nothing it did is lost
  restart     the session id is stored on the task; after a crash or a quit
              the task resumes that session instead of starting again
  quota       a usage limit puts the task in waiting_quota, like any model

Claude Code runs lean here (no user MCP servers, skills or settings) so a task
behaves the same on every machine and costs only what it does.
"""
from __future__ import annotations

import threading
import time

_NOTIFY_EVERY = 90.0              # seconds between spoken progress lines
_MAX_RESUMES = 6                  # steer/finish restarts per run

_EXTRA_RULES = """
[WHERE YOU WORK]
- Your current folder is this task's own folder. Save anything longer than a few
  paragraphs there as a file and give its full path in the result.
- Files the user attached are in inputs/.
- Your last message is the result the user reads or hears. Make it complete on
  its own; do not end with a question.
"""


def _settings() -> dict:
    try:
        from memory.config_manager import get_provider_config
        return dict(get_provider_config("claude_code") or {})
    except Exception:
        return {}


def run(engine, tid: str, flags: dict, profile) -> None:
    from core import claude_code

    task = engine.store.get(tid)
    workdir = engine.store.workdir(tid)
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = _settings()

    tools = list(profile.tools)
    has_web = any(t in ("WebSearch", "WebFetch") for t in tools)
    system = engine._system_prompt(task, profile, has_web) + "\n" + _EXTRA_RULES

    now = engine._clock()
    engine.store.mutate(tid, lambda x: x.update(status="running",
                                                started=x.get("started") or now))
    if not task.get("started"):
        engine._notify("started", tid, "")
    engine._event(tid, "state", "running (Claude Code)")

    session = str(task.get("cc_session") or "")
    if session:
        prompt = ("[The app was restarted while you worked.] Carry on with the task "
                  "from where you were, then write the final result.")
    else:
        prompt = f"[TASK]\n{task['task']}"
        if task.get("files"):
            prompt += "\n\n[ATTACHED FILES, in inputs/]\n" + "\n".join(task["files"])

    used_s = float(task["used"]["seconds"])
    turns = int(task["used"]["rounds"])
    last_text = ""
    last_notify = 0.0
    finishing = bool(flags.get("finish") or task.get("wrap_up"))
    if finishing:
        prompt = ("[STOP — the user asked you to stop and write it up] Do not use any "
                  "more tools. Write the final result now from what you have, and say "
                  "plainly what is unfinished.")

    unannounced = ""

    def on_event(kind: str, text: str) -> None:
        # A line of text is only known to be PROGRESS once a tool follows it;
        # the last one is the result, and announcing it as progress first
        # would have the user hear it twice.
        nonlocal last_text, last_notify, session, unannounced
        if kind == "init" and text:
            session = text
            engine.store.update(tid, cc_session=text)
        elif kind == "tool":
            engine._event(tid, "tool", text)
            if unannounced and time.monotonic() - last_notify > _NOTIFY_EVERY:
                last_notify = time.monotonic()
                engine._notify("progress", tid, unannounced[:200])
            unannounced = ""
        elif kind == "text":
            last_text = unannounced = text
            engine._event(tid, "progress", text)

    for _attempt in range(_MAX_RESUMES + 1):
        budget_s = float(engine.store.get(tid)["budget"]["minutes"]) * 60
        left = max(60.0, budget_s - used_s) if not finishing else 180.0
        stop = threading.Event()
        why: list[str] = []

        def watch():
            while not stop.wait(0.5):
                fresh = engine.store.get(tid) or {}
                if flags.get("cancel") or flags.get("halt"):
                    why.append("cancel" if flags.get("cancel") else "halt")
                elif flags.get("finish") and not finishing:
                    why.append("finish")
                elif fresh.get("notes_in"):
                    why.append("steer")
                else:
                    continue
                cancel.set()
                return

        cancel = threading.Event()
        threading.Thread(target=watch, daemon=True, name=f"task-{tid}-watch").start()
        t0 = engine._clock()
        res = claude_code.run_agent(
            prompt, workdir, system_append=system,
            model=str(cfg.get("model") or "").strip(),
            effort=str(cfg.get("effort") or "").strip(),
            tools=tools, allowed=tools, permission_mode="acceptEdits",
            lean=True, persist=True, resume=session,
            max_budget_usd=_float(cfg.get("max_budget_usd")),
            timeout=left, on_event=on_event, cancel=cancel,
            binary=str(cfg.get("binary") or "").strip())
        stop.set()
        used_s += engine._clock() - t0
        turns += res.turns
        engine._account(tid, used_s, turns)

        reason = why[0] if why else ""
        if reason == "halt":
            return                                   # shutdown: resumes later
        if reason == "cancel":
            engine._finish(tid, "cancelled")
            return
        if reason == "steer":
            fresh = engine.store.get(tid)
            notes = list(fresh.get("notes_in") or [])
            engine.store.mutate(tid, lambda x: x.update(notes_in=[]))
            if not session:                      # stopped before it began
                prompt = f"[TASK]\n{task['task']}\n\n"
            else:
                prompt = ""
            prompt += ("[UPDATE FROM THE USER, while you work]\n"
                      + "\n".join(f"- {n}" for n in notes)
                      + "\nTake this into account and carry on.")
            continue
        if reason == "finish":
            finishing = True
            prompt = ("[STOP — the user asked you to stop and write it up] Do not use "
                      "any more tools. Write the final result now from what you have, "
                      "and say plainly what is unfinished.")
            continue

        if res.ok:
            result = res.text or last_text or "The task ended without a written result."
            made = sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*")
                          if p.is_file() and not p.relative_to(workdir).as_posix().startswith("inputs/"))
            if made:
                result += ("\n\nFiles from this task, in " + str(workdir) + ":\n"
                           + "\n".join(f"- {m}" for m in made[:30]))
            engine._complete(tid, result)
            return

        err = res.error or "Claude Code stopped without a result"
        if err.startswith("429") or err.startswith("503"):
            from core.tasks.engine import QUOTA_WAIT
            until = engine._clock() + QUOTA_WAIT
            engine.store.update(tid, status="waiting_quota", quota_until=until)
            engine._event(tid, "state", "waiting for Claude Code: " + err[:160])
            engine._notify("waiting", tid, err)
            return
        if err.startswith("504") and last_text:
            # The budget ran out mid-work: what it said last is better than nothing.
            engine._complete(tid, last_text + "\n\n(Stopped when the time budget ran out.)")
            return
        engine._finish(tid, "failed", error=err[:400])
        return

    engine._complete(tid, (last_text or "The task was interrupted too many times.")
                     + "\n\n(Stopped after repeated updates.)")


def _float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
