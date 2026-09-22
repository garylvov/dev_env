"""matrix.py -- read agent_trigger_matrix.md and say whether it is sound.

The guide is a MARKDOWN document, because a person reads it as often as the
router does: prose, one chart of kinds, one budget table, then worked examples.
This module is the only place that knows its shape.

Two tables are parsed and nothing else.  They are located by their HEADER ROW,
never by position, so prose, code fences and the whole Examples section can be
rewritten without touching a parsed row -- that is what
`test_examples_do_not_affect_parsing` pins.

A parse failure is NOT an exception the caller may ignore: it raises
MatrixError, and every caller on the hook path turns that into "allow, and say
so in an event row".  A router that blocks a spawn is worse than one that
misses.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: `prefer` grammar.  One grammar for every engine.
CANDIDATE_RE = re.compile(r"^(codex|claude):([A-Za-z0-9][A-Za-z0-9._-]*):(low|medium|high|xhigh)$")

#: The closed set of work shapes, written in the chart the way a person says
#: them.  The value is the name the hook and the tests use.
SHAPE_WORDS = {
    "codex does it all": "codex-direct",
    "claude does it all": "claude-direct",
    "claude plans, codex executes": "claude-plans-codex-executes",
}
SHAPES = tuple(sorted(set(SHAPE_WORDS.values())))

#: Claude tiers, cheapest first.  MEASURED per-token list price, not folklore:
#: sonnet $2, opus $5, fable $10 per million input tokens.  The ladder rule
#: below is stated against this order and nothing else.
CLAUDE_TIERS = ("sonnet", "opus", "fable")

#: Used only when the budget table cannot be read.  The document is the
#: authority -- these exist so a torn file cannot leave the cap unbounded.
DEFAULT_WARN, DEFAULT_FLOOR, DEFAULT_HARD = 150, 230, 250

#: The kit root: .../token_kit, three parents up from this file.
KIT_DIR = Path(__file__).resolve().parent.parent.parent.parent

#: Nothing shipped in this kit may name a project, a repo, a cluster, a
#: doctrine file or one user's history.  The guard greps the shipped guide and
#: every generated agent against this list; it is never run on the hook path.
BANNED_WORDS = ("agrescap", "mega", "wbc", "grove4", "imprint", "retread",
                "oscar", "stellex", "curric", "protomotions", "isaac")

CHART_COLUMNS = ("kind", "use when", "who does it", "prefer", "done when")
BUDGET_COLUMNS = ("threshold", "calls", "what happens")


class MatrixError(Exception):
    """The guide could not be read as a routing guide."""


# --------------------------------------------------------------------------
# the markdown table parser -- strict, small, and located by header row
# --------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    inner = line.strip()
    if not inner.startswith("|"):
        return []
    inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip().strip("`").strip() for c in inner.split("|")]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def find_table(text: str, columns: tuple[str, ...]) -> list[dict]:
    """Every row of the ONE table whose header row is exactly `columns`.

    Matching on the header row is what makes the surrounding document free:
    a code fence in the examples is not a table, and a table with different
    columns is not this one.
    """
    lines = text.splitlines()
    want = [c.lower() for c in columns]
    for i, line in enumerate(lines):
        if [c.lower() for c in _cells(line)] != want:
            continue
        if i + 1 >= len(lines) or not _is_separator(lines[i + 1]):
            raise MatrixError(f"table '{columns[0]}' has no separator row under its header")
        rows = []
        for raw in lines[i + 2:]:
            if not raw.strip().startswith("|"):
                break
            cells = _cells(raw)
            if len(cells) != len(columns):
                raise MatrixError(
                    f"table '{columns[0]}': a row has {len(cells)} cells, not {len(columns)}: "
                    f"{raw.strip()[:80]}")
            rows.append(dict(zip(want, cells)))
        return rows
    raise MatrixError(f"no table whose header row is {list(columns)}")


def parse_prefer(cell: str) -> list[str]:
    return [p.strip().strip("`").strip() for p in cell.split(">") if p.strip()]


class Matrix:
    """One parsed agent_trigger_matrix.md."""

    def __init__(self, text: str, source: Path):
        self.source = source
        self.text = text
        self.rows: list[dict] = []
        for r in find_table(text, CHART_COLUMNS):
            self.rows.append({
                "name": r["kind"],
                "use_when": r["use when"],
                "shape_words": r["who does it"],
                "shape": SHAPE_WORDS.get(r["who does it"].lower(), r["who does it"]),
                "prefer": parse_prefer(r["prefer"]),
                "stop": r["done when"],
            })
        self._budget_rows = find_table(text, BUDGET_COLUMNS)

    # -- lookups ----------------------------------------------------------
    def names(self) -> list[str]:
        return [r["name"] for r in self.rows if r["name"]]

    def row(self, name: str) -> dict | None:
        for r in self.rows:
            if r["name"] == name:
                return r
        return None

    def budget(self) -> tuple[int, int, int]:
        """warn / floor / hard, read from the budget table.  env overrides.

        The one thing that cannot be faked: change a number in the document
        and the cap changes.  Nothing else may define it.
        """
        table = {}
        for r in self._budget_rows:
            try:
                table[r["threshold"].lower()] = int(re.sub(r"[^0-9]", "", r["calls"]))
            except (KeyError, ValueError):
                continue

        def pick(env: str, key: str, fallback: int) -> int:
            raw = os.environ.get(env) or table.get(key, fallback)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback

        return (pick("LANE_RECYCLER_WARN", "warn", DEFAULT_WARN),
                pick("LANE_RECYCLER_FLOOR", "floor", DEFAULT_FLOOR),
                pick("LANE_RECYCLER_HARD", "hard", DEFAULT_HARD))


def matrix_path() -> Path:
    """Where the guide lives.  env.sh sets LANE_RECYCLER_MATRIX at install."""
    env = os.environ.get("LANE_RECYCLER_MATRIX")
    return Path(env) if env else KIT_DIR / "agent_trigger_matrix.md"


#: (path, mtime, size) -> Matrix.  The hook pays one parse per EDIT of the
#: guide, not one per tool call.  Measured: see the lane's out.md.
_CACHE: dict[tuple[str, float, int], Matrix] = {}


def load(path: Path | str | None = None) -> Matrix:
    src = Path(path) if path is not None else matrix_path()
    try:
        st = src.stat()
        key = (str(src), st.st_mtime, st.st_size)
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise MatrixError(f"{src}: {exc}") from exc
    m = Matrix(text, src)
    if not m.rows:
        raise MatrixError(f"{src}: the chart has no rows")
    _CACHE.clear()
    _CACHE[key] = m
    return m


# --------------------------------------------------------------------------
# the optional per-machine overrides
# --------------------------------------------------------------------------
def config_path() -> Path:
    env = os.environ.get("TOKEN_KIT_CONFIG")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "token_kit" / "config.toml"


def router_config() -> dict:
    """The `[router]` table of the one optional config file, or {}.

    Everything here is an OVERRIDE of a code default.  The file being absent
    is the normal case and is never an error: a kit that needs a config file
    to run is not project-agnostic.
    """
    import tomllib
    try:
        with config_path().open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    table = data.get("router")
    return table if isinstance(table, dict) else {}


# --------------------------------------------------------------------------
# validation -- what `install --probe`, `--validate` and the guards assert
# --------------------------------------------------------------------------
def tier_index(model: str) -> int:
    return CLAUDE_TIERS.index(model) if model in CLAUDE_TIERS else -1


def validate(m: Matrix) -> list[str]:
    """Every way this guide can be internally wrong, as one line each."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(m.rows):
        name = r["name"] or f"<row {i}>"
        if not r["name"]:
            problems.append(f"row {i}: no kind name")
        elif name in seen:
            problems.append(f"{name}: duplicate kind")
        seen.add(name)

        if r["shape"] not in SHAPES:
            problems.append(f"{name}: 'who does it' is {r['shape_words']!r}, "
                            f"not one of {sorted(SHAPE_WORDS)}")
        if not r["use_when"]:
            problems.append(f"{name}: no 'use when' (the guide is generated from it)")
        if not r["stop"]:
            problems.append(f"{name}: no 'done when'")

        prefer = r["prefer"]
        if not prefer:
            problems.append(f"{name}: empty prefer ladder")
            continue
        for cand in prefer:
            if not CANDIDATE_RE.match(cand):
                problems.append(f"{name}: prefer entry {cand!r} is not <engine>:<model>:<effort>")
        # Provider-neutral ladders may end in Codex, including Codex-only policies.
        problems.extend(climb_problems(name, prefer))

    for key in ("warn", "floor", "hard"):
        if not any(r.get("threshold", "").lower() == key for r in m._budget_rows):
            problems.append(f"the budget table has no {key} row")
    warn, floor, hard = m.budget()
    if not warn <= floor <= hard:
        problems.append(f"the budget table is not ordered: {warn}/{floor}/{hard}")
    return problems


