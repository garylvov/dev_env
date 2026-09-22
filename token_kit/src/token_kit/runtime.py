"""Managed lifecycle hooks and checkpoint-gated, same-engine rollover.

The hook observes exact transcript paths supplied by the client, never scans a
user's session history. Stop is a turn boundary, not proof external jobs finished.
Current contracts: https://learn.chatgpt.com/docs/hooks and
https://code.claude.com/docs/en/hooks .
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from token_kit.core import ledger
from token_kit.core.store import Store, read_json, write_json

EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolUse", "Stop",
          "SubagentStart", "SubagentStop", "PreCompact")


def token_limit(text: str) -> int:
    value = text.lower()
    multiplier = {"k": 1000, "m": 1000000}.get(value[-1:], 1)
    try:
        result = int(value[:-1] if multiplier != 1 else value) * multiplier
    except ValueError as exc:
        raise ValueError("Use a positive integer token count, optionally suffixed k or m") from exc
    if result <= 0:
        raise ValueError("Token count must be positive")
    return result


def hooks() -> dict:
    command = shlex.join([sys.executable, str(Path(__file__).resolve())])
    return {event: [{"hooks": [{"type": "command", "command": command,
                               "timeout": 3 if event == "SessionEnd" else 15}]}]
            for event in EVENTS}


def codex_config() -> list[str]:
    # JSON strings are valid TOML basic strings; inline objects need TOML '='.
    result = ["--enable", "hooks"]
    for event, groups in hooks().items():
        command = json.dumps(groups[0]["hooks"][0]["command"])
        timeout = groups[0]["hooks"][0]["timeout"]
        value = '[{ hooks = [{ type = "command", command = ' + command + f', timeout = {timeout} }}] }}]'
        result.extend(["-c", f"hooks.{event}={value}"])
    return result


def validate_hooks(result: dict) -> None:
    expected = {event[0].lower() + event[1:] for event in EVENTS}
    command = hooks()["Stop"][0]["hooks"][0]["command"]
    found = {}
    for group in result.get("data", []):
        if group.get("errors"):
            raise ValueError("Codex reported errors loading lifecycle hooks")
        for hook in group.get("hooks", []):
            if hook.get("command") == command and hook.get("source") == "sessionFlags":
                found[hook.get("eventName")] = hook
    if not expected.issubset(found):
        raise ValueError("This Codex build did not load all required lifecycle hooks; protected rollover unavailable")
    if any(not found[event].get("enabled") or found[event].get("trustStatus") != "trusted"
           for event in expected):
        raise ValueError("Codex hooks need trust: run token-kit hooks --engine codex, review Token Kit in /hooks, then retry")


def verify_codex(executable: str = "codex", workspace: Path | None = None) -> None:
    """Inspect local hook inventory/trust without creating a thread or model turn."""
    from token_kit.codex.dispatch import AppServerClient, CodexUnavailable
    try:
        client = AppServerClient([executable, *codex_config(), "app-server", "--stdio"],
                                 dict(os.environ), lambda *args: None)
    except CodexUnavailable as exc:
        raise ValueError("Codex hook inventory could not start") from exc
    try:
        def request(method, params):
            identifier = client.request(method, params)
            deadline = time.monotonic() + 15
            while True:
                message = client.read_message(deadline)
                if message is None:
                    raise ValueError("Codex hook preflight timed out")
                if message.get("id") == identifier:
                    if "error" in message:
                        raise ValueError("Codex hook inventory unavailable; update the client before protected rollover")
                    return message.get("result") or {}
        request("initialize", {"clientInfo": {"name": "token_kit_hook_check", "version": "1"}})
        client.notify("initialized", {})
        validate_hooks(request("hooks/list", {"cwds": [str(workspace or Path.cwd())]}))
    except CodexUnavailable as exc:
        raise ValueError("Codex hook inventory could not be read") from exc
    finally:
        client.close()


def review_hooks() -> int:
    environment = dict(os.environ)
    for name in ("TOKEN_KIT_TASK", "TOKEN_KIT_AGENT", "TOKEN_KIT_RUN"):
        environment.pop(name, None)
    print("Open /hooks and review Token Kit's lifecycle hooks, then exit and rerun token-kit run.", file=sys.stderr)
    return subprocess.call(["codex", *codex_config()], env=environment)


def initialize(store: Store, agent: str, run: Path, engine: str, threshold: int | None) -> None:
    store.safe(run / "usage-cursors").mkdir()
    write_json(run / "runtime.json", {"phase": "running", "engine": engine,
               "threshold": threshold, "session_id": None, "active_children": [], "sample": None})
    ledger.record(store, agent, run.name, engine, {"status": "unavailable", "models": {}})


def halt(control: dict, reason: str) -> dict:
    control.update(phase="halted", reason=reason)
    return {"continue": False, "stopReason": reason, "systemMessage": "Token Kit: " + reason}


def handle(store: Store, agent: str, run: Path, payload: dict) -> dict:
    # Hooks for one run can overlap (native children). Serialize their markers.
    with store.safe(run / "runtime.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        control = read_json(store.safe(run / "runtime.json"))
        result = _handle(store, agent, run, payload, control)
        write_json(store.safe(run / "runtime.json"), control)
        return result


def _handle(store: Store, agent: str, run: Path, payload: dict, control: dict) -> dict:
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return {}
    session = payload.get("session_id")
    if event == "SessionStart" and not control["session_id"]:
        if not session:
            return halt(control, "SessionStart did not identify its session")
        control["session_id"] = session
    native = str(payload.get("agent_id") or "")
    if session and control["session_id"] and session != control["session_id"]:
        native = native or str(session)
    if event == "SubagentStart":
        control["active_children"] = sorted(set(control["active_children"]) | {native or "unknown"})
        ledger.record(store, agent, run.name, control["engine"],
                      {"status": "unavailable", "models": {}}, native or "unknown")
        return {}
    if event == "PreCompact":
        return halt(control, "Compaction requested; stopping rather than compacting. Inspect state and use a lower rollover threshold.")
    transcript = payload.get("agent_transcript_path") if event == "SubagentStop" else payload.get("transcript_path")
    # Parent transcript fields in child hooks must not be billed to the child.
    if native and event != "SubagentStop":
        return {}
    if transcript:
        try:
            key = hashlib.sha256((str(transcript) + "\0" + native).encode()).hexdigest()
            cache = store.safe(run / "usage-cursors" / (key + ".json"))
            sample = ledger.summarize(Path(transcript), control["engine"], sidechain=bool(native), cache=cache)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            sample = {"status": "unavailable", "models": {}, "error": str(exc)}
        ledger.record(store, agent, run.name, control["engine"], sample, native)
        if not native:
            control["sample"] = sample
    if event == "SubagentStop":
        # The native thread is stopping, not necessarily closed. Require the
        # coordinator's checkpoint to reconcile any later continuation/jobs.
        control["active_children"] = [item for item in control["active_children"] if item != native]
        return {}
    if event == "Stop" and not transcript:
        control["sample"] = {"status": "unavailable", "models": {}}
        ledger.record(store, agent, run.name, control["engine"], control["sample"])
    if control["phase"] in ("ready", "halted"):
        if event == "UserPromptSubmit":
            return {"decision": "block", "reason": "Token Kit is stopping this segment; wait for its successor."}
        return {"continue": False, "stopReason": "Token Kit segment ended"}
    if event not in ("Stop", "PostToolUse") or not control["threshold"]:
        return {}
    if event == "PostToolUse" and control["phase"] == "checkpoint_requested":
        return {}
    sample = control.get("sample") or {}
    context = sample.get("context_tokens")
    if sample.get("status") != "reported" or context is None:
        if event == "PostToolUse" and not sample.get("error"):
            return {}  # streaming usage may not be published until the next response
        return halt(control, "Context usage unavailable; automatic rollover cannot proceed safely")
    threshold = control["threshold"]
    if sample.get("context_window"):
        threshold = min(threshold, int(sample["context_window"] * 0.8))
    if context < threshold and control["phase"] == "running":
        return {}
    bundle = store.resume_bundle(agent)
    if control["phase"] == "running":
        control.update(phase="checkpoint_requested", previous_checkpoint=bundle["checkpoint"])
        command = shlex.join(["token-kit", "checkpoint", str(store.path), "--agent", agent])
        reason = (
            "Token Kit rollover: stop starting new work. Drain/reconcile your native children and external jobs. "
            "Update STATE.md with completed work, evidence, unresolved operations, next steps and user overrides; "
            f"then run {command} with relevant --evidence and --incorporated IDs. "
            "Return immediately afterward. A fresh same-engine session will continue from this checkpoint.")
        if event == "PostToolUse":
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": reason}}
        return {"decision": "block", "reason": reason}
    if control["active_children"]:
        return halt(control, "Native children are still active; reconcile them before restarting")
    if bundle["checkpoint"] == control["previous_checkpoint"] or bundle["working_state_changed"]:
        return halt(control, "No fresh committed checkpoint after rollover request; refusing restart")
    if bundle["changed_evidence"] or bundle["head_changed"]:
        return halt(control, "Checkpoint evidence changed before rollover; refusing restart")
    control.update(phase="ready", checkpoint=bundle["checkpoint"])
    return {"continue": False, "stopReason": "Token Kit checkpoint committed; rolling over"}


def wait_segment(child, store: Store, agent: str, run: Path, stop_child,
                 *, startup_timeout: float = 30) -> tuple[int, dict]:
    """Only stop a running client for an explicit hook marker; never kill at a token count."""
    started = time.monotonic()
    while True:
        control = read_json(store.safe(run / "runtime.json"))
        phase = control["phase"]
        if phase in ("ready", "halted"):
            stop_child(child)
            if phase == "ready":
                bundle = store.resume_bundle(agent)
                if (bundle["checkpoint"] != control["checkpoint"] or bundle["working_state_changed"]
                        or bundle["changed_evidence"] or bundle["head_changed"]):
                    halt(control, "Checkpoint changed while stopping the old session")
            return child.wait(), control
        rc = child.poll()
        if rc is not None:
            return rc, control
        if not control["session_id"] and time.monotonic() - started > startup_timeout:
            halt(control, "Lifecycle hook startup not confirmed; check client hook trust/settings")
            stop_child(child)
            write_json(run / "runtime.json", control)
            return child.wait(), control
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    if not os.environ.get("TOKEN_KIT_TASK"):
        # The explicit hook-review command does not start a managed task.
        print("{}")
        return 0
    try:
        task = os.environ["TOKEN_KIT_TASK"]
        agent = os.environ["TOKEN_KIT_AGENT"]
        run_id = os.environ["TOKEN_KIT_RUN"]
        store = Store(Path(task))
        run = store.safe(store.agent_path(agent) / "runs" / run_id)
        raw = sys.stdin.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Hook input exceeds 1MB")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Expected hook object")
        print(json.dumps(handle(store, agent, run, payload)))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Token Kit lifecycle hook failed: {exc}", file=sys.stderr)
        try:
            with store.safe(run / "runtime.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                control = read_json(store.safe(run / "runtime.json"))
                halt(control, f"Lifecycle hook failed: {exc}")
                write_json(store.safe(run / "runtime.json"), control)
        except (OSError, ValueError, KeyError, UnboundLocalError):
            pass
        # Exit 2 at Stop would ask for another model turn and could loop.
        print(json.dumps({"continue": False, "stopReason": "Token Kit lifecycle hook failed"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
