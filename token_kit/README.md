# Token Kit

Resumable Claude/Codex agents. Requires `uv` and Python >=3.11.

## Run

From `dev_env`:

```bash
source token_kit/add_to_bashrc.bash
token-kit run "Fix parser" --engine codex --rollover-perc 80 --yolo
```

Titles start idle. Use `--prompt "..."` to start work and `--workspace PATH` for source. `--yolo` bypasses permission checks; Codex
disables sandboxing. `--task PATH` resumes; `--dry-run` previews.
`--install-project` persists instructions; add `--codegraph` for CodeGraph.

`token-kit pick parser` lists resume matches; choose one, or Enter cancels.
`--print` prints the command; scripts use `--select N`. Resume commands
print at startup and exit.

## Delegation

[Pyramid](trigger_pyramid.md): model tiers. [Matrix](agent_trigger_matrix.md):
delegation and recovery policy.

## Resume files

```text
<task>/
  task.json
  trigger_pyramid.md
  TOKEN_LEDGER.md
  agents/<id>/
    in.md
    STATE.md
    historical_state.md  # only after STATE compression
    out.md
    lifecycle.json
    checkpoints/
    assignments/
    messages/, artifacts/, runs/
```

Keep five-section `STATE.md` near 200 words;
archive a dated prior STATE before rewriting it only when compression is needed.
Checkpoints capture optional history. Resume reads committed checkpoints, not
transcripts; reconcile workers before replacement.

## Recovery and accounting

Launches attempt compaction recovery without rollover flags. Legacy stops
require matching run/runtime evidence. Unknown stops, errors, and manual interrupts
are excluded. Recovery reconciles state, workers, and operations before continuing.
Dead-runner orphans use verified retirement, not presumed success. Live/uncertain
operations remain blockers; retirement never authorizes release retries.

Rollover defaults off. `--rollover-perc 80` means 80% of the reported window
(currently Codex); absolute `--rollover-tokens 500k` is capped at 80% when known.
`--max-rollovers` defaults to 10.
`token-kit ledger TASK` shows reported usage. No native-child reattachment or provider failover.
