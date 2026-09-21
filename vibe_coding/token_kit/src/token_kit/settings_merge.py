"""Merging into ~/.claude/settings.json without damaging what is already there.

Two pure functions, no I/O, so the guard can test the merge itself rather than
a file dance. The rules they encode:

  * a key already present keeps ITS value -- the kit never overwrites a
    deliberate choice; the difference is reported as a conflict instead;
  * key ORDER is preserved and new keys are appended (Python dicts keep
    insertion order, and json.dump writes them in that order);
  * the hook is registered at most once, found by its COMMAND, not by its
    position, so a re-run cannot duplicate it;
  * an existing entry with the same matcher is EXTENDED, not replaced, so an
    unrelated hook sharing that matcher survives.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field


@dataclass
class MergeReport:
    keys_added: list[str] = field(default_factory=list)
    keys_same: list[str] = field(default_factory=list)
    key_conflicts: list[tuple[str, object, object]] = field(default_factory=list)
    hook_added: bool = False
    hook_present: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.keys_added) or self.hook_added


def merge(settings: dict, add_keys: dict, hook_command: str, matcher: str,
          event: str = "PreToolUse") -> tuple[dict, MergeReport]:
    """Return (new settings, report). `settings` is not modified."""
    out = copy.deepcopy(settings)
    rep = MergeReport()

    for key, want in add_keys.items():
        if key not in out:
            out[key] = want
            rep.keys_added.append(key)
        elif out[key] == want:
            rep.keys_same.append(key)
        else:
            rep.key_conflicts.append((key, out[key], want))

    hooks = out.setdefault("hooks", {})
    entries = hooks.setdefault(event, [])
    already = any(
        h.get("command") == hook_command
        for entry in entries
        for h in entry.get("hooks", [])
    )
    rep.hook_present = already
    if not already:
        for entry in entries:
            if entry.get("matcher") == matcher:
                entry.setdefault("hooks", []).append(
                    {"type": "command", "command": hook_command})
                break
        else:
            entries.append({"matcher": matcher,
                            "hooks": [{"type": "command", "command": hook_command}]})
        rep.hook_added = True

    return out, rep


def unmerge(settings: dict, added_keys: list[str], hook_command: str | None,
            event: str = "PreToolUse") -> dict:
    """Remove exactly what merge() added: the listed keys and that one hook.

    Empty containers left behind are removed, so an uninstall on a settings
    file that had no `hooks` at all leaves it with no `hooks` again.
    """
    out = copy.deepcopy(settings)
    for key in added_keys:
        out.pop(key, None)

    hooks = out.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get(event), list):
        kept = []
        for entry in hooks[event]:
            inner = [h for h in entry.get("hooks", []) if h.get("command") != hook_command]
            if inner:
                entry = dict(entry, hooks=inner)
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
        if not hooks:
            out.pop("hooks", None)
    return out
