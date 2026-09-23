# Token Kit

Resumable Claude/Codex agents. Requires `uv` and Python >=3.11.

## Run

From `dev_env`:

```bash
source token_kit/add_to_bashrc.bash # Shell setup
# "Fix parser": session name. Sessions: ~/.config/token_kit.
token-kit run "Fix parser" --engine codex --rollover-at 80% --yolo
```

Claude uses absolute targets such as `--rollover-tokens 500k`; Codex supports
`--rollover-at 80%`. Names are labels; sessions wait for instructions.
`--prompt "..."` starts work; `--workspace PATH` selects source.
`--yolo` bypasses permission checks; Codex also disables sandboxing.

`--task PATH` resumes; `--dry-run` previews. `--install-project` persists instructions;
add `--codegraph` for CodeGraph.

`token-kit pick parser` lists fuzzy matches; choose a number to resume with saved
settings; Enter cancels. `--print` prints the command; noninteractive uses
`--select N`. Resume commands print at startup/exit.

## Delegation

[Pyramid](trigger_pyramid.md): model tiers. [Matrix](agent_trigger_matrix.md): when to delegate.

## Resume files

```text
<task>/
  task.json
  trigger_pyramid.md
  TOKEN_LEDGER.md
  agents/<id>/
    in.md
    STATE.md
    out.md
    lifecycle.json
    checkpoints/
    assignments/
    messages/, artifacts/, runs/
```

Resume reads committed checkpoints, not transcripts. Parents reconcile workers
before replacement (`token-kit worker --help`). Keep `STATE.md` current.

## Rollover and accounting

Rollover defaults off. `--rollover-at 80%` uses the reported context window
(currently Codex); absolute `--rollover-tokens 500k` is capped at 80% when a
window is known. Restarts are checkpoint-gated at turn boundaries;
`--max-rollovers` defaults to 10. Threshold measures context, not spend;
overshoot is possible. Missing hooks/checkpoints stop recovery.

`token-kit ledger TASK` shows reported usage.

Codex opens hook review when needed: approve in `/hooks`, then exit.
Claude requests `DISABLE_COMPACT=1`; Codex requests a compaction veto. Live behavior
uncertified. No native-child reattachment or automatic provider failover.
