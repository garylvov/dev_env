# Token Kit

Token Kit keeps work in task folders and starts a fresh Claude session
when context reaches 80%. Save the work in the folder so the next session can continue.
GPT models are used only as Shunt-backed Claude Code subagents, never as the main session.

## Everyday use

`tk` is short for `token-kit`. `tk my task` (quotes optional) continues the task named "my task", or starts it if none exists;
`tk r`, `tk c`, `tk s`, `tk p` and `tk l` are run, continue, status, pick and list.

```bash
token-kit run "Fix parser" --prompt "Investigate and fix the parser failure"
token-kit continue retread
token-kit run "My task"
```

New tasks default to `--model opus` and `--codegraph` (`--no-codegraph` or another `--model` overrides; saved per task).
A new task (or a folder whose sessions never started) opens with Claude's short overview of the task folder, delegation, rollover
and CodeGraph state (answered from its system prompt, no file reads), then waits for your
first message, or starts on `--prompt` if given. Headless runs skip the overview. New tasks default to 80%
rollover; continuation reuses saved settings unless overridden. Use
`--rollover-perc 70` or `--rollover-tokens 100k` to choose another threshold,
`--no-rollover` to disable restarts, and `--help` for all arguments.
Every session, including rollover successors, is named after the task (`claude --name`).
`pick` selects a task; `status NAME --full` is a read-only preview of the recovery text.
Run unattended with `token-kit run "Title" --headless --prompt "..."` (Claude print mode).
`token-kit continue NAME --headless` reuses saved options; `--no-headless` restores interactive launches.
Tasks saved by the old Codex engine continue on Claude with a one-line notice; their model, effort and `--yolo` are reset.
Client stdout/stderr streams into `.token-kit/runs/<run>/stdout.log`; the launcher prints each log path and returns the final client exit code.
Client permissions remain in effect unless you pass `--yolo`
(`--dangerously-skip-permissions`); headless never enables it automatically.

```text
<repo>/agent_tasks/<task>/   (outside git: ~/.config/token_kit/<task>/)
  <task>_in.md             verbatim conversation of user messages, append-only
  asks.md                 every request upsampled into Open/Done lines
  experiments.md          launched runs: Open/Closed lines with question, command, verdict
  <task>_out.md            running state and results
  models.md               main-agent model choices, sources and dated probes
  loops.md                hook-written session loops to recreate after rollover
  docs/                   longer findings and decisions, linked from output
  artifacts/              optional
  agents/<name>_in.md      worker prompt (hook-written for Agent-tool workers)
  agents/<name>_out.md     worker progress and final report
  .token-kit/             launcher records (never committed)
```

### Where tasks live

`token-kit run` inside a git repo creates the task in `<repo top>/agent_tasks/<task>/`,
so the repo's own git is the backup; outside git it uses `~/.config/token_kit/`, and
`--root DIR` always wins. Token Kit never commits: agents are told to commit the task
folder with their work commits. `agent_tasks/*/.token-kit/` goes in the repo's local
exclude file (`.git/info/exclude`). If the origin repo is public (`gh repo view`, 5 s cap;
failure counts as unknown), the whole task folder is excluded too, since `_in.md` holds
the operator's messages verbatim. If an existing ignore rule already hides `agent_tasks/`,
creation prints one note that the folder will not be backed up; the rule is left alone.

`continue`, `status` and `pick` search this repo's `agent_tasks/`, then every worktree's
(`git worktree list`), then `~/.config/token_kit/`. A copy in the current checkout wins;
the same task name found only in several other places is ambiguous and needs `--root`.
A task folder that is moved, cloned or checked out elsewhere works in its new checkout:
the workspace is stored relative to the repo (if that path is missing there, launch
stops and asks for `--workspace` rather than using the old checkout), and without
`.token-kit/` it is the checkout holding `agent_tasks/`. Every launch re-adds the
`.token-kit/` exclude line, so a fresh clone stays clean too.

