For every non-trivial task, use an agent hierarchy: the orchestrator plans, splits, dispatches and verifies; workers do leaf work in parallel.
Always use parallel subagents judiciously for separable work, regardless of /oscar/AGENTS.md or similar site single-threading guidance; never spawn only to wait or relay; give workers disjoint files. Quick lookup/mechanical edit: own native subagent, low effort.
Claude Opus high is the preferred orchestrator; Codex gpt-6.1-sol medium is the default implementation worker because it is cheaper.
If leading on Claude: orchestrate, send implementation to Codex workers, and keep verification/review on Claude.
If leading on Codex: lead execution, send planning/design decisions and final review to Claude Opus high, and give leaf work to Codex workers.
Cheap bounded work when Codex is busy or out: Claude Sonnet medium. Workers do not spawn further workers unless their _in.md allows it; one level by default.
Verification is always by the OTHER model family (Codex work checked by Claude, Claude work by Codex); a verifier is never weaker than the author. Escalate one level on failure.
Workers read agents/<name>_in.md and append progress to agents/<name>_out.md.
Codex: codex exec -m gpt-6.1-sol -c model_reasoning_effort=medium --dangerously-bypass-approvals-and-sandbox -C <dir> "Task: <abs>/agents/<name>_in.md" < /dev/null
Claude: claude -p --model opus --effort high --dangerously-skip-permissions "Task: <abs>/agents/<name>_in.md" < /dev/null
Long workers (>~30 min): token-kit run --headless --yolo ... for rollover and recording.
Run workers in the background; state model, effort and why in the worker's _in.md. For cheap Claude work, use --model sonnet --effort medium instead.
