"""Durable logical-worker attempts and transactional parent notifications.

A reservation is not an exactly-once native spawn: after a lost response it stays
uncertain until explicitly reconciled. Stop hooks are observations, never proof
that a native thread or its external jobs have been closed.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from pathlib import Path

from .store import component, fingerprint, now, read_json, workspace_head, write_json

ACTIONABLE = {"checkpoint_requested", "rollover_requested", "completion_requested",
              "needs_reconciliation", "stopped"}


def parent_of(store, agent):
    meta = read_json(store.agent_path(agent) / "agent.json")
    return meta.get("parent_agent", None if agent == "coordinator" else "coordinator")


def path_of(store, agent):
    return store.safe(store.agent_path(agent) / "lifecycle.json")


def read_locked(store, agent):
    path = path_of(store, agent)
    return read_json(path) if path.exists() else None


def event(state, kind, detail):
    state.setdefault("events", []).append({"message_id": uuid.uuid4().hex, "created_at": now(),
        "kind": kind, "agent_id": state["agent_id"], "ticket": state["ticket"],
        "generation": state["generation"], "text": detail})


def publish_locked(store, state):
    # Commit the outbox together with the state. Retrying publication or resuming
    # the parent repairs a crash between this atomic write and message delivery.
    write_json(path_of(store, state["agent_id"]), state)
    deliver_locked(store, state)


def deliver_locked(store, state):
    parent = store.agent_path(state["parent_agent"])
    for item in state.get("events", []):
        target = store.safe(parent / "messages" / (component(item["message_id"]) + ".json"))
        if not target.exists():
            write_json(target, {**item, "source": "worker_lifecycle"})


def children_locked(store, parent):
    result = []
    for directory in sorted((store.path / "agents").iterdir()):
        path = store.safe(directory / "lifecycle.json")
        if not path.is_file():
            continue
        state = read_json(path)
        if state["parent_agent"] == parent:
            if state.get("owner_run") and state["phase"] not in ("stopped", "completed"):
                owner = store.safe(store.agent_path(state["owner_agent"]) / "runs"
                                   / component(state["owner_run"]) / "run.json")
                if read_json(owner)["status"] not in ("starting", "running"):
                    _apply_observation(state, "owner_ended")
                    write_json(path, state)
            deliver_locked(store, state)
            result.append({key: value for key, value in state.items() if key not in ("events", "history")})
    return result


def inspect(store, agent):
    with store.locked():
        state = read_locked(store, agent)
        if state:
            deliver_locked(store, state)
        return state


def checkpoint_locked(store, agent):
    checkpoint = store.latest(agent)
    if checkpoint is None:
        raise ValueError("Worker needs a committed checkpoint")
    manifest = read_json(checkpoint / "manifest.json")
    if fingerprint(store.agent_path(agent) / "STATE.md") != manifest["state_sha256"]:
        raise ValueError("Commit the worker's current STATE.md before continuing")
    if workspace_head(store.workspace) != manifest["head"]:
        raise ValueError("Git HEAD changed; refresh the worker checkpoint")
    for item in manifest["evidence"]:
        path = Path(item["path"])
        if not path.is_file() or fingerprint(path) != item["sha256"]:
            raise ValueError("Worker checkpoint evidence changed; refresh the checkpoint")
    return str(checkpoint)


def current_locked(store, agent, ticket):
    state = read_locked(store, agent)
    if not state or state["ticket"] != ticket:
        raise ValueError("Stale or unknown worker attempt ticket")
    return state


def prepare(store, agent, *, engine=None, model=None, threshold=None, owner_agent=None, owner_run=None):
    from ..worker_policy import brief
    with store.locked():
        parent = parent_of(store, agent)
        if not parent:
            raise ValueError("The coordinator is not a child worker")
        store.agent_path(parent)
        old = read_locked(store, agent)
        for path in (store.agent_path(agent) / "runs").glob("*/run.json"):
            if read_json(store.safe(path))["status"] in ("starting", "running", "interrupted"):
                raise ValueError("Reconcile managed worker runs before reserving a native attempt")
        if old and old["phase"] != "stopped":
            # In particular, repeating prepare after a lost native spawn response
            # NEVER grants a second spawn authorization.
            return {"spawn_authorized": False, "worker": old,
                    "instruction": "Do not spawn again. Inspect/reconcile the current attempt first."}
        checkpoint = checkpoint_locked(store, agent)
        if old and old.get("checkpoint") != checkpoint:
            raise ValueError("Checkpoint changed since stop reconciliation; reconcile the stopped attempt again")
        owner_agent = owner_agent or parent
        ancestor = parent
        while ancestor and ancestor != owner_agent:
            ancestor = parent_of(store, ancestor)
        if not ancestor:
            raise ValueError("Native owner must be the parent or one of its ancestors")
        if owner_run:
            record = read_json(store.safe(store.agent_path(owner_agent) / "runs" / component(owner_run) / "run.json"))
            if record["status"] not in ("starting", "running"):
                raise ValueError("Native owner run is no longer active")
            if threshold is None:
                threshold = record.get("rollover_tokens")
        engine = engine or (old or {}).get("engine")
        if engine not in ("claude", "codex"):
            raise ValueError("Specify --engine claude or codex for the first attempt")
        if threshold is not None and threshold <= 0:
            raise ValueError("Worker rollover threshold must be positive")
        # Validate the task snapshot and construct the complete spawn brief
        # before publishing a launching reservation. A malformed map or prompt
        # cannot strand a hidden ticket that claims a worker was reserved.
        trigger_pyramid = store._trigger_pyramid_locked(seed=True)
        state = {"schema_version": 1, "agent_id": agent, "parent_agent": parent,
                 "phase": "launching", "ticket": uuid.uuid4().hex,
                 "generation": (old or {}).get("generation", 0) + 1,
                 "engine": engine, "model": model or (old or {}).get("model"),
                 "rollover_tokens": threshold if threshold is not None else (old or {}).get("rollover_tokens"),
                 "owner_agent": owner_agent, "owner_run": owner_run,
                 "native_id": None, "checkpoint": checkpoint, "started_checkpoint": checkpoint,
                 "created_at": now(), "events": (old or {}).get("events", []),
                 "history": (old or {}).get("history", [])}
        if old:
            state["history"] = state["history"] + [{key: value for key, value in old.items()
                                                   if key not in ("history", "events")}]
        event(state, "worker_reserved", f"Worker {agent} attempt {state['generation']} reserved. Spawn at most once, then bind its native ID.")
        base = ["token-kit", "worker"]
        request = shlex.join([*base, "request-rollover", str(store.path), "--agent", agent,
                             "--ticket", state["ticket"], "--reason", "Context budget reached"])
        complete = shlex.join([*base, "complete", str(store.path), "--agent", agent, "--ticket", state["ticket"]])
        checkpoint_cmd = shlex.join(["token-kit", "checkpoint", str(store.path), "--agent", agent])
        prompt = (f"Continue logical worker {agent}, attempt {state['generation']}. Read your immutable assignment "
                  f"and checkpoint via token-kit resume {shlex.quote(str(store.path))} --agent {agent}. "
                  f"Your attempt ticket is {state['ticket']}. Before exhausting context, update your STATE.md, "
                  f"run {checkpoint_cmd} with evidence/message IDs, then {request} and return. "
                  f"When finished instead, checkpoint, write out.md, run {complete}, and return. "
                  "Never start your own replacement or use a sibling/parent identity.")
        spawn_prompt = brief(prompt, str(store.path), agent, pyramid=trigger_pyramid)
        publish_locked(store, state)
        return {"spawn_authorized": True, "worker": state,
                "native_task_name": f"{agent}_{state['ticket'][:8]}",
                "spawn_prompt": spawn_prompt}


def _observation_path(store, owner_agent, owner_run, native):
    key = hashlib.sha256(json.dumps([owner_agent, owner_run, native]).encode()).hexdigest()
    return store.safe(store.path / "native-events" / (key + ".json"))


def bind(store, agent, ticket, native):
    if not native.strip() or len(native) > 512:
        raise ValueError("Native ID must be nonempty and at most 512 characters")
    with store.locked():
        state = current_locked(store, agent, ticket)
        if state["phase"] in ("stopped", "completed"):
            raise ValueError("Cannot bind a reconciled attempt")
        if state["native_id"] and state["native_id"] != native:
            raise ValueError("Attempt already bound to a different native worker")
        identity = _identity_path(store, state["owner_agent"], state["owner_run"], native)
        hook_native = read_json(identity)["native_id"] if identity.exists() else native
        for directory in (store.path / "agents").iterdir():
            other = read_locked(store, directory.name)
            for attempt in ([other] + other.get("history", [])) if other else []:
                if (attempt["ticket"] != ticket
                        and ({native, hook_native} & {attempt.get("native_id"), attempt.get("hook_native_id")})
                        and attempt.get("owner_agent") == state["owner_agent"]
                        and attempt.get("owner_run") == state["owner_run"]):
                    raise ValueError("Native worker already belongs to another attempt")
        state["native_id"] = native
        if identity.exists():
            state["hook_native_id"] = hook_native
        state["hook_identity_status"] = ("pending_metadata" if native.startswith("/root/")
                                          and not state.get("hook_native_id") else "bound")
        if state["phase"] == "launching":
            state["phase"] = "running"
        observation = _observation_path(store, state["owner_agent"], state["owner_run"],
                                        state.get("hook_native_id", native))
        if observation.exists():
            _apply_observation(state, read_json(observation)["kind"])
        publish_locked(store, state)
        return state


def request(store, agent, ticket, reason, *, complete=False):
    if not reason.strip() or len(reason) > 2000:
        raise ValueError("A short, nonempty reason is required")
    with store.locked():
        state = current_locked(store, agent, ticket)
        desired = "completion_requested" if complete else "rollover_requested"
        if state["phase"] == desired:
            deliver_locked(store, state)
            return state
        if state["phase"] in ("stopped", "completed", "completion_requested"):
            raise ValueError("Attempt already reconciled or complete")
        checkpoint = checkpoint_locked(store, agent)
        if checkpoint == state["started_checkpoint"]:
            raise ValueError("Commit a fresh checkpoint for this worker attempt")
        if complete:
            output = store.safe(store.agent_path(agent) / "out.md")
            if not output.is_file() or not output.read_text().strip():
                raise ValueError("Write a nonempty out.md before completing the worker")
            state["output_sha256"] = fingerprint(output)
        state.update(phase=desired, checkpoint=checkpoint, reason=reason, requested_at=now())
        event(state, "worker_" + desired, f"Worker {agent}: {reason}. Checkpoint {checkpoint}. "
              "Confirm the native attempt and its operations stopped before replacement; inspect token-kit worker status.")
        publish_locked(store, state)
        return state


def stopped(store, agent, ticket, note):
    if not note.strip() or len(note) > 2000:
        raise ValueError("A stop/reconciliation note is required")
    with store.locked():
        state = current_locked(store, agent, ticket)
        for path in (store.agent_path(agent) / "runs").glob("*/run.json"):
            if read_json(store.safe(path))["status"] in ("starting", "running", "interrupted"):
                raise ValueError("Reconcile managed worker runs before confirming native stop")
        checkpoint = checkpoint_locked(store, agent)
        if state["phase"] == "completed":
            return state
        if state["phase"] == "completion_requested":
            if checkpoint != state["checkpoint"]:
                raise ValueError("Worker checkpoint changed after completion request")
            if fingerprint(store.safe(store.agent_path(agent) / "out.md")) != state["output_sha256"]:
                raise ValueError("Worker result changed after completion request")
            phase = "completed"
        else:
            phase = "stopped"
        state.update(phase=phase, checkpoint=checkpoint, reconciliation=note, reconciled_at=now())
        event(state, "worker_" + phase, f"Worker {agent}: native stop explicitly reconciled. "
              + ("Result is complete." if phase == "completed" else "A fresh attempt may now be reserved."))
        publish_locked(store, state)
        return state


def _apply_observation(state, kind):
    if state["phase"] in ("stopped", "completed"):
        return
    if state.get("last_native_observation") == kind:
        return
    state["last_native_observation"] = kind
    if state["phase"] not in ("rollover_requested", "completion_requested"):
        state["phase"] = "needs_reconciliation"
    event(state, "worker_native_" + kind, f"Worker {state['agent_id']}: native {kind} observed. "
          "This is not proof of closure. Inspect its checkpoint/result and close or reconcile the native attempt.")


def _identity_path(store, owner_agent, owner_run, alias):
    key = hashlib.sha256(json.dumps([owner_agent, owner_run, alias]).encode()).hexdigest()
    return store.safe(store.path / "native-identities" / (key + ".json"))


def record_native_identity(store, owner_agent, owner_run, alias, native):
    """Associate a verified transcript identity, never guess from a worker name."""
    if not alias.startswith("/root/") or not native or len(alias) > 512:
        return
    with store.locked():
        target = _identity_path(store, owner_agent, owner_run, alias)
        if target.exists() and read_json(target)["native_id"] != native:
            raise ValueError("Native path reused with a different thread; use a unique attempt path")
        for directory in (store.path / "agents").iterdir():
            other = read_locked(store, directory.name)
            for attempt in ([other] + other.get("history", [])) if other else []:
                if (attempt.get("owner_agent") == owner_agent and attempt.get("owner_run") == owner_run
                        and native in (attempt.get("native_id"), attempt.get("hook_native_id"))
                        and attempt.get("native_id") != alias):
                    raise ValueError("Native UUID already bound under another identity")
        write_json(target, {"native_id": native, "alias": alias})
        for directory in (store.path / "agents").iterdir():
            state = read_locked(store, directory.name)
            if (state and state.get("owner_agent") == owner_agent and state.get("owner_run") == owner_run
                    and state.get("native_id") == alias):
                if state.get("hook_native_id") == native:
                    continue
                state["hook_native_id"] = native
                state["hook_identity_status"] = "bound"
                observation = _observation_path(store, owner_agent, owner_run, native)
                if observation.exists():
                    _apply_observation(state, read_json(observation)["kind"])
                publish_locked(store, state)


def observe_native(store, owner_agent, owner_run, native, kind):
    with store.locked():
        target = _observation_path(store, owner_agent, owner_run, native)
        write_json(target, {"kind": kind, "native_id": native, "observed_at": now()})
        for directory in (store.path / "agents").iterdir():
            state = read_locked(store, directory.name)
            if (state and state.get("owner_agent") == owner_agent and state.get("owner_run") == owner_run
                    and native in (state.get("native_id"), state.get("hook_native_id"))):
                _apply_observation(state, kind)
                publish_locked(store, state)
                return state
    return None


def native_worker(store, owner_agent, owner_run, native):
    with store.locked():
        for directory in (store.path / "agents").iterdir():
            state = read_locked(store, directory.name)
            if (state and state.get("owner_agent") == owner_agent and state.get("owner_run") == owner_run
                    and native in (state.get("native_id"), state.get("hook_native_id"))):
                return state
    return None


def budget_nudge(store, agent, ticket, context, window=None):
    with store.locked():
        state = current_locked(store, agent, ticket)
        threshold = state.get("rollover_tokens")
        if state["phase"] not in ("running", "launching") or not threshold or context is None:
            return None
        threshold = min(threshold, int(window * .8)) if window else threshold
        if context < threshold:
            return None
        state["phase"] = "checkpoint_requested"
        event(state, "worker_checkpoint_requested", f"Worker {agent} reached its context target; checkpoint and handoff requested.")
        publish_locked(store, state)
        return (f"Token Kit worker budget reached. Update your own STATE.md, then run "
                + shlex.join(["token-kit", "checkpoint", str(store.path), "--agent", agent])
                + " with relevant evidence. Then run "
                + shlex.join(["token-kit", "worker", "request-rollover", str(store.path), "--agent", agent,
                              "--ticket", ticket, "--reason", "Context budget reached"])
                + " and return. Do not start new work or your own replacement.")


def notice(store, parent):
    with store.locked():
        children = children_locked(store, parent)
    pending = [state for state in children if state["phase"] in ACTIONABLE]
    if not pending:
        return None
    signature = hashlib.sha256(json.dumps(pending, sort_keys=True).encode()).hexdigest()
    rows = [{"agent": row["agent_id"], "ticket": row["ticket"], "phase": row["phase"]} for row in pending[:8]]
    text = ("Token Kit worker lifecycle needs attention: " + json.dumps(rows) + ". "
            "Run " + shlex.join(["token-kit", "resume", str(store.path), "--agent", parent]) +
            " for all child states and pending messages. For checkpoint_requested, let the worker finish its handoff. "
            "For other requests, inspect/close the old native attempt, "
            "record worker stopped with its ticket and a reconciliation note, then worker prepare "
            "to reserve a replacement. Spawn only when spawn_authorized is true; bind its returned native ID. "
            "Do not acknowledge completion or blindly duplicate an uncertain worker.")
    return signature, text
