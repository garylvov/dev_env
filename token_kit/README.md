# Token Kit

Token Kit keeps work in task folders and starts a fresh Claude or Codex session
when context reaches 60%. Save the work in the folder so the next session can continue.

## Everyday use

```bash
token-kit run "Fix parser" --engine codex --prompt "Investigate and fix the parser failure"
token-kit continue retread --engine codex
token-kit run "My task" --engine claude
```

Without `--prompt`, a new task opens an idle session. New tasks default to Claude
and 60% rollover; continuation reuses saved settings unless overridden. Use
`--rollover-perc 70` or `--rollover-tokens 100k` to choose another threshold,
`--no-rollover` to disable restarts, and `--help` for all arguments.
`pick` selects a task; `status` displays records; `resume` is a read-only preview.
Run unattended with `token-kit run "Title" --engine codex --headless --prompt "..."`; Claude headless uses its print mode.
`token-kit continue NAME --headless` reuses saved options; `--no-headless` restores interactive launches. If hooks do not arm, Codex telemetry triggers a summary turn after client exit, then a fresh successor; missing telemetry stops with an error.
Client stdout/stderr streams into `.token-kit/runs/<run>/stdout.log`; the launcher prints each log path and returns the final client exit code.
Pass `--yolo` explicitly (Codex: `--dangerously-bypass-approvals-and-sandbox`) or choose `--sandbox workspace-write`; headless never enables bypass automatically.

Client trust and permissions remain in effect unless you pass `--yolo`; with Codex
it also adds `--dangerously-bypass-hook-trust` so the rollover hooks fire.

```text
<task>/
  <task>_in.md             verbatim conversation of user messages, append-only
  asks.md                 every request upsampled into Open/Done lines
  <task>_out.md            running state and results
  models.md               main-agent model choices, sources and dated probes
  docs/                   longer findings and decisions, linked from output
  artifacts/              optional
  agents/<name>_in.md      optional worker assignment
  agents/<name>_out.md     optional worker progress
  .token-kit/             launcher records
```

The filenames use the actual task folder name. Managed `UserPromptSubmit` hooks
append every user message verbatim, dated and numbered, before the agent works;
the file is never edited. Initial assignments are saved on creation; Token Kit's
kickoff and successor prompts are excluded. `asks.md` holds status: every request
in every message gets a line (asides too), open asks first, superseded or
conflicting asks are marked with which one wins. Agents re-read it before ending a
turn and before rollover, and the final reply lists open asks. If managed hooks are
unavailable, the agent must record messages itself.

Read input and output first at session start; re-read input about every five
minutes and when edited. Keep a sparse `Current state` at the output's top: done,
in progress, next and open decisions. Update it and append progress every few
minutes. After every ask append a `History` entry headed
`### #N <date> <first line of ask>` with a 2-6 line response linking results,
commits or files. History is append-only. Rollover requests are logged there too;
the summary request reminds the agent to cover every ask.

Put longer designs, findings and how-tos in `docs/<topic>.md`, linked from state
and history. Successors open docs only as needed; no index is required. Workers
follow the same rules. Existing `task.md`/`in.md`, `STATE.md` and `out.md` layouts
remain readable in place. No migration or checkpoint commands are needed.

[dispatch_guide.md](dispatch_guide.md) supplies model-neutral dispatch preferences and
command templates. The main agent chooses models in `<task>/models.md`, refreshing
missing or >~24h-old decisions from local catalogs and available official docs,
with bounded availability probes. Workers and rollover successors reuse it unless
stale or a model fails. Codex is preferred wherever capable and available;
cross-family verification still applies. The launcher injects this guidance for
both engines on startup and successor launch; it only passes a top-level model
when the operator explicitly supplies `--model`. The built-in missing-guide fallback
uses the same decision process.

The old trigger pyramid and matrix are archived in [docs/legacy/](docs/legacy/).
`src/token_kit/router/` remains for old installs; the simplified launcher does not
execute it. Its only router path checks detect or clean up legacy hooks.

## Rollover

At the threshold, a model-visible hook asks: “Context is at N%. Finish summarizing
everything into the task folder now,” naming the output and input files, asking
for remaining asks to be recorded, then asking the agent to stop. After verifying
the old client stopped, the launcher starts a successor with both files and saved
notes. Rollovers are unlimited unless a restart limit is set. Normal completion,
manual interruption and arbitrary errors do not restart.

Missing telemetry or untrusted hooks cause a diagnostic and leave native
compaction available. Active workers defer managed rollover. Missing fresh notes
produce a recovery limitation; process ownership checks prevent overlapping launches.
Check uncertain external outcomes before retrying operations.

