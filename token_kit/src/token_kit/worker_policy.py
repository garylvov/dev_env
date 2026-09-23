"""Small, portable worker instructions; no transcript or project installation.

Also executable as a Claude PreToolUse command hook. Input rewriting deliberately
omits a permission decision, leaving the client's permission checks in place.
See https://code.claude.com/docs/en/hooks#pretooluse-decision-control .
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys


POLICY = """<!-- token-kit worker policy v1 -->
Main thread: orchestrator only; workers execute. If delegation is blocked, explain
why and ask before substantial direct execution.
Follow applicable repository instructions and the assignment's source boundaries.
Use your own stable agent record: in.md, STATE.md, out.md, checkpoints/, artifacts/.
Never use the parent's identity. Request a record before source edits.
Recover with token-kit resume TASK --agent ID; read its assignment, committed state,
and messages. Verify evidence and unfinished operations before repeating work.
STATE.md is the current snapshot: replace stale status; keep history in artifacts/checkpoints.
Preserve unresolved actions and scoped model overrides. Sections: Objective, Completed,
Evidence, Unresolved, Next. Checkpoint
after milestones and before returning: token-kit checkpoint TASK --agent ID.
Include changed-file --evidence and addressed --incorporated message IDs; detail in
artifacts/, final result in out.md. Agents maintain checkpoints.

Explicit scoped user model/provider/effort requests override these ordered defaults:
plan/implement Opus -> Astra; review/debug Astra -> Opus; loops Luna -> Sonnet -> Terra
-> Sol; docs Sol -> Luna -> Sonnet; scout/summarize/mechanical-edit Luna -> Sonnet.
Effort: medium; Luna high except scouting/mechanical xhigh. Fable is explicit-only. "Use Codex" or
"conserve Claude" excludes Claude fallbacks. Skip unavailable candidates; record
override scope/expiry in state. Do not infer availability or silently switch
an explicitly required model. Stay in your scoped role. If complexity exceeds it,
checkpoint and report evidence/blockers via your parent to the main thread; only
the main thread authorizes scope/model promotion within user constraints.

Prefer native same-engine delegation, nesting when permitted. Create children using
worker prepare --brief TEXT --parent YOUR_ID; pass scoped overrides, not transcripts.
Prefer completion notifications; avoid short waits and status-only messages.
Use token-kit worker prepare/bind; spawn only when authorized. Track direct children
with token-kit resume TASK --agent YOUR_ID; status TASK shows all attempts/parents.
Honor attempt tickets: checkpoint, request-rollover or complete, then return. Parents
confirm worker stopped before replacements; never repeat an uncertain spawn.
<!-- /token-kit worker policy -->"""


def brief(text: str, task: str, agent: str | None = None) -> str:
    """Refresh leading policy wrappers without changing the assignment body."""
    prefix = POLICY + "\nToken Kit record: "
    previous = None
    opening = re.compile(r"<!-- token-kit worker policy(?: v[\w.-]+)? -->\n")
    closing = re.compile(r"^<!-- /token-kit worker policy -->(?:\n|$)", re.MULTILINE)
    while start := opening.match(text):
        end = closing.search(text)
        # Do not let a broken outer marker consume a later complete wrapper.
        if end is None or "<!-- token-kit worker policy" in text[start.end():end.start()]:
            break
        original = text[end.end():]
        record = None
        if original.startswith("Token Kit record: "):
            raw, separator, body = original[len("Token Kit record: "):].partition("\n\n")
            try:
                record = json.loads(raw)
            except ValueError:
                break
            if (not separator or not isinstance(record, dict)
                    or not isinstance(record.get("task"), str)
                    or ("agent" in record and not isinstance(record["agent"], str))):
                break
            original = body
        elif original.startswith("\n"):
            original = original[1:]
        if previous is None and record is not None:
            previous = record
        text = original
    context = {"task": str(task)}
    if agent is not None:
        context["agent"] = agent
    elif previous is not None and previous["task"] == str(task) and "agent" in previous:
        context["agent"] = previous["agent"]
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
