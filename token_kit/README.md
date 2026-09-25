# Token Kit

Resumable Claude/Codex agents. Requires `uv` and Python >=3.11.

## Run

From `dev_env`:

```bash
source token_kit/add_to_bashrc.bash
token-kit run "Fix parser" --engine codex --rollover-perc 80 --yolo
token-kit continue parser
```

Titles start idle; `--prompt "..."` starts work. `--workspace PATH` sets source.
`--yolo` bypasses permissions and Codex sandboxing. `--dry-run` previews.
`--install-project` persists instructions; `--codegraph` adds CodeGraph.

`continue QUERY` resumes one match or offers a chooser. Paths work directly or
with `--task PATH`. Latest launch flags carry forward; explicit flags override.
Use `--select N` for scripts, `--print` to preview.
A live managed runner receives a cooperative stop request; verified shutdown
precedes fresh recovery. This does not reattach the native UI.
`pick QUERY` lists matches. `resume TASK`/`status TASK` summarize; `--full` includes history.

## Delegation

[Pyramid](trigger_pyramid.md): model tiers. [Matrix](agent_trigger_matrix.md): policy.

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

Keep five-section STATE near 200 words. Archive dated prior STATE only before
compression. Checkpoints preserve optional history. Recovery reads committed checkpoints, not transcripts,
and preserves newer working state. Reconcile workers and operations before continuing;
retirement never authorizes release retries.

## Recovery

Compaction recovery is automatic; `continue` also handles interrupted recovery.
Process uncertainty blocks restart. Rollover defaults off; `--rollover-perc 80`
uses reported windows or Claude defaults; `--context-window N` overrides. `--rollover-tokens 500k`
is capped at 80% when known. Restarts default unlimited; `--max-rollovers N` caps them,
`0` disables automatic restarts, and `unlimited` removes the cap.
`token-kit ledger TASK` shows reported usage.
