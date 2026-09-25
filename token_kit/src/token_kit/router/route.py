"""route.py -- find the kind the spawning thread named, and nothing else.

THE ROUTER IS SOFT.  It reads ONE line out of the spawn prompt:

    KIND: <name>        (or ROW: <name>, the older spelling, still accepted)

and that is the whole of routing.  It never guesses a kind from topic words or
path globs, never denies a spawn for a routing reason, and never applies
defaults to a spawn that named nothing.

WHAT WAS DELETED HERE, and why it is not coming back: topic-word matching
against the description, glob matching against every path in the brief, a
once-per-session deny that showed a menu, and a `refuse` shape that denied a
spawn outright.  A guide that refuses is not a guide; an unnamed spawn is the
delegating thread's choice, not a defect to correct.  The code is gone, not
just its configuration, so there is nothing to re-enable by accident.
"""

from __future__ import annotations

import re

KIND_LINE_RE = re.compile(r"^[ \t]*(?:KIND|ROW):[ \t]*([A-Za-z0-9_-]+)", re.MULTILINE)


def named_kind(prompt: str) -> str | None:
    """The kind the prompt names, or None.  The first such line wins."""
    m = KIND_LINE_RE.search(prompt or "")
    return m.group(1) if m else None
