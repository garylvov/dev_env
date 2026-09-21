# The live-consumer gap — named, not built

`tools/lane_recycler/` belongs to another lane. This file names the gap
precisely so that lane can close it; nothing here was implemented.

## Measured state of the consumer, 2026-09-20

- `/users/glvov/.claude/settings.json` registers exactly one `PreToolUse`
  hook: `hooks/block_irreversible.py`, `matcher: "Bash"`. `lane_recycler.sh`
  is not registered. Its own header says so: *"Install (PROPOSED, never
  applied by this script)"*.
- `grep -n 'agent_trigger_matrix\|matrix' tools/lane_recycler/*` returns
  nothing from `lane_recycler.sh`, `lane_recycler.conf` or
  `LANE_BRIEF_TEMPLATE.md`. The hook has never read the matrix.
- `grep -n 'Agent\|tool_name\|subagent_type' lane_recycler.sh` returns one
  line, `TOOL=\(.tool_name // "")`, used only to label an event row. There is
  no branch for a spawn.
- `[budget]` — the one block with a consumer — is consumed as *duplicated
  constants* in `lane_recycler.conf` (`WARN=150 FLOOR=230 HARD=250`), not as a
  read of the matrix. Editing `[budget]` in the matrix changes nothing.

So the matrix has **zero** readers, and the appearance of one is a coincidence
of two files carrying the same three numbers.

## The smallest change that makes it live

Three steps, in this order. Step 2 is the only new code.

1. **Register the hook.** Add to `settings.json` `hooks.PreToolUse[]`:
   `{"matcher":"Agent","hooks":[{"type":"command","command":".../lane_recycler.sh"}]}`
   — a second entry alongside the existing `Bash` one, not a replacement.
   (settings.json is the operator's; this lane may not edit it.)

2. **One branch in `lane_recycler.sh`, before its existing `agent_id`
   discriminator.** When `tool_name == "Agent"` and there is no `agent_id`
   (a spawn from the main thread, not a tool call inside a lane): read
   `tool_input.subagent_type` and `tool_input.prompt`, walk `[[row]]` blocks in
   file order, take the first whose `topics` word appears in the prompt or
   whose `globs` match a path in it, and compare the row's `agent` and `model`
   to what was requested. On a mismatch, `deny` with the row name, the agent
   and model it requires, and its `load` — the exact mechanism the script
   already uses for the budget. On a match, or on no match at all, `allow`
   (an unrouted spawn is a signal that the table needs a row, per `[defaults]`,
   not an error).

3. **Make `[budget]` a read, not a copy.** Have the conf sourcing fall back to
   the matrix's `[budget]` values so the two cannot drift.

## The one thing to check before writing step 2

It is **not established** that a `PreToolUse` hook here can *rewrite* a
spawn's `subagent_type` or `model` rather than only allow/deny it. This lane
did not verify it and does not assert either way. If rewriting is supported,
step 2 becomes silent re-routing and the matrix is fully live. If it is not,
deny-with-reason is the whole mechanism available — which still works, is
still a live consumer, and is the shape `lane_recycler.sh` is already built
around. Check that first; design step 2 around the answer, not around the
hope.

## Also open, and owned elsewhere

A lane is live evaluating codex integration surfaces and may change which
engine a codex row dispatches to. The `engine`, `codex_model` and
`codex_effort` columns were left exactly as found and are deliberately
untouched by this lane's edits. Whether a codex row selected from a login
session correctly falls back to the row's Claude `model` (ruling R2) remains
UNTESTED — R2 says so itself.
