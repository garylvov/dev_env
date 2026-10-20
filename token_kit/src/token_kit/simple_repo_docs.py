"""Repo knowledge docs: report the index path and stale docs, never their contents.

Each doc in <repo>/docs/agents/ starts with one marker line:
    <!-- verified: <commit> files: path/a.py path/b/ -->
A doc is stale when any covered file changed in a commit after <commit>.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

INDEX = Path("docs/agents/README.md")
MARKER = re.compile(r"<!--\s*verified:\s*(\S+)\s+files:\s*(.*?)\s*-->")
MAX_DOCS = 100


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                              text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def stale_docs(workspace: Path) -> list[str]:
    """Doc names whose marker is missing, unknown or older than a covered file's last commit."""
    stale = []
    docs = sorted(p for p in (workspace / INDEX.parent).glob("*.md") if p.name != INDEX.name)
    for doc in docs[:MAX_DOCS]:
        try:
            with doc.open(encoding="utf-8") as stream:
                head = stream.read(4096)
        except (OSError, UnicodeDecodeError):
            stale.append(doc.name)
            continue
        found = MARKER.search(head)
        files = found.group(2).split() if found else []
        if not files or found.group(1).startswith("-"):
            stale.append(doc.name)
            continue
        result = _git(workspace, "log", "-1", "--format=%H", f"{found.group(1)}..HEAD", "--", *files)
        if result is None or result.returncode or result.stdout.strip():
            stale.append(doc.name)
    return stale


def kickoff_note(workspace: Path) -> str:
    """One or two lines for startup and successor prompts; empty without an index."""
    index = workspace / INDEX
    if not index.is_file():
        return ""
    note = f"Repo docs index: {index} (open listed docs only when relevant).\n"
    stale = stale_docs(workspace)
    if stale:
        note += "Repo docs possibly stale (covered files changed since verified; verify before trusting): " + ", ".join(stale) + "\n"
    return note
