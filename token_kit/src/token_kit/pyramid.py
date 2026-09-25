"""Read the task-local trigger-pyramid snapshot without changing it."""
from __future__ import annotations

from pathlib import Path


MAX_BYTES = 16 * 1024
NAME = "trigger_pyramid.md"
REPOSITORY_PATH = Path(__file__).resolve().parents[2] / NAME


def _check_path(path: Path, root: Path | None = None) -> None:
    """Reject symlinks in a task's internal path before reading it."""
    if path.is_symlink():
        raise ValueError(f"Trigger pyramid must not be a symlink: {path}")
    if root is None:
        return
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError(f"Task internals must not be symlinks: {item}")


def _content(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Cannot read trigger pyramid: {path}") from exc
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"Trigger pyramid must be nonempty and at most {MAX_BYTES} bytes: {path}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Trigger pyramid must be UTF-8: {path}") from exc
    if not content.strip():
        raise ValueError(f"Trigger pyramid must contain non-whitespace text: {path}")
    return content


def read_pyramid(task: Path | str | None = None) -> dict[str, str]:
    """Return ``path``, ``source`` and validated ``content`` for the map.

    Missing task snapshots use the repository file and never create a task
    directory or file.  Callers that own a task lock may seed the task copy
    through ``Store._trigger_pyramid_locked``.
    """
    task_path = Path(task) if task is not None else None
    candidate = None
    if task_path is not None:
        if task_path.is_symlink():
            raise ValueError(f"Task path must not be a symlink: {task_path}")
        candidate = task_path / NAME
        _check_path(candidate, task_path)
        if candidate.exists():
            return {"path": str(candidate), "source": "task", "content": _content(candidate)}
    _check_path(REPOSITORY_PATH)
    return {"path": str(REPOSITORY_PATH), "source": "repository",
            "content": _content(REPOSITORY_PATH)}


# Small aliases keep the helper easy to discover without adding a resolver or CLI.
read = read_pyramid
load = read_pyramid
