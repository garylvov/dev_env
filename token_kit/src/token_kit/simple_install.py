"""Optional, ownership-safe guidance upgrade; no launch-time installation.

Only explicitly named files are inspected. Preview is read-only. Applying a
preview requires the caller to establish that affected old sessions have exited.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile

from . import project_install as legacy
from .manifest import Manifest
from .simple_guidance import CONCURRENCY, TASK_RULES, EFFORT

_PREVIOUS_SIMPLE_INSTRUCTIONS = """Token Kit keeps work in plain task folders and can roll over managed sessions.
Read task.md (or in.md), STATE.md, and any user preferences before continuing.
Write useful progress and next steps in STATE.md; use any format and length.
Save results in out.md or artifacts/ when useful. Worker folders are optional.
Before rollover, save enough context for the next session to continue.
Check uncertain external outcomes before repeating those operations.
Follow user and site permissions. Token Kit notes do not grant extra authority.
"""
_PREVIOUS_CURRENT_INSTRUCTIONS = """Read task.md/in.md, STATE.md and user preferences. Save useful progress and rollover context in STATE.md, with results in out.md or artifacts/. Formats and worker folders are optional. Check uncertain outcomes before retrying.

""" + CONCURRENCY + '\n'
INSTRUCTIONS = TASK_RULES + EFFORT + CONCURRENCY + "\n"


@dataclass(frozen=True)
class Edit:
    path: Path
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class Preview:
    edits: tuple[Edit, ...]
    diagnostics: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"changed": [str(e.path) for e in self.edits],
                "diagnostics": list(self.diagnostics),
                "requires_old_sessions_stopped": bool(self.edits)}


def _read(path: Path) -> bytes | None:
    # Never follow a config symlink, including a symlinked parent directory.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise legacy.InstallConflict(f"Refusing config symlink: {part}")
    return path.read_bytes() if path.exists() else None


def _block(body: str) -> str:
    return "<!-- token-kit:begin -->\n" + body.rstrip() + "\n<!-- token-kit:end -->"


def strip_owned_hooks(settings: dict, manifest: Path) -> tuple[dict, list[str]]:
    """Remove only exact generated hook objects recorded in the legacy manifest.

    Extra fields mean the hook was customized. Retain those hooks and report them
    through inspect_legacy_hooks rather than silently discarding user changes.
    """
    _read(manifest)
    owned = set(Manifest(manifest).hook_rows())
    out = copy.deepcopy(settings)
    removed = []
    hooks = out.get("hooks", {})
    if not isinstance(hooks, dict):
        raise legacy.InstallConflict("Expected hooks object")
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            raise legacy.InstallConflict(f"Expected hook list for {event}")
        kept_entries = []
        event_removed = False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
                raise legacy.InstallConflict(f"Malformed hook entry for {event}")
            kept = []
            for hook in entry.get("hooks", []):
                command = hook.get("command") if isinstance(hook, dict) else None
                matcher = entry.get("matcher") or ""
                if (event, matcher, command) in owned and hook == {"type": "command", "command": command}:
                    removed.append(f"{event}: {command}")
                    event_removed = True
                else:
                    kept.append(hook)
            if kept or not entry.get("hooks") or set(entry) - {"matcher", "hooks"}:
                kept_entries.append(dict(entry, hooks=kept))
        if event_removed:
            if kept_entries:
                hooks[event] = kept_entries
            else:
                hooks.pop(event)
    if removed and not hooks and "hooks" in out:
        out.pop("hooks")
    return out, removed


def inspect_legacy_hooks(settings: Path, manifest: Path | None = None) -> list[str]:
    raw = _read(settings)
    if raw is None:
        return []
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise legacy.InstallConflict(f"Malformed settings: {settings}")
    owned = set()
    if manifest is not None:
        _read(manifest)
        owned = set(Manifest(manifest).hook_rows())
    findings = []
    for event, entries in value.get("hooks", {}).items():
        if not isinstance(entries, list):
            raise legacy.InstallConflict(f"Malformed hook list: {settings}: {event}")
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                if not isinstance(command, str):
                    continue
                exact = (event, entry.get("matcher") or "", command) in owned
                if exact or any(s in command for s in ("token-kit", "token_kit", "/router/hook.py", "/respawn/")):
                    kind = "owned legacy hook" if exact else "possible legacy hook (ownership unverified)"
                    findings.append(f"{settings}: {event}: {kind}: {command}")
    return findings


def preview(project: Path | None = None, *, settings: Path | None = None,
            manifest: Path | None = None) -> Preview:
    """Prepare optional project guidance and explicit legacy hook cleanup."""
    edits = []
    diagnostics = []
    if project is not None:
        project = project.absolute()
        if not project.is_dir():
            raise legacy.InstallConflict(f"Project directory does not exist: {project}")
        path = project / "AGENTS.md"
        before = _read(path)
        text = (before or b"").decode()
        start, end = legacy._markers("AGENTS.md")
        block = _block(INSTRUCTIONS)
        if start in text or end in text:
            if text.count(start) != 1 or text.count(end) != 1:
                raise legacy.InstallConflict(f"Ambiguous Token Kit markers: {path}")
            left, right = text.index(start), text.index(end) + len(end)
            known = {_block(v) for v in (*legacy._LEGACY_INSTRUCTION_VERSIONS, legacy.INSTRUCTIONS,
                                        _PREVIOUS_SIMPLE_INSTRUCTIONS, _PREVIOUS_CURRENT_INSTRUCTIONS, INSTRUCTIONS)}
            if right <= left or text[left:right] not in known:
                raise legacy.InstallConflict(f"Customized Token Kit guidance preserved: {path}")
            after = text[:left] + block + text[right:]
        else:
            after = text + ("\n\n" if text else "") + block + "\n"
        if after.encode() != before:
            edits.append(Edit(path, before, after.encode()))
        # Update only the existing owned AGENTS record; leave MCP config intact.
        mp = project / legacy.MANIFEST
        mb = _read(mp)
        if mb:
            data = json.loads(mb)
            record = data.get("files", {}).get("AGENTS.md")
            if record:
                legacy._owned_text("AGENTS.md", text, record)
                record["sha256"] = hashlib.sha256(block.encode()).hexdigest()
                ma = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
                if ma != mb:
                    edits.append(Edit(mp, mb, ma))
    if settings is not None:
        settings = settings.absolute()
        raw = _read(settings)
        if raw is not None:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise legacy.InstallConflict(f"Expected JSON object: {settings}")
            if manifest is not None:
                cleaned, removed = strip_owned_hooks(value, manifest)
                if removed:
                    edits.append(Edit(settings, raw, (json.dumps(cleaned, indent=2) + "\n").encode()))
                    diagnostics.extend(f"Remove {s}" for s in removed)
            else:
                diagnostics.append("No legacy manifest supplied; hook settings preserved.")
            diagnostics.extend(inspect_legacy_hooks(settings, manifest))
    return Preview(tuple(edits), tuple(diagnostics))


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".token-kit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if path.exists():
            os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def apply(plan: Preview, backup_dir: Path, *, old_sessions_stopped: bool = False) -> Path:
    """Back up every original before editing; write an exact rollback script.

    Caller owns serialization against other config writers. Byte preconditions
    reject stale previews. A partial failure leaves backups and rollback intact.
    """
    if not old_sessions_stopped:
        raise legacy.InstallConflict("Defer installed-policy changes until affected old sessions exit")
    for edit in plan.edits:
        if _read(edit.path) != edit.before:
            raise legacy.InstallConflict(f"Config changed since preview: {edit.path}")
    backup_dir = backup_dir.absolute()
    _read(backup_dir / "unused")
    backup_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)
    recipe = ["#!/bin/sh", "set -eu", "# Stop candidate sessions before restoring configuration."]
    for index, edit in enumerate(plan.edits):
        dest = shlex.quote(str(edit.path))
        if edit.before is None:
            recipe.append(f"rm -f -- {dest}")
        else:
            backup = backup_dir / f"{index:03d}.original"
            backup.write_bytes(edit.before)
            os.chmod(backup, edit.path.stat().st_mode & 0o777)
            recipe.append(f"cp -p -- {shlex.quote(str(backup))} {dest}")
    rollback = backup_dir / "rollback.sh"
    rollback.write_text("\n".join(recipe) + "\n")
    os.chmod(rollback, 0o700)
    (backup_dir / "preview.json").write_text(json.dumps(plan.as_dict(), indent=2) + "\n")
    for edit in plan.edits:
        if _read(edit.path) != edit.before:
            raise legacy.InstallConflict(f"Config changed during apply; rollback at {rollback}: {edit.path}")
        _atomic(edit.path, edit.after)
    return rollback
