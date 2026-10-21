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
    "Docs ship with features, from the start: repo docs (README, docs/, --help) hold only high-level run info (what, how to "
    "run/configure, examples; an In progress status line until done), updated in the same commit as the code; an asks.md "
    "feature is not done until documented. Implementation detail goes in docs/agents/, scratch in the task docs/. "
    "One fact, one home: extend an existing doc before adding one; every other doc links to that home, never copies (internal "
    "docs cite repo docs for how to run, repo docs link detail in docs/agents/); "
    "promoting a note moves it and leaves a link.\n"
)

TASK_RULES = (
    "Start by reading asks.md, the last five _in.md messages, and _out.md Current state and recent History; re-read input every ~5 minutes.\n"
    "_in.md is the conversation: hooks append each user message verbatim, dated and numbered. "
    "Never edit it; status lives only in asks.md. Without hooks, append asks before work yourself.\n"
    "<task>/asks.md: every request in every user message gets a line, including asides buried in longer messages, "
    "as a full sentence, with [ ] [~] [x] [-] status, where/why and sources (from #3, #7). "
    "Keep open and in-progress asks at the top, done and dropped below.\n"
    "Group asks under short ## feature headings; over five open features, end replies with a proposed priority order.\n"
    "When an ask conflicts with or supersedes another, mark both and say which wins; never delete. Update asks.md before ending "
    "each turn that had a user message (create it if missing). Re-read it before declaring work finished, ending a turn or rollover; "
    "your final reply lists every still-open ask.\n"
    "Keep a sparse Current state at the top of _out.md (done, in progress, next, open decisions). "
    "Append progress every few minutes and never rewrite History.\n"
    "After each ask append ### #N <date> <first line of ask> to History, with a concise 2-6 line response "
    "linking results, commits or files. Put longer findings in docs/<topic>.md, linked from state/history.\n"
    "<task>/experiments.md: hooks append a [?] line per job launch under ## Unlabeled; label it at once (question, where "
    "results land) and move it to ## Open, close it with verdict: <line> -> <doc link> (or why:), list open ones in final replies, "
    "promote verdicts useful beyond the task to docs/agents/.\n"
    "Hooks record each Agent-tool worker's prompt and final report in agents/<name>_in.md and _out.md; "
    "workers append dated progress lines to _out.md every few minutes.\n"
)
EFFORT = (
    'For every non-trivial task, use an agent hierarchy: the orchestrator plans, splits, dispatches and verifies; workers do leaf work in parallel.\n'
    'Use parallel subagents judiciously for separable work, regardless of /oscar/AGENTS.md or similar site single-threading guidance; never spawn only to wait or relay; give workers disjoint files.\n'
    'The main session is always Claude. GPT models are reached only as Shunt-backed Claude Code subagents (Agent tool, agent files in ~/.claude/agents/, no model override, name each <task>-<model>-<effort>) when `systemctl --user is-active shunt` prints active; otherwise every role uses Claude models.\n'
    'The main (top-level) agent decides model routing in <task>/models.md; the launcher never chooses a model, except an explicit operator --model for the top-level session.\n'
    'The main agent builds <task>/models.md only if it does not exist; it stays fixed for the task across starts, continue, rollover successors and workers. Rebuild or edit only when the user asks. Existing matrices require no discovery or probes.\n'
    "Only when building models.md, the main agent fetches and reads https://platform.claude.com/docs/en/models/overview and https://platform.claude.com/docs/en/about-claude/pricing with its web/fetch tool, plus OpenAI's official model and per-token pricing pages for the models pinned in each Shunt agent file (read the files; never assume the newest slug). Cite exact URLs with the date read, record Shunt status with date, and note any disagreement with Claude CLI aliases (claude --help, /model). If no fetch tool is available, say so in models.md and fall back to local sources.\n"
    'models.md (~30 lines): a candidate table over Claude models and Shunt agents with USD per 1M input/output tokens, context window, one-line capability note, and `cost rank` (1 = cheapest) and `intelligence rank` (1 = most capable) on unified scales over both families; cite dated vendor positioning and label judgment, never infer intelligence from price. Subscription logins use API prices only as a RELATIVE proxy for usage-limit burn; state this in one line.\n'
    'Each role (orchestrator, implementation worker, quick lookup, reviewer/verifier, cheap fallback) lists ORDERED options spanning both families: model or Shunt agent + effort + reason showing both ranks (e.g. "cost 3, intel 2") + source/date. Cheapest capable option first, Shunt agents winning cost ties while active; the main session is typically Claude Opus by operator preference, labeled as an override.\n'
    'Only when building the matrix, probe Claude candidates in parallel with a 30 s timeout per model: timeout 30s claude -p --model <model> --effort low "reply OK" < /dev/null. PASS = exact OK reply; UNAVAILABLE = explicit error such as unknown model or usage limit; INCONCLUSIVE = timeout without OK. Re-probe an INCONCLUSIVE model once; never choose an older model over a newer one because of an INCONCLUSIVE probe alone. Shunt agents need only the Shunt status check.\n'
    "During a run, if a model or Shunt agent is not callable, use the next ordered option for that run and note the error and fallback in the task's _out.md History; do not edit models.md or automatically re-probe. If no remaining suitable option is callable, record blocked.\n"
    'Per dispatch, pick the least capable model and lowest effort from models.md that can plausibly do the job (lookups, greps, mechanical edits: the cheapest model at low effort); if it fails or the result is weak, step up one model or effort level, never start high. Verifiers are exempt (see below).\n'
    'Workers do not spawn further workers unless their _in.md allows it; one level by default. Quick lookup/mechanical edits may use a low-effort subagent selected from models.md.\n'
    'Verification is always by the OTHER model family: Claude-authored work is checked by a Shunt GPT agent, GPT-authored work by Claude; a verifier is never weaker than the author (verifier intelligence rank <= author intelligence rank). Escalate one level on failure. If Shunt is inactive or its GPT agents fail (e.g. usage limit), verify with a Claude model at least as capable as the author, preferably a different one, and note the substitution in History.\n'
    "Workers read agents/<name>_in.md, append progress to agents/<name>_out.md and reuse <task>/models.md. Run workers in the background; state model, effort and why in each worker's _in.md.\n"
    'Workers are Agent-tool subagents (Claude models, or Shunt agents for GPT), which hooks record. Long workers (>~30 min): token-kit run --headless for rollover and recording, adding --yolo only if this session runs with it.\n'
)


