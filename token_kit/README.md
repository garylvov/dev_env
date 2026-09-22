# Token Kit

Token Kit keeps assignments, agent state, checkpoints, messages, and run records in
one shared format for Claude Code and Codex. Agents retain their identities across
sessions and clients. Source workspaces and task records can live separately.

The core handles persistence and recovery; adapters prepare client launches.
Project integrations install shared instructions and optional CodeGraph settings.

**Current limit:** portable recovery works with either client's file tools, but
strict Codex launches are blocked until a disable-all-compaction control is
verified. Claude launches request `DISABLE_COMPACT=1`; runtime enforcement has not
been certified, and managed policy can override the setting. Automatic rollover,
quota-triggered failover, and combined token-budget enforcement are not implemented.

## Setup

Requires `uv`, Python >=3.11, and a client. From this repository's `token_kit` directory:

```bash
export PATH="$PWD/src/token_kit/bin:$PATH"
token-kit --help
token-kit install --project /path/to/source --engine both --dry-run
token-kit install --project /path/to/source --engine both
```

Choose `--engine claude`, `codex`, or `both`. Installation adds shared `AGENTS.md`
guidance and a `CLAUDE.md` import when needed, preserving unrelated content. It
records ownership under `.token-kit` and rejects conflicting edits. It does not
install client binaries, change global settings, launch agents, or permanently
add commands to your shell's PATH.

Use `token-kit uninstall --project /path/to/source --engine both --dry-run` to
inspect removal, then omit `--dry-run`. Uninstall removes owned content and
preserves unrelated settings; empty files and directories can remain. Interrupted
multi-file installs report ownership conflicts rather than assuming ownership of
unrecorded edits.

## Start a task and delegate work

```bash
token-kit new "Fix parser" --workspace /path/to/source
token-kit new "Fix parser" --workspace /path/to/source --root /path/to/tasks
```

Each command creates a separate task and prints its path. The default root is
`$XDG_STATE_HOME/token_kit/work`, or `~/.local/state/token_kit/work` when unset.
Use that path below. Assignment files specify objectives, allowed source paths,
and completion criteria:

```bash
token-kit agent /path/to/task parser --assignment-file /path/to/brief.md
token-kit status /path/to/task
token-kit resume /path/to/task --agent parser
```

Omit `--agent` to address the task's `coordinator`. `resume` prints recovery JSON;
it does not launch a client or acknowledge messages. Either client can read the
referenced assignment, checkpoint, evidence, and pending messages to continue.
Opening a client directly does not enforce Token Kit's compaction policy.

Manage tasks without changing their identities:

```bash
token-kit list --open
token-kit find parser
token-kit retitle /path/to/task "Fix date parser" --summary "Handle timezone offsets"
token-kit done /path/to/task
token-kit reopen /path/to/task
```

Use `--root /path/to/tasks` with `list` and `find` for a custom task root.
`done` refuses tasks with running or unreconciled agent runs.

```text
<task>/
  task.json
  STATE.md                    # copy of the coordinator's committed state
  agents/<stable-agent-id>/
    in.md                     # assignment working copy
    assignments/0001.md       # committed assignment revision
    STATE.md                  # editable progress and next steps
    out.md                    # result written by the agent when finished
    checkpoints/<id>/         # committed state and integrity manifest
    messages/<id>.json        # durable incoming messages
    artifacts/                # detailed evidence and outputs
    runs/<id>/                # run metadata, resume bundle, launch prompt
```

## Checkpoint and resume

Every agent maintains its own `STATE.md` with nonempty `Objective`, `Completed`,
`Evidence`, `Unresolved`, and `Next` sections. Commit it after meaningful milestones:

```bash
token-kit checkpoint /path/to/task --agent parser --evidence src/parser.py
token-kit send /path/to/task --agent parser "Also update the regression test"
token-kit checkpoint /path/to/task --agent parser --incorporated MESSAGE_ID
```

`send` returns the message ID. Messages remain pending until a checkpoint records
them as incorporated; queueing does not imply delivery to a running model. Use
`send /path/to/task --agent parser -` to read a message from standard input.

Evidence paths are relative to the source workspace, or absolute inside the
workspace/task. Later checkpoints retain previous evidence paths unless a new
`--evidence` list replaces them. State is limited to 32KB; put detailed output in
`artifacts/`. Checkpoints publish their pointer only after writing the snapshot,
and recovery validates state/assignment hashes and checkpoint identity.

