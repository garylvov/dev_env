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

try:
    from .pyramid import read_pyramid
except ImportError:  # Direct hook execution: this file's directory is on sys.path.
    from pyramid import read_pyramid

_OPEN = "<!-- token-kit worker policy v2 -->"
_CLOSE = "<!-- /token-kit worker policy -->"
_BODY = """Main: orchestrator only; workers execute. Blocked: explain; ask before execution. Follow repo rules.
Own identity. Resume: token-kit resume TASK --agent ID.
STATE: Objective/Completed/Evidence/Unresolved/Next; ~200 words. Compression only: dated verbatim STATE in
historical_state.md; no initial/routine checkpoint/recovery append. Read selectively; checkpoint evidence/message IDs.

Smart coordinator; bounded Mid work. Announce route/tier/model/reason/changes. Scoped overrides beat map; isolate siblings.
Map supersedes default prose, not explicit assignments. Escalate for complexity, not delay: checkpoint/report to the main thread.

Native prepare/bind --parent; status TASK. Checkpoint, ticketed rollover/complete, confirm stop.
Never retry uncertain spawn. Managed recovery: structured compaction or exact legacy run/runtime evidence;
Never unknown stops/errors/manual interrupts. Reconcile/checkpoint/close prior run per matrix. Dead PID proves no external completion; parent exit no native closure.
Orphans: worker retire per agent_trigger_matrix.md.
Retired means neither native closure nor success/retry authority. Replace remaining work only.
Live/unknown operations block; report once, never poll."""

# Kept as a stable base for callers and documentation. ``brief`` renders the
# current map between these same replaceable markers.
POLICY = f"{_OPEN}\n{_BODY}\n{_CLOSE}"


def _render(pyramid: dict[str, str]) -> str:
    content = pyramid["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Trigger pyramid content must be nonempty text")
    return f"{_OPEN}\n{_BODY}\n{content.rstrip()}\n{_CLOSE}"


def brief(text: str, task: str, agent: str | None = None,
          *, pyramid: dict[str, str] | None = None) -> str:
    """Refresh leading policy wrappers without changing the assignment body."""
    current = pyramid if pyramid is not None else read_pyramid(task)
    prefix = _render(current) + "\nToken Kit record: "
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
