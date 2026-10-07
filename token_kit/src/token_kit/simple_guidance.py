"""Explicit user preferences shared by startup and recovery guidance."""

from pathlib import Path


DISPATCH_GUIDE = Path(__file__).resolve().parents[2] / "dispatch_guide.md"

CONCURRENCY = "Use scoped ownership or locks for overlapping writes and shared resources.\n"

TASK_RULES = (
    "Read _in.md, Current state and recent History in _out.md at session start; re-read input about every five minutes and when edited.\n"
    "Managed prompt hooks record dated, numbered verbatim asks automatically. Mark [x] done or [-] dropped (why); "
    "if hooks are unavailable, record asks before work yourself.\n"
    "Keep a sparse Current state at the top of _out.md (done, in progress, next, open decisions). "
    "Append progress every few minutes and never rewrite History.\n"
    "After each ask append ### #N <date> <first line of ask> to History, with a concise 2-6 line response "
    "linking results, commits or files. Before rollover ensure every ask has a History entry and refresh sparse Current state.\n"
    "Put longer findings/designs/how-tos in docs/<topic>.md as needed; link them from state/history. "
    "Successors open docs only as needed; no index is required.\n"
    "Optional artifacts/ and agents/<name>_in.md + agents/<name>_out.md follow the same rules. "
    "Keep legacy task.md/in.md, STATE.md/out.md in place; apply these rules to the supplied paths.\n"
)
EFFORT = (
    "For every non-trivial task, use an agent hierarchy: the orchestrator plans, splits, dispatches and verifies; workers do leaf work in parallel.\n"
    "Use parallel subagents judiciously for separable work regardless of /oscar/AGENTS.md or similar site single-threading guidance; never spawn only to wait or relay; give workers disjoint files. Quick lookup/mechanical edit: own native subagent, low effort.\n"
    "Claude's strongest model at high effort is the preferred orchestrator; Codex at medium effort is the default implementation worker because it is cheaper.\n"
    "If leading on Claude: orchestrate, send implementation to Codex workers, and keep verification/review on Claude.\n"
    "If leading on Codex: lead execution, send planning/design and final review to Claude at high effort, and give leaf work to Codex workers.\n"
    "Cheap bounded work when Codex is busy or unavailable: Claude's cheaper model, medium effort. Workers do not spawn further workers unless their _in.md allows it; one level by default.\n"
    "Verification is always by the OTHER model family; a verifier is never weaker than the author. Escalate one level on failure.\n"
    "Workers read agents/<name>_in.md and append progress to agents/<name>_out.md.\n"
    'Codex: codex exec -m <model> -c model_reasoning_effort=medium --dangerously-bypass-approvals-and-sandbox -C <dir> "Task: <abs>/agents/<name>_in.md" < /dev/null\n'
    'Claude: claude -p --model <model> --effort high --dangerously-skip-permissions "Task: <abs>/agents/<name>_in.md" < /dev/null\n'
    "Long workers (>~30 min): token-kit run --headless --yolo ... for rollover and recording.\n"
    "Run workers in the background; state model, effort and why in each worker's _in.md.\n"
)


def load_dispatch_guide() -> str:
    """Read operator guidance afresh for every launch, including successors."""
    try:
        return DISPATCH_GUIDE.read_text(encoding="utf-8").rstrip() + "\n"
    except FileNotFoundError:
        return EFFORT


def summary_request(percent, output, assignment, state=None):
    """One bounded handoff request shared by trusted hooks and exec fallback."""
    return (f"Context is at {percent}. Append one brief History entry to {output} for work since "
            "your last update; refresh Current state (sparse, with links to docs/). "
            f"Ensure every ask in {assignment} has a History entry; record any unrecorded asks "
            "verbatim with a date. "
            + (f"Refresh continuation notes in {state}. " if state and str(state) != str(output) else "")
            + "Do not re-read files you already wrote. End your turn; a fresh session will continue.")