Checkpoints record agent-declared progress. The runtime does not intercept every
tool operation, capture transcripts, extract `PROMPTS.md`, or reattach native
children. Recovery detects declared evidence changes and Git HEAD changes; inspect
other dirty/untracked files and unfinished jobs before continuing. Reasoning that
was never recorded cannot be recovered.

## Launch and reconcile

```bash
token-kit launch /path/to/task --agent parser --engine claude --dry-run
token-kit launch /path/to/task --agent parser --engine claude
token-kit launch /path/to/task --agent parser --engine codex --dry-run
```

The last command currently fails with an explicit unsupported-compaction error.
There is no CLI opt-out from strict compaction policy. Claude's plan sets
`DISABLE_COMPACT=1` in both its environment and inline session settings. Dry runs
print the plan without inherited environment secrets and do not create a run.
Launches are fresh interactive sessions; no native-resume or permission-bypass
flags are added. `--model NAME` selects a model when preparing the launch.

A run with an uncertain outcome blocks another run of that agent. Once its
supervisor and child have stopped, inspect the workspace and external jobs, then:

```bash
token-kit close-run /path/to/task --agent parser RUN_ID --note "Verified command outcomes and inspected the workspace"
```

This records your reconciliation, not proof that external operations stopped. It
does not kill processes. Live or remote processes cannot be cleared this way.
Successful client exit also does not prove every external job finished. Different
workers do not receive enforced source-path ownership or isolated worktrees; avoid
concurrent writers with overlapping source ownership.

## CodeGraph

```bash
token-kit install --project /path/to/source --engine both --codegraph --dry-run
token-kit install --project /path/to/source --engine both --codegraph
```

Install [CodeGraph](https://github.com/colbymchenry/codegraph) separately first. Token
Kit adds `.mcp.json` and/or `.codex/config.toml` entries for `codegraph serve --mcp`.
It does not download CodeGraph, build an index, or start its server. Client trust
and MCP approvals remain under each client's control. Both clients use the source
checkout, while task checkpoints retain progress independently of CodeGraph.

## Existing task folders

```bash
token-kit migrate /path/to/legacy-task --root /path/to/tasks
```

Migration creates a new shared-format task without changing the legacy folder.
Review its imported assignments and state before launching. `token-kit-task` and
`token-kit-workflow` are aliases for the same task commands; old layouts require
explicit migration, rather than silently retaining a second workflow.

## Advanced compatibility commands

The older global hook/matrix installer is available only through
`token-kit legacy install ...`; use `token-kit legacy --help` for its command
surface. Its router, supervisor, and Codex transport remain separate compatibility
tools. They do not share the new task accounting or provide its recovery guarantees.
Previously installed router and recovery hooks stay silent inside projects with
shared Token Kit installation records and during shared managed launches, so the
trigger matrix is not injected into the main workflow.

Scripts remain under `src/token_kit/codex/bin/`; jobs have a detached owner:

```bash
codex-job start --model MODEL --effort high --cwd /path/to/source --task-file /path/to/in.md --name parser
codex-job send JOB_ID "Update the regression test too"
codex-job wait JOB_ID
codex-job status JOB_ID
codex-job list
codex-job stop JOB_ID
```

Model and effort are required. `send` steers a running turn; after completion it
continues the same native thread, using its owner during `--linger-s` (default 120)
or resuming later. Delivery rows distinguish steered, queued, resumed, and failed
messages. `wait` blocks and prints the answer and delivery rows; background it when
your harness supports completion notifications. `status` and `list` only read files.
Use `codex-dispatch` for a single immediate turn instead of a continuing job.

Exit codes are 0 for answered, 2 for usage, 3 for turn failure, and 42 for unavailable
(`absent`, `auth`, `busy`, `protocol`, or `quota`). Quota creates a cooldown marker;
`--ignore-cooldown` overrides it. This is not automatic provider failover.

With shared job directories, live remote owners accept queued messages and stop
requests. An exited owner on another host cannot be resumed; messages are retained
in `undelivered/`. `codex-run`, `codex-run --reseed`, and `codex-run update` use the
legacy launcher, including optional node-local `CODEX_HOME` and auth synchronization.
That launcher uses bypassed approvals; it is not the new strict launch adapter.

## Tests

```bash
cd token_kit
uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .
```

Offline tests cover checkpoints, messages, evidence, run ownership, launches,
environment privacy, strict Codex refusal, and installer conflicts. They do not
certify live client compaction behavior.
