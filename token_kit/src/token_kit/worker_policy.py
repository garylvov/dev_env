"""Small, portable worker instructions; no transcript or project installation.

Also executable as a Claude PreToolUse command hook. Input rewriting deliberately
omits a permission decision, leaving the client's permission checks in place.
See https://code.claude.com/docs/en/hooks#pretooluse-decision-control .
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys


POLICY = """<!-- token-kit worker policy v1 -->
Follow applicable repository instructions and the assignment's source boundaries.
Use your own stable agent record: in.md, STATE.md, out.md, checkpoints/, artifacts/.
Never use the parent's identity. If no record is assigned, ask the coordinator to
register one with token-kit agent before changing source files.
Recover with token-kit resume TASK --agent ID; read its assignment, committed state,
and pending messages. Verify evidence and unfinished operations before repeating work.
Maintain STATE.md sections Objective, Completed, Evidence, Unresolved, Next. Checkpoint
after milestones and before returning: token-kit checkpoint TASK --agent ID.
Include changed-file --evidence and addressed --incorporated message IDs. Put detail in
artifacts/ and the final result in out.md. Checkpoints are agent-maintained, not automatic.

Explicit scoped user model/provider/effort requests override these ordered defaults:
plan/implement Opus -> Astra; review/debug Astra -> Opus; loops Luna -> Sonnet -> Terra
-> Sol; docs Sol -> Luna -> Sonnet; scout/summarize/mechanical-edit Luna -> Sonnet.
Effort: medium; Luna high except scouting/mechanical xhigh. Fable is explicit-only. "Use Codex" or
"conserve Claude" excludes Claude fallbacks. Skip unavailable candidates; record
overrides and their scope/expiry in state. Do not infer availability or silently switch
an explicitly required model. Stay in your scoped role. If complexity exceeds it,
checkpoint and report evidence/blockers via your parent to the main thread; only
the main thread authorizes scope/model promotion within user constraints.

Prefer native same-engine delegation, including nesting when permitted. Register
each child with --parent YOUR_ID. Pass this policy and scoped overrides, not transcripts.
Use token-kit worker prepare/bind; spawn only when authorized. Track direct children
with token-kit resume TASK --agent YOUR_ID; status TASK shows all attempts/parents.
Honor your attempt ticket:
checkpoint, request-rollover or complete, then return. Parents confirm closure with
worker stopped before reserving replacements; never blindly repeat an uncertain spawn.
<!-- /token-kit worker policy -->"""


def brief(text: str, task: str, agent: str | None = None) -> str:
    """Idempotently add policy, preserving the original assignment verbatim."""
    prefix = POLICY + "\nToken Kit record: "
    if text.startswith(prefix):
        record, separator, original = text[len(prefix):].partition("\n\n")
        try:
            previous = json.loads(record)
        except ValueError:
            previous = None
        if separator and isinstance(previous, dict):
            if previous.get("task") == str(task) and (agent is None or previous.get("agent") == agent):
                return text
            # A coordinator may reuse a brief for another registered worker.
            # Replace the record reference instead of inheriting the old identity.
            text = original
    context = {"task": str(task)}
    if agent is not None:
        context["agent"] = agent
    return prefix + json.dumps(context) + "\n\n" + text


def managed_brief(text: str) -> str:
    """Opt in only inside a managed session; never inherit the parent's identity."""
    task = os.environ.get("TOKEN_KIT_TASK")
    return brief(text, task) if task else text


def hook_command(task: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), str(task)])


def rewrite(payload: object, task: str) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return {}
    if payload.get("tool_name") not in ("Agent", "Task"):
        return {}
    arguments = payload.get("tool_input")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("prompt"), str):
        return {}
    updated = brief(arguments["prompt"], task)
    if updated == arguments["prompt"]:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "updatedInput": {**arguments, "prompt": updated}}}


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        raw = sys.stdin.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("hook input exceeds 1MB")
        result = rewrite(json.loads(raw), sys.argv[1])
    except (ValueError, TypeError) as exc:
        print(f"Token Kit worker policy could not be applied: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
