"""Explicit user preferences shared by startup and recovery guidance."""

from pathlib import Path


DISPATCH_GUIDE = Path(__file__).resolve().parents[2] / "dispatch_guide.md"

CONCURRENCY = "Use scoped ownership or locks for overlapping writes and shared resources.\n"

REPO_DOCS = (
    "Repo knowledge outlives tasks in the workspace repo's docs/agents/: README.md indexes one line per doc "
    "(- [topic](topic.md) -- hook); read the index and open docs only when relevant. Docs hold only what code cannot say "
    "(how to run/test, gotchas, decisions and why, dead ends), first line <!-- verified: <commit> files: <paths> -->.\n"
    "At rollover and when the task finishes, promote durable repo knowledge from the task docs/ into docs/agents/, "
    "refreshing its index line and verified commit. Never write docs/agents/ in repos other than the task workspace.\n"
)

TASK_RULES = (
    "Read _in.md, Current state and recent History in _out.md at session start; re-read input about every five minutes and when edited.\n"
    "_in.md is the conversation: managed prompt hooks append each user message verbatim, dated and numbered. "
    "Never edit it; status lives only in asks.md. If hooks are unavailable, append asks before work yourself.\n"
    "<task>/asks.md guards against dropped asks as requests pile up: every request in every user message gets a line, "
    "including asides buried in longer messages, as a full sentence, with [ ] [~] [x] [-] status, "
    "where/why and sources (from #3, #7). Keep open and in-progress asks at the top, done and dropped below.\n"
    "Group asks under short ## feature headings; a new ask joins the feature it extends. "
    "Over five open features: end replies with a proposed priority order.\n"
    "When an ask conflicts with or supersedes another, mark both and say which wins; never delete. Update asks.md before ending "
    "each turn that had a user message (create it if missing). Re-read it before declaring work finished, ending a turn or rollover; "
    "your final reply lists every still-open ask. Successors read it first.\n"
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
    'The main session is always Claude. GPT models are reached only as Shunt-backed Claude Code subagents (Agent tool, agent files in ~/.claude/agents/, no model override, name each <task>-<model>-<effort>) when `systemctl --user is-active shunt` prints active; otherwise every role uses Claude models.\n'
    'The main (top-level) agent decides model routing in <task>/models.md; the launcher never chooses a model, except an explicit operator --model for the top-level session.\n'
    'The main agent builds <task>/models.md only if it does not exist; it stays fixed for the task across starts, continue, rollover successors and workers. Rebuild or edit only when the user asks, recording that ask in _in.md. Existing matrices require no discovery or probes.\n'
    "Only when building models.md, the main agent fetches and reads https://platform.claude.com/docs/en/models/overview and https://platform.claude.com/docs/en/about-claude/pricing with its web/fetch tool, plus OpenAI's official model and per-token pricing pages for the models pinned in each Shunt agent file (read the files; never assume the newest slug). Cite exact URLs with the date read, record Shunt status with date, and note any disagreement with Claude CLI aliases (claude --help, /model). If no fetch tool is available, say so in models.md and fall back to local sources.\n"
    'models.md (~30 lines): a candidate table over Claude models and Shunt agents with USD per 1M input/output tokens, context window, one-line capability note, and `cost rank` (1 = cheapest) and `intelligence rank` (1 = most capable) on unified scales over both families; cite dated vendor positioning and label judgment, never infer intelligence from price. Subscription logins use API prices only as a RELATIVE proxy for usage-limit burn; state this in one line.\n'
    'Each role (orchestrator, implementation worker, quick lookup, reviewer/verifier, cheap fallback) lists ORDERED options spanning both families: model or Shunt agent + effort + reason showing both ranks (e.g. "cost 3, intel 2") + source/date. Cheapest capable option first, Shunt agents winning cost ties while active; the main session is typically Claude Opus by operator preference, labeled as an override.\n'
    'Only when building the matrix, probe Claude candidates in parallel with a 30 s timeout per model: timeout 30s claude -p --model <model> --effort low "reply OK" < /dev/null. PASS = exact OK reply; UNAVAILABLE = explicit error such as unknown model or usage limit; INCONCLUSIVE = timeout without OK. Re-probe an INCONCLUSIVE model once; never choose an older model over a newer one because of an INCONCLUSIVE probe alone. Shunt agents need only the Shunt status check.\n'
    "During a run, if a model or Shunt agent is not callable, use the next ordered option for that run and note the error and fallback in the task's _out.md History; do not edit models.md or automatically re-probe. If no remaining suitable option is callable, record blocked.\n"
    'Per dispatch, pick the least capable model and lowest effort from models.md that can plausibly do the job (lookups, greps, mechanical edits: the cheapest model at low effort); if it fails or the result is weak, step up one model or effort level, never start high. Verifiers are exempt (see below).\n'
    'Workers do not spawn further workers unless their _in.md allows it; one level by default. Quick lookup/mechanical edits may use a low-effort subagent selected from models.md.\n'
    'Verification is always by the OTHER model family: Claude-authored work is checked by a Shunt GPT agent, GPT-authored work by Claude; a verifier is never weaker than the author (verifier intelligence rank <= author intelligence rank). Escalate one level on failure. If Shunt is inactive or its GPT agents fail (e.g. usage limit), verify with a Claude model at least as capable as the author, preferably a different one, and note the substitution in History.\n'
    "Workers read agents/<name>_in.md, append progress to agents/<name>_out.md and reuse <task>/models.md. Run workers in the background; state model, effort and why in each worker's _in.md.\n"
    'Claude worker: claude -p --model <model> --effort <effort> --dangerously-skip-permissions "Task: <abs>/agents/<name>_in.md" < /dev/null, or an Agent-tool subagent. GPT worker: a Shunt agent subagent.\n'
    'Long workers (>~30 min): token-kit run --headless --yolo ... for rollover and recording.\n'
)


def load_dispatch_guide() -> str:
    """Read operator guidance afresh for every launch, including successors."""
    try:
        return DISPATCH_GUIDE.read_text(encoding="utf-8").rstrip() + "\n"
    except FileNotFoundError:
        return EFFORT


def summary_request(percent, output, assignment, state=None, asks=None):
    """One bounded handoff request shared by trusted hooks and exec fallback."""
    return (f"Context is at {percent}. Append one brief History entry to {output} for work since "
            "your last update; refresh Current state (sparse, with links to docs/). "
            f"Ensure every ask in {assignment} has a History entry; record any unrecorded asks "
            "verbatim with a date. "
            + (f"Re-read {asks}; make sure every request from every message has a line, open ones first. " if asks else "")
            + (f"Refresh continuation notes in {state}. " if state and str(state) != str(output) else "")
            + "Promote durable repo knowledge to the workspace docs/agents/ (index line, verified commit). "
            + "Do not re-read files you already wrote. End your turn; a fresh session will continue.")
