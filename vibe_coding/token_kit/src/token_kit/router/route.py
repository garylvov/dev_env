"""route.py -- pick the row, in the one order the operator ruled.

    1. an explicit `ROW: <name>` line in the spawn prompt wins.  An unknown
       name is a deny that names the valid rows.
    2. otherwise `topics`, as whole words/phrases, against the spawn's
       `description` ONLY.
    3. otherwise `globs`, against the paths that appear in the prompt.
    4. otherwise nothing -- the caller applies [defaults] and logs `unrouted`.

Rule 2 is the measured one and must not be softened.  Replaying 510 historical
spawns through the bash reader: matching topics against the prompt BODY sends
426 of them (83.5%) to `watch-poll-wait`, which REFUSES, because every brief we
write contains "no background waits" and "do not poll".  Against the
description alone, 3 of 510 (0.6%) are refused.  That 140x difference is the
whole argument, and `rule2-THE-RULING-body-words-never-route` is its guard.
"""

from __future__ import annotations

import fnmatch
import re

ROW_LINE_RE = re.compile(r"^[ \t]*ROW:[ \t]*([A-Za-z0-9_-]+)", re.MULTILINE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: The two shapes a path takes in a brief: absolute, or root-relative.
_ABS_PATH_RE = re.compile(r"/[A-Za-z0-9._*/-]+")
_REL_PATH_RE = re.compile(r"[A-Za-z0-9._*-]+(?:/[A-Za-z0-9._*-]+)+")

#: Matching every path in a 6 KB brief against every glob of every row is the
#: hook's only quadratic step; the bash reader capped it here and so do we.
MAX_PATHS = 100


class UnknownRow(Exception):
    """A `ROW:` line named a row the table does not have."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def normalise(text: str) -> str:
    """Lowercase, and every run of non-alphanumerics becomes one space.

    Whole-word matching is then a plain substring test on " word ", which is
    why `watch` does not match inside `rewatching` or `stopwatchers`.
    """
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def explicit_row(prompt: str) -> str | None:
    m = ROW_LINE_RE.search(prompt or "")
    return m.group(1) if m else None


def match_topics(rows: list[dict], description: str) -> str | None:
    if not description:
        return None
    hay = f" {normalise(description)} "
    for r in rows:
        for topic in r.get("topics", []) or []:
            t = normalise(str(topic))
            if t and f" {t} " in hay:
                return str(r.get("name", "")) or None
    return None


def paths_in(prompt: str) -> list[str]:
    found: set[str] = set()
    for rx in (_ABS_PATH_RE, _REL_PATH_RE):
        found.update(rx.findall(prompt or ""))
    return sorted(found)[:MAX_PATHS]


def glob_to_fnmatch(pattern: str) -> str:
    """`**/x` and `**` both collapse to `*`.

    fnmatch's `*` crosses `/` (unlike a shell's, like bash's `case`), which is
    exactly the behaviour the bash reader relied on.
    """
    return pattern.replace("**/", "*").replace("**", "*")


def match_globs(rows: list[dict], prompt: str, data_root: str) -> str | None:
    cands = paths_in(prompt)
    if not cands:
        return None
    root = (data_root or "").rstrip("/")
    for r in rows:
        for raw in r.get("globs", []) or []:
            pat = glob_to_fnmatch(str(raw))
            for p in cands:
                rel = p[len(root) + 1:] if root and p.startswith(root + "/") else p.lstrip("/")
                if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(p, pat):
                    return str(r.get("name", "")) or None
    return None


def route(matrix, description: str, prompt: str, data_root: str) -> tuple[str | None, str | None]:
    """(row name, how) or (None, None).  Raises UnknownRow for a bad ROW: line."""
    want = explicit_row(prompt)
    if want:
        if matrix.row(want) is None:
            raise UnknownRow(want)
        return want, "row_line"

    name = match_topics(matrix.rows, description)
    if name:
        return name, "topics_description"

    name = match_globs(matrix.rows, prompt, data_root)
    if name:
        return name, "globs"

    return None, None
