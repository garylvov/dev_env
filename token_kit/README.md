# Token Kit

Resumable Claude/Codex agents. Requires `uv`, Python >=3.11, and an authenticated client.

## Run

From your source project, replace `/path/to/dev_env` with your clone:

```bash
export PATH="/path/to/dev_env/token_kit/src/token_kit/bin:$PATH"
token-kit run "Fix the parser" --yolo
```

This sets up shared project instructions, creates a task, and launches Claude.
It prints the task path and command to continue later.
Omit `--yolo` to retain client permission settings; with it, Claude skips
permission checks and Codex disables approvals and sandboxing.

Use `--task PATH` to continue, `--dry-run` to preview, or `--codegraph` to wire
an already-installed CodeGraph. Advanced commands: `token-kit --help`.

## Delegation

Coordinator -> leads -> workers, each with assignment/state/results.
Plan/implement: Opus -> Astra. Scout: Luna xhigh -> Sonnet.
Loops: Luna -> Sonnet -> Terra -> Sol. Docs: Sol -> Luna -> Sonnet.
Your instructions override defaults; "use Codex" excludes Claude.
Effort: medium; Luna high except scouting xhigh. Fable is explicit-request-only.
Checkpoint overrides; skip unavailable candidates.

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
enforcement is uncertified. Provider failover is unfinished.
