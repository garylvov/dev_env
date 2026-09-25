"""Durable logical-worker attempts and transactional parent notifications.

A reservation is not an exactly-once native spawn: after a lost response it stays
uncertain until explicitly reconciled. Stop hooks are observations, never proof
that a native thread or its external jobs have been closed.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import socket
import uuid
from datetime import datetime
from pathlib import Path

from .. import rollover
from .store import component, fingerprint, now, process_identity, read_json, workspace_head, write_json

ACTIONABLE = {"checkpoint_requested", "rollover_requested", "completion_requested",
              "needs_reconciliation"}


def execution_reconciled(state):
    return (state.get("phase") in ("stopped", "completed") or
            (state.get("phase") == "retired" and state.get("operations_reconciled") is True))


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
            if state.get("owner_run") and not execution_reconciled(state):
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
    history = store.safe(store.agent_path(agent) / "historical_state.md")
    history_hash = manifest.get("historical_state_sha256")
    if (history_hash is None and history.exists()) or (history_hash is not None and
            (not history.is_file() or fingerprint(history) != history_hash)):
        raise ValueError("Worker historical state changed; refresh the checkpoint")
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
    # Normalize before entering the reservation workflow so malformed input
    # cannot leave a partially prepared attempt behind.
    if threshold is not None:
        threshold = rollover.parse_limit(threshold)
    with store.locked():
        parent = parent_of(store, agent)
        if not parent:
            raise ValueError("The coordinator is not a child worker")
        store.agent_path(parent)
        old = read_locked(store, agent)
        for path in (store.agent_path(agent) / "runs").glob("*/run.json"):
            if read_json(store.safe(path))["status"] in ("starting", "running", "interrupted"):
                raise ValueError("Reconcile managed worker runs before reserving a native attempt")
        if old and not (old["phase"] == "stopped" or
                        (old["phase"] == "retired" and old.get("operations_reconciled") is True)):
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
                if threshold is not None:
                    threshold = rollover.parse_limit(threshold)
        engine = engine or (old or {}).get("engine")
        if engine not in ("claude", "codex"):
            raise ValueError("Specify --engine claude or codex for the first attempt")
        if threshold is None and old and old.get("rollover_tokens") is not None:
            threshold = rollover.parse_limit(old["rollover_tokens"])
        # Validate the task snapshot and construct the complete spawn brief
        # before publishing a launching reservation. A malformed map or prompt
        # cannot strand a hidden ticket that claims a worker was reserved.
        trigger_pyramid = store._trigger_pyramid_locked(seed=True)
        state = {"schema_version": 1, "agent_id": agent, "parent_agent": parent,
                 "phase": "launching", "ticket": uuid.uuid4().hex,
                 "generation": (old or {}).get("generation", 0) + 1,
                 "engine": engine, "model": model or (old or {}).get("model"),
                 "rollover_tokens": threshold,
                 "effective_threshold": None, "observed_window": None,
                 "rollover_error": None,
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
        result = {"spawn_authorized": True, "worker": state,
                  "native_task_name": f"{agent}_{state['ticket'][:8]}",
                  "spawn_prompt": spawn_prompt}
        if engine == "claude":
            command = ["token-kit", "launch", str(store.path), "--agent", agent,
                       "--ticket", state["ticket"], "--engine", engine]
            if state.get("model"):
                command += ["--model", state["model"]]
            result["managed_launch_command"] = shlex.join(command)
            if isinstance(threshold, str) and threshold.endswith("%"):
                result["managed_launch_note"] = ("Claude does not report a context window; "
                    "supply an absolute --rollover-tokens budget.")
        return result


def claim_managed_locked(store, agent, ticket, engine, model, recovery_from=None):
    """Validate a reservation before Store publishes its managed run under the same lock."""
    state = current_locked(store, agent, ticket)
    if state.get("phase") in ("stopped", "completed", "retired"):
        raise ValueError("Managed worker attempt is already reconciled")
    if state.get("engine") != engine or state.get("model") != model:
        raise ValueError("Managed launch engine/model must match the reserved worker")
    if state.get("native_id") or state.get("hook_native_id"):
        raise ValueError("Worker reservation is already bound to a native worker")
    if state.get("owner_run"):
        owner = read_json(store.safe(store.agent_path(state["owner_agent"]) / "runs"
                                    / component(state["owner_run"]) / "run.json"))
        if owner.get("status") not in ("starting", "running"):
            raise ValueError("Managed worker owner run is no longer active")
    previous = state.get("managed_run_id")
    if previous:
        record = read_json(store.safe(store.agent_path(agent) / "runs" / component(previous) / "run.json"))
        if record.get("worker_ticket") != ticket:
            raise ValueError("Managed worker run/ticket linkage is inconsistent")
        if recovery_from == previous:
            # Store subsequently validates exact structured recovery evidence,
            # process closure, and all recovery-chain invariants.
            if record.get("status") != "interrupted":
                raise ValueError("Managed worker recovery predecessor is not interrupted")
        elif not (recovery_from is None and state.get("segment_ready") is True
                  and record.get("status") == "exited"):
            raise ValueError("Managed worker ticket was already consumed; reconcile its run")
        store._assert_process_dead(record, "child")
    elif state.get("phase") != "launching":
        raise ValueError("Managed launch requires a fresh launching worker reservation")
    elif recovery_from is not None:
        raise ValueError("Fresh worker reservation cannot claim another attempt's recovery")
    if recovery_from is None:
        checkpoint_locked(store, agent)
    return state


def finish_managed(store, agent, ticket, run_id, *, returncode, rollover_ready=False):
    """Record a supervised client's exit, never infer external-operation completion.

    The launcher must wait for the child and persist its terminal run record first.
    Only a worker's committed completion request proves logical task completion.
    """
    with store.locked():
        state = current_locked(store, agent, ticket)
        if state.get("managed_run_id") != run_id:
            raise ValueError("Managed worker exit does not match its current run")
        record = read_json(store.safe(store.agent_path(agent) / "runs" / component(run_id) / "run.json"))
        if record.get("worker_ticket") != ticket or record.get("status") not in ("exited", "interrupted"):
            raise ValueError("Managed worker needs a matching terminal run record")
        if record.get("host") != socket.gethostname():
            raise ValueError("Managed worker exit must be confirmed on its original host")
        store._assert_process_dead(record, "child")
        if execution_reconciled(state):
            return state
        phase = "needs_reconciliation"
        if (returncode == 0 or rollover_ready) and record.get("status") == "exited" and state.get("phase") == "completion_requested":
            checkpoint = checkpoint_locked(store, agent)
            if checkpoint != state["checkpoint"]:
                raise ValueError("Worker checkpoint changed after completion request")
            if fingerprint(store.safe(store.agent_path(agent) / "out.md")) != state["output_sha256"]:
                raise ValueError("Worker result changed after completion request")
            phase = "completed"
        elif (rollover_ready or (returncode == 0 and state.get("phase") == "rollover_requested")) and record.get("status") == "exited":
            checkpoint = checkpoint_locked(store, agent)
            if checkpoint == state["started_checkpoint"]:
                raise ValueError("Managed rollover requires a fresh checkpoint")
            if state.get("phase") == "rollover_requested" and checkpoint != state["checkpoint"]:
                raise ValueError("Worker checkpoint changed after rollover request")
            state.update(checkpoint=checkpoint, started_checkpoint=checkpoint, segment_ready=True)
            phase = "rollover_requested"
        state.update(phase=phase, managed_exit_code=returncode, managed_exited_at=now())
        if phase == "completed":
            state.update(reconciled_at=now(), reconciliation="Managed client exited after verified completion request")
        event(state, "worker_" + phase, f"Worker {agent}: managed client exited; phase {phase}.")
        publish_locked(store, state)
        return state


def _observation_path(store, owner_agent, owner_run, native):
    key = hashlib.sha256(json.dumps([owner_agent, owner_run, native]).encode()).hexdigest()
    return store.safe(store.path / "native-events" / (key + ".json"))


def bind(store, agent, ticket, native):
    if not native.strip() or len(native) > 512:
        raise ValueError("Native ID must be nonempty and at most 512 characters")
    with store.locked():
        state = current_locked(store, agent, ticket)
        if state.get("managed_run_id"):
            raise ValueError("Managed worker attempt cannot bind a native worker")
        if state["phase"] in ("stopped", "completed", "retired"):
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
        if state["phase"] in ("stopped", "completed", "retired", "completion_requested"):
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
        if state["phase"] == "retired":
            raise ValueError("Retired attempts cannot claim native closure")
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


def retire(store, agent, ticket, note, *, operations_reconciled=False,
           recovery_agent=None, recovery_run=None):
    """Fence an orphan's execution authority; never assert native closure or success."""
    if operations_reconciled is not True:
        raise ValueError("--operations-reconciled is required: inspect operations and establish none are live or uncertain")
    if not note.strip() or len(note) > 2000:
        raise ValueError("A reconciliation note describing inspected operations and outcomes is required")
    if not recovery_agent or not recovery_run:
        raise ValueError("An identifiable recovery agent and run are required")
    with store.locked():
        state = current_locked(store, agent, ticket)
        if state["phase"] in ("stopped", "completed", "retired"):
            raise ValueError("Attempt already reconciled")
        index = store._validate_recovery_links_locked()
        successor_entry = index.get((recovery_agent, recovery_run))
        if successor_entry is None:
            raise ValueError("Unknown recovery successor")
        successor = successor_entry[1]
        ancestors = store._recovery_ancestors_locked(recovery_agent, recovery_run, index)
        old_id = state.get("owner_run")
        old = next((record for record in ancestors if record.get("run_id") == old_id), None)
        if old is None or state.get("owner_agent") != recovery_agent:
            raise ValueError("Worker does not belong to this recovery predecessor")
        leaf = store._active_recovery_leaf_locked(recovery_agent, old_id, index)[1]
        if (leaf.get("run_id") != recovery_run
                or not successor.get("recovery_pending")
                or successor.get("recovery_completed")
                or old.get("status") != "interrupted"
                or not old.get("recovery_pending")):
            raise ValueError("An active linked recovery successor is required")
        supervisor_pid = store._pid(successor, "supervisor")
        supervisor_identity = successor.get("supervisor_identity")
        if (supervisor_pid is None or not supervisor_identity
                or process_identity(supervisor_pid) != supervisor_identity
                or store._process_dead(successor, "supervisor")):
            raise ValueError("Recovery successor supervisor identity is not confirmed active")
        if state.get("owner_agent") != recovery_agent or state.get("owner_run") != old_id:
            raise ValueError("Worker does not belong to this recovery predecessor")
        if not (state.get("engine") == old.get("engine") == successor.get("engine")):
            raise ValueError("Recovery and orphan must use the same engine")
        if old.get("host") != socket.gethostname() or successor.get("host") != socket.gethostname():
            raise ValueError("Cross-host orphan retirement is not supported")
        if store._pid(old, "child") is None:
            raise ValueError("Recorded predecessor child PID is required")
        store._assert_process_dead(old, "child")
        store._assert_supervisor_close_safe(old, successor)
        for path in (store.agent_path(agent) / "runs").glob("*/run.json"):
            if read_json(store.safe(path))["status"] in ("starting", "running", "interrupted"):
                raise ValueError("Reconcile managed worker runs before retirement")
        checkpoint = checkpoint_locked(store, agent)
        if checkpoint == state["started_checkpoint"]:
            raise ValueError("Commit a fresh worker checkpoint recording operations and unresolved outcomes")
        manifest = read_json(Path(checkpoint) / "manifest.json")
        try:
            checkpoint_time = datetime.fromisoformat(manifest["created_at"])
            recovery_time = datetime.fromisoformat(successor["created_at"])
            if checkpoint_time.tzinfo is None or recovery_time.tzinfo is None:
                raise ValueError("timestamps must include timezone")
            if checkpoint_time < recovery_time:
                raise ValueError("checkpoint predates recovery")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Commit a fresh worker checkpoint during this recovery") from exc
        state.update(phase="retired", checkpoint=checkpoint, operations_reconciled=True,
                     reconciliation=note, reconciled_at=now(),
                     retirement={"recovery_agent": recovery_agent, "recovery_run": recovery_run,
                                 "predecessor_run": old_id, "host": old["host"],
                                 "child_pid": old["child_pid"],
                                 "child_identity": old.get("child_identity"),
                                 "proof": "recorded owner process dead; execution authority retired",
                                 "native_closure_confirmed": False})
        event(state, "worker_retired", f"Worker {agent}: orphan execution authority retired; "
              "operations inspected with no live or uncertain operations. Outcomes remain in the checkpoint. "
              "This does not imply completion, successful publication, or permission to retry.")
        publish_locked(store, state)
        return state


