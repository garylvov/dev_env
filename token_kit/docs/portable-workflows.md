# Portable workflows: first implementation milestone

This is an additive preview alongside the existing Claude-oriented kit. It
implements persistent logical agents, committed checkpoints, queued messages,
explicit fresh-session launch preparation, and installation of project instructions
for either client. It does not replace the legacy router or supervisor yet.

## What works

- A task records its source workspace independently of its artifact directory.
- Each logical agent has an immutable assignment revision, editable STATE.md,
  immutable checkpoint snapshots, durable messages, and separate execution runs.
- Checkpoint publication writes the snapshot before updating its pointer.
- Recovery validates state and assignment hashes and workspace/checkpoint identity.
- Declared evidence files are fingerprinted. Later checkpoints retain their paths
  unless a caller explicitly supplies a replacement evidence list.
- Pending messages survive process restarts until a checkpoint explicitly records
  their IDs as incorporated. Queueing does not imply delivery to a running model.
- Run records preserve the logical agent ID across engine changes and mark usage
  unknown rather than pretending it is zero.
- A run with an uncertain outcome prevents another run of that agent until it is
  reconciled. Cross-host reconciliation is refused; local process identity is checked.
- Project-local installation supports Claude, Codex, or both and optional CodeGraph
  MCP configuration. It preserves unrelated settings and rejects ownership conflicts.

## Important boundaries

Strict Codex launches currently fail closed. The installed Codex 0.153.4 exposes an
automatic-compaction threshold, but a disable-all-compaction control has not been
verified. A huge threshold is not used as a substitute. The adapter can construct
a nonstrict plan for future callers, but this CLI does not expose a compaction
opt-out: its default matches the requested no-compaction policy.

The Claude adapter requests DISABLE_COMPACT=1 through both the environment and
inline session settings. Managed policy can still override session configuration.
Offline tests validate the launch request, not live client enforcement. No live
model calls have been made to certify either engine's runtime behavior.

This milestone has no automatic context supervision, quota-triggered switching,
tool-operation interception, native-child reattachment, transcript capture,
PROMPTS.md extraction, or combined token-budget enforcement. Manual checkpoints
are agent-declared, not proof of every intervening operation. Only declared
evidence and Git HEAD changes are detected; other dirty/untracked changes require
inspection. Existing commands remain available with their original semantics.

Different logical workers do not yet receive enforced source-path ownership or
isolated worktrees. Do not launch concurrent writers with overlapping source
ownership. Host shutdown does not prove an external job stopped. Inspect such
jobs before restarting an interrupted run; the runtime cannot promise exactly-once
execution of arbitrary commands.

## Commands

From the token_kit directory, make its existing source shims discoverable in the
current shell (Python >=3.11 is resolved through uv):

```bash
export PATH="$PWD/src/token_kit/bin:$PATH"
token-kit-workflow --help
token-kit-workflow new "Fix parser" --workspace /path/to/source --root /path/to/tasks
```

The new command prints the task directory. Use that printed path below. Create an
assignment file with your objective, allowed source paths, and completion criteria:

```bash
token-kit-workflow agent /path/to/task parser --assignment-file /path/to/brief.md
token-kit-workflow status /path/to/task
token-kit-workflow resume /path/to/task --agent parser
```

`resume` exports recovery information as JSON. It does not start a provider,
rewrite checkpoints, or mark messages incorporated. Both clients can use that
information to continue the same assignment through their normal file tools.
Opening an unmanaged client does not enforce the no-compaction policy.

Update agents/parser/STATE.md with nonempty Objective, Completed, Evidence,
Unresolved, and Next sections, then commit a milestone:

```bash
token-kit-workflow checkpoint /path/to/task --agent parser --evidence src/parser.py
token-kit-workflow send /path/to/task --agent parser "Also update the regression test"
token-kit-workflow checkpoint /path/to/task --agent parser --incorporated MESSAGE_ID
```

Evidence paths are relative to the source workspace, or absolute inside the
workspace/task. Snapshots limit state to 32KB so detailed outputs belong in
artifacts. out.md is written by the agent when it completes the assignment.

Preview a fresh launch without executing a client or printing its environment:

```bash
token-kit-workflow launch /path/to/task --agent parser --engine claude --dry-run
token-kit-workflow launch /path/to/task --agent parser --engine codex --dry-run
```

The Codex command currently returns an explicit unsupported-compaction error.
Remove --dry-run from the Claude command to start its interactive client. No
permission-bypass or native-resume flags are added. The launcher exposes the
workflow shim to the child so checkpoint commands can be invoked there.

An interrupted process may require explicit reconciliation after its supervisor
and child have stopped:

```bash
token-kit-workflow close-run /path/to/task --agent parser RUN_ID --note "Verified the command outcome and inspected the workspace"
```

This records the operator's evidence; it does not kill processes or verify external
jobs on the operator's behalf. A live or remote process cannot be cleared this way.

## Project instructions and CodeGraph

From token_kit:

```bash
PYTHONPATH=src uv run --python '>=3.11' --no-project python -m token_kit.project_install install --project /path/to/source --engine both --codegraph --dry-run
```

Remove --dry-run to write the requested project configuration. Omit --codegraph
for shared instructions only. Use uninstall with the same --project and selected
--engine to remove owned configuration. The installer writes a hash manifest under
.token-kit and coordinates concurrent installers with a project-directory lock.
It does not install the CLI on PATH, change global settings, launch CodeGraph,
index source, or run agents. CodeGraph must already be installed. Client project
trust/MCP approval remains controlled by each client.

Install produces shared AGENTS.md guidance, a CLAUDE.md import where needed,
and optional .mcp.json / .codex/config.toml MCP entries using codegraph serve --mcp.
Interrupted multi-file installs report ownership conflicts on retry rather than
guessing which unrecorded edits belong to the installer. Uninstall preserves
unrelated content and leaves empty files/directories instead of deleting them.

## Validation and next milestone

Offline tests cover torn checkpoint publication, assignment/state tampering,
messages surviving restarts, changed evidence, run ownership, launch argument
preservation, strict Codex refusal, environment privacy, and installer conflicts.

Next: certify compaction control per client and worker mode, add normalized runtime
events, then implement supervised rollover and provider failover using this same
checkpoint core. Migrate old lanes explicitly; the preview never rewrites them.