def climb_problems(name: str, prefer: list[str]) -> list[str]:
    """THE NO-POINTLESS-CLIMB RULE, in code.

    A dearer Claude model at the same effort is an AVAILABILITY FLOOR, never
    an upgrade.  So: consecutive claude candidates may step at most ONE tier
    up, and no candidate may sit more than one tier above the ladder's FIRST
    claude candidate.  Cheap work that falls through stays cheap; that is the
    rule that forbids a sonnet-medium kind from listing the top tier at the
    same effort as its floor.
    """
    out: list[str] = []
    tiers = [tier_index(c.split(":")[1]) for c in prefer if c.startswith("claude:")]
    tiers = [t for t in tiers if t >= 0]
    if not tiers:
        return out
    first = tiers[0]
    for prev, nxt in zip(tiers, tiers[1:]):
        if nxt - prev > 1:
            out.append(f"{name}: prefer climbs {CLAUDE_TIERS[prev]} -> {CLAUDE_TIERS[nxt]}, "
                       f"more than one tier -- a dearer model is a floor, not an upgrade")
    for t in tiers:
        if t - first > 1:
            out.append(f"{name}: prefer reaches {CLAUDE_TIERS[t]}, more than one tier above its "
                       f"first claude candidate {CLAUDE_TIERS[first]}")
            break
    return out


def banned_words_in(text: str) -> list[str]:
    """Project words that may not appear in anything this kit ships."""
    hay = text.lower()
    return [w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", hay)]


# --------------------------------------------------------------------------
# the SessionStart guide -- generated FROM the document, so it cannot drift
# --------------------------------------------------------------------------
EXAMPLES_HEADING = "## Examples"


def guide_text(m: Matrix, with_examples: bool = True) -> str:
    """The document itself, whole or without its Examples section.

    There is nothing to generate and nothing to drift: the file a person reads
    IS the context the delegating thread gets.  The only choice is whether the
    worked examples ride along; `session_start` decides that by measured size.
    """
    if with_examples:
        return m.text.rstrip("\n")
    head = m.text.split(EXAMPLES_HEADING)[0].rstrip("\n")
    return (f"{head}\n\nWorked examples -- dispatching a big task, interrupting it, resuming "
            f"versus respawning it, and a Claude agent running its mechanical steps through "
            f"codex -- are in the '{EXAMPLES_HEADING.strip('# ')}' section of {m.source}.")


def approx_tokens(text: str) -> int:
    """chars/4.  A rough count, named as one wherever it is reported."""
    return len(text) // 4
