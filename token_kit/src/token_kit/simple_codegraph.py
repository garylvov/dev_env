"""Opt-in CodeGraph MCP for one launched session; never writes client config files.

The index lives in <repo>/.codegraph (CODEGRAPH_DIR overrides the name). Sync
skips files whose (size, mtime) match and content-hashes the rest, so a worktree
seeded with a copy of its main's index re-parses only files that differ.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

INSTALL_HINT = "npm install -g @colbymchenry/codegraph"
SYNC_TIMEOUT = 120
INIT_TIMEOUT = 900
# Database files only; daemon pid, sockets and locks belong to the source checkout.
SEED_FILES = ("codegraph.db", "codegraph.db-wal", ".gitignore")


@dataclass(frozen=True)
class Attachment:
    argv: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

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


def main_worktree(root: Path) -> Path | None:
    """The base checkout of a linked worktree, from `git worktree list`."""
    try:
        result = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = next((line[9:] for line in result.stdout.splitlines() if line.startswith("worktree ")), None)
    if result.returncode or not first or Path(first).resolve() == root.resolve():
        return None
    return Path(first)


def seed(root: Path, main: Path) -> None:
    source, target = index_dir(main), index_dir(root)
    staging = target.with_name(target.name + ".token-kit-seed")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        for name in SEED_FILES:
            if (source / name).is_file():
                shutil.copy2(source / name, staging / name)
        staging.rename(target)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _run(argv: list[str], timeout: int) -> str | None:
    """None on success, else a short failure reason."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except OSError as exc:
        return str(exc)
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()
        return f"exit {result.returncode}" + (f": {tail[-1][:200]}" if tail else "")
    return None


def mcp_argv(engine: str, executable: str, root: Path) -> tuple[str, ...]:
    args = ["serve", "--mcp", "--path", str(root)]
    if engine == "claude":
        server = {"type": "stdio", "command": executable, "args": args, "alwaysLoad": True}
        return ("--mcp-config", json.dumps({"mcpServers": {"codegraph": server}}))
    return ("-c", "mcp_servers.codegraph.command=" + json.dumps(executable),
            "-c", "mcp_servers.codegraph.args=" + json.dumps(args))


def attach(workspace: Path, engine: str, requested: bool | None) -> Attachment:
    """Sync (or seed/build) the index, then return MCP argv for this session only.

    requested None means auto: attach only when the repo already has an index.
    """
    if requested is False:
        return Attachment()
    root = repo_root(workspace)
    if requested is None and not has_index(root):
        return Attachment()
    executable = shutil.which("codegraph")
    if executable is None:
        return Attachment(diagnostics=(f"CodeGraph requested but `codegraph` is not on PATH; continuing without it. Install: {INSTALL_HINT}",))
    notes = ()
    if not has_index(root):
        main = main_worktree(root)
        if main is not None and has_index(main):
            try:
                seed(root, main)
                notes = (f"CodeGraph index seeded from {main}.",)
            except OSError as exc:
                notes = (f"CodeGraph seed from {main} failed ({exc}); building fresh.",)
    if has_index(root):
        failure = _run([executable, "sync", "--quiet", str(root)], SYNC_TIMEOUT)
        action = "sync"
    else:
        print(f"Token Kit: building CodeGraph index for {root} (one-time, up to {INIT_TIMEOUT}s)", flush=True)
        failure = _run([executable, "init", "--yes", str(root)], INIT_TIMEOUT)
        action = "index build"
    if failure:
        return Attachment(diagnostics=notes + (f"CodeGraph {action} failed ({failure}); continuing without the CodeGraph server.",))
    return Attachment(mcp_argv(engine, executable, root), notes)


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
