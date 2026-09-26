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
