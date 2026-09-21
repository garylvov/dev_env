"""matrix.py -- read agent_trigger_matrix.toml and say whether it is sound.

The table is the spec; this module is the only place that knows its shape.
tomllib replaces the awk compiler the bash reader had to carry, so the whole
"compile to JSON, cache on mtime" apparatus is gone: a 34 KB table parses in
about a millisecond and the hook pays that once per call.

A parse failure is NOT an exception the caller may ignore: it raises
MatrixError, and every caller on the hook path turns that into "allow, and say
so in an event row".  A router that blocks a spawn is worse than one that
misses.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

#: `prefer` grammar, fixed by the operator ruling of 2026-09-20.
CANDIDATE_RE = re.compile(r"^(codex|claude):([A-Za-z0-9][A-Za-z0-9._-]*):(low|medium|high)$")

#: The closed set of work shapes.  A row outside it is a defect, not a default.
SHAPES = ("codex-direct", "claude-direct", "claude-plans-codex-executes", "refuse")

#: Used only when the table carries no [budget] at all.  The table is the
#: authority -- these exist so a torn file cannot leave the cap unbounded.
DEFAULT_WARN, DEFAULT_FLOOR, DEFAULT_HARD = 150, 230, 250

#: The kit root: .../token_kit, three parents up from this file.
KIT_DIR = Path(__file__).resolve().parent.parent.parent.parent


class MatrixError(Exception):
    """The table could not be read as a routing table."""


class Matrix:
    """One parsed agent_trigger_matrix.toml."""

    def __init__(self, data: dict, source: Path):
        self.source = source
        self.data = data
        self.rows: list[dict] = [r for r in data.get("row", []) if isinstance(r, dict)]
        self.defaults: dict = data.get("defaults", {}) or {}
        self.codex: dict = data.get("codex", {}) or {}
        self.concurrency: dict = data.get("concurrency", {}) or {}
        self.personas: dict = data.get("personas", {}) or {}

    # -- lookups ----------------------------------------------------------
    def names(self) -> list[str]:
        return [str(r.get("name", "")) for r in self.rows if r.get("name")]

    def row(self, name: str) -> dict | None:
        for r in self.rows:
            if r.get("name") == name:
                return r
        return None

    def budget(self) -> tuple[int, int, int]:
        """warn / floor / hard.  The table is the READ; env is an override.

        The one case in router_cases.jsonl that cannot be faked: change
        `warn` in the TOML and the cap changes.  Nothing else may define it.
        """
        b = self.data.get("budget", {}) or {}

        def pick(env: str, key: str, fallback: int) -> int:
            raw = os.environ.get(env) or b.get(key, fallback)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback

        return (pick("LANE_RECYCLER_WARN", "warn", DEFAULT_WARN),
                pick("LANE_RECYCLER_FLOOR", "floor", DEFAULT_FLOOR),
                pick("LANE_RECYCLER_HARD", "hard", DEFAULT_HARD))

    def default_model(self) -> str:
        return str(self.defaults.get("model", "sonnet"))


def matrix_path() -> Path:
    """Where the table lives.  env.sh sets LANE_RECYCLER_MATRIX at install."""
    env = os.environ.get("LANE_RECYCLER_MATRIX")
    return Path(env) if env else KIT_DIR / "agent_trigger_matrix.toml"


def load(path: Path | str | None = None) -> Matrix:
    src = Path(path) if path is not None else matrix_path()
    try:
        with src.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MatrixError(f"{src}: {exc}") from exc
    m = Matrix(data, src)
    if not m.rows:
        raise MatrixError(f"{src}: no [[row]] blocks")
    return m


# --------------------------------------------------------------------------
# validation -- what `install --probe` and the guard test assert
# --------------------------------------------------------------------------
def validate(m: Matrix) -> list[str]:
    """Every way this table can be internally wrong, as one line each.

    An empty list means sound.  This is the Python half of the bash
    guard_matrix.sh checks A6/A14; it is called by the tests and is worth
    calling from an installer probe.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(m.rows):
        name = str(r.get("name", "")) or f"<row {i}>"
        if not r.get("name"):
            problems.append(f"row {i}: no name")
        elif name in seen:
            problems.append(f"{name}: duplicate row name")
        seen.add(name)

        shape = r.get("shape")
        if shape not in SHAPES:
            problems.append(f"{name}: shape {shape!r} is not one of {SHAPES}")
        if not str(r.get("use_when", "")).strip():
            problems.append(f"{name}: no use_when (the menu is generated from it)")

        prefer = r.get("prefer")
        if prefer is None:
            problems.append(f"{name}: no prefer list")
            continue
        if not isinstance(prefer, list):
            problems.append(f"{name}: prefer is not a list")
            continue
        for cand in prefer:
            if not isinstance(cand, str) or not CANDIDATE_RE.match(cand):
                problems.append(f"{name}: prefer entry {cand!r} is not <engine>:<model>:<effort>")
        if shape == "refuse":
            if prefer:
                problems.append(f"{name}: a refuse row must have an empty prefer list")
            continue
        if not prefer:
            problems.append(f"{name}: empty prefer list on a non-refuse row")
        elif not str(prefer[-1]).startswith("claude:"):
            problems.append(
                f"{name}: prefer list does not END in a claude candidate "
                f"(last is {prefer[-1]!r}) -- the row could be blocked by codex being full")

    for key in ("warn", "floor", "hard"):
        if key not in (m.data.get("budget") or {}):
            problems.append(f"[budget] has no {key}")
    b = m.data.get("budget") or {}
    if all(k in b for k in ("warn", "floor", "hard")) and not (b["warn"] <= b["floor"] <= b["hard"]):
        problems.append(f"[budget] is not ordered: {b['warn']}/{b['floor']}/{b['hard']}")
    return problems


