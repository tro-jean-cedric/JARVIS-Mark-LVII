# Prompt: keep adding Torvis features to JARVIS

Paste everything below the line into a new Claude Code session opened in this
repository. Change the first line if you want a specific feature instead of
the next one on the list.

---

Continue porting features from Torvis into JARVIS. Work on: **the next ⬜ item
at the top of the "To do" list in `progress.md`** (if it turns out to be much
bigger than its estimate, build a useful first slice and record the rest).

**Read first, in this order:** `progress.md` (what is done and what is left),
`AGENTS.md` (rules for code in this app), `CLAUDE.md`, the
`.claude/docs/*.md` file(s) that `CLAUDE.md` says apply to the area you touch,
and the `TORVIS_PARITY.md` row for the feature. Read a module's top docstring
before changing it.

**Where the specification comes from.** Torvis is a commercial app built with
Nuitka; its text constants are the only source. If
`$CLAUDE_JOB_DIR/tmp/torvis_x/Torvis.strings` (or another copy you are given)
does not exist, rebuild it from the installer kept outside git in `infos/`:

1. Copy the SquashFS image out of the AppImage into a new empty folder: the
   image starts right after the ELF section headers (look for the `hsqs`
   magic; `tail -c +<offset+1>`).
2. Read it with `PySquashfsImage` + `zstandard` installed in a throwaway venv
   in the job's temp folder, extract only `usr/lib/torvis/Torvis`.
3. `strings -n 4 Torvis > Torvis.strings`.
Never run anything from the installer. Find the feature's constants with
`grep -n` (module names look like `plugins._screen_eyes`, `core.uiact`; a
module's strings sit right after its name) and read nearby lines for tool
descriptions, help lines and messages. The REA skill
(`/rea:reverse-engineer-anything`) cannot read inside Nuitka code, so it is
only useful for other targets.

**Clean-room rule — required.** Torvis's text describes *behaviour*; it is not
material to reuse. Copy no code, prompts, user-facing strings, models or
assets. Write every string in your own words. Before committing, check every
file you added or changed with this script (it reports any run of six words
also found in the dump; the result must be `0 overlapping runs`, except for
text that already existed in JARVIS before you touched it):

```python
# save as $CLAUDE_JOB_DIR/tmp/find_copied.py; run:
#   python -I find_copied.py <repo> <Torvis.strings> <file> [<file> ...]
import ast, re, sys
from pathlib import Path
repo, N = Path(sys.argv[1]), 6
norm = lambda s: re.sub(r"\s+", " ", s.lower()).strip()
sh = set()
for line in open(sys.argv[2], encoding="utf-8", errors="replace"):
    if len(line) >= 30:
        w = norm(line).split()
        sh.update(" ".join(w[i:i + N]) for i in range(len(w) - N + 1))
hits = 0
for rel in sys.argv[3:]:
    for node in ast.walk(ast.parse((repo / rel).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            w = norm(node.value).split()
            for i in range(len(w) - N + 1):
                if " ".join(w[i:i + N]) in sh:
                    hits += 1
                    print(f"{rel}:{node.lineno}: {' '.join(w[i:i + N])}")
                    break
print(f"{hits} overlapping runs")
```

Models Torvis ships from third parties (OpenCV model zoo, MediaPipe, Silero)
are downloaded from their own upstream at first use, under their own licences,
like `plugins/_faces.py` does — never extracted from Torvis.

**How to build it (this app's rules):**
- Work in a git worktree branched from local `main`; commit there.
- Plugins/actions never touch Qt widgets; anything on screen goes through a
  thread-safe `player.*` method (new window code lives in `gui/`, driven by a
  `pyqtSignal` on `MainWindow` + a thin `JarvisUI` method).
- Tools return `core.outcome` lines; say `done` only after checking it
  happened; wrap outside text with `outcome.untrusted`. Reversible changes push
  undo; irreversible ones go through `core.confirm`.
- Model calls go through `core.gemini` and handle `None`. Optional or heavy
  dependencies are imported lazily; prefer what is already in
  `requirements.txt` — ask before adding a new dependency.
- Settings via `memory/config_manager.py` / `PLUGIN_SETTINGS`; never write
  `config/api_keys.json` directly.
- Keep the files' line endings (several are CRLF).

**Verify before calling it done:**
- Add or extend a self-test (`python -m plugins._<name>_tests`, or a section
  in `providers/_tests.py`) for the logic, with synthetic inputs.
- Run: `python -m providers._tests`, `python -m providers.schema`,
  `python -m plugins._holo_desk_tests`, `python -m plugins._face_lock_tests`,
  and `python core/_smoke_harness.py . '{"imports": [...], "plugins": [...]}'`
  for what you changed; `core.code_checks.static_problems` must report none.
- For anything on screen, render the real window off-screen
  (`QT_QPA_PLATFORM=offscreen`), save screenshots and look at them.
- Say plainly what could not be tested (real camera, real accounts, other OS).

**Finish by updating the records in the same commit:** move the item in
`progress.md` (⬜ → ✅ or 🟡 with what is missing, add the commit), add a line
to its Log, update the feature's row in `TORVIS_PARITY.md`, and mention any
new self-test in `.claude/docs/testing_strategy.md`. Then report what was
built, how it was verified, and the next item on the list.
