"""Portable checkpoints, messages and execution records, with one writer per task.

No provider transcript is needed to construct a fresh-session resume bundle.
Locks coordinate local operations; this module makes no distributed takeover claim.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import uuid
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
SECTIONS = ("Objective", "Completed", "Evidence", "Unresolved", "Next")
LEGACY_COMPACTION_REASON = (
    "Compaction requested; stopping rather than compacting. "
    "Inspect state and use a lower rollover threshold."
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path}")
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_bytes(path: Path, data: bytes) -> None:
    """Atomically publish bytes while preserving their exact hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_identity(pid: int) -> str | None:
    """Linux process start ticks protect against PID reuse; unknown elsewhere."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("IDs must be one path component containing letters, digits, _ or -")
    return value


def task_component(value: str) -> str:
    """Task folder names include local clock colons; agent IDs remain stricter."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,127}", value):
        raise ValueError("Task ID must be one safe task-folder component")
    return value


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_head(workspace: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Store:
    def __init__(self, task: Path | str):
        self.path = Path(task).resolve()
        meta = read_json(self.safe(self.path / "task.json"))
        if meta.get("schema_version") != SCHEMA:
            raise ValueError("Unsupported task schema")
        self.workspace = Path(meta["workspace"])
        self.task_id = meta["task_id"]

    def safe(self, path: Path) -> Path:
        if not path.is_relative_to(self.path):
            raise ValueError("Path escapes task")
        for item in (path, *path.parents):
            if item == self.path:
                break
            if item.is_symlink():
                raise ValueError(f"Task internals must not be symlinks: {item}")
        return path

    @classmethod
    def create(cls, root: Path, title: str, workspace: Path, *, assignment: str | None = None) -> "Store":
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir() or not title.strip():
            raise ValueError("A workspace directory and nonempty title are required")
        if assignment is not None and not assignment.strip():
            raise ValueError("Assignment must be nonempty when supplied")
        from ..pyramid import NAME, read_pyramid
        defaults = read_pyramid()
        normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")[:64].rstrip("_") or "session"
        local = datetime.now().astimezone()
        months = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sept", "oct", "nov", "dec")
        stamp = f"{local.hour % 12 or 12}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'}_{months[local.month - 1]}{local.day}"
        # Short UUID suffixes can collide. Reserve atomically and retry without
        # touching existing tasks; the full creation date remains in metadata.
        for _ in range(32):
            task_id = f"{slug}_{stamp}_{uuid.uuid4().hex[:4]}"
            path = root.resolve() / task_id
            try:
                path.mkdir(parents=True)
                break
            except FileExistsError:
                continue
        else:
            raise ValueError("Could not reserve a unique task folder; retry creation")
        sync_directory(path.parent)
        write_json(path / "task.json", {"schema_version": SCHEMA, "task_id": task_id,
                   "title": title, "status": "open", "workspace": str(workspace), "created_at": now()})
        store = cls(path)
        with store.locked():
            atomic_text(store.safe(path / NAME), defaults["content"])
        store.add_agent("coordinator", title if assignment is None else assignment)
        return store

    def update_task(self, **fields) -> None:
        if set(fields) - {"title", "summary", "status"}:
            raise ValueError("Only display title, summary and task status can be changed")
        if "title" in fields and not fields["title"].strip():
            raise ValueError("Task title cannot be empty")
        if "status" in fields and fields["status"] not in ("open", "done"):
            raise ValueError("Task status must be open or done")
        with self.locked():
            if fields.get("status") == "done":
                for path in (self.path / "agents").glob("*/lifecycle.json"):
                    if read_json(self.safe(path))["phase"] != "completed":
                        raise ValueError("Reconcile/complete worker lifecycle attempts before closing the task")
                for path in (self.path / "agents").glob("*/runs/*/run.json"):
                    record = read_json(self.safe(path))
                    if record.get("status") in ("starting", "running", "interrupted"):
                        raise ValueError("Reconcile active or interrupted runs before closing the task")
            path = self.safe(self.path / "task.json")
            value = read_json(path)
            value.update(fields, updated_at=now())
            write_json(path, value)

    @contextlib.contextmanager
    def locked(self):
        with self.safe(self.path / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def trigger_pyramid(self, *, seed: bool = False) -> dict[str, str]:
        """Read the current map, optionally seeding a missing task snapshot."""
        with self.locked():
            return self._trigger_pyramid_locked(seed=seed)

    def _trigger_pyramid_locked(self, *, seed: bool = False) -> dict[str, str]:
        """Caller owns the task lock; existing snapshots are never overwritten."""
        from ..pyramid import NAME, read_pyramid
        target = self.safe(self.path / NAME)
        current = read_pyramid(self.path)
        if seed and current["source"] == "repository":
            atomic_text(target, current["content"])
            current = {"path": str(target), "source": "task", "content": current["content"]}
        return current

    def agent_path(self, agent: str) -> Path:
        path = self.safe(self.path / "agents" / component(agent))
        for name in ("agent.json", "STATE.md", "historical_state.md", "in.md", "assignments", "checkpoints", "messages", "runs", "artifacts"):
            self.safe(path / name)
        if not (path / "agent.json").is_file():
            raise ValueError(f"Unknown agent: {agent}")
        if path.is_symlink():
            raise ValueError("Agent directories must not be symlinks")
        return path

    def add_agent(self, agent: str, assignment: str, parent: str | None = None) -> Path:
        from ..worker_policy import brief
        component(agent)
        if not assignment.strip():
            raise ValueError("Assignment cannot be empty")
        parent = parent or (None if agent == "coordinator" else "coordinator")
        if parent == agent or (agent == "coordinator" and parent is not None):
            raise ValueError("Invalid parent agent")
        with self.locked():
            if parent:
                self.agent_path(parent)
            pyramid = self._trigger_pyramid_locked()
            saved = brief(assignment, str(self.path), agent, pyramid=pyramid)
            path = self.safe(self.path / "agents" / agent)
            path.mkdir(parents=True, exist_ok=False)
            sync_directory(path.parent)
            for name in ("assignments", "checkpoints", "messages", "runs", "artifacts"):
                (path / name).mkdir()
            write_json(path / "agent.json", {"schema_version": SCHEMA, "agent_id": agent,
                       "parent_agent": parent,
                       "assignment_revision": 1, "created_at": now(), "latest_checkpoint": None})
            atomic_text(path / "assignments" / "0001.md", saved + "\n")
            atomic_text(path / "in.md", saved + "\n")
            atomic_text(path / "STATE.md", "# Agent state\n\n## Objective\n" + assignment +
                        "\n\n## Completed\nNone yet.\n\n## Evidence\nNone yet.\n\n"
                        "## Unresolved\nVerify the assignment and workspace.\n\n"
                        "## Next\nRead in.md, inspect the relevant source, and begin.\n")
        self.checkpoint(agent)
        return path

    def checkpoint(self, agent: str, evidence: list[str] | None = None,
                   incorporated: list[str] | None = None) -> Path:
        with self.locked():
            path = self.agent_path(agent)
            state = (path / "STATE.md").read_text(encoding="utf-8")
            if len(state.encode()) > 32_000:
                raise ValueError("STATE.md exceeds 32KB; move detailed history into historical_state.md or artifacts")
            for section in SECTIONS:
                match = re.search(r"^## " + section + r"\s*\n(.*?)(?=^## |\Z)", state, re.M | re.S)
                if not match or not match.group(1).strip():
                    raise ValueError(f"STATE.md requires nonempty ## {section}")
            meta = read_json(path / "agent.json")
            incorporated_ids = set(incorporated or [])
            previous = self.latest(agent)
            if previous:
                incorporated_ids.update(read_json(previous / "manifest.json")["incorporated_messages"])
            for message in incorporated_ids:
                if not self.safe(path / "messages" / (component(message) + ".json")).is_file():
                    raise ValueError(f"Unknown message: {message}")
            facts = []
            # Routine checkpoints retain previously declared evidence paths.
            entries = evidence if evidence is not None else ([e["path"] for e in
                       read_json(previous / "manifest.json")["evidence"]] if previous else [])
            for entry in entries:
                target = Path(entry)
                target = (target if target.is_absolute() else self.workspace / target).resolve(strict=True)
                if not (target.is_relative_to(self.workspace) or target.is_relative_to(self.path)):
                    raise ValueError("Evidence must be inside the source workspace or task")
                facts.append({"path": str(target), "sha256": fingerprint(target)})
            checkpoint_id = uuid.uuid4().hex
            snapshot = path / "checkpoints" / checkpoint_id
            snapshot.mkdir()
            sync_directory(snapshot.parent)
            atomic_text(snapshot / "STATE.md", state)
            historical = self.safe(path / "historical_state.md")
            historical_info = None
            if historical.exists():
                if not historical.is_file() or historical.is_symlink():
                    raise ValueError("historical_state.md must be a regular file")
                history_bytes = historical.read_bytes()
                atomic_bytes(snapshot / "historical_state.md", history_bytes)
                historical_info = {"path": str(historical),
                                   "sha256": fingerprint(snapshot / "historical_state.md")}
            manifest = {"schema_version": SCHEMA, "checkpoint_id": checkpoint_id,
                        "agent_id": agent, "task_id": self.task_id, "created_at": now(), "assignment_revision": meta["assignment_revision"],
                        "assignment_sha256": fingerprint(self.safe(path / "assignments" / f'{meta["assignment_revision"]:04d}.md')),
                        "workspace": str(self.workspace), "head": workspace_head(self.workspace),
                        "state_sha256": fingerprint(snapshot / "STATE.md"), "evidence": facts,
                        "incorporated_messages": sorted(incorporated_ids),
                        "run_ids": sorted(p.name for p in (path / "runs").iterdir() if p.is_dir()),
                        "coverage": "agent-declared checkpoint; tool operations are not yet intercepted"}
            # This field is optional for backwards compatibility with manifests
            # written before bounded history snapshots existed.
            if historical_info is not None:
                manifest["historical_state_path"] = historical_info["path"]
                manifest["historical_state_sha256"] = historical_info["sha256"]
            write_json(snapshot / "manifest.json", manifest)
            # Snapshot is complete before publishing the pointer. Orphan snapshots are harmless.
            meta["latest_checkpoint"] = checkpoint_id
            write_json(path / "agent.json", meta)
            if agent == "coordinator":
                atomic_text(self.safe(self.path / "STATE.md"), state)
            return snapshot

    def latest(self, agent: str) -> Path | None:
        path = self.agent_path(agent)
        checkpoint = read_json(path / "agent.json").get("latest_checkpoint")
        if checkpoint is None:
            return None
        snapshot = self.safe(path / "checkpoints" / component(checkpoint))
        manifest = read_json(self.safe(snapshot / "manifest.json"))
        if (manifest.get("schema_version") != SCHEMA or manifest.get("task_id") != self.task_id
                or manifest.get("agent_id") != agent or manifest.get("checkpoint_id") != checkpoint
                or manifest.get("workspace") != str(self.workspace)):
            raise ValueError("Checkpoint identity or workspace does not match task")
        if manifest["state_sha256"] != fingerprint(self.safe(snapshot / "STATE.md")):
            raise ValueError("Committed checkpoint has been modified; refusing recovery")
        history_hash = manifest.get("historical_state_sha256")
        if history_hash is not None:
            history_snapshot = self.safe(snapshot / "historical_state.md")
            if (not history_snapshot.is_file()
                    or fingerprint(history_snapshot) != history_hash):
                raise ValueError("Committed historical_state.md has been modified; refusing recovery")
        assignment = self.safe(path / "assignments" / f'{manifest["assignment_revision"]:04d}.md')
        if manifest["assignment_sha256"] != fingerprint(assignment):
            raise ValueError("Committed assignment has been modified; refusing recovery")
        return snapshot

    def send(self, agent: str, text: str) -> str:
        if not text.strip() or len(text.encode()) > 32_000:
            raise ValueError("Message must be nonempty and at most 32KB")
        with self.locked():
            path = self.agent_path(agent)
            message = uuid.uuid4().hex
            write_json(path / "messages" / (message + ".json"),
                       {"message_id": message, "created_at": now(), "text": text})
            return message

    def resume_bundle(self, agent: str) -> dict:
        with self.locked():
            from .lifecycle import children_locked
            path = self.agent_path(agent)
            children = children_locked(self, agent)
            snapshot = self.latest(agent)
            if snapshot is None:
                raise ValueError("No committed checkpoint")
            manifest = read_json(snapshot / "manifest.json")
            messages = [read_json(self.safe(p)) for p in (path / "messages").glob("*.json")]
            messages = sorted((m for m in messages if m["message_id"] not in manifest["incorporated_messages"]),
                              key=lambda m: (m["created_at"], m["message_id"]))
            changed = [e["path"] for e in manifest["evidence"]
                       if not Path(e["path"]).is_file() or fingerprint(Path(e["path"])) != e["sha256"]]
            history_path = self.safe(path / "historical_state.md")
            history_checkpoint = (self.safe(snapshot / "historical_state.md")
                                  if manifest.get("historical_state_sha256") is not None else None)
            history_hash = manifest.get("historical_state_sha256")
            if history_hash is None:
                history_changed = history_path.exists()
            else:
                history_changed = (not history_path.is_file()
                                   or fingerprint(history_path) != history_hash)
            return {"agent_id": agent, "workspace": str(self.workspace), "checkpoint": str(snapshot),
                    "trigger_pyramid": self._trigger_pyramid_locked(),
                    "children": children,
                    "assignment": str(path / "assignments" / f'{manifest["assignment_revision"]:04d}.md'),
                    "pending_messages": messages, "changed_evidence": changed,
                    "head_changed": workspace_head(self.workspace) != manifest["head"],
                    "working_state_changed": fingerprint(path / "STATE.md") != manifest["state_sha256"],
                    "working_state": str(path / "STATE.md"),
                    "historical_state": str(history_path),
                    "checkpoint_historical_state": str(history_checkpoint) if history_checkpoint else None,
                    "historical_state_changed": history_changed,
                    "runs": [read_json(self.safe(p)) for p in sorted((path / "runs").glob("*/run.json"))]}

    def _run_records_locked(self) -> list[tuple[str, Path, dict]]:
        """Return all managed run records; caller owns the task lock."""
        rows = []
        agents = self.safe(self.path / "agents")
        if not agents.is_dir() or agents.is_symlink():
            raise ValueError("Task agents directory is missing or unsafe")
        for directory in sorted(agents.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or directory.is_symlink():
                continue
            runs = self.safe(directory / "runs")
            if not runs.is_dir() or runs.is_symlink():
                continue
            for target in sorted(runs.glob("*/run.json")):
                target = self.safe(target)
                rows.append((directory.name, target, read_json(target)))
        return rows

    @staticmethod
    def _run_error_unmarked(record: dict) -> bool:
        """An error blocks takeover until an agent explicitly marks it handled."""
        if record.get("unmarked_errors") or record.get("unresolved_errors"):
            return True
        if record.get("error"):
            return not bool(record.get("error_marked") or record.get("error_resolved"))
        errors = record.get("errors")
        if errors:
            return not bool(record.get("errors_marked") or record.get("errors_resolved"))
        return False

    def _validate_recovery_links_locked(self, rows: list[tuple[str, Path, dict]] | None = None) -> dict[tuple[str, str], tuple[Path, dict]]:
        """Validate every recovery edge. Incomplete edges are never repaired."""
        rows = self._run_records_locked() if rows is None else rows
        index = {(agent, record.get("run_id")): (target, record)
                 for agent, target, record in rows if record.get("run_id")}
        for agent, target, record in rows:
            run_id = record.get("run_id")
            outgoing = record.get("recovery_to")
            incoming = record.get("recovery_from")
            if outgoing is not None:
                successor = index.get((agent, outgoing))
                if successor is None or successor[1].get("recovery_from") != run_id:
                    raise ValueError(f"Incomplete recovery link for run {run_id}; refusing automatic repair")
                if record.get("recovery_pending") is not True and not successor[1].get("recovery_completed"):
                    raise ValueError(f"Recovery reservation for run {run_id} has inconsistent pending state")
            if incoming is not None:
                predecessor = index.get((agent, incoming))
                if predecessor is None or predecessor[1].get("recovery_to") != run_id:
                    raise ValueError(f"Incomplete recovery link for run {run_id}; refusing automatic repair")
                if record.get("recovery_pending") is not True and not record.get("recovery_completed"):
                    raise ValueError(f"Recovery reservation for run {run_id} has inconsistent pending state")
            if record.get("recovery_pending") and not (outgoing or incoming):
                raise ValueError(f"Unfinished recovery reservation for run {run_id}")
        for agent, _, record in rows:
            seen = set()
            cursor = record
            while cursor.get("recovery_to") is not None:
                identity = cursor.get("run_id")
                if identity in seen:
                    raise ValueError("Cyclic recovery links; refusing continuation")
                seen.add(identity)
                cursor = index[(agent, cursor["recovery_to"])][1]
        return index

    def _recovery_ancestors_locked(self, agent: str, run_id: str, index: dict) -> list[dict]:
        if (agent, run_id) not in index:
            raise ValueError("Unknown recovery run")
        result = []
        cursor = index[(agent, run_id)][1]
        while cursor.get("recovery_from") is not None:
            cursor = index[(agent, cursor["recovery_from"])][1]
            result.append(cursor)
        return list(reversed(result))

    def recovery_ancestors(self, agent: str, run_id: str) -> list[dict]:
        with self.locked():
            index = self._validate_recovery_links_locked()
            return self._recovery_ancestors_locked(agent, component(run_id), index)

    def _active_recovery_leaf_locked(self, agent: str, run_id: str, index: dict) -> tuple[Path, dict]:
        entry = index[(agent, run_id)]
        while entry[1].get("recovery_to") is not None:
            entry = index[(agent, entry[1]["recovery_to"])]
        if entry[1].get("status") not in ("starting", "running"):
            raise ValueError("Recovery successor is not active; refusing reconciliation")
        if self._continuation_candidate_locked(agent, index) != entry[1]["run_id"]:
            raise ValueError("Recovery successor is not the unique continuation tip")
        return entry

    def _continuation_candidate_locked(self, agent: str, index: dict | None = None) -> str | None:
        index = self._validate_recovery_links_locked() if index is None else index
        unresolved_owners = set()
        for directory in self.safe(self.path / "agents").iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            lifecycle = self.safe(directory / "lifecycle.json")
            if lifecycle.is_file():
                state = read_json(lifecycle)
                if (state.get("owner_agent") == agent
                        and not (state.get("phase") in ("stopped", "completed") or
                                 (state.get("phase") == "retired" and state.get("operations_reconciled") is True))):
                    unresolved_owners.add(state.get("owner_run"))
        active = [record for (owner, run_id), (_, record) in index.items()
                  if owner == agent and (record.get("status") in ("starting", "running", "interrupted")
                                         or (record.get("continuation_requested") is True and record.get("status") != "reconciled")
                                         or (record.get("status") == "exited" and run_id in unresolved_owners))]
        if not active:
            return None
        tips = [record for record in active if record.get("recovery_to") is None]
        if len(tips) != 1:
            raise ValueError("Disconnected or ambiguous continuation runs")
        tip = tips[0]
        ancestors = self._recovery_ancestors_locked(agent, tip["run_id"], index)
        connected = {record["run_id"] for record in ancestors} | {tip["run_id"]}
        if any(record["run_id"] not in connected for record in active):
            raise ValueError("Disconnected continuation runs")
        if any(record.get("status") not in ("interrupted", "reconciled") for record in ancestors):
            raise ValueError("Continuation ancestors must be interrupted or reconciled")
        return tip["run_id"]

    def continuation_candidate(self, agent: str) -> str | None:
        with self.locked():
            return self._continuation_candidate_locked(agent)

    @staticmethod
    def _pid(record: dict, prefix: str) -> int | None:
        value = record.get(prefix + "_pid")
        if value is None:
            return None
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid {prefix} PID in run record") from exc
        if value <= 0:
            raise ValueError(f"Invalid {prefix} PID in run record")
        return value

    @classmethod
    def _process_dead(cls, record: dict, prefix: str) -> bool:
        pid = cls._pid(record, prefix)
        if pid is None:
            return True
        expected = record.get(prefix + "_identity")
        actual = process_identity(pid)
        # A different start identity proves the recorded process is gone even
        # when its PID has already been reused by an unrelated process.
        if expected and actual and expected != actual:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise ValueError(f"Cannot establish that old {prefix} process stopped") from exc
        except OSError as exc:
            raise ValueError(f"Cannot establish that old {prefix} process stopped") from exc
        return False

    @classmethod
    def _assert_process_dead(cls, record: dict, prefix: str) -> None:
        if not cls._process_dead(record, prefix):
            raise ValueError(f"Old {prefix} process may still be alive; refusing recovery")

    @classmethod
    def _assert_supervisor_takeover_safe(cls, record: dict) -> None:
        pid = cls._pid(record, "supervisor")
        expected = record.get("supervisor_identity")
        current = process_identity(os.getpid())
        if pid == os.getpid() and expected and current == expected:
            return
        cls._assert_process_dead(record, "supervisor")

    @classmethod
    def _assert_supervisor_close_safe(cls, old: dict, successor: dict) -> None:
        pid = cls._pid(old, "supervisor")
        successor_pid = cls._pid(successor, "supervisor")
        old_identity = old.get("supervisor_identity")
        successor_identity = successor.get("supervisor_identity")
        if (successor.get("status") in ("starting", "running")
                and pid is not None and pid == successor_pid and old_identity
                and old_identity == successor_identity
                and process_identity(pid) == successor_identity):
            return
        cls._assert_process_dead(old, "supervisor")

    def _compaction_recovery_source(self, target: Path, record: dict) -> str | None:
        """Classify a parent halt without mutating historical run evidence."""
        kind = record.get("halt_kind")
        if kind == "compaction":
            return "structured_compaction"
        if kind is not None or record.get("rollover_error") != LEGACY_COMPACTION_REASON:
            return None
        try:
            control = read_json(self.safe(target.parent / "runtime.json"))
        except (OSError, ValueError):
            return None
        if (control.get("phase") != "halted"
                or control.get("reason") != LEGACY_COMPACTION_REASON
                or control.get("halt_kind") not in (None, "compaction")
                or control.get("engine", record.get("engine")) != record.get("engine")):
            return None
        return "legacy_compaction_corroborated"

    def _recovery_candidate_locked(self, agent: str) -> str | None:
        path = self.agent_path(agent)
        rows = [(owner, target, record) for owner, target, record in self._run_records_locked()
                if owner == agent]
        index = self._validate_recovery_links_locked()
        active = [record for _, _, record in rows
                  if record.get("status") in ("starting", "running", "interrupted")]
        candidates = []
        for _, target, record in rows:
            if (record.get("status") == "interrupted"
                    and not record.get("continuation_requested")
                    and self._compaction_recovery_source(target, record) is not None):
                if self._run_error_unmarked(record):
                    continue
                if record.get("recovery_pending") or record.get("recovery_to"):
                    raise ValueError(f"Run {record.get('run_id')} has an unfinished recovery reservation")
                predecessor_id = record.get("recovery_from")
                if predecessor_id is not None:
                    predecessor = index.get((agent, predecessor_id))
                    if predecessor is None or predecessor[1].get("status") != "reconciled":
                        raise ValueError("Recovery chain predecessor is unresolved")
                    if not record.get("recovery_completed"):
                        raise ValueError("Recovery chain predecessor is unresolved")
                candidates.append(record)
        if len(candidates) > 1:
            raise ValueError("Multiple interrupted compaction runs are eligible; refusing ambiguous recovery")
        if candidates:
            candidate_id = candidates[0].get("run_id")
            if any(record.get("run_id") != candidate_id for record in active):
                raise ValueError("Another starting, running, or interrupted run is competing with recovery")
            return candidate_id
        return None

    def recovery_candidate(self, agent: str) -> str | None:
        with self.locked():
            return self._recovery_candidate_locked(agent)

    def claim_run(self, agent: str, engine: str, strict: bool, recovery_from: str | None = None, continuation: bool = False) -> Path:
        if engine not in ("claude", "codex"):
            raise ValueError("Unsupported engine")
        with self.locked():
            if not continuation:
                with self.safe(self.path / ".continue.lock").open("a") as handoff_lock:
                    try:
                        fcntl.flock(handoff_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise ValueError("A continuation handoff is in progress") from exc
                    finally:
                        fcntl.flock(handoff_lock, fcntl.LOCK_UN)
            path = self.agent_path(agent)
            from .lifecycle import read_locked
            worker = read_locked(self, agent)
            if worker and not (worker["phase"] in ("stopped", "completed") or
                               (worker["phase"] == "retired" and worker.get("operations_reconciled") is True)):
                raise ValueError("Reconcile the native worker attempt before a managed launch")
            rows = [(owner, target, record) for owner, target, record in self._run_records_locked()
                    if owner == agent]
            index = self._validate_recovery_links_locked()
            active = [record for _, _, record in rows
                      if record.get("status") in ("starting", "running", "interrupted")]
            old = None
            old_target = None
            baseline = None
            recovery_source = None
            if recovery_from is not None:
                recovery_from = component(recovery_from)
                old_entry = index.get((agent, recovery_from))
                if old_entry is None:
                    raise ValueError(f"Unknown recovery predecessor: {recovery_from}")
                old_target, old = old_entry
                candidate = (self._continuation_candidate_locked(agent, index) if continuation
                             else self._recovery_candidate_locked(agent))
                if candidate != recovery_from:
                    raise ValueError("Requested recovery predecessor is not the sole eligible candidate")
                if old.get("status") != "interrupted":
                    raise ValueError("Continuation predecessor must be interrupted before claiming")
                recovery_source = ("explicit_continuation" if continuation
                                   else self._compaction_recovery_source(old_target, old))
                if recovery_source is None:
                    raise ValueError("Recovery predecessor halt evidence changed")
                ancestors = self._recovery_ancestors_locked(agent, recovery_from, index)
                for predecessor in [*[item for item in ancestors if item.get("status") != "reconciled"], old]:
                    if predecessor.get("engine") != engine:
                        raise ValueError("Recovery requires the predecessor's same engine")
                    if predecessor.get("host") != socket.gethostname():
                        raise ValueError("Cross-host recovery is not supported; verify on the original host")
                    self._assert_process_dead(predecessor, "child")
                    self._assert_supervisor_takeover_safe(predecessor)
                if not continuation and ancestors and (ancestors[-1].get("status") != "reconciled"
                                                       or not old.get("recovery_completed")):
                    raise ValueError("Recovery chains require a resolved predecessor")
                checkpoint = self.latest(agent)
                if checkpoint is None:
                    raise ValueError("Recovery requires a committed baseline checkpoint")
                baseline = {"checkpoint_id": checkpoint.name, "path": str(checkpoint)}
                if not continuation and any(record.get("run_id") != recovery_from for record in active):
                    raise ValueError("Another starting, running, or interrupted run is competing with recovery")
            elif active:
                previous = active[0]
                raise ValueError(f"Run {previous['run_id']} is not reconciled; inspect its operations, then close it explicitly")
            run_id = uuid.uuid4().hex
            run = path / "runs" / run_id
            run.mkdir()
            sync_directory(run.parent)
            run_record = {"schema_version": SCHEMA, "run_id": run_id,
                          "engine": engine, "agent_id": agent, "created_at": now(), "status": "starting",
                          "supervisor_pid": os.getpid(), "supervisor_identity": process_identity(os.getpid()),
                          "host": socket.gethostname(), "strict_no_compaction_requested": strict,
                          "usage": None, "recovery_from": recovery_from,
                          "recovery_checkpoint": baseline, "recovery_pending": recovery_from is not None,
                          "recovery_completed": False, "recovery_source": recovery_source}
            # The successor record is published first. A crash before the
            # predecessor edge is written leaves an explicit incomplete link
            # that all future recovery attempts reject.
            write_json(run / "run.json", run_record)
            if old is not None:
                old["recovery_source"] = recovery_source
                old["recovery_to"] = run_id
                old["recovery_pending"] = True
                write_json(old_target, old)
            return run

    def update_run(self, agent: str, run_id: str, **fields) -> None:
        with self.locked():
            target = self.safe(self.agent_path(agent) / "runs" / component(run_id) / "run.json")
            record = read_json(target)
            record.update(fields)
            write_json(target, record)

    def close_run(self, agent: str, run_id: str, note: str) -> None:
        if not note.strip():
            raise ValueError("A reconciliation note is required")
        with self.locked():
            run_id = component(run_id)
            target = self.safe(self.agent_path(agent) / "runs" / run_id / "run.json")
            record = read_json(target)
            if record.get("host") != socket.gethostname():
                raise ValueError("Cross-host run reconciliation is not supported; verify on the original host")
            rows = self._run_records_locked()
            index = self._validate_recovery_links_locked(rows)
            if record.get("recovery_to") is not None:
                successor_entry = index.get((agent, record["recovery_to"]))
                if successor_entry is None:
                    raise ValueError("Incomplete recovery link; refusing automatic repair")
                successor_target, successor = successor_entry
                if record.get("status") == "reconciled" and successor.get("recovery_completed"):
                    return
                ancestors = self._recovery_ancestors_locked(agent, run_id, index)
                if any(item.get("status") != "reconciled" for item in ancestors):
                    raise ValueError("Recovery predecessor is unresolved; close ancestors oldest-first")
                direct_successor = successor
                direct_target = successor_target
                successor_target, successor = self._active_recovery_leaf_locked(agent, run_id, index)
                if successor.get("host") != socket.gethostname():
                    raise ValueError("Cross-host recovery reconciliation is not supported")
                if successor.get("status") not in ("starting", "running"):
                    raise ValueError("Recovery successor is not active; refusing reconciliation")
                self._assert_process_dead(record, "child")
                self._assert_supervisor_close_safe(record, successor)
                self._assert_no_unresolved_native_children_locked(agent, run_id)
                baseline = successor.get("recovery_checkpoint") or {}
                baseline_id = baseline.get("checkpoint_id") if isinstance(baseline, dict) else None
                checkpoint = self.latest(agent)
                if checkpoint is None or checkpoint.name == baseline_id:
                    raise ValueError("Recovery requires a fresh committed checkpoint")
                manifest = read_json(self.safe(checkpoint / "manifest.json"))
                if successor.get("run_id") not in manifest.get("run_ids", []):
                    raise ValueError("Fresh recovery checkpoint does not include successor run")
                if fingerprint(self.agent_path(agent) / "STATE.md") != manifest["state_sha256"]:
                    raise ValueError("Working STATE.md changed after recovery checkpoint")
                if workspace_head(self.workspace) != manifest.get("head"):
                    raise ValueError("Workspace HEAD changed after recovery checkpoint")
                for evidence in manifest.get("evidence", []):
                    evidence_path = Path(evidence["path"])
                    if (not evidence_path.is_file()
                            or fingerprint(evidence_path) != evidence["sha256"]):
                        raise ValueError("Recovery checkpoint evidence changed")
                history_hash = manifest.get("historical_state_sha256")
                history_path = self.safe(self.agent_path(agent) / "historical_state.md")
                if history_hash is not None:
                    if (not history_path.is_file() or fingerprint(history_path) != history_hash):
                        raise ValueError("Historical state changed after recovery checkpoint")
                elif history_path.exists():
                    raise ValueError("Historical state was created after a historyless recovery checkpoint")
                # latest() verifies the immutable historical_state snapshot;
                # the live file was checked above as a freshness condition.
                direct_successor["recovery_completed"] = True
                direct_successor["recovery_pending"] = bool(direct_successor.get("recovery_to"))
                write_json(direct_target, direct_successor)
                record.update(status="reconciled", reconciliation=note,
                              closed_at=now(), recovery_pending=False)
                write_json(target, record)
                return
            if record.get("recovery_from") is not None and not record.get("recovery_completed"):
                raise ValueError("Recovery predecessor is unresolved; close the predecessor first")
            for key in ("supervisor_pid", "child_pid"):
                pid = record.get(key)
                if pid:
                    expected = record.get(key.replace("_pid", "_identity"))
                    actual = process_identity(pid)
                    if expected and actual and expected != actual:
                        continue
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    except PermissionError:
                        raise ValueError("Cannot establish that the old process stopped")
                    raise ValueError(f"Process {pid} may still be alive; refusing to close run")
            record.update(status="reconciled", reconciliation=note, closed_at=now())
            write_json(target, record)

    def _assert_no_unresolved_native_children_locked(self, owner_agent: str, owner_run: str) -> None:
        """Lifecycle observations are the only durable ownership source."""
        agents = self.safe(self.path / "agents")
        for directory in agents.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            lifecycle = self.safe(directory / "lifecycle.json")
            if not lifecycle.is_file():
                continue
            state = read_json(lifecycle)
            if (state.get("owner_agent") == owner_agent and state.get("owner_run") == owner_run
                    and not (state.get("phase") in ("stopped", "completed") or
                             (state.get("phase") == "retired" and state.get("operations_reconciled") is True))):
                raise ValueError("Native children owned by the old run remain unresolved")
