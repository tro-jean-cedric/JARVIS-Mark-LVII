# Persistent data, background tasks and routines

Read before changing anything that is saved to disk, the task engine
(`core/tasks/`), the routine engine (`core/routines/`), or an agent profile
(`agents/*.md`). There is no database; everything is JSON or Markdown files
created on first run.

## Files on disk (desktop)

| Path | Owner | Notes |
|---|---|---|
| `config/api_keys.json` | `memory/config_manager.py` | key + all settings. **Secret.** |
| `config/certs/` | `dashboard/server.py` | self-signed TLS pair, only if HTTPS opted in |
| `memory/long_term.json` | `memory/memory_manager.py` | what the assistant knows about the user |
| `memory/routines.json` | `core/routines/engine.py` | routines + quiet hours |
| `memory/tasks/` | `core/tasks/store.py` | task docs, checkpoints, workdirs |
| `reports/*.md` | `core/tasks/engine.py:466` | finished task reports |
| `agents/*.md` | `core/tasks/agents.py` | background agent profiles (committed) |

## config/api_keys.json

- Always via `memory/config_manager.py`: typed getters/setters
  (`get_voice`/`save_voice`, `get_plugin_config`/`save_plugin_config`,
  `get_providers`/`save_providers`, ...) or `update_config(**fields)`
  (`memory/config_manager.py:367`). Writes are atomic and under an `RLock`
  (`_write_config`, `:49`); an unparseable file is moved aside as
  `api_keys.json.broken-<ts>`.
- Main keys: `gemini_api_key`, `assistant_name`, `user_name`, `voice_name`,
  `ui_color`, `ui_scale`, `hud_style`, `wake_word_enabled`,
  `push_to_talk_enabled`, `input_device`/`output_device`, `plugins_enabled{}`,
  `plugin_config{namespace:{}}`, `providers{mode, pipeline, creds, sidecar,
  sidecar_first, coding}`, `dashboard_https`, `tasks_max_parallel`,
  `morning_brief_enabled`, `os_system`.
- Drift: ~15 modules read the file directly, and
  `actions/screen_processor.py:59` writes it in place. Don't add more.

## memory/long_term.json

`{identity, preferences, projects, relationships, wishes, notes}`, each
`key -> {"value": str ≤380, "updated": "YYYY-MM-DD"}`, plus `sessions` (last 3
summaries). API: `update_memory`, `remember`, `forget`, `search_memory`,
`format_memory_for_prompt` (`memory/memory_manager.py:194`, budgeted to ~900
chars + an index of the rest). Writes are **not** atomic and load→save isn't
under one lock — keep writers few.

## Task engine (`core/tasks/`)

- `TaskEngine.start(task, agent, minutes, rounds, ...)` —
  `core/tasks/engine.py:192`. States: queued/scheduled → running →
  waiting_quota → done/failed/cancelled. Scheduler thread `task-scheduler`
  runs up to `tasks_max_parallel` (default 2) worker threads.
- Each round = one model turn (`core/tasks/brain.py:140`) + tool calls; turns
  are checkpointed after every round, so a crash costs one step. On start,
  `running` tasks are re-queued (max 3 attempts).
- `cancel(finish=True)` gives one final tool-less round; `steer(note)` injects
  a user update next round.
- Store (`core/tasks/store.py`): `<id>.json`, `<id>.turns.json`, workdir
  `<id>/` (inputs, files, notes.md; research adds pages/, sources.json,
  findings.jsonl). Ids `t<MMDD><6hex>`. Use `write_json` (`:31`, atomic) /
  `read_json` (never raises).
- **Agents can only read and write their own workdir.** `core/tasks/tools.py:31`
  `KNOWN` is the allowlist (search_web, web_search, fetch_page, notes,
  write_file, file_processor, recall_memory); web tools and memory are never
  given to the same agent. Don't add tools that act on the computer.
- `core/tasks/web.py:52 check_url` refuses private/loopback addresses and
  re-checks every redirect.
- Research mode (`core/tasks/research.py`): PLAN → INVESTIGATE → REVIEW →
  WRITE → VERIFY; quotes are checked against saved page text in code
  (`quote_support`, `:111`).

## Agent profiles (`agents/*.md`)

Hand-parsed header (not YAML) between `---` lines, then the instructions body
(`AgentProfile`, `core/tasks/agents.py:39`): `name` (required,
`^[a-z][a-z0-9_-]{1,31}$`), `title`, `description`, `mode` (`""` or
`research`), `tools` (required, from `KNOWN`), `minutes` (1-720, default 10),
`rounds` (default 40; engine clamps to 400). Files starting `_`/`.` and
`README.md` are skipped. See `agents/researcher.md`.

## Routine engine (`core/routines/`)

- Rules (`core/routines/rules.py`): `once{at_ts}`, `weekly{time,days}`,
  `monthly{day,time}` (31 = last day), `interval{minutes≥5,start,end,days}`;
  days ISO 1=Mon. `next_fire` (`:53`) is strictly after `after`.
- Routine kinds `say`/`prompt`/`task`/`builtin`; triggers `time`, `startup`,
  `idle`. `RoutineEngine.add` — `core/routines/engine.py:187`. Thread
  `routines` ticks every 5 s; late routines past a per-kind grace window are
  skipped; one-offs are deleted after firing; built-ins are disabled, never
  removed.
- Quiet hours: one `"HH:MM-HH:MM"` window; only `quiet=True` routines wait.
- `core/routines/desktop.py` seeds the built-ins (briefing, check-in, news
  watch) once and mirrors the old `morning_brief_enabled` switch.
- The same engines run on the cloud server with roots under `cloud_state/`.
