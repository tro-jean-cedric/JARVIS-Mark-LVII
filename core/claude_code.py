"""core/claude_code.py — Claude Code (the `claude` command) run headless.

One place that knows how to start the CLI, feed it a request and read what
comes back. Three callers use it:

  * providers/llm_claude_code.py   — Claude Code as a model in the providers
    panel: background requests, the CODING slot, ② THINKING, the task ladder.
    Those are ONE-SHOT answers (`ask`, `stream_text`): no tools, no settings.
  * actions/dev_agent.py           — the coding agent hands a whole project to
    Claude Code working in the folder (`run_agent`).
  * core/tasks/claude_task.py      — a background task run by Claude Code with
    its own web and file tools (`run_agent`).

WHY THE ONE-SHOT FLAGS LOOK THE WAY THEY DO
    Measured 2026-11-25, Claude Code 2.1.332, a one-word answer:
      plain `claude -p`                                  104,072 tokens, ~$0.10, 5.7 s
      + --tools "" --strict-mcp-config
        --disable-slash-commands --setting-sources ""
        + our own --system-prompt-file                      599 tokens, ~$0.0001, 1.2 s
    The difference is the user's plugins, MCP servers, skills and the coding
    system prompt, none of which a "summarise this page" request needs. Agent
    runs for coding DO keep the user's own setup (their CLAUDE.md, skills and
    login are why they chose Claude Code); background tasks do not.

WHY EVERYTHING GOES THROUGH STDIN AS stream-json
    A request can carry a screenshot, and a system prompt can be 15,000
    characters of tool declarations. stream-json input takes image blocks, and
    stdin has no length limit — the Windows command line stops at 32,767
    characters. The system prompt goes in a temporary file for the same reason.

Nothing here raises across a boundary except `ClaudeCodeError`, which the
callers catch: the model ladder reads its message to decide whether to rest
this rung (a usage limit is reported as "429 …" so it is rested like a quota).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

# The default for a request with no system prompt of its own. Claude Code's own
# prompt is a coding agent's and thousands of tokens long; a one-shot request
# wants an answer.
_PLAIN_SYSTEM = ("You answer the request directly and completely. You have no "
                 "tools in this conversation. Do not mention being Claude Code.")

# Flags that make a one-shot call cost what the request costs and nothing more.
# See the module docstring for the measurement.
_LEAN = ("--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "")

_found: dict[str, str] = {}


class ClaudeCodeError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Finding the command
# ─────────────────────────────────────────────────────────────────────────────
def _candidates() -> list[Path]:
    """Where the installers put it. A GUI app started from the Dock or the
    Start menu does not inherit a terminal's PATH, so `which` alone misses the
    most common install (~/.local/bin) on exactly the machines that have it."""
    home = Path.home()
    out = [home / ".local" / "bin" / "claude",
           home / ".claude" / "local" / "claude",
           home / ".npm-global" / "bin" / "claude",
           Path("/opt/homebrew/bin/claude"),
           Path("/usr/local/bin/claude"),
           Path("/usr/bin/claude")]
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        out = [home / ".local" / "bin" / "claude.exe",
               appdata / "npm" / "claude.cmd",
               local / "Programs" / "claude" / "claude.exe",
               local / "AnthropicClaude" / "claude.exe"] + out
    return out


def find_binary(explicit: str = "") -> str:
    """The path to `claude`, or ''. An explicit path from the settings wins."""
    explicit = (explicit or "").strip().strip('"')
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.is_file() else ""
    if "auto" in _found:
        return _found["auto"]
    hit = shutil.which("claude") or ""
    if not hit:
        for cand in _candidates():
            if cand.is_file():
                hit = str(cand)
                break
    _found["auto"] = hit
    return hit


def available(explicit: str = "") -> bool:
    return bool(find_binary(explicit))


def version(explicit: str = "") -> str:
    exe = find_binary(explicit)
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=15, env=_env(), **_popen_kwargs())
        return (r.stdout or "").strip().split("\n")[0]
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Process plumbing
# ─────────────────────────────────────────────────────────────────────────────
def _env() -> dict:
    env = dict(os.environ)
    # Started from inside a Claude Code session (a developer running JARVIS from
    # one), the child would think it is nested and behave differently.
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _popen_kwargs() -> dict:
    """Own process group, so a timeout or a cancel stops everything Claude Code
    started (a test run, a dev server) and not only the CLI itself. No console
    window flashing up on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def user_message(text: str, images: list[tuple[str, str]] | None = None) -> str:
    """One stream-json input line. `images` are (mime type, base64 data)."""
    content: list[dict] = [{"type": "text", "text": text or "(no text)"}]
    for mime, data in images or ():
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": data}})
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def _failure(result: dict, stderr: str) -> str:
    """A result event that reports an error, as one line the ladder can
    classify: usage limits read as 429, overload as 503."""
    text = " ".join(str(result.get("result") or result.get("subtype") or "").split())
    status = result.get("api_error_status")
    low = (text + " " + stderr).lower()
    if status == 429 or "usage limit" in low or "rate limit" in low or "limit reached" in low:
        return f"429 Claude Code usage limit — {text[:160]}"
    if status in (500, 502, 503, 529) or "overloaded" in low:
        return f"503 Claude Code is overloaded — {text[:160]}"
    if "log in" in low or "login" in low or "not logged" in low or "authenticat" in low:
        return ("Claude Code is not signed in — run `claude` once in a terminal "
                f"and log in ({text[:120]})")
    return f"Claude Code: {text[:200] or stderr[-200:] or 'failed'}"


