"""Explicit user preferences shared by startup and recovery guidance."""

CONCURRENCY = (
    "User concurrency preference: bounded lightweight parallel subagents, lanes and background work "
    "are allowed, overriding only /oscar/AGENTS.md's blanket single-thread, no-background-job, "
    "no-parallel-subagent and mandatory global flock restrictions. Use scoped locks or serialized "
    "ownership for overlapping writes and shared resources. Keep heavy computation, large scans "
    "and indexing on authorized compute allocations; retain job-submission approval and polling "
    "restrictions. Respect enforced hooks, application locks, permissions, scheduler controls "
    "and all remaining site guidance."
)

TASK_RULES = (
    "Read _in.md and _out.md first at session start; re-read input about every five minutes and when edited.\n"
    "Managed prompt hooks record dated, numbered verbatim asks automatically. Mark [x] done or [-] dropped (why); "
    "if hooks are unavailable, record asks before work yourself.\n"
    "Keep a sparse Current state at the top of _out.md (done, in progress, next, open decisions). "
    "Append progress every few minutes and never rewrite History.\n"
    "After each ask append ### #N <date> <first line of ask> to History, with a concise 2-6 line response "
    "linking results, commits or files. Before rollover ensure every ask has a History entry and summarize fully.\n"
    "Put longer findings/designs/how-tos in docs/<topic>.md as needed; link them from state/history. "
    "Successors open docs only as needed; no index is required.\n"
    "Optional artifacts/ and agents/<name>_in.md + agents/<name>_out.md follow the same rules. "
    "Keep legacy task.md/in.md, STATE.md/out.md in place; apply these rules to the supplied paths.\n"
)
EFFORT = (
    "Choose each subagent's model and effort deliberately; state why in its _in.md.\n"
    "Use low for lookups/mechanical edits, medium for well-specified implementation, high for unknown "
    "root causes, design and verification whose verdict is trusted.\n"
    "A verifier must never be weaker than its author; escalate one level on failure rather than starting high.\n"
    "Never spawn an agent only to wait or relay.\n"
)
