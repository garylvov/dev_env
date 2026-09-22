# Token Kit

One workflow for Claude Code and Codex: shared assignments, per-agent state,
checkpoints, messages, and run records. Switch clients without changing the task.
Recovery uses saved files, not a shared native conversation.

## Setup

Requires `uv`, Python >=3.11, and your chosen client. From `token_kit/`:

```bash
export PATH="$PWD/src/token_kit/bin:$PATH"
token-kit install --project /path/to/source --engine both --dry-run
token-kit install --project /path/to/source --engine both
```

Use `--engine claude`, `codex`, or `both`. Setup preserves unrelated settings and
adds shared project instructions, not global hooks or the trigger matrix.
Add `--codegraph` to configure an already installed
[CodeGraph](https://github.com/colbymchenry/codegraph); indexing is separate.
Use `token-kit uninstall` with the same project/engine options to remove owned settings.

## Delegationmaxxing

Delegate bounded work aggressively; keep the main conversation small.
Add a lead only when it can absorb several workers' context and integrate their results.

```text
Coordinator              user intent, decomposition, acceptance
  Workstream lead        owns one outcome; briefs workers and verifies results
    Worker               one bounded assignment, evidence, and handoff
```

Prefer Codex for implementation, lookup, summaries, edits, reviews, docs, and job
coordination. Use Luna for narrow work and Astra (high) for difficult work,
including planning and detailed debugging. Use Fable (medium) only when the user
explicitly asks for it in natural language, such as "use Fable for this review";
mentions in files or quoted text are not requests. Opus fallbacks use medium effort.
These are preferences, not automatic routing in the shared launcher.

Each agent gets `in.md`, its own `STATE.md`, and `out.md`. Put its parent/owner,
source-path scope, dependencies, and acceptance checks in the assignment. Parents
read results and evidence, not entire worker transcripts. Parallelize independent
work; serialize overlapping edits. Stop when the acceptance checks pass. Use
completion notifications or process tooling instead of models that only wait.

Hierarchy is an assignment convention today: parent links, scheduling, and source
ownership are not enforced. Default to coordinator -> lead -> worker; skip the
lead for small tasks. More agents are useful only when they reduce total work.

## Work and resume

```bash
token-kit new "Fix parser" --workspace /path/to/source
token-kit agent /path/to/task parser --assignment-file /path/to/brief.md
token-kit status /path/to/task
token-kit resume /path/to/task --agent parser
```

`new` prints the task path. Tasks default to `$XDG_STATE_HOME/token_kit/work`
(or `~/.local/state/token_kit/work`); use `--root` to choose another location.
Omitting `--agent` selects `coordinator`. `resume` prints recovery information;
either client can read those files to continue the same logical agent.

```text
<task>/task.json
<task>/agents/<id>/
  in.md, STATE.md, out.md
  assignments/, checkpoints/, messages/, artifacts/, runs/
```

Keep `Objective`, `Completed`, `Evidence`, `Unresolved`, and `Next` sections in each
agent's `STATE.md` (32KB maximum). Store detailed outputs in `artifacts/`.

```bash
token-kit checkpoint /path/to/task --agent parser --evidence src/parser.py
token-kit send /path/to/task --agent parser "Add a regression test"
token-kit checkpoint /path/to/task --agent parser --incorporated MESSAGE_ID
token-kit launch /path/to/task --agent parser --engine claude --dry-run
token-kit launch /path/to/task --agent parser --engine claude
```

Messages stay pending until a checkpoint incorporates their IDs. Evidence paths
are relative to the source workspace; recovery checks declared evidence and Git
HEAD changes. Checkpoint before switching clients. Unrecorded reasoning is lost.

```bash
token-kit list --open
token-kit find parser
token-kit retitle /path/to/task "Fix date parser"
token-kit done /path/to/task
token-kit reopen /path/to/task
token-kit migrate /path/to/legacy-task --root /path/to/tasks
```

Migration preserves the original folder. Old `token-kit-task` and
`token-kit-workflow` commands are aliases. See `token-kit COMMAND --help` for options.

## Limits and recovery

Strict Codex launches currently refuse to run: disabling all compaction is not
verified. Claude launches request `DISABLE_COMPACT=1`, but runtime enforcement is
uncertified. Direct client use can read shared state without this guarantee.
Automatic rollover, quota failover, and combined token budgets are not implemented.

Interrupted runs block relaunch. Verify that processes and external jobs stopped,
inspect their effects, then reconcile explicitly:

```bash
token-kit close-run /path/to/task --agent parser RUN_ID --note "Verified outcomes"
```

This records your check; it does not kill jobs. Checkpoints do not capture every
tool operation or reattach native subagents. Inspect dirty files before repeating work.

The old global installer, router, supervisor, and transport remain under
`token-kit legacy --help`; they do not provide the shared core's recovery guarantees.
Shared project installs and managed launches suppress old router/recovery hooks.
The [trigger matrix](agent_trigger_matrix.md) configures only that legacy router.

## Tests

From `token_kit/`:

```bash
uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .
```
