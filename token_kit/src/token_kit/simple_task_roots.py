"""Where task folders live: <repo>/agent_tasks/<task>/ inside git, else one fixed root.

The repo's own git is the backup. Runtime records (.token-kit/) are kept out of
commits through the local exclude file; Token Kit never commits.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

AGENT_TASKS = "agent_tasks"
RUNTIME_EXCLUDE = AGENT_TASKS + "/*/.token-kit/"


def _git(cwd: Path, *args: str, timeout: float = 10) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if result.returncode else result.stdout.strip()


def toplevel(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def worktrees(path: Path) -> list[Path]:
    listing = _git(path, "worktree", "list", "--porcelain") or ""
    return [Path(line[9:]) for line in listing.splitlines() if line.startswith("worktree ")]


def creation_root(workspace: Path, default: Path) -> Path:
    top = toplevel(workspace)
    return top / AGENT_TASKS if top else default


def search_roots(cwd: Path, default: Path) -> list[Path]:
    """This repo's agent_tasks/, then every worktree's, then the fixed root; no duplicates."""
    top = toplevel(cwd)
    candidates = ([top / AGENT_TASKS] + [tree / AGENT_TASKS for tree in worktrees(top)] if top else []) + [default]
    roots: list[Path] = []
    for root in candidates:
        if root.resolve() not in [seen.resolve() for seen in roots]:
            roots.append(root)
    return roots


def repo_of(task: Path) -> Path | None:
    """The checkout holding <checkout>/agent_tasks/<task>, without running git."""
    parent = task.parent
    return parent.parent if parent.name == AGENT_TASKS and (parent.parent / ".git").exists() else None


def add_exclude(repo: Path, line: str) -> bool:
    """Append a line to the repo's local exclude file once; never edits .gitignore."""
    path = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if not path:
        return False
    exclude = Path(path)
    text = exclude.read_text() if exclude.is_file() else ""
    if line in text.splitlines():
        return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as stream:
        stream.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")
    return True


def ignored(repo: Path, path: Path) -> bool:
    return _git(repo, "check-ignore", "-q", "--no-index", str(path.relative_to(repo))) is not None


def visibility(repo: Path) -> str | None:
    """PUBLIC, PRIVATE or INTERNAL for the origin remote; None when unknown."""
    origin = _git(repo, "remote", "get-url", "origin")
    if not origin:
        return None
    try:
        result = subprocess.run(["gh", "repo", "view", origin, "--json", "visibility", "-q", ".visibility"],
                                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip().upper() or None


def protect(task: Path) -> list[str]:
    """Keep runtime records out of commits; keep the whole task local in public repos."""
    repo = repo_of(task)
    if repo is None:
        return []
    notes = []
    add_exclude(repo, RUNTIME_EXCLUDE)
    if visibility(repo) == "PUBLIC":
        add_exclude(repo, f"/{AGENT_TASKS}/{task.name}/")
        notes.append(f"{repo} is a public repo, so this task folder is kept local (added to .git/info/exclude): {task}")
    elif ignored(repo, task / "asks.md"):
        notes.append(f"Task folder is git-ignored in {repo} (existing ignore rule), so it will not be committed or backed up: {task}")
    return notes


def committable(task: Path) -> bool:
    repo = repo_of(task)
    return repo is not None and not ignored(repo, task / "asks.md")
