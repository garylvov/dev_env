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

Client trust and permissions remain in effect; Token Kit does not enable `--yolo`.

```text
<task>/
  <task>_in.md             objective and user asks
  <task>_out.md            running state and results
  docs/                   longer findings and decisions, linked from output
  artifacts/              optional
  agents/<name>_in.md      optional worker assignment
  agents/<name>_out.md     optional worker progress
  .token-kit/             launcher records
```

The filenames use the actual task folder name. Managed `UserPromptSubmit` hooks
record every user ask verbatim with a date and numbered `[ ]` status, before the
agent works. Initial assignments are saved on creation; Token Kit's kickoff and
successor prompts are excluded. Mark `[x]` done or `[-] dropped (why)`. If managed
hooks are unavailable, the agent must record asks itself.

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

Choose each subagent's model and effort deliberately and explain the choice in its
input file. Use low for lookups/mechanical edits, medium for clear implementation,
and high for unknown root causes, design and trusted verification. A verifier is
never weaker than the author. Escalate one level after failure; never spawn only to wait or relay.

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
Token Kit never bypasses hook trust. Stop an unmanaged active session normally
before launching `continue`.

## Installing the candidate

Keep the old checkout available for sessions that depend on it; read the
[cutover guide](docs/SIMPLE-CUTOVER.md) before changing an installed command.

```bash
bash token_kit/install.sh --legacy-root /path/to/previous/checkout
```

Use `--dry-run` first to preview. The installer preserves a legacy launcher and
prints a rollback command for changed installations. Task files and client hook
configuration are untouched. Optional legacy cleanup previews owned changes and
backs them up; customized guidance and unrelated hooks survive. The
[legacy guide](docs/legacy-guide.md) describes the old workflow.