def _base_cmd(exe: str, model: str) -> list[str]:
    cmd = [exe, "-p", "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    return cmd


def _write_temp(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="jarvis_cc_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _events(cmd: list[str], stdin_text: str, cwd: str | None, timeout: float,
            cancel: Optional[threading.Event] = None) -> Iterator[dict]:
    """Start the CLI and yield each JSON event it prints. Kills it on timeout
    or cancel and raises ClaudeCodeError when it ends without a result."""
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=_env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, **_popen_kwargs())
    except OSError as e:
        raise ClaudeCodeError(f"Claude Code would not start: {e}") from None

    err_buf: list[str] = []

    def drain_err():
        for line in proc.stderr:
            err_buf.append(line)
            del err_buf[:-200]

    def feed():
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    stop = threading.Event()
    why: list[str] = []

    def watchdog():
        deadline = time.monotonic() + timeout
        while not stop.wait(0.25):
            if cancel is not None and cancel.is_set():
                why.append("cancelled")
                _kill(proc)
                return
            if time.monotonic() > deadline:
                why.append(f"504 Claude Code did not finish within {timeout:.0f}s")
                _kill(proc)
                return

    for fn in (drain_err, feed, watchdog):
        threading.Thread(target=fn, daemon=True, name=f"claude-code-{fn.__name__}").start()

    got_result = False
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                got_result = True
            yield ev
        proc.wait(timeout=10)
    finally:
        stop.set()
        _kill(proc)
    if why:
        raise ClaudeCodeError(why[0])
    if not got_result:
        tail = "".join(err_buf).strip()
        print(f"[ClaudeCode] exited {proc.returncode} without an answer: {tail[-400:]}")
        raise ClaudeCodeError(_failure({}, tail) if tail
                              else f"Claude Code exited ({proc.returncode}) without an answer")


# ─────────────────────────────────────────────────────────────────────────────
#  One-shot answers — what the model ladder uses
# ─────────────────────────────────────────────────────────────────────────────
def _oneshot_cmd(exe: str, model: str, system_file: str, partial: bool) -> list[str]:
    cmd = _base_cmd(exe, model) + ["--tools", "", "--no-session-persistence",
                                   "--system-prompt-file", system_file, *_LEAN]
    if partial:
        cmd.append("--include-partial-messages")
    return cmd


def ask(text: str, *, system: str = "", images=None, model: str = "",
        timeout: float = 120.0, binary: str = "") -> str:
    """One answer, no tools. Raises ClaudeCodeError."""
    exe = find_binary(binary)
    if not exe:
        raise ClaudeCodeError("404 the `claude` command was not found — install Claude Code")
    sys_file = _write_temp(system or _PLAIN_SYSTEM)
    try:
        for ev in _events(_oneshot_cmd(exe, model, sys_file, False),
                          user_message(text, images), tempfile.gettempdir(), timeout):
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    raise ClaudeCodeError(_failure(ev, ""))
                return str(ev.get("result") or "")
    finally:
        _unlink(sys_file)
    return ""


