"""Opt-in CodeGraph MCP for one launched Claude session; never writes client config files.

The index lives in <repo>/.codegraph (CODEGRAPH_DIR overrides the name). A linked
worktree without its own index reads its main checkout's index in place: no copy,
and no sync from the worktree, since a session in main may be writing it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

INSTALL_HINT = "npm install -g --prefix ~/.local @colbymchenry/codegraph"
SYNC_TIMEOUT = 120
INIT_TIMEOUT = 900
ENV = {"CODEGRAPH_TELEMETRY": "0"}


@dataclass(frozen=True)
class Attachment:
    argv: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    guidance: str = ""

    @property
    def attached(self) -> bool:
        return bool(self.argv)


def index_dir(root: Path) -> Path:
    return root / os.environ.get("CODEGRAPH_DIR", ".codegraph")


def has_index(root: Path) -> bool:
    return (index_dir(root) / "codegraph.db").is_file()


def repo_root(workspace: Path) -> Path:
    """Nearest ancestor holding .git (directory or worktree file), else the workspace."""
    for directory in (workspace, *workspace.parents):
        if (directory / ".git").exists():
            return directory
    return workspace


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if result.returncode else result.stdout.strip()


def main_worktree(root: Path) -> Path | None:
    """The base checkout of a linked worktree, from `git worktree list`."""
    listing = _git(root, "worktree", "list", "--porcelain") or ""
    first = next((line[9:] for line in listing.splitlines() if line.startswith("worktree ")), None)
    if not first or Path(first).resolve() == root.resolve():
        return None
    return Path(first)


def _run(argv: list[str], timeout: int) -> str | None:
    """None on success, else a short failure reason."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env={**os.environ, **ENV}, check=False)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except OSError as exc:
        return str(exc)
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()
        return f"exit {result.returncode}" + (f": {tail[-1][:200]}" if tail else "")
    return None


def mcp_argv(executable: str, root: Path) -> tuple[str, ...]:
    server = {"type": "stdio", "command": executable, "args": ["serve", "--mcp", "--path", str(root)],
              "env": ENV, "alwaysLoad": True}
    return ("--mcp-config", json.dumps({"mcpServers": {"codegraph": server}}))


def exclude(root: Path) -> None:
    """Hide the index from git status via the local exclude file, never .gitignore."""
    from .simple_task_roots import add_exclude
    add_exclude(root, "/" + index_dir(root).name + "/")


def guidance(root: Path) -> str:
    return (f"CodeGraph MCP is attached for {root}: query it before grepping or reading files to locate code. "
            "In another checkout pass its root as projectPath; if that has no .codegraph/ index, read files directly.\n")


def shared_guidance(root: Path, main: Path) -> str:
    main_head = _git(main, "rev-parse", "HEAD")
    base = (_git(root, "merge-base", "HEAD", main_head) if main_head else None) or main_head or "<merge-base>"
    return (f"CodeGraph answers from {main} (main's code); for files changed in this worktree "
            f"(git diff --name-only {base[:12]}), read them directly.\n")


def attach(workspace: Path, requested: bool | None) -> Attachment:
    """Sync (or build) the index, then return MCP argv for this session only.

    Order: the repo's own index, else (linked worktree) main's index read in place,
    else build, only in a main checkout. requested None attaches only an existing index.
    """
    if requested is False:
        return Attachment()
    root = repo_root(workspace)
    main = None if has_index(root) else main_worktree(root)
    shared = main is not None and has_index(main)
    if requested is None and not has_index(root) and not shared:
        return Attachment()
    executable = shutil.which("codegraph")
    if executable is None:
        return Attachment(diagnostics=(f"CodeGraph requested but `codegraph` is not on PATH; continuing without it. Install: {INSTALL_HINT}",))
    if shared:
        # Never sync main from here; the shared serve daemon is codegraph's single writer.
        return Attachment(mcp_argv(executable, main), (), guidance(main) + shared_guidance(root, main))
    if main is not None:
        return Attachment(diagnostics=(f"CodeGraph skipped: neither this worktree nor its main checkout {main} has an index; "
                                       "run codegraph init in the main checkout to share one.",))
    notes = ()
    if has_index(root):
        failure = _run([executable, "sync", "--quiet", str(root)], SYNC_TIMEOUT)
        action = "sync"
    else:
        print(f"Token Kit: building CodeGraph index for {root} (one-time, up to {INIT_TIMEOUT}s)", flush=True)
        failure = _run([executable, "init", "--yes", str(root)], INIT_TIMEOUT)
        action = "index build"
    if failure:
        return Attachment(diagnostics=notes + (f"CodeGraph {action} failed ({failure}); continuing without the CodeGraph server.",))
    try:
        exclude(root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        notes += (f"Could not add .codegraph/ to the local git exclude file: {exc}",)
    return Attachment(mcp_argv(executable, root), notes, guidance(root))


def hook_lines(main: Path) -> str:
    """Git hook lines that keep indexed checkouts fresh; printed, never installed.

    Hooks are shared by every worktree of the repo and run at the worktree top.
    """
    command = "[ -f .codegraph/codegraph.db ] && command -v codegraph >/dev/null && (codegraph sync --quiet . >/dev/null 2>&1 &)"
    try:
        result = subprocess.run(["git", "-C", str(main), "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Not a git checkout: {main} ({exc})")
    if result.returncode or not result.stdout.strip():
        raise ValueError(f"Not a git checkout: {main}")
    hooks = Path(result.stdout.strip())
    return "".join(f"# append to {hooks / name}\n{command}\n" for name in ("post-merge", "post-checkout", "post-rewrite"))