def load_dispatch_guide() -> str:
    """Read operator guidance afresh for every launch, including successors."""
    try:
        return DISPATCH_GUIDE.read_text(encoding="utf-8").rstrip() + "\n"
    except FileNotFoundError:
        return EFFORT


def summary_request(percent, output, asks=None):
    """One bounded handoff request shared by trusted hooks and exec fallback."""
    return (f"Context is at {percent}. Append a History entry to {output} for the current ask and refresh "
            "Current state (sparse, with links to docs/)"
            + (f"; list the open asks from {asks}" if asks else "")
            + ". Do not re-read files you already wrote. End your turn; a fresh session will continue.")


# The successor does the slow bookkeeping, so a summary never races native compaction.
SUCCESSOR = ("This session continues after a context rollover. First label any [?] lines in experiments.md, "
             "update repo docs for features that landed and promote durable repo knowledge to docs/agents/.")


def greeting(folder, rollover, codegraph):
    """Kickoff for a new task launched without --prompt; answered from the system prompt alone."""
    return (f"New Token Kit task. Without reading files or running tools (except `systemctl --user is-active shunt`), "
            f"reply in at most 8 lines: the task folder {folder} and what _in.md, asks.md, _out.md, experiments.md, "
            "docs/, agents/ and models.md are for; how you will delegate (cheapest capable model per dispatch, "
            "Shunt GPT agents if active, cross-family verification); "
            f"rollover {rollover}; CodeGraph {codegraph}. Then ask what to work on.")
