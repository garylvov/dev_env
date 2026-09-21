"""ladder.py -- "prefer codex, if not available use opus", executed.

A row carries an ORDERED `prefer` list in one grammar for every engine:

    "<engine>:<model>:<effort>"      engine in {codex, claude}
                                     effort in {low, medium, high}

The router takes the FIRST AVAILABLE candidate.  Availability is a CLOSED SET
-- nothing here guesses, and nothing here asks a model:

    codex   binary/launcher absent from this host        -> binary_absent
            a live quota-cooldown marker for the session -> quota_cooldown
            the per-host codex bound is full             -> codex_bound_full
            the codex wrapper agents_dir does not exist  -> wrapper_unavailable
    claude  [concurrency] for that model is full         -> concurrency_full

Every skip appends `skip_candidate<TAB><row>/<candidate>/<reason>` and the
chosen one appends `candidate<TAB><row>/<candidate>`.  That log is the whole
point: it is how the operator sees the dynamics without asking anybody.

THE HONEST GAP, stated rather than faked: there is no cheap live count of
Claude subagents per model.  A PreToolUse hook's stdin carries no
`background_tasks` (measured, CLI 2.1.278: the keys are cwd, effort,
hook_event_name, permission_mode, prompt_id, session_id, tool_input, tool_name,
tool_use_id, transcript_path, plus agent_id and agent_type inside a subagent),
and the `agent-<id>.meta.json` sidecars beside each transcript carry
{agentType, description, toolUseId, spawnDepth, requestShape, model} with NO
start time, no end time and no exit marker -- so "is this one still running"
could only be guessed from the transcript's mtime, which says "silent", not
"finished".  A long-thinking agent and a finished one look identical.  So a
claude candidate is treated as AVAILABLE unless its configured limit is zero or
negative, i.e. the operator has taken that tier out of service.  That is the
one criterion here with no live producer; it is named in the lane's out.md
under UNPROVEN and it is what `[concurrency] fable = 0` exercises in the cases.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .matrix import CANDIDATE_RE

DEFAULT_QUOTA_COOLDOWN_S = 1800


@dataclass(frozen=True)
class Candidate:
    engine: str
    model: str
    effort: str
    text: str

    @property
    def is_codex(self) -> bool:
        return self.engine == "codex"


def parse(text: str) -> Candidate | None:
    m = CANDIDATE_RE.match(str(text).strip())
    if not m:
        return None
    return Candidate(m.group(1), m.group(2), m.group(3), m.group(0))


def candidates(row: dict) -> list[Candidate]:
    out = []
    for raw in row.get("prefer", []) or []:
        c = parse(raw)
        if c is not None:
            out.append(c)
    return out


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------
def cooldown_marker(codex: dict, session_state: Path) -> Path:
    return session_state / str(codex.get("cooldown_marker", "codex_quota_cooldown"))


def cooldown_live(codex: dict, session_state: Path, now: float | None = None) -> bool:
    """A quota refusal armed the marker and it has not aged out yet."""
    marker = cooldown_marker(codex, session_state)
    try:
        age = (now or time.time()) - marker.stat().st_mtime
    except OSError:
        return False
    try:
        window = float(codex.get("quota_cooldown_s", DEFAULT_QUOTA_COOLDOWN_S))
    except (TypeError, ValueError):
        window = DEFAULT_QUOTA_COOLDOWN_S
    return age < window


def arm_cooldown(codex: dict, session_state: Path) -> Path:
    """Called by `router cooldown --arm` when a dispatcher refused on quota."""
    session_state.mkdir(parents=True, exist_ok=True)
    marker = cooldown_marker(codex, session_state)
    marker.write_text(f"{time.time()}\n", encoding="utf-8")
    return marker


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def codex_slots_full(codex: dict) -> bool:
    """The per-host bound, read from the SAME lock directory dispatch.py uses.

    token_kit.codex.dispatch.Slot takes `slot-<i>` directories under
    slot_root() with mkdir as the atomic primitive and writes the holder's pid
    inside; a slot whose pid is gone is a crashed holder's, not a live child.
    We only read -- reclaiming is the dispatcher's job, never the hook's.
    """
    from token_kit.codex import dispatch  # local: the hook path must stay thin

    try:
        max_children = int(codex.get("max_children", dispatch.DEFAULT_MAX_CHILDREN))
    except (TypeError, ValueError):
        max_children = dispatch.DEFAULT_MAX_CHILDREN
    if max_children < 1:
        return True
    root = dispatch.slot_root()
    held = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return False
    for entry in entries:
        if not entry.name.startswith("slot-") or not entry.is_dir():
            continue
        try:
            pid = int((Path(entry.path) / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            held += 1
    return held >= max_children


def codex_binary_present(codex: dict) -> bool:
    binary = str(codex.get("binary", "codex"))
    if os.path.sep in binary:
        return Path(os.path.expanduser(binary)).exists()
    return shutil.which(binary) is not None


def codex_unavailable_reason(codex: dict, session_state: Path,
                             now: float | None = None) -> str | None:
    """None when codex can be used on this host, else the reason word."""
    if not codex_binary_present(codex):
        return "binary_absent"
    if cooldown_live(codex, session_state, now):
        return "quota_cooldown"
    if codex_slots_full(codex):
        return "codex_bound_full"
    agents_dir = codex.get("agents_dir")
    if agents_dir and not Path(os.path.expanduser(str(agents_dir))).is_dir():
        return "wrapper_unavailable"
    return None


def claude_unavailable_reason(model: str, concurrency: dict) -> str | None:
    """See THE HONEST GAP in the module docstring: only a zero limit refuses."""
    limit = concurrency.get(model)
    if limit is None:
        return None
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return None
    return "concurrency_full" if limit <= 0 else None


# --------------------------------------------------------------------------
# the choice
# --------------------------------------------------------------------------
def choose(row: dict, matrix, session_state: Path,
           now: float | None = None) -> tuple[Candidate | None, list[tuple[Candidate, str]]]:
    """(chosen, skipped) for one row.  Both are what the event log records."""
    skipped: list[tuple[Candidate, str]] = []
    codex_reason: str | None = None
    codex_checked = False
    for cand in candidates(row):
        if cand.is_codex:
            if not codex_checked:
                codex_reason = codex_unavailable_reason(matrix.codex, session_state, now)
                codex_checked = True
            reason = codex_reason
        else:
            reason = claude_unavailable_reason(cand.model, matrix.concurrency)
        if reason is None:
            return cand, skipped
        skipped.append((cand, reason))
    return None, skipped


def dispatch_command(codex: dict, cand: Candidate) -> str:
    """The row's dispatch one-liner, re-pointed at THIS candidate.

    The table carries one template; a candidate carries the model and the
    effort.  Never trust the template's own defaults -- ruling R2 says every
    codex call names both explicitly.
    """
    template = str(codex.get("dispatch", "") or "")
    if not template:
        return ""
    out = re.sub(r"--model\s+\S+", f"--model {cand.model}", template)
    out = re.sub(r"--effort\s+\S+", f"--effort {cand.effort}", out)
    if "--model" not in out:
        out = f"{out} --model {cand.model} --effort {cand.effort}"
    return out