The filenames use the actual task folder name. Managed `UserPromptSubmit` hooks
append every user message verbatim, dated and numbered, before the agent works;
the file is never edited. Initial assignments are saved on creation; Token Kit's
kickoff and successor prompts are excluded. `asks.md` holds status: every request
in every message gets a line (asides too), open asks first, superseded or
conflicting asks are marked with which one wins. Asks are grouped under feature headings, so many asks roll up into a few features. Agents re-read it before ending a
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

Worker records are automatic for Agent-tool subagents (including Shunt GPT agents
and background workers). When the Agent call returns, a hook writes
`agents/<name>_in.md` with the date, agentId, type, model and the prompt verbatim
(`<name>` is the Agent `name`, else the description plus a short agentId). When the
worker finishes, `SubagentStop` appends a dated `### final` section with its report
(a `SubagentHandback` message if seen, else its last message) and transcript path
to `agents/<name>_out.md`, and one `- worker <name> (<model>) finished: ...` line
to the task output. Workers append their own dated progress lines in between.
Raw `claude -p` workers have no hooks and are not recorded. Agent-tool workers live
inside the session, so ending it ends them: rollover waits until no subagent, teammate
or workflow is running (named workers report as `teammate`), naming them to Claude at each
turn end. After 30 minutes it proceeds anyway, ends them and says so in History; their
`agents/` records let the successor relaunch them. Long workers (submitters, pollers) run detached instead:
`setsid -f token-kit run --headless --prompt ... </dev/null >log 2>&1`.

`experiments.md` tracks runs the way `asks.md` tracks requests. A Bash `PreToolUse`
hook (main session and workers) matches launch commands (`systemd-run`, `sbatch`,
`srun`, `fleet`, `nohup`, `setsid`, `torchrun`, `accelerate launch`, `ray job submit`,
`train*.py`, `python -m ...train`; `LAUNCH_PATTERNS` in `simple_records.py`) and
appends `- [?] E<n> <date> unlabeled launch by <who>: <command>` to the end of the
file under `## Unlabeled`; it never rewrites the file. Status queries such as `fleet
status`, `squeue`, `pytest` or `cat train.py` are skipped. The hook never blocks a
command; recording errors land in the run's `control.json`. Agents label each line
with its question and where results land, move it to `## Open`, then to `## Closed`
with `verdict: <line> -> <doc link>` (or `why:` when abandoned). Each
launch and successor gets one line such as `Open experiments: E3 (running), E8
(unlabeled)` with ids only.

[dispatch_guide.md](dispatch_guide.md) supplies model-neutral dispatch preferences and
worker rules. The main agent builds `<task>/models.md` once, from official docs
with bounded availability probes; it stays fixed for the task (workers and rollover
successors reuse it, and a failing model falls back to the next listed option)
until the user asks for a rebuild. GPT options are Shunt subagents (when `shunt` is active);
cross-family verification uses them for Claude-authored work. The launcher injects
this guidance on startup and successor launch; it only passes a top-level model
when the operator explicitly supplies `--model`. The built-in missing-guide fallback
uses the same decision process.

The old trigger pyramid and matrix are archived in [docs/legacy/](docs/legacy/).
`src/token_kit/router/` remains for old installs; the simplified launcher does not
execute it. Its only router path checks detect or clean up legacy hooks.

## Rollover

At the threshold, a model-visible hook asks: “Context is at N%. Append a History
entry to <output> for the current ask and refresh Current state; list the open asks
from asks.md ... End your turn.” If native compaction starts while that summary is
being written, it proceeds and the launcher still waits for the end of the turn.
After verifying the old client exited, the launcher starts a successor with the
task file paths plus one line asking it to label new experiments and promote docs. Rollovers are unlimited unless a restart limit is set. Normal completion,
manual interruption and arbitrary errors do not restart.

Session loops (`/loop`, `CronCreate`, `ScheduleWakeup`) die with the client. At each
main-session turn end the `Stop` hook mirrors its `session_crons` into `<task>/loops.md`
(schedule, prompt, first-seen date) plus still-running background shells and monitors,
which cannot be resumed; the shell list always follows the live snapshot, and the file
disappears when nothing is scheduled, so deleted loops stay deleted. Rollover successors and `continue` get one kickoff line asking the model
to recreate the listed loops with its own tools. Loop firings are not recorded as asks.

