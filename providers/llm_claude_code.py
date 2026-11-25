"""
Claude Code as a model — the `claude` command, signed in with the user's own
Claude account (or an API key it was set up with).

WHERE IT CAN BE CHOSEN
    Anywhere the providers panel offers a model: BACKGROUND REQUESTS (reading a
    document, looking at the screen, summarising a page), CODING (and then the
    coding agent hands whole projects to Claude Code — see actions/dev_agent.py),
    ② THINKING in pipeline mode, and through those the background-task ladder.

HOW IT TALKS
    Every request is one headless `claude -p` run through core/claude_code.py.
    The conversation is folded into one message, images included, and tools are
    described in the prompt (`tool_format: "prompt"`) — the CLI's own tools are
    switched off for these, so a request to summarise a page can never edit a
    file. That makes it slower to first word than an HTTP API (about a second
    to start the CLI): fine for background work and coding, noticeable in a
    spoken conversation.

COSTS NOTHING WHEN UNUSED
    Nothing here runs until the panel lists providers; the CLI is only looked
    for (never started) to decide whether to show "needs install".
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncIterator

from .base import DEFAULT_CAPS, Field, FunctionCall

# Aliases the CLI resolves to the newest model of each family, so this list
# does not go stale when a model is released. Typing a full id still works.
MODELS = ("", "sonnet", "opus", "haiku", "fable")


def _text_of(content: Any) -> tuple[str, list[tuple[str, str]]]:
    """(text, images) from one OpenAI-style message content."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content if isinstance(content, list) else [content]:
        if not isinstance(part, dict):
            texts.append(str(part))
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if url.startswith("data:") and ";base64," in url:
                head, data = url[5:].split(";base64,", 1)
                images.append((head or "image/png", data))
    return "\n".join(t for t in texts if t), images


def fold(messages: list[dict]) -> tuple[str, str, list[tuple[str, str]]]:
    """(system, prompt, images) from an OpenAI-style message list.

    A single user message — what nearly every background request is — goes
    through untouched. A conversation becomes a transcript ending in an
    instruction to write the next assistant turn."""
    system_parts, turns, images = [], [], []
    for m in messages or ():
        role = m.get("role")
        text, imgs = _text_of(m.get("content"))
        images.extend(imgs)
        if role == "system":
            system_parts.append(text)
            continue
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls = json.dumps({"tool": fn.get("name"), "args": args},
                                   ensure_ascii=False)
                text = (text + "\n" + calls).strip()
        if role == "tool":
            turns.append(("TOOL RESULT" + (f" ({m.get('name')})" if m.get("name") else ""),
                          text))
        else:
            turns.append(("ASSISTANT" if role == "assistant" else "USER", text))

    system = "\n\n".join(s for s in system_parts if s.strip())
    if len(turns) == 1 and turns[0][0] == "USER":
        return system, turns[0][1], images
    lines = ["The conversation so far:", ""]
    for who, text in turns:
        lines += [f"[{who}]", text, ""]
    lines.append("Write the ASSISTANT's next turn — only that, with no "
                 "[ASSISTANT] label.")
    return system, "\n".join(lines), images


def _tool_prompt(tools: list[dict]) -> str:
    from . import schema
    decls = [t.get("function", t) if isinstance(t, dict) else t for t in tools]
    return schema.to_prompt(decls)


def parse_call(text: str, known: set[str]) -> FunctionCall | None:
    """A {"tool": ..., "args": ...} reply, when the name is one it was given."""
    if not text or "{" not in text:
        return None
    from core.gemini import parse_json
    obj = parse_json(text)
    if not isinstance(obj, dict):
        return None
    name = str(obj.get("tool") or obj.get("name") or "").strip()
    if name not in known:
        return None
    args = obj.get("args") or obj.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return FunctionCall(id=f"cc_{name}", name=name,
                        args=args if isinstance(args, dict) else {})


