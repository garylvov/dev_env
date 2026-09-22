# Token Kit

Resumable agent work for Claude Code and Codex. Requires `uv`, Python >=3.11,
and the client installed and authenticated.

## Run

From your source project, replace `/path/to/dev_env` with your clone:

```bash
export PATH="/path/to/dev_env/token_kit/src/token_kit/bin:$PATH"
token-kit run "Fix the parser" --yolo
```

This sets up shared project instructions, creates a task, and launches Claude.
It prints the task path and the exact command to continue later.
Omit `--yolo` to retain client permission settings; with it, Claude skips
permission checks and Codex disables approvals and sandboxing.

Use `--task PATH` to continue, `--dry-run` to preview, or `--codegraph` to wire
an already-installed CodeGraph. Advanced commands: `token-kit --help`.

## Delegation

Coordinator -> leads -> bounded workers, each with its own assignment, state,
and results. Prefer Codex; Astra for difficult work, Luna for narrow work.
Fable requires an explicit request. Fable and Opus use medium effort.
Record temporary overrides in checkpointed state.

## Resume files

```text
<task>/
  task.json
  agents/<id>/
    in.md                 # assignment
    STATE.md              # working progress
    out.md                # result
    checkpoints/          # committed recovery state
    assignments/          # assignment revisions
    messages/, artifacts/, runs/
```

Resume reads committed checkpoints, not transcripts. Workers have independent state;
briefs and file handoffs limit context sharing. Savings are unmeasured.

## Not finished yet

Automatic rollover is **not connected** to this launcher, including at 500k.
There is no enforced session context cap. Checkpoint before restarting.

`--engine codex` currently refuses strict launches because disabling all
compaction is unverified. Claude requests `DISABLE_COMPACT=1`, but runtime
enforcement is uncertified. Automatic provider failover is also unfinished.