def stream_text(text: str, *, system: str = "", images=None, model: str = "",
                timeout: float = 120.0, binary: str = "",
                cancel: Optional[threading.Event] = None) -> Iterator[str]:
    """The answer as it is written, piece by piece. Raises ClaudeCodeError."""
    exe = find_binary(binary)
    if not exe:
        raise ClaudeCodeError("404 the `claude` command was not found — install Claude Code")
    sys_file = _write_temp(system or _PLAIN_SYSTEM)
    streamed = False
    try:
        for ev in _events(_oneshot_cmd(exe, model, sys_file, True),
                          user_message(text, images), tempfile.gettempdir(),
                          timeout, cancel):
            kind = ev.get("type")
            if kind == "stream_event":
                delta = (ev.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed = True
                    yield delta["text"]
            elif kind == "result":
                if ev.get("is_error"):
                    raise ClaudeCodeError(_failure(ev, ""))
                if not streamed and ev.get("result"):
                    yield str(ev["result"])
                return
    finally:
        _unlink(sys_file)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Agent runs — Claude Code doing a job with its own tools
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AgentResult:
    ok: bool = False
    text: str = ""                 # Claude Code's final message
    error: str = ""
    session_id: str = ""           # `claude --resume <id>` continues it
    cost_usd: float = 0.0
    turns: int = 0
    seconds: float = 0.0
    denied: list[str] = field(default_factory=list)   # tools it asked for and was refused


def _hint(tool_input: dict) -> str:
    for key in ("file_path", "path", "command", "pattern", "url", "query",
                "description", "prompt"):
        v = (tool_input or {}).get(key)
        if v:
            return " ".join(str(v).split())[:120]
    return ""


def run_agent(prompt: str, cwd: str | Path, *,
              system_append: str = "",
              model: str = "",
              effort: str = "",
              tools: list[str] | None = None,
              allowed: list[str] | None = None,
              disallowed: list[str] | None = None,
              permission_mode: str = "acceptEdits",
              lean: bool = False,
              persist: bool = True,
              resume: str = "",
              max_budget_usd: float = 0.0,
              timeout: float = 1800.0,
              on_event: Optional[Callable[[str, str], None]] = None,
              cancel: Optional[threading.Event] = None,
              binary: str = "") -> AgentResult:
    """Run Claude Code on a job in `cwd` until it finishes. Never raises.

    `on_event(kind, text)` is called from this thread with kind "text" (what it
    says as it works), "tool" (a tool it is using, with a short hint) and
    "init" (its session id). `resume` continues an earlier session with
    `prompt` as the next message. `tools` limits which built-in tools exist at all;
    `allowed` are approved without asking — anything else that needs approval
    is refused, because nobody is at a terminal to say yes."""
    res = AgentResult()
    exe = find_binary(binary)
    if not exe:
        res.error = "the `claude` command was not found — install Claude Code and sign in"
        return res

    def emit(kind: str, text: str) -> None:
        if on_event is None or not text:
            return
        try:
            on_event(kind, text)
        except Exception:
            pass

    cmd = _base_cmd(exe, model) + ["--permission-mode", permission_mode]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        cmd += ["--disallowedTools", ",".join(disallowed)]
    if effort:
        cmd += ["--effort", effort]
    if max_budget_usd and max_budget_usd > 0:
        cmd += ["--max-budget-usd", f"{max_budget_usd:.2f}"]
    if resume:
        cmd += ["--resume", resume]
    if not persist:
        cmd.append("--no-session-persistence")
    if lean:
        cmd += list(_LEAN)
    sys_file = ""
    if system_append:
        sys_file = _write_temp(system_append)
        cmd += ["--append-system-prompt-file", sys_file]

    t0 = time.monotonic()
    try:
        for ev in _events(cmd, user_message(prompt), str(cwd), timeout, cancel):
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                res.session_id = str(ev.get("session_id") or "")
                emit("init", res.session_id)
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or ():
                    if block.get("type") == "text":
                        emit("text", " ".join(str(block.get("text") or "").split()))
                    elif block.get("type") == "tool_use":
                        name = str(block.get("name") or "tool")
                        hint = _hint(block.get("input") or {})
                        emit("tool", f"{name} {hint}".strip())
            elif kind == "result":
                res.session_id = str(ev.get("session_id") or res.session_id)
                res.cost_usd = float(ev.get("total_cost_usd") or 0.0)
                res.turns = int(ev.get("num_turns") or 0)
                res.denied = sorted({str(d.get("tool_name") or "?")
                                     for d in ev.get("permission_denials") or ()
                                     if isinstance(d, dict)})
                res.text = str(ev.get("result") or "").strip()
                if ev.get("is_error"):
                    res.error = _failure(ev, "")
                else:
                    res.ok = True
    except ClaudeCodeError as e:
        res.error = str(e)
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    finally:
        if sys_file:
            _unlink(sys_file)
        res.seconds = time.monotonic() - t0
    return res