def _apply_observation(state, kind):
    if state["phase"] in ("stopped", "completed", "retired"):
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
            if (state and state.get("phase") != "retired"
                    and state.get("owner_agent") == owner_agent and state.get("owner_run") == owner_run
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
        requested = state.get("rollover_tokens")
        if state["phase"] not in ("running", "launching") or not requested or context is None:
            return None
        try:
            threshold = rollover.effective_limit(requested, window)
        except ValueError as exc:
            # Native hooks cannot safely raise through the parent client. Make
            # the uncertainty durable and let the normal parent notice/outbox
            # path request reconciliation.
            reason = str(exc)
            previous_phase = state["phase"]
            changed = (state.get("rollover_error") != reason
                       or previous_phase != "needs_reconciliation"
                       or state.get("observed_window") != window)
            state["rollover_error"] = reason
            state["effective_threshold"] = None
            state["observed_window"] = window
            state["phase"] = "needs_reconciliation"
            if previous_phase in ("running", "launching"):
                event(state, "worker_rollover_unresolved",
                      f"Worker {agent} rollover target could not be resolved: {reason}. Reconcile its native attempt.")
            if changed:
                publish_locked(store, state)
            return None
        changed = (state.get("effective_threshold") != threshold
                   or state.get("observed_window") != window
                   or state.get("rollover_error") is not None)
        state["effective_threshold"] = threshold
        state["observed_window"] = window
        state["rollover_error"] = None
        if context < threshold:
            if changed:
                publish_locked(store, state)
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
    rows = sorted(({"agent": row["agent_id"], "ticket": row["ticket"], "phase": row["phase"]}
                   for row in pending), key=lambda row: (row["agent"], row["ticket"], row["phase"]))
    signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    rows = rows[:8]
    text = ("Token Kit worker lifecycle needs attention: " + json.dumps(rows) + ". "
            "Run " + shlex.join(["token-kit", "resume", str(store.path), "--agent", parent]) +
            " for all child states and pending messages. For checkpoint_requested, let the worker finish its handoff. "
            "For other requests, inspect/close the old native attempt, "
            "record worker stopped when native closure is confirmed. For an orphan owned by a dead predecessor, "
            "a linked recovery session may checkpoint the worker and use " +
            shlex.join(["token-kit", "worker", "retire", str(store.path)]) +
            " --agent ID --ticket T --note TEXT --operations-reconciled after inspecting operations "
            "and confirming none are live or uncertain. Recovery identity defaults to TOKEN_KIT_AGENT/RUN. "
            "Do not ask a dead runner to confirm closure. "
            "Retirement does not claim native closure or task success. Use worker prepare only if work remains. "
            "Spawn only when spawn_authorized is true; bind its returned native ID. "
            "Do not acknowledge completion or blindly duplicate an uncertain worker.")
    return signature, text