# --------------------------------------------------------------------------
# the row menu -- generated FROM the table, so it cannot drift
# --------------------------------------------------------------------------
def menu_text(m: Matrix) -> str:
    lines = [
        "AGENT TRIGGER MATRIX - put a line 'ROW: <name>' in every Agent spawn prompt.",
        "The row sets the model, the agent and the work shape. Pick by 'use when'.",
    ]
    for r in m.rows:
        name = r.get("name")
        if not name:
            continue
        lines.append(f"  ROW: {name} [{r.get('shape', 'claude-direct')}]"
                     f" - {r.get('use_when', '(no use_when)')}")
    lines += [
        "Shapes: codex-direct = the codex candidate does it all;"
        " claude-direct = the Claude model does it;",
        "claude-plans-codex-executes = Claude judges, every mechanistic sub-step goes to codex;",
        "refuse = the spawn is denied and a shell substitute is given.",
    ]
    lines += codex_menu_lines(m)
    return "\n".join(lines)


def codex_menu_lines(m: Matrix) -> list[str]:
    """The codex surface, as a POINTER rather than a paste.

    The `[codex] usage_text` key names the file that documents the four job
    commands. The menu rides on every session start, so it names the commands
    and the file instead of inlining it -- a reader who needs the detail opens
    one file, and the menu stays small enough to be free.
    """
    dispatch = str(m.codex.get("dispatch") or "").split()[0:1]
    job = str(m.codex.get("job") or "")
    usage = os.path.expandvars(os.path.expanduser(str(m.codex.get("usage_text") or "")))
    if not job and not dispatch:
        return []
    out = [f"codex: `{dispatch[0] if dispatch else 'codex-dispatch'}` for one turn you read "
           f"right away; `{job or 'codex-job'} start|send|wait|status` for a background turn "
           "you can message (run `wait` as a BACKGROUND command)."]
    if usage and Path(usage).is_file():
        out.append(f"  the four commands in full: {usage}")
    return out
