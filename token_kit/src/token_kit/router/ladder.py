"""ladder.py -- "prefer codex, if not available use opus", executed.

A kind carries an ORDERED `prefer` ladder in one grammar for every engine:

    "<engine>:<model>:<effort>"      engine in {codex, claude}
                                     effort in {low, medium, high}

The router takes the FIRST AVAILABLE candidate.  Availability is a CLOSED SET
-- nothing here guesses, and nothing here asks a model:

    codex   binary/launcher absent from this host        -> binary_absent
            a live quota-cooldown marker for the session -> quota_cooldown
            the per-host codex bound is full             -> codex_bound_full
            the codex runner agent's directory is absent -> wrapper_unavailable
    claude  always available

Every skip appends `skip_candidate<TAB><kind>/<candidate>/<reason>` and the
chosen one appends `candidate<TAB><kind>/<candidate>`.  That log is the whole
point: it is how the dynamics are visible without asking anybody.

A CLAUDE CANDIDATE IS ALWAYS AVAILABLE, and the per-model concurrency table
that used to sit in the guide is DELETED rather than faked.  There is no cheap
live count of Claude subagents per model: a PreToolUse hook's stdin carries no
`background_tasks` (measured, CLI 2.1.278), and the `agent-<id>.meta.json`
sidecars beside each transcript carry no start time, no end time and no exit
marker -- so "is this one still running" could only be guessed from a
transcript's mtime, which says "silent", not "finished".  A criterion with no
live producer is a defect, so the criterion is gone, not softened.

CODEX SETTINGS ARE CODE DEFAULTS.  They are machine facts, not delegation
guidance, so they do not belong in a document a person reads for advice.  Every
one can be overridden from the `[router]` table of the one optional config file
(`~/.config/token_kit/config.toml`), which is normally absent.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .matrix import CANDIDATE_RE, router_config

DEFAULT_QUOTA_COOLDOWN_S = 1800

#: Code defaults; `[router]` in the optional config file overrides any of them.
CODEX_DEFAULTS = {
    "binary": "codex",
    "refusal_code": 42,
    "quota_cooldown_s": DEFAULT_QUOTA_COOLDOWN_S,
    "cooldown_marker": "codex_quota_cooldown",
    "max_children": None,      # None = the dispatcher's own per-host bound
    "agents_dir": "~/.claude/agents",
    "runner_agent": "kit-codex-runner",
    "dispatch": ("codex-dispatch --model <model> --effort <effort> "
                 "--cwd <abs dir> --task-file <abs step.md> --out <abs answer.md>"),
    "job": "codex-job",
}


def codex_settings() -> dict:
    """The codex facts, as code defaults plus the optional `[router]` table."""
    out = dict(CODEX_DEFAULTS)
    for key, value in router_config().items():
        if key in out:
            out[key] = value
    return out


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


def dispatcher_cooldown_live(now: float | None = None) -> bool:
    """Is the marker the DISPATCHER itself writes still live?

    THE GAP THIS CLOSES. There were two cooldown markers and they never met.
    `token_kit.codex.errors.board_quota()` writes a machine-wide JSON marker
    the moment a real codex turn comes back "out of quota"; the router read a
    different, per-session file that only the manual `router cooldown --arm`
    verb ever wrote. So the one event that knows codex is out of quota -- the
    refusal itself -- did not reach the one component that can route around
    it, and every later spawn paid for the same refusal. A writer with no
    reader and a reader with no writer, which is the same defect twice.

    Both are read now. The per-session marker stays: an agent that hit a
    refusal in a lane can still arm it explicitly, and a session-scoped
    cooldown is the narrower claim of the two.
    """
    try:
        from token_kit.codex import errors as codex_errors
        return codex_errors.cooldown_active(now) > 0.0
    except Exception:  # noqa: BLE001 -- availability must never raise on the hook path
        return False


def cooldown_live(codex: dict, session_state: Path, now: float | None = None) -> bool:
    """A quota refusal armed a marker -- either one -- and it is still live."""
    if dispatcher_cooldown_live(now):
        return True
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


def profile_codex_reachable() -> bool:
    """Agree with the dispatcher: the PROFILE decides how codex is reached.

    Cheap on purpose -- one TOML parse and two stat()s; it never prepares a
    home.  A profile that cannot be read says nothing, so it does not veto.
    """
    try:
        from token_kit.codex import launcher as launcher_mod
        cfg = launcher_mod.load_config()
    except Exception:  # noqa: BLE001 -- availability never raises on the hook path
        return True
    if cfg.node_local_home:
        return bool(launcher_mod.resolve_binary(cfg)) and os.access(cfg.tmp_root, os.W_OK)
    named = os.path.expanduser(cfg.launcher or "codex")
    if os.path.sep in named:
        return Path(named).exists()
    return shutil.which(named) is not None


def codex_binary_present(codex: dict) -> bool:
    binary = str(codex.get("binary", "codex"))
    if os.path.sep in binary:
        return Path(os.path.expanduser(binary)).exists()
    if binary != "codex":  # an explicit matrix override decides alone
        return shutil.which(binary) is not None
    return profile_codex_reachable()


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


# --------------------------------------------------------------------------
# the choice
# --------------------------------------------------------------------------
def choose(row: dict, codex: dict, session_state: Path,
           now: float | None = None) -> tuple[Candidate | None, list[tuple[Candidate, str]]]:
    """(chosen, skipped) for one kind.  Both are what the event log records."""
    skipped: list[tuple[Candidate, str]] = []
    codex_reason: str | None = None
    codex_checked = False
    for cand in candidates(row):
        if cand.is_codex:
            if not codex_checked:
                codex_reason = codex_unavailable_reason(codex, session_state, now)
                codex_checked = True
            reason = codex_reason
        else:
            reason = None
        if reason is None:
            return cand, skipped
        skipped.append((cand, reason))
    return None, skipped


def dispatch_command(codex: dict, cand: Candidate) -> str:
    """The dispatch one-liner, re-pointed at THIS candidate.

    One template; the candidate carries the model and the effort.  Never trust
    the template's own defaults -- every codex call names both explicitly,
    because `codex-dispatch` itself refuses to assume either.
    """
    template = str(codex.get("dispatch", "") or "")
    if not template:
        return ""
    out = re.sub(r"--model\s+\S+", f"--model {cand.model}", template)
    out = re.sub(r"--effort\s+\S+", f"--effort {cand.effort}", out)
    if "--model" not in out:
        out = f"{out} --model {cand.model} --effort {cand.effort}"
    return out
