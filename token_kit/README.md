# Token Kit

Resumable Claude/Codex agents. Requires `uv`, Python >=3.11, and an authenticated client.

## Run

From `dev_env` (setup once/computer):

```bash
source token_kit/add_to_bashrc.bash # Install Token Kit on your Bash PATH
# "Finish retread": session name. Sessions: ~/.config/token_kit.
token-kit run "Finish retread" --engine claude --rollover-tokens 500k --yolo
```

Codex: `--engine codex`. Session-only.
Names are labels; new sessions wait for input. `--prompt "..."` starts work immediately.
Source directory: `--workspace PATH`.
`--yolo` bypasses permission checks; Codex also disables sandboxing.

`--task PATH` resumes; `--dry-run` previews. `--install-project` persists instructions;
add `--codegraph` for installed CodeGraph.

## Delegation

See the [agent trigger matrix](agent_trigger_matrix.md) for model preferences,
effort, and user overrides.

## Resume files

```text
<task>/
  task.json
  TOKEN_LEDGER.md          # reported usage
  agents/<id>/
    in.md                 # assignment
    STATE.md              # working progress
    out.md                # result
    lifecycle.json        # attempts, parent notifications
    checkpoints/          # committed recovery state
    assignments/          # assignment revisions
    messages/, artifacts/, runs/
```

Resume reads committed checkpoints, not transcripts. Parents reconcile workers
before replacing them under the same ID (`token-kit worker --help`).

## Rollover and accounting

Default token limit: unlimited (no token-triggered rollover). `--rollover-tokens 500k`
opts into checkpoint-gated, same-engine restarts at turn boundaries.
`--max-rollovers` defaults to 10. Threshold measures context,
not spend; overshoot is possible. Missing hooks/checkpoints stop recovery.

`token-kit ledger TASK` shows agent/run/model input, cache, output and totals.
Not billing/quota.

Codex opens hook review automatically when needed: approve in `/hooks`, then exit
to continue. Manual review: `token-kit hooks --engine codex`.
Claude requests `DISABLE_COMPACT=1`; Codex requests a compaction veto. Live behavior
uncertified. No native-child reattachment or automatic provider failover.
