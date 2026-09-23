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

    def agent_path(self, agent: str) -> Path:
        path = self.safe(self.path / "agents" / component(agent))
        for name in ("agent.json", "STATE.md", "in.md", "assignments", "checkpoints", "messages", "runs", "artifacts"):
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
            path = self.safe(self.path / "agents" / agent)
            path.mkdir(parents=True, exist_ok=False)
            sync_directory(path.parent)
            for name in ("assignments", "checkpoints", "messages", "runs", "artifacts"):
                (path / name).mkdir()
            write_json(path / "agent.json", {"schema_version": SCHEMA, "agent_id": agent,
                       "parent_agent": parent,
                       "assignment_revision": 1, "created_at": now(), "latest_checkpoint": None})
            saved = brief(assignment, str(self.path), agent)
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
                raise ValueError("STATE.md exceeds 32KB; move detailed evidence into artifacts")
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
            manifest = {"schema_version": SCHEMA, "checkpoint_id": checkpoint_id,
                        "agent_id": agent, "task_id": self.task_id, "created_at": now(), "assignment_revision": meta["assignment_revision"],
                        "assignment_sha256": fingerprint(self.safe(path / "assignments" / f'{meta["assignment_revision"]:04d}.md')),
                        "workspace": str(self.workspace), "head": workspace_head(self.workspace),
                        "state_sha256": fingerprint(snapshot / "STATE.md"), "evidence": facts,
                        "incorporated_messages": sorted(incorporated_ids),
                        "run_ids": sorted(p.name for p in (path / "runs").iterdir() if p.is_dir()),
                        "coverage": "agent-declared checkpoint; tool operations are not yet intercepted"}
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
            return {"agent_id": agent, "workspace": str(self.workspace), "checkpoint": str(snapshot),
                    "children": children,
                    "assignment": str(path / "assignments" / f'{manifest["assignment_revision"]:04d}.md'),
                    "pending_messages": messages, "changed_evidence": changed,
                    "head_changed": workspace_head(self.workspace) != manifest["head"],
                    "working_state_changed": fingerprint(path / "STATE.md") != manifest["state_sha256"],
                    "runs": [read_json(self.safe(p)) for p in sorted((path / "runs").glob("*/run.json"))]}

    def claim_run(self, agent: str, engine: str, strict: bool) -> Path:
        if engine not in ("claude", "codex"):
            raise ValueError("Unsupported engine")
        with self.locked():
            path = self.agent_path(agent)
            from .lifecycle import read_locked
            worker = read_locked(self, agent)
            if worker and worker["phase"] not in ("stopped", "completed"):
                raise ValueError("Reconcile the native worker attempt before a managed launch")
            for previous in (path / "runs").glob("*/run.json"):
                record = read_json(self.safe(previous))
                if record["status"] in ("starting", "running", "interrupted"):
                    raise ValueError(f"Run {record['run_id']} is not reconciled; inspect its operations, then close it explicitly")
            run_id = uuid.uuid4().hex
            run = path / "runs" / run_id
            run.mkdir()
            sync_directory(run.parent)
            write_json(run / "run.json", {"schema_version": SCHEMA, "run_id": run_id,
                       "engine": engine, "agent_id": agent, "created_at": now(), "status": "starting",
                       "supervisor_pid": os.getpid(), "supervisor_identity": process_identity(os.getpid()),
                       "host": socket.gethostname(), "strict_no_compaction_requested": strict,
                       "usage": None})
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
            target = self.safe(self.agent_path(agent) / "runs" / component(run_id) / "run.json")
            record = read_json(target)
            if record.get("host") != socket.gethostname():
                raise ValueError("Cross-host run reconciliation is not supported; verify on the original host")
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
