# Token Kit

Resumable Claude/Codex agents. Requires `uv`, Python >=3.11, authenticated client.

## Run

From `dev_env`:

```bash
source token_kit/add_to_bashrc.bash # Install Token Kit on your Bash PATH
# "Finish retread": session name. Sessions: ~/.config/token_kit.
token-kit run "Finish retread" --engine claude --rollover-tokens 500k --yolo
```

Codex: `--engine codex`. Session-only.
Names are labels; sessions await instructions. `--prompt "..."` starts work;
`--workspace PATH` selects source.
`--yolo` bypasses permission checks; Codex also disables sandboxing.

`--task PATH` resumes; `--dry-run` previews. `--install-project` persists instructions;
add `--codegraph` for installed CodeGraph.

`token-kit pick parser` fuzzy-selects a task and prints its resume command with saved
settings. Flags override settings; `--select N` selects without a terminal.
Resume commands also print at startup/exit.

## Delegation

See [agent trigger matrix](agent_trigger_matrix.md) for model preferences/overrides.

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
before replacement (`token-kit worker --help`). Keep `STATE.md` current; archive history.

## Rollover and accounting

Rollover defaults off. `--rollover-tokens 500k` enables checkpoint-gated restarts at
turn boundaries; `--max-rollovers` defaults to 10. Threshold measures context,
not spend; overshoot is possible. Missing hooks/checkpoints stop recovery.

`token-kit ledger TASK` shows reported usage, not billing/quota.

Codex opens hook review when needed: approve in `/hooks`, then exit.
Claude requests `DISABLE_COMPACT=1`; Codex requests a compaction veto. Live behavior
uncertified. No native-child reattachment or automatic provider failover.
