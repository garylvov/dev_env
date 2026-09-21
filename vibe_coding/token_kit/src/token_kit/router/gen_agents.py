"""gen_agents.py -- one agent file per (row, claude candidate), from the table.

WHY THIS FILE EXISTS, and what changed the answer.  The previous lane searched
the binary for an agent-frontmatter `effort` key, did not find one, and
concluded that effort could only ride in the prompt header.  That conclusion
was wrong, and it was wrong in the expensive direction.  Measured here, live,
CLI 2.1.278, with a PreToolUse hook that dumped its own stdin:

    agent frontmatter `effort: low`   -> the SUBAGENT's hook stdin carried
                                         effort = {"level": "low"}
    the same file changed to `high`   -> effort = {"level": "high"}
    the main thread, both runs        -> effort = {"level": "medium"}

So `effort` IS a frontmatter key, it reaches the runtime, and it is per FILE.
That is why this generator emits one file per (row, claude candidate) rather
than one per row: a row whose `prefer` list names two Claude tiers at two
efforts needs two files, because a file can only carry one of each.

WHAT IT DOES NOT DO.  It does not re-point the router at these files.  The
behaviour contract (router_cases.jsonl) pins `subagent_type` to each row's
`agent` column -- grizzly-veteran for harness-broker, general-purpose for
cluster-state -- and a persona IS the load for those rows (measured: the four
persona bodies are 0.04% of all spend, so replacing them saves nothing and
loses the judgement).  Switching subagent_type to a generated file is a
separate, guarded change to the cases; see the lane's out.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import ladder, matrix as matrix_mod

#: A description the CLI's own auto-selection will never pick by accident:
#: these files are for the router to name, not for a model to choose.
DESC_PREFIX = "matrix row"


def agent_name(row_name: str, cand: ladder.Candidate) -> str:
    return f"row-{row_name}-{cand.model}-{cand.effort}"


def body(row: dict, cand: ladder.Candidate, budget: tuple[int, int, int]) -> str:
    warn, floor, hard = budget
    lines = [
        "---",
        f"name: {agent_name(str(row.get('name')), cand)}",
        f"description: {DESC_PREFIX} {row.get('name')} - {row.get('use_when', '')}",
        f"model: {cand.model}",
        f"effort: {cand.effort}",
    ]
    tools = row.get("tools")
    if tools:
        lines.append(f"tools: {', '.join(tools) if isinstance(tools, list) else tools}")
    lines += [
        "---",
        "",
        f"[MATRIX ROW: {row.get('name')} | shape={row.get('shape', 'claude-direct')}]",
        f"load: {row.get('load') or 'none - the row carries its own judgement'}",
        f"stop: {row.get('stop') or '(none named)'}",
        f"max_report: {row.get('max_report', 2000)} bytes - a longer report is truncated "
        f"with a pointer",
        f"repo_home: {row.get('repo_home', 'unset')} (law 7)",
        f"effort: {cand.effort}",
    ]
    if row.get("reviewer"):
        lines.append(f"reviewer: {row['reviewer']} must run on the result before it is done")
    lines += [
        f"budget: warn {warn} / floor {floor} / hard {hard} calls; past the floor only a",
        "write of your own out.md and SubagentHandback are permitted.",
        "",
    ]
    return "\n".join(lines)


def generate(m, out_dir: Path) -> list[Path]:
    """Write one file per (row, distinct claude candidate).  Returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = m.budget()
    written: list[Path] = []
    for row in m.rows:
        name = str(row.get("name") or "")
        if not name or row.get("shape") == "refuse":
            continue
        seen: set[tuple[str, str]] = set()
        for cand in ladder.candidates(row):
            if cand.is_codex or (cand.model, cand.effort) in seen:
                continue
            seen.add((cand.model, cand.effort))
            path = out_dir / f"{agent_name(name, cand)}.md"
            path.write_text(body(row, cand, budget), encoding="utf-8")
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: gen_agents <output-dir> [--matrix <path>]", file=sys.stderr)
        return 2
    out_dir = Path(argv[0])
    src = None
    if "--matrix" in argv:
        src = argv[argv.index("--matrix") + 1]
    written = generate(matrix_mod.load(src), out_dir)
    for p in written:
        print(p)
    print(f"gen_agents: {len(written)} file(s) -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
