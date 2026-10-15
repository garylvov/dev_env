"""gen_agents.py -- thin generic agent files, generated from the guide.

WHY FILES EXIST AT ALL.  Effort is a real agent-frontmatter key and it reaches
the runtime per FILE.  Measured live, CLI 2.1.278, with a PreToolUse hook that
dumped its own stdin:

    agent frontmatter `effort: low`   -> the SUBAGENT's hook stdin carried
                                         effort = {"level": "low"}
    the same file changed to `high`   -> effort = {"level": "high"}
    the main thread, both runs        -> effort = {"level": "medium"}

A file can carry one model and one effort, so the unit is one file per (kind,
distinct claude candidate), plus ONE generic codex runner whose body is the
dispatch instructions.

WHAT THESE FILES ARE NOT.  They carry no persona, no project, no doctrine
pointer and no prose worth reading: the guide holds the judgement and the
spawn header carries the rest.  Every body below is four facts and a budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import ladder, matrix as matrix_mod

#: A description the CLI's own auto-selection will never pick by accident:
#: these files are for the router to name, not for a model to choose.
DESC_PREFIX = "kit agent for the"

PREFIX = "kit"


def agent_name(kind: str, model: str, effort: str) -> str:
    return f"{PREFIX}-{kind}-{model}-{effort}"


def runner_name(codex: dict | None = None) -> str:
    codex = codex if codex is not None else ladder.codex_settings()
    return str(codex.get("runner_agent") or "kit-codex-runner")


def body(row: dict, cand: ladder.Candidate, budget: tuple[int, int, int]) -> str:
    warn, floor, hard = budget
    kind = str(row.get("name"))
    return "\n".join([
        "---",
        f"name: {agent_name(kind, cand.model, cand.effort)}",
        f"description: {DESC_PREFIX} '{kind}' kind - {row.get('use_when', '')}",
        f"model: {cand.model}",
        f"effort: {cand.effort}",
        "---",
        "",
        f"[KIND: {kind} | {row.get('shape_words', row.get('shape', ''))}]",
        f"use when: {row.get('use_when', '')}",
        f"done when: {row.get('stop', '')} -- stop there, and say so.",
        f"budget: warn {warn} / floor {floor} / hard {hard} calls; past the floor only a",
        "write of your own out.md and SubagentHandback are permitted.",
        "",
    ])


def runner_body(codex: dict, budget: tuple[int, int, int]) -> str:
    warn, floor, hard = budget
    return "\n".join([
        "---",
        f"name: {runner_name(codex)}",
        f"description: {DESC_PREFIX} codex dispatch - runs one step through codex and reports it",
        "model: sonnet",
        "effort: medium",
        "---",
        "",
        "You do not do this step yourself: you dispatch it to codex and report what came back.",
        "The spawn header names the model and the effort to use; codex refuses to assume either.",
        "",
        f"  {codex.get('dispatch', '')}",
        "",
        f"For a long step you want to steer or be told about, use "
        f"`{codex.get('job', 'codex-job')} start|send|wait|stop` instead and run `wait` as a",
        "BACKGROUND command, so being told it finished costs no polling call.",
        "",
        f"Exit {codex.get('refusal_code', 42)} means codex is unavailable "
        f"(reason=absent|auth|busy|protocol|quota): do the step yourself with the Claude model in",
        "the header, and say in your report that codex refused and why.",
        "",
        f"budget: warn {warn} / floor {floor} / hard {hard} calls; past the floor only a",
        "write of your own out.md and SubagentHandback are permitted.",
        "",
    ])


def generate(m, out_dir: Path) -> list[Path]:
    """One file per (kind, distinct claude candidate), plus the codex runner."""
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = m.budget()
    codex = ladder.codex_settings()
    written: list[Path] = []
    for row in m.rows:
        kind = str(row.get("name") or "")
        if not kind:
            continue
        seen: set[tuple[str, str]] = set()
        for cand in ladder.candidates(row):
            if cand.is_codex or (cand.model, cand.effort) in seen:
                continue
            seen.add((cand.model, cand.effort))
            path = out_dir / f"{agent_name(kind, cand.model, cand.effort)}.md"
            path.write_text(body(row, cand, budget), encoding="utf-8")
            written.append(path)
    runner = out_dir / f"{runner_name(codex)}.md"
    runner.write_text(runner_body(codex, budget), encoding="utf-8")
    written.append(runner)
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
