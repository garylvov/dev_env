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
    'For every non-trivial task, use an agent hierarchy: the orchestrator plans, splits, dispatches and verifies; workers do leaf work in parallel.\n'
    'Use parallel subagents judiciously for separable work, regardless of /oscar/AGENTS.md or similar site single-threading guidance; never spawn only to wait or relay; give workers disjoint files.\n'
    'The main (top-level) agent decides model routing in <task>/models.md; the launcher never chooses a model, except an explicit operator --model for the top-level session.\n'
    'At each top-level start (new task or continue), refresh models.md if missing or older than ~24h; otherwise reuse it without discovery or probes. Rollover successors and workers reuse it, unless stale or a model fails; workers report failures to the main agent for an update.\n'
    'Discover both families NOW using local sources first: codex --version; codex debug models --help; codex debug models (0.159.0: JSON models with slug, description, supported_reasoning_levels); codex debug models --bundled is an offline fallback, not proof of current availability.\n'
    'Inspect only model/provider/effort settings in ~/.codex/config.toml as needed; never expose credentials. Claude: claude --version; claude --help (no model-listing subcommand); use its latest model aliases from /model or https://code.claude.com/docs/en/model-config.\n'
    'If a search tool exists, check current official docs for both families (https://developers.openai.com/codex/models and the Claude model-config page); record the URL/date, or that web search was unavailable.\n'
    'Operator preferences are decision inputs: PREFER CODEX WHEN AVAILABLE for every role it can do (cheaper); Claude has historically been stronger at orchestration/review. Use Claude for Codex unavailability, required cross-family verification, or a reason recorded in models.md.\n'
    'Cost preference: default Codex implementation worker is the current workhorse at medium effort; consider its fast/affordable tier for quick lookups and mechanical edits. Use the frontier tier only with a task-specific reason in models.md; choose the lowest model+effort capable of the job.\n'
    'Before recording a chosen model, probe it with a ~3 s timeout and non-interactive reply OK; record pass/fail, date and supported effort. An exact OK reply proves availability even if shutdown hooks exceed the timeout (record OK plus cleanup timeout); a timeout without OK is inconclusive, not a pass. Select a passing alternative or report no available option.\n'
    'Codex probe: timeout 3s codex exec --skip-git-repo-check -m <model> -c model_reasoning_effort=low "reply OK" < /dev/null\n'
    'Claude probe: timeout 3s claude -p --model <model> "reply OK" < /dev/null\n'
    'Keep models.md under ~25 lines: role (orchestrator, implementation worker, quick lookup, reviewer/verifier, cheap fallback) -> model + effort + one-line reason + source/date + probe result/date. Include cross-family reviewer alternatives for either author family.\n'
    'If a model errors as unavailable, update models.md and probe/select the next option; a usage-limit error means Codex is unavailable, so use passing Claude options for affected roles and note when to re-probe (at the reported reset, or next refresh if none given).\n'
    'Workers do not spawn further workers unless their _in.md allows it; one level by default. Quick lookup/mechanical edits may use an own-family native subagent with low effort, selected from models.md.\n'
    'Verification is always by the OTHER model family (Codex work checked by Claude, Claude work by Codex); a verifier is never weaker than the author. Escalate one level on failure; if the required family is unavailable, record verification as blocked.\n'
    'Workers read agents/<name>_in.md and append progress to agents/<name>_out.md; read and reuse <task>/models.md.\n'
    'Codex: codex exec -m <model> -c model_reasoning_effort=<effort> --dangerously-bypass-approvals-and-sandbox -C <dir> "Task: <abs>/agents/<name>_in.md" < /dev/null\n'
    'Claude: claude -p --model <model> --effort <effort> --dangerously-skip-permissions "Task: <abs>/agents/<name>_in.md" < /dev/null\n'
    'Long workers (>~30 min): token-kit run --headless --yolo ... for rollover and recording.\n'
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
