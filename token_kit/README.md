# Token Kit

Resumable Claude/Codex agents. Requires `uv`, Python >=3.11, and an authenticated client.

## Run

From your project:

```bash
export PATH="/path/to/dev_env/token_kit/src/token_kit/bin:$PATH"
# "Finish retread": session name. Sessions: ~/.config/token_kit.
token-kit run "Finish retread" --root ~/.config/token_kit --engine claude --yolo
```

Starts Claude with session-only guidance; prints the resume command.
Records persist; project files stay untouched. Plain clients skip Token Kit
unless previously installed.
`--yolo` bypasses permission checks; Codex also disables sandboxing.

`--task PATH` resumes; `--dry-run` previews. `--install-project` persists instructions;
add `--codegraph` for installed CodeGraph. More: `token-kit --help`.

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

Resume reads committed checkpoints, not transcripts. Workers have independent state.
Saved briefs, managed Claude spawns and Codex jobs/dispatch carry compact policy.
Client hook restrictions still apply. Savings are unmeasured.

## Not finished yet

Automatic rollover is **not connected** to this launcher, including at 500k.
There is no enforced session context cap. Checkpoint before restarting.

`--engine codex` currently refuses strict launches because disabling all
compaction is unverified. Claude requests `DISABLE_COMPACT=1`, but runtime
enforcement is uncertified. Provider failover is unfinished.
