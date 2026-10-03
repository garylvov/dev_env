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
    "Read both task files at session start; re-read _in.md about every five minutes and whenever it changes.\n"
    "Before working on each user ask, append it verbatim with a date to _in.md as a numbered "
    "[ ] item; mark [x] when done or [-] dropped (why). The user may edit this file any time.\n"
    "Append dated progress to _out.md every few minutes: done, in progress, next, decisions, dead ends "
    "and open asks; fully summarize before rollover so a cold session can continue.\n"
    "Optional artifacts/ and agents/<name>_in.md + agents/<name>_out.md use the same read/update rules. "
    "Keep existing task.md/in.md, STATE.md and out.md layouts in place.\n"
)
EFFORT = (
    "Choose each subagent's model and effort deliberately; state why in its _in.md.\n"
    "Use low for lookups/mechanical edits, medium for well-specified implementation, high for unknown "
    "root causes, design and verification whose verdict is trusted.\n"
    "A verifier must never be weaker than its author; escalate one level on failure rather than starting high.\n"
    "Never spawn an agent only to wait or relay.\n"
)
