# Token Kit

Token Kit keeps work in plain folders and continues managed sessions when their
context fills. Start or continue a task, save useful notes, and keep working.

The simplified implementation is a separate candidate release. Keep the original
checkout intact while existing sessions still depend on it. See the
[cutover guide](docs/SIMPLE-CUTOVER.md) before changing an installed command.

## Everyday use

Use the candidate `token-kit run` to start, `token-kit continue` to continue, or
`token-kit pick` to select an existing task. `status` displays records. The old
`resume` spelling remains read-only; use `continue` to launch a session.
Run `token-kit --help` for exact arguments.

Start a new Codex task with automatic rollover at 80% context:

```bash
token-kit run "My new task" --engine codex --rollover-perc 80
```

This opens an idle session in your current workspace; tell the agent what to do.
To start work immediately, include an assignment:

```bash
token-kit run "Fix parser" --engine codex --rollover-perc 80 --prompt "Investigate and fix the parser failure"
```

Continue an existing task by name with the same explicit settings:

```bash
token-kit continue retread --engine codex --rollover-perc 80
```

If several tasks match, choose one from the list. New tasks default to Claude when
`--engine` is omitted; use `--engine claude` explicitly if preferred. New tasks
default to 80% rollover, while continuation reuses saved settings unless overridden.

Plain launch does not require project installation. Client trust and permissions
remain in effect; Token Kit does not enable `--yolo` automatically.

```text
task/
  task.md
  STATE.md
  out.md                  optional
  artifacts/              optional
  agents/                 optional worker folders
  .token-kit/             launcher records
```

Existing task layouts remain readable in place. Write STATE in any format and
length. Save results directly. No registration, checkpoint command, completion
ticket, or folder closure is required. Optional model preferences remain ordinary
text; explicit user instructions take precedence.

## Rollover

The default threshold is 80% of the known context window, with unlimited successful
rollovers. A finite restart limit is optional. `--no-rollover` disables all automatic
restarts. Normal completion, manual interruption, and arbitrary errors do not restart.

Before rollover, the client is asked to save current notes. After verifying that the
old client stopped, the launcher starts its successor with the latest saved files.
Missing fresh notes produce a recovery limitation, not a checkpoint requirement.
If the client cannot provide trustworthy context and safe-stop events, the launcher
reports that automatic rollover is unavailable. Native worker compaction remains
available; a child event never authorizes restarting its parent.

Codex configurations with overlapping existing hook arrays retain those hooks and
use native compaction until managed hooks can be combined without replacing policy.
An active session without working rollover hooks must be stopped normally before
`continue` can launch a fresh session. Saved folders remain usable throughout.

Process ownership checks prevent overlapping launches. They do not judge whether
results are complete or correct. Check uncertain external operations before retrying
them; continue unrelated authorized work.

## Existing installations

Old global Token Kit caps or lifecycle hooks can still affect new launches. The
candidate reports these conflicts; disabling every client hook is not a remedy.
The optional cleanup helper previews exact owned changes and creates backups with
an executable rollback recipe. User-edited guidance and unrelated hooks survive.
Policy cleanup affecting old sessions waits until those sessions exit.

The old installer, runtime, and lifecycle commands remain pinned for existing
consumers. New obsolete worker commands fail without mutating records; workers can
save their result and report back normally. Historical documentation is in
[docs/legacy-guide.md](docs/legacy-guide.md) and the dated handoffs. It describes the
old installation, not the simplified workflow.

## Installing the launcher

From the new checkout, run:

```bash
bash token_kit/install.sh --legacy-root /path/to/previous/checkout
```

Use `--dry-run` to list changes first. The installer updates the user command and
shell startup, and forwards the previous executable path to the new launcher so
existing shells with a cached path work immediately. It retains a sibling legacy
launcher for old hooks and managed-session utilities. Explicit new launches and
continuations use the simplified launcher even with inherited legacy variables.
The previous installer path forwards here too. Repeating installation is a no-op;
each changed installation prints a standalone rollback command. Task files and
client hook configuration are untouched. Historical hook installation is archived
in `install-legacy.sh`.
