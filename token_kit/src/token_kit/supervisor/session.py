"""Session facts: the registry, the transcript, the pid, the context size.

Two laws shape this file.

  * No pattern scan of the process table. A scan can match itself (the scanner's
    own argv contains the pattern), so nothing here reads `ps`. A pid is either
    handed to us or reached by DESCENDING from the tmux pane pid through
    /proc/<pid>/task/<pid>/children, and is then confirmed against the session
    registry plus the kernel start time.
  * A kill is verified by the pid's start time no longer matching, never by an
    exit code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_STAT_COMM = re.compile(r"^\d+ \(.*\) ")


def proc_start(pid: int | str) -> str:
    """Kernel start time (field 22 of /proc/<pid>/stat), or "" if the pid is gone.

    `comm` can contain spaces and parentheses, so the leading "<pid> (comm) " is
    stripped before the remaining fields are split; start time is then field 20.
    """
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return ""
    return _STAT_COMM.sub("", raw, count=1).split()[19]


def session_json(claude_home: str | Path, pid: int | str) -> Path:
    return Path(claude_home) / "sessions" / f"{pid}.json"


def _registry(claude_home, pid) -> dict:
    try:
        return json.loads(session_json(claude_home, pid).read_text())
    except (OSError, ValueError):
        return {}


def slug_of(cwd: str) -> str:
    """Project slug: cwd with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcript_of_pid(claude_home: str | Path, pid: int | str) -> Path | None:
    reg = _registry(claude_home, pid)
    sid, cwd = reg.get("sessionId"), reg.get("cwd")
    if not sid or not cwd:
        return None
    return Path(claude_home) / "projects" / slug_of(cwd) / f"{sid}.jsonl"


def session_status(claude_home: str | Path, pid: int | str) -> str:
    """busy | idle | unknown.

    MEASURED LAG: the registry stayed `busy` for ~6 s after a turn had actually
    ended. A 60 s poll never notices; a `once` driver taking two passes in the
    same second does, which is why the drain band also has a timeout.
    """
    return _registry(claude_home, pid).get("status", "unknown")


def is_live_session(claude_home: str | Path, pid) -> bool:
    """The pid is alive, registered, AND its start time still matches (pid reuse)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    recorded = _registry(claude_home, pid).get("procStart")
    if not recorded:
        return False
    now = proc_start(pid)
    return bool(now) and str(recorded) == str(now)


def subtree(root: int | str) -> list[int]:
    """Every pid under `root`, root first. Descend-only: never a pattern scan."""
    out: list[int] = []
    stack = [int(root)]
    seen: set[int] = set()
    while stack:
        pid = stack.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        try:
            kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            kids = []
        stack.extend(int(k) for k in kids)
    return out


def context_tokens(transcript: str | Path) -> int:
    """Context size of the LAST main-thread assistant usage record.

    Sidechain (subagent) records are excluded: their window is not the session's
    window. Verified against a real compaction record: the last main-thread
    assistant record before a compaction read 999519 by this arithmetic against
    preTokens 1000519 in the compaction record one turn later -- the same animal.
    """
    path = Path(transcript)
    value = 0
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                usage = (rec.get("message") or {}).get("usage")
                if not usage:
                    continue
                value = (usage.get("input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0))
    except OSError:
        return 0
    return int(value)
