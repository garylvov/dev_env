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