Codex 0.159.0 empirically merges `$CODEX_HOME/hooks.json` with session `-c` hooks,
so user JSON hooks can coexist with rollover without being copied or overwritten.
Token Kit inspects home and ancestor project `.codex/hooks.json` files, including
legacy-hook conflicts; project-file execution was not observed in the disposable
probe. Overlapping TOML hook tables still disable automatic rollover because
session overrides could replace those arrays. Unreadable hook settings are reported.
For Codex, explicit `--yolo` also bypasses hook trust when the client supports it. Stop an unmanaged active session normally
before launching `continue`.

## Turning it on and off

Token Kit is opt-in per session. It never edits `~/.claude/settings.json`,
`~/.codex/config.toml` or `~/.codex/hooks.json`; its hooks are passed only to the
session it launches (Claude `--settings`, Codex `-c`) and vanish when it exits.
Plain `claude` and `codex` sessions, including ones already running, are unaffected.

- On: `token-kit run ...` or `token-kit continue ...`.
- Off for a session: start `claude` or `codex` directly.
- Keep recording but no restarts: `--no-rollover`.
- Uninstall: run the `rollback.py` command the installer printed; it removes the
  command and the `~/.bashrc` block.

## Installing

```bash
bash token_kit/install.sh --dry-run   # preview
bash token_kit/install.sh             # fresh machine
bash token_kit/install.sh --legacy-root /path/to/previous/checkout   # replacing an old install
```

The installer adds only the `token-kit` command (a symlink in `~/.local/bin` and a
PATH block in `~/.bashrc`, backed up first). See the
[cutover guide](docs/SIMPLE-CUTOVER.md) when replacing an older install.

Use `--dry-run` first to preview. The installer preserves a legacy launcher and
prints a rollback command for changed installations. Task files and client hook
configuration are untouched. Optional legacy cleanup previews owned changes and
backs them up; customized guidance and unrelated hooks survive. The
[legacy guide](docs/legacy-guide.md) describes the old workflow.

Automatic rollover requires growth since the first session telemetry sample of at least max(10% of the context window, 20,000 tokens), so very low thresholds are clamped by this startup loop guard. After two consecutive rollovers that add only rollover entries to the output, further automatic rollover is disabled and the successor continues with native compaction; the default restart count remains unlimited.

Startup and successor prompts pass task-file paths without embedding their contents. Read Current state and recent History; open docs/ only when needed. At rollover, whole older History entries above roughly 8,000 characters move verbatim to `docs/history.md` in chronological order, with a link left in the output. A single oversized newest entry remains intact.

## Repo docs

Knowledge that should outlive a task goes in the workspace repo's `docs/agents/`,
committed with the code: `README.md` is an index of one line per doc
(`- [topic](topic.md) -- hook`), and each doc starts with
`<!-- verified: <commit> files: <paths> -->`. Docs hold what code cannot say: how
to run and test, gotchas, decisions and why, dead ends. Agents promote durable
findings from the task's `docs/` there at rollover and when the task finishes.
At launch and rollover Token Kit passes only the index path and the names of docs
whose covered files changed after their verified commit (missing markers count as
stale). It never creates, edits or commits `docs/agents/` itself.

## CodeGraph

`--codegraph` (saved like other options; `--no-codegraph` turns it off) attaches the
[CodeGraph](https://github.com/colbymchenry/codegraph) MCP server to the launched
session only: Claude `--mcp-config`, Codex `-c mcp_servers.codegraph.*`. Without the
flag it attaches when the repo already has a `.codegraph/` index. Before launch and
at each rollover Token Kit runs `codegraph sync` (120 s cap); a failure prints a
warning and the session starts without the server. A worktree without an index
copies its main checkout's index and syncs it (ProtoMotions, 20k files, 310 MB index:
74 s fresh build vs 1.5 s copy and sync at the same commit, 14 s with 68 files
different; each copy costs the index size on disk); otherwise `--codegraph` builds one.
No client or project config is written; `/.codegraph/` goes in the local git exclude
file (never `.gitignore`), and CodeGraph telemetry is off (`CODEGRAPH_TELEMETRY=0`).
In another checkout (an agent-made worktree or clone) agents pass its root as
`projectPath`; unindexed checkouts get a "not indexed" reply and agents read files.
Install with `npm install -g @colbymchenry/codegraph`.
`token-kit codegraph-hooks MAIN` prints git hook lines that resync indexed
checkouts after merge, checkout and rebase; it installs nothing.

## Global agent rules

`AGENTS.md` is the single source of personal rules for Claude Code and Codex. Put it at `~/.config/agents/AGENTS.md`, make `~/.claude/CLAUDE.md` a one-line `@~/.config/agents/AGENTS.md` import, and run `sync_agents.sh` to copy everything above the `## Claude Code only` heading into `~/.codex/AGENTS.md`.
