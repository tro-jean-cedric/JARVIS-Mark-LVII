# JARVIS — Mark LVII

A cross-platform (Windows / macOS / Linux) desktop voice assistant. It holds a
real-time spoken conversation (Gemini Live by default, or a local/third-party
STT → LLM → TTS pipeline), sees the screen, and drives the computer through
voice-callable tools. It also runs background research tasks and scheduled
routines, and can be controlled from a phone or a self-hosted server. The
design priorities: report what was *observed*, never what was attempted;
optional features cost nothing when unused; nothing crashes the voice session.

## Rules file — read first

`AGENTS.md` holds the hand-written rules for any agent changing this code
(threading, plugin contract, model calls, gui changes, config). **Read it
before writing code.** The app's own coding agent (`actions/dev_agent.py`)
also reads it via `core/code_checks.py`, so keep rules that agent needs in
AGENTS.md, not here.

## Tech Stack

- Python 3.11–3.13 (`setup.py` checks it). No package manifest, just
  `requirements.txt` with per-OS `sys_platform` markers.
- PyQt6 GUI (software-rendered HUD/avatar, no OpenGL), sounddevice + numpy audio.
- `google-genai` (Gemini Live + REST); `httpx` for OpenAI-compatible providers.
- FastAPI + uvicorn for the phone dashboard; Playwright for browser control.
- Persistence is plain JSON/Markdown files. No database.

## Project Structure

| Path | What belongs there |
|---|---|
| `main.py` | `JarvisLive`: voice session, reconnect loop, tool dispatch, inline tools |
| `ui.py` | `JarvisUI`, the thread-safe `player` facade; re-exports all `gui/` names |
| `gui/` | the Qt window (Qt main thread only); palette `gui/theme.py`, fonts `gui/style.py` |
| `actions/` | bundled tools that drive *the computer* (`TOOL` dict + handler) |
| `plugins/` | drop-in tools for everything else (`PLUGIN` + `run`); only `_template.py` ships |
| `core/` | shared services: `gemini.py` (all model calls), `outcome.py`, `undo.py`, `confirm.py`, loaders, avatar/audio |
| `core/tasks/`, `agents/` | background task engine and its agent profiles (`*.md`) |
| `core/routines/` | routine engine: briefing, check-ins, reminders, quiet hours |
| `providers/` | optional non-Gemini engines; never imported unless configured |
| `memory/` | `config_manager.py` (settings), `memory_manager.py` (user facts) |
| `dashboard/` | phone remote (FastAPI) |
| `core/prompt.txt` | the system prompt; `{tokens}` are filled at startup |

`core/llm_client.py`, `core/installer.py`, `core/stt.py` and `core/tts.py` are
dead legacy code with no importers. The readme's project tree lists plugins
that are not in this checkout.

## Commands

```bash
python setup.py                 # install deps for this OS + Playwright browsers
pip install -r requirements.txt # manual alternative
python main.py                  # run the app (first launch asks for a Gemini key)
python -m providers._tests      # providers self-test; needs deps, no network/key
python -m providers.schema      # tool-declaration/budget check; stdlib only
python -m core.cloud --help     # self-hosted server CLI (deploy/install/pair)
```

There is no pytest, linter config or CI. UI, audio and OS-automation changes
are verified by running the app.

## Conventions

- **Threads:** widgets only on the Qt main thread. Tools run on worker threads
  and reach the UI only through `player.*` methods, which emit a `pyqtSignal`.
- **Tool results:** return `core.outcome` lines (`done`/`unverified`/`failed`/
  `ask`/`started`). Say `done` only after checking it happened. Wrap outside
  text in `outcome.untrusted(...)`.
- **Reversibility:** reversible changes call `core.undo.push_undo`.
  Irreversible ones go through `core.confirm.request`, never through a
  model-supplied "confirmed" flag.
- **Never raise across a boundary.** Tools, loaders, config reads and model calls
  return a sentence or `None`. Callers of `core.gemini` must handle `None`.
- **Optional = free when unused.** Import heavy or optional deps lazily
  (`core/lazyimport.py` or inside the function). A missing extra disables one
  feature, not the app.
- **Settings:** go through `memory/config_manager.py`. Never write
  `config/api_keys.json` directly. It holds secrets.
- Prompt wording lives in `core/prompt.txt`, not in Python strings.
- Read a module's top docstring before "simplifying" it; many record measured
  reasons for odd-looking code.

## Additional Documentation

Read these when the task calls for it. Don't load them preemptively.

- `.claude/docs/tools_and_plugins.md`: read before adding or changing an
  action, plugin or inline tool, or changing what a tool returns.
- `.claude/docs/architectural_patterns.md`: read before touching startup, the
  voice session/reconnect loop, threading, or the `gui/` window and theming.
- `.claude/docs/model_and_providers.md`: read before calling a model from new
  code, editing `core/gemini.py`, or working in `providers/`.
- `.claude/docs/data_model.md`: read before changing anything saved to disk,
  the task engine, routines, or an `agents/*.md` profile.
- `.claude/docs/remote_surfaces.md`: read before touching `dashboard/`,
  `core/cloud.py`, pairing, tokens or TLS.
- `.claude/docs/testing_strategy.md`: read before adding tests or deciding how
  to verify a change.
