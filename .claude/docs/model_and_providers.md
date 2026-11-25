# Model calls and the providers layer

Read before calling a model from new code, touching `core/gemini.py`, or
working in `providers/` (local/third-party STT, LLM, TTS engines).

## One-shot model calls: `core/gemini.py`

All non-conversation model calls go through this module — it owns the model
ladder, deadlines, cooldowns and the user's chosen sidecar provider.

- `gemini.call(contents, tier=FAST, config=None, timeout_ms=10_000, key="", provider="")`
  — `core/gemini.py:714`. Returns the SDK response or **`None`** when every
  rung failed. Only `call` accepts `provider=`.
- `gemini.text(...)` (`:814`, default `""`) and `gemini.as_json(...)` (`:824`,
  default `None`; tolerant parser `parse_json` strips `<think>` and fences).
- Tiers `FAST`, `SMART`, `SEARCH` (`:84-86`). An unknown tier string is treated
  as an explicit model name tried first.
- Ladders `_LADDERS` (`:168`): FAST/SMART start with a `LIVE` rung (a
  throwaway Live session, max 3 concurrent) then a list of `gemini-*` REST
  models; SEARCH is REST only. Requests carrying audio/video skip LIVE and
  provider rungs.
- Failures cool a model down: 429 → 5 min, 503/504 → 30 min, 404/403 → 6 h
  (`:256-263`). A refused key skips all remaining Gemini rungs.
- Sidecar provider: `providers.sidecar` in config becomes a `prov:<id>` rung
  after Gemini (before it if `sidecar_first`); with no Gemini key it is the
  whole ladder (`:737-759`).
- `MIN_TIMEOUT_MS = 10_000` — the API rejects shorter deadlines.

Callers must handle `None` with a small fallback. Actions mostly use
`gemini.call` (`actions/web_search.py:90`, `actions/flight_finder.py:165`).
`core/tasks/brain.py:140` builds its own tool-calling ladder from gemini's
private names (`_LADDERS`, `_cool`, `_provider_rungs`) — change those
carefully.

## Claude Code (`core/claude_code.py`, `providers/llm_claude_code.py`)

The `claude` CLI, run headless. One runner, three uses:

- **As a model** — provider id `claude_code` (kind `llm`, no catalog). Chosen
  under BACKGROUND REQUESTS, CODING or ② THINKING like any provider, so every
  `gemini.call(..., provider=...)` and the task ladder can use it.
  `complete()`/`chat()` fold the OpenAI-style messages into one stream-json
  request (images included), run with `--tools ""` and the lean flags
  (`--strict-mcp-config --disable-slash-commands --setting-sources ""` plus our
  own system prompt file — ~600 tokens instead of ~104k), and describe tools in
  the prompt (`tool_format: "prompt"`). A usage limit raises `429 …` so the
  ladder rests the rung like a quota error.
- **As the coding agent** — when CODING is `claude_code`, `dev_agent` improve
  and build jobs go to `_claude_code_job`: Claude Code works in the folder
  (`acceptEdits`, Bash only if "Coding jobs may run commands"), and dev_agent
  snapshots before, diffs the disk after, restores protected files, backs up
  originals to `.jarvis_backup/`, pushes one undo entry and re-runs syntax,
  profile checks and tests against the pre-change baseline.
- **As a background agent** — `agents/claude_code.md` (`mode: claude_code`),
  see `data_model.md`.

Settings live under `providers.creds.claude_code` (model, allow_commands,
effort, max_budget_usd, job_minutes, binary). The binary is found on PATH or
in the usual install folders (`find_binary`). Tests: section [20] of
`python -m providers._tests` drives the runner against a fake `claude` script.

## The conversation engine

Default: Gemini Live, opened in `main.py:_open_session` (`main.py:2647`); model
chosen by `gemini.live_model()` / `note_live_failure`. When config
`providers.mode == "pipeline"`, `_resolve_engine_caps` (`main.py:2587`) imports
`providers.registry` and `_open_session` builds a
`providers.engine_pipeline.PipelineEngine` (`providers/engine_pipeline.py:106`)
instead. Otherwise **nothing in `providers/` is imported** — keep it that way
(guarded by `test_costs_nothing_when_unused` in `providers/_tests.py`).

## providers/ contract (`providers/base.py`)

- Design rule (`providers/base.py:30-43`): *a provider speaks Gemini Live's
  dialect, never the reverse.* main.py is unchanged; providers emit
  Live-shaped events (`Msg`, `ServerContent`, `ToolCall`, `providers/base.py:156-212`)
  whose every attribute exists so main.py's truthiness checks work.
- Protocols: `Engine` (`:218`), `LLM.chat` (`:247`), `STT.transcribe` (`:262`),
  `TTS.speak` → 24 kHz mono PCM (`:276`). Capabilities in `DEFAULT_CAPS`
  (`:71`); `max_tool_chars` is used but not listed there.
- Each module declares `PROVIDER = {id, kind, label, factory, ...}`
  (`:297-312`); `validate()` returns a reason string, never raises. Heavy
  imports go **inside `factory()`**.
- Voice providers include `tts_openai` (Kokoro / OpenAI / any /v1/audio/speech; raw 24 kHz PCM, WAV fallback).
- `providers/registry.py`: modules are listed in `_MODULES` (`:35`) — **add new
  provider modules there**; `discover(kind)` runs only when the settings panel
  opens; `build()` (`:133`) caches by config fingerprint. Per-service ids look
  like `openai_compat@groq` (`split_id`).
- `providers/schema.py`: converts Gemini tool declarations to
  OpenAI/Anthropic/prompt form (`convert`, `:240`) and trims the tool list for
  small models (`budget`, `:272` — ranks by `tier`, uses `brief`, fills until
  `max_tools`/`max_tool_chars` bite). `python -m providers.schema` prints
  the result for the real tool set.
- `PipelineEngine` send methods return immediately and do work as tasks,
  because main.py calls `send_tool_response` from inside its own `receive()`
  loop — blocking there deadlocks.

Settings for all of this live under the `providers` key of
`config/api_keys.json` via `config_manager.get_providers` / `get_provider_config`
/ `save_providers` — see `data_model.md`.
