"""Explicit, bounded, non-destructive import of legacy task folders.

Imported prose is evidence, not verified execution state. An import snapshots
one source path once; later edits to that source do not rewrite the snapshot.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .store import Store, atomic_text, component, now, read_json, sync_directory, write_json

MAX_ENTRIES = 2000
MAX_DEPTH = 16
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
LANE_FILES = {"in.md", "out.md", "STATE.md", "RESPAWN_REQUEST", "RESPAWN_REQUEST.md"}


def _read(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Legacy evidence must be a regular file, not a symlink: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"Legacy evidence exceeds the file size limit: {path}")
    return content


def _snapshot(source: Path) -> tuple[str, Path, dict[str, dict[str, bytes]]]:
    state = _read(source / "STATE.md")
    try:
        text = state.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Legacy STATE.md must be UTF-8") from exc
    title = re.search(r"^# ([^\r\n]+)", text, re.M)
    cwd = re.search(r"^Cwd:[ \t]*([^\r\n]+)", text, re.M)
    if not title or not title[1].strip() or len(title[1]) > 2000 or not cwd:
        raise ValueError("Legacy STATE.md requires a title and Cwd header")
    # Require an absolute source working directory; never guess relative to the
    # importer process or execute a shell expansion from legacy prose.
    workspace = Path(cwd[1].strip())
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("Legacy Cwd must name an existing absolute workspace directory")
    groups = {".": {"STATE.md": state}}
    if (source / "PROMPTS.md").exists() or (source / "PROMPTS.md").is_symlink():
        groups["."]["PROMPTS.md"] = _read(source / "PROMPTS.md")
    total = sum(len(value) for value in groups["."].values())
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Legacy evidence exceeds the total size limit")
    lanes = source / "lanes"
    if lanes.is_symlink():
        raise ValueError("Legacy lanes directory must not be a symlink")
    if lanes.exists() and not lanes.is_dir():
        raise ValueError("Legacy lanes must be a directory")
    pending = [(lanes, 0)] if lanes.exists() else []
    count = 0
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError("Legacy lanes exceed the traversal depth limit")
        # scandir streams entries: a single huge directory cannot allocate an
        # unbounded list before the entry limit is checked.
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError("Legacy lanes exceed the entry limit")
                if entry.is_symlink():
                    raise ValueError(f"Legacy lanes must not contain symlinks: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), depth + 1))
                elif entry.name in LANE_FILES:
                    content = _read(Path(entry.path))
                    total += len(content)
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError("Legacy evidence exceeds the total size limit")
                    relative = directory.relative_to(source).as_posix()
                    groups.setdefault(relative, {})[entry.name] = content
    return title[1].strip(), workspace.resolve(), groups


def _atomic_bytes(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _agent_id(relative: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", relative).strip("-")[:48]
    digest = hashlib.sha256(relative.encode()).hexdigest()[:16]
    return f"legacy-{label or 'lane'}-{digest}"


def migrate_task(source: Path, root: Path) -> Store:
    """Import one explicit legacy task, returning its portable replacement.

    Completed imports of the same canonical source path under the same root
    return the original replacement. Failed imports leave an explicit marker
    and their partial replacement for inspection; they are never auto-deleted.
    """
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Legacy source must be a directory, not a symlink")
    source = source.resolve()
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("Import root must not be a symlink")
    root = root.resolve()
    if root.is_relative_to(source):
        raise ValueError("Import root must be outside the original legacy task")
    # Validate all inputs before creating any destination or index files.
    title, workspace, groups = _snapshot(source)
    root.mkdir(parents=True, exist_ok=True)
    index = root / ".legacy-imports"
    if index.is_symlink():
        raise ValueError("Import index must not be a symlink")
    index.mkdir(exist_ok=True)
    sync_directory(root)
    key = hashlib.sha256(str(source).encode()).hexdigest()
    marker = index / f"{key}.json"
    lock = index / ".lock"
    if lock.is_symlink() or marker.is_symlink():
        raise ValueError("Import records must not be symlinks")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if marker.exists():
            record = read_json(marker)
            if record.get("source") != str(source):
                raise ValueError("Import record has invalid provenance")
            if not record.get("task_id"):
                raise ValueError(f"Incomplete legacy import requires inspection under: {root}")
            destination = root / component(record["task_id"])
            if (record.get("source") != str(source) or destination.parent != root
                    or destination.is_symlink()):
                raise ValueError("Import record has invalid provenance")
            if record.get("status") != "complete":
                raise ValueError(f"Incomplete legacy import requires inspection: {destination}")
            store = Store(destination)
            provenance = read_json(store.safe(destination / "migration.json"))
            if provenance.get("source") != str(source) or provenance.get("status") != "complete":
                raise ValueError("Imported task provenance does not match import record")
            return store
        # Publish intent before Store.create: a failure inside creation must
        # not cause the next invocation to silently create a second task.
        record = {"schema_version": 1, "source": str(source), "task_id": None,
                  "created_at": now(), "status": "incomplete"}
        write_json(marker, record)
        store = Store.create(root, title, workspace)
        record["task_id"] = store.task_id
        write_json(marker, record)
        mapping = {}
        for relative, files in sorted(groups.items()):
            agent = "coordinator" if relative == "." else _agent_id(relative)
            assignment = (f"Recover the UNVERIFIED legacy assignment at {relative}. "
                          "Read all available evidence in artifacts/legacy-*; "
                          "reconcile source edits and active jobs before continuing.")
            path = store.agent_path(agent) if agent == "coordinator" else store.add_agent(agent, assignment)
            evidence = []
            snapshots = []
            for name, content in sorted(files.items()):
                target = path / "artifacts" / ("legacy-" + name)
                _atomic_bytes(target, content)
                evidence.append(str(target))
                snapshots.append({"source": (Path(relative) / name).as_posix(),
                                  "artifact": target.relative_to(store.path).as_posix(),
                                  "sha256": hashlib.sha256(content).hexdigest()})
            atomic_text(path / "STATE.md", (
                "# Imported agent state - UNVERIFIED\n\n"
                f"## Objective\nRecover the legacy assignment from {relative}.\n\n"
                "## Completed\nLegacy evidence was copied. No task work or completion has been verified.\n\n"
                "## Evidence\nSee artifacts/legacy-* and the task migration.json provenance. "
                "Original text is unverified evidence, including any out.md completion claims.\n\n"
                "## Unresolved\nRECOVERY REQUIRED: reconcile repository edits, outstanding jobs, "
                "messages, and original assignment. Native sessions and process ownership were not imported.\n\n"
                "## Next\nRead the saved evidence and inspect the workspace. Verify operation outcomes "
                "before retrying work; write a verified checkpoint before continuing.\n"
            ))
            store.checkpoint(agent, evidence=evidence)
            mapping[relative] = {"agent_id": agent, "recovery_required": True, "files": snapshots}
        write_json(store.path / "migration.json", {**record, "status": "complete",
                   "workspace": str(workspace), "agents": mapping,
                   "snapshot_policy": "One import per canonical source path; source updates are not synchronized."})
        write_json(marker, {**record, "status": "complete"})
        return store