Missing telemetry or untrusted hooks cause a diagnostic and leave native
compaction available. Active workers defer managed rollover. Missing fresh notes
produce a recovery limitation; process ownership checks prevent overlapping launches.
Check uncertain external outcomes before retrying operations.

Legacy Token Kit hooks in Claude settings block a launch until cleaned up; unrelated
hooks are preserved. Stop an unmanaged active session normally before launching `continue`.
`continue` against a live managed session waits for that session's turn to end; an
idle session never reaches that boundary, so exit it first (`/exit`).

Each client run appends `startup_tokens`, `peak_tokens` and whether it rolled over to
`<task>/.token-kit/usage.jsonl` and prints one summary line. `/clear` inside a managed
session starts a new session id; the hooks follow it and note it in History.

## Turning it on and off

Token Kit is opt-in per session. It never edits `~/.claude/settings.json`; its hooks
are passed only to the session it launches (`--settings`) and vanish when it exits.
Plain `claude` sessions, including ones already running, are unaffected.

- On: `token-kit run ...` or `token-kit continue ...`.
- Off for a session: start `claude` directly.
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

Rollover percentages apply to the model's documented window (1M for current Opus, Sonnet
and Fable). A cloud provider or remote gateway in `ANTHROPIC_BASE_URL` counts as 200K;
a loopback URL (a local pass-through proxy such as Shunt) keeps the documented window.
Managed launches set `"idleCompaction": false`: Claude Code otherwise compacts an idle
session before its prompt cache expires, at any size, and rollover replaces compaction.
Cross-session delivery notices are not recorded as asks.
A run whose supervisor died without recording its exit is marked `died` at the next launch.

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

## Docs layers

One fact, one home; every other doc links to it, never copies. Internal docs cite the repo docs for how to run things, and repo docs link to `docs/agents/` for detail.

- Repo docs (README, `docs/`, `--help`): high-level run info only, shipped with the code from the start, with an "In progress" status line until the feature is done.
- `docs/agents/`: implementation detail that outlives tasks (decisions and why, gotchas, dead ends).
- Task `docs/`: working notes. Promoting a note moves it and leaves a link.

## CodeGraph

`--codegraph` (saved like other options; `--no-codegraph` turns it off) attaches the
[CodeGraph](https://github.com/colbymchenry/codegraph) MCP server to the launched
session only (`--mcp-config`). Without the
flag a task saved before this default attaches only when the repo already has a `.codegraph/` index. Before launch and
at each rollover Token Kit runs `codegraph sync` (120 s cap); a failure prints a
warning and the session starts without the server. A linked worktree without its
own index attaches its main checkout's index in place: nothing is copied and Token Kit
never syncs main from a worktree. The agent is told CodeGraph answers with main's code
and to read files changed in the worktree (`git diff --name-only <merge-base>`) directly.
A worktree that has its own index keeps it. Token Kit never builds an index at launch:
with no index anywhere, the server is attached without one and the session is told to
work out its repo first, then run `codegraph init --yes` once in that repo's main checkout
before querying (launching from a folder of many repos must not index all of them).
`codegraph serve` is not byte-read-only: it starts one shared daemon per indexed path
that opens the database as its writer (and, with its file watcher, syncs it from main's files).
No client or project config is written; `/.codegraph/` goes in the local git exclude
file (never `.gitignore`), and CodeGraph telemetry is off (`CODEGRAPH_TELEMETRY=0`).
In another checkout (an agent-made worktree or clone) agents pass its root as
`projectPath`; unindexed checkouts get a "not indexed" reply and agents read files.
Install with `npm install -g --prefix ~/.local @colbymchenry/codegraph`.
`token-kit codegraph-hooks MAIN` prints git hook lines that resync indexed
checkouts after merge, checkout and rebase; it installs nothing.

## Global agent rules

`AGENTS.md` is the single source of personal rules for Claude Code and Codex. Put it at `~/.config/agents/AGENTS.md`, make `~/.claude/CLAUDE.md` a one-line `@~/.config/agents/AGENTS.md` import, and run `sync_agents.sh` to copy everything above the `## Claude Code only` heading into `~/.codex/AGENTS.md`.