class ClaudeCodeLLM:
    def __init__(self, cfg: dict):
        self.model = str(cfg.get("model") or "").strip()
        self.binary = str(cfg.get("binary") or "").strip()
        self.timeout = float(cfg.get("timeout") or 180.0)
        self.cfg = dict(cfg)
        self.caps = dict(DEFAULT_CAPS)
        self.caps.update({"tools": True, "tool_format": "prompt", "vision": True})

    def _request(self, messages, tools):
        system, prompt, images = fold(messages)
        if tools:
            system = (system + "\n\n" + _tool_prompt(tools)).strip()
        return system, prompt, images

    # -- one-shot: what core/gemini.py and core/tasks/brain.py use ----------
    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 *, timeout: float | None = None,
                 temperature: float | None = None) -> tuple[str, list[FunctionCall]]:
        from core import claude_code
        system, prompt, images = self._request(messages, tools)
        text = claude_code.ask(prompt, system=system, images=images,
                               model=self.model, binary=self.binary,
                               # the CLI takes a second or two to start, so a
                               # 10 s FAST deadline gets a little room
                               timeout=max(timeout or self.timeout, 30.0))
        if tools:
            known = {str((t.get("function") or t).get("name") or "")
                     for t in tools if isinstance(t, dict)}
            call = parse_call(text, known)
            if call is not None:
                return "", [call]
        return text, []

    # -- streaming: what the pipeline engine uses ---------------------------
    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   *, stream: bool = True) -> AsyncIterator[dict]:
        """Text as it is written. Tool calls come back as JSON in the text and
        the pipeline parses them (caps tool_format "prompt")."""
        from core import claude_code
        system, prompt, images = self._request(messages, tools)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()
        done = object()

        def work():
            try:
                for piece in claude_code.stream_text(
                        prompt, system=system, images=images, model=self.model,
                        binary=self.binary, timeout=self.timeout, cancel=cancel):
                    loop.call_soon_threadsafe(queue.put_nowait, piece)
            except Exception as e:                       # handed to the awaiting side
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=work, daemon=True, name="claude-code-chat").start()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    return
                if isinstance(item, Exception):
                    raise item
                yield {"text": item}
        finally:
            cancel.set()          # barge-in or a new turn: stop the CLI too

    # -- for the settings panel ---------------------------------------------
    def models(self, timeout: float = 6.0) -> list[str]:
        return [m for m in MODELS if m]

    def probe(self, check_vision: bool = False) -> tuple[bool, str]:
        import time
        from core import claude_code
        exe = claude_code.find_binary(self.binary)
        if not exe:
            return False, ("The `claude` command was not found. Install Claude "
                           "Code, run `claude` once to sign in, then TEST again.")
        t0 = time.monotonic()
        try:
            text = claude_code.ask("Reply with the single word: ok",
                                   model=self.model, binary=self.binary, timeout=60)
        except Exception as e:
            return False, str(e)[:150]
        if not text.strip():
            return False, "Claude Code answered with nothing."
        ver = claude_code.version(self.binary)
        return True, (f"Answered in {time.monotonic() - t0:.1f}s"
                      + (f"  |  {ver}" if ver else ""))


def _make(cfg: dict) -> ClaudeCodeLLM:
    return ClaudeCodeLLM(cfg)


PROVIDER = {
    "id": "claude_code",
    "kind": "llm",
    "label": "Claude Code",
    "hint": "Your Claude subscription, through the claude command",
    # Not a Python package: the panel shows "needs install" from available().
    "requires": ("the `claude` command (Claude Code)",),
    "needs": (
        Field("model", "Model", kind="choice", choices=MODELS, default="",
              required=False,
              hint="Empty = Claude Code's default. sonnet · opus · haiku · fable, or a full model id"),
        Field("allow_commands", "Coding jobs may run commands", kind="bool",
              default=True, required=False,
              hint="Lets Claude Code run tests and installs inside the project folder"),
        Field("effort", "Effort for coding jobs", kind="choice",
              choices=("", "low", "medium", "high", "xhigh", "max"), default="",
              required=False, advanced=True,
              hint="Empty = Claude Code's default"),
        Field("max_budget_usd", "Spending cap per job (USD)", kind="number",
              default=0, required=False, advanced=True,
              hint="0 = no cap. Applies to coding jobs and background tasks"),
        Field("job_minutes", "Time limit per coding job (minutes)", kind="number",
              default=30, required=False, advanced=True),
        Field("binary", "Path to claude", kind="path", default="", required=False,
              advanced=True, hint="Empty = found automatically"),
    ),
    "caps": {"tools": True, "tool_format": "prompt", "vision": True},
    "factory": _make,
}


def available() -> bool:
    from core import claude_code
    return claude_code.available()
