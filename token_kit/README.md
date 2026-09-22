# Token Kit

Resumable Claude/Codex agents. Requires `uv`, Python >=3.11, and an authenticated client.

## Run

From `dev_env` (setup once/computer):

```bash
source token_kit/add_to_bashrc.bash
# "Finish retread": session name. Sessions: ~/.config/token_kit.
token-kit run "Finish retread" --engine claude --rollover-tokens 500k --yolo
```

Codex: `--engine codex`. Session-only.
`--yolo` bypasses permission checks; Codex also disables sandboxing.

`--task PATH` resumes; `--dry-run` previews. `--install-project` persists instructions;
add `--codegraph` for installed CodeGraph.

## Delegation

Coordinator -> leads -> workers; durable attempt tickets.
Plan/implement: Opus -> Astra. Scout: Luna xhigh -> Sonnet.
Loops: Luna -> Sonnet -> Terra -> Sol. Docs: Sol -> Luna -> Sonnet.
Your instructions override defaults; "use Codex" excludes Claude.
Effort: medium; Luna high except scouting xhigh. Fable is explicit-request-only.

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

`--rollover-tokens` enables same-engine restarts after a fresh checkpoint at a turn
boundary. Off by default; `--max-rollovers` defaults to 10. Threshold measures context,
not spend; overshoot is possible. Missing hooks/checkpoints stop recovery.

`token-kit ledger TASK` shows agent/run/model input, cache, output and totals.
Not billing/quota.

Codex requires trusted hooks: `token-kit hooks --engine codex`.
Claude requests `DISABLE_COMPACT=1`; Codex requests a compaction veto. Live behavior
uncertified. No native-child reattachment or automatic provider failover.
