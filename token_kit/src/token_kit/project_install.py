"""Project-local instructions and opt-in CodeGraph wiring; never launches a server.

Run ``token-kit install --project PATH``. Ownership
is tracked per marked text block or JSON entry, leaving other settings alone.
This configures a project only; it does not install CLI executables on PATH.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib


MANIFEST = ".token-kit/project-install.json"
# Freeze the exact previously installed text so upgrades only recognize known
# installer versions; unrelated or customized text is never inferred as ours.
_LEGACY_INSTRUCTIONS = """Use token-kit-workflow to maintain portable task and agent records.
Consult token-kit-workflow --help for new, agent, checkpoint, launch, and status.
Keep each agent's assignment in in.md, progress and next action in STATE.md,
and completed results with verification evidence in out.md.
Checkpoint at meaningful milestones and before switching clients. Resume the
same logical agent using its files; native sessions are optional conveniences.
Prefer Codex for bounded delegated work when delegation is authorized.
Treat interrupted operations as uncertain until their outcomes are checked.
These instructions alone do not enforce budgets, prevent compaction, or
automatically switch providers; use the workflow launcher and its capabilities.
"""
_LEGACY_INSTRUCTION_VERSIONS = (
    _LEGACY_INSTRUCTIONS,
    _LEGACY_INSTRUCTIONS.replace("new, agent,", "new, agent add,"),
)
INSTRUCTIONS = _LEGACY_INSTRUCTIONS.replace("token-kit-workflow", "token-kit").replace(
    "checkpoint, launch,", "checkpoint, resume, launch,")
_LEGACY_INSTRUCTION_VERSIONS += (INSTRUCTIONS,)
INSTRUCTIONS += """Delegationmaxxing: delegate bounded independent work when authorized. Keep the
coordinator focused on user intent and acceptance; use workstream leads only to
integrate several workers. Default to coordinator -> lead -> worker, skipping
the lead for small tasks. Each agent owns its state and returns evidence in out.md.
Record parent/owner, source-path scope, dependencies, and acceptance checks in
in.md; parent relationships and source ownership are conventions, not enforced.
Parents inspect results and evidence instead of whole worker transcripts.
Prefer Codex: Luna for narrow work, Astra for difficult work, including reviews.
Only planning and detailed debugging prefer Fable (medium) over Astra (high).
Use medium effort for Opus too. These are preferences, not automatic routing.
Serialize overlapping edits; stop at acceptance; never spawn agents only to wait.
"""
_LEGACY_INSTRUCTION_VERSIONS += (INSTRUCTIONS,)
INSTRUCTIONS = INSTRUCTIONS.replace(
    "Only planning and detailed debugging prefer Fable (medium) over Astra (high).",
    "Prefer Astra (high) for planning and detailed debugging too. Use Fable (medium)\n"
    "only when the user explicitly requests it in natural language, not merely\n"
    "when its name appears in files, examples, or quoted text. Never auto-fallback to Fable.")
_LEGACY_INSTRUCTION_VERSIONS += (INSTRUCTIONS,)
INSTRUCTIONS += """Fable (medium) may red-team a design only when the user explicitly asks for it;
a generic request to red-team still defaults to Codex. Record user model overrides
such as 'Opus while we have it' in the coordinator's STATE.md and checkpoint them.
Include the user's exact wording, model/effort, affected tasks or roles, and expiry
condition; copy the effective override into affected workers' in.md assignments.
Opus defaults to medium effort. 'While we have it' means a temporary preference
while access/quota is available, not a permanent default or permission to buy access.
On resume, read these overrides before delegating. If access becomes unavailable,
record that the override ended and return to Codex defaults; do not silently
substitute Fable. If scope is unclear, ask before broadening it. These records are
maintained by agents, not an automatic quota detector or override scheduler.
"""
_LEGACY_INSTRUCTION_VERSIONS += (INSTRUCTIONS,)
INSTRUCTIONS = INSTRUCTIONS.replace("Astra (high)", "Astra (medium)")
INSTRUCTIONS += "Default to medium effort for every model; always run Luna at high effort.\n"
_LEGACY_INSTRUCTION_VERSIONS += (INSTRUCTIONS,)
INSTRUCTIONS += """Explicit user task/role model requests override routing defaults. For example,
'Opus for the big stuff' assigns major reasoning/design/implementation to Opus
(medium); 'Luna for run loops' assigns iterative execution to Luna (high);
'Sol for run loops' assigns iterative execution to Sol (medium). Do not replace
an explicitly requested Sol with Luna or Astra merely because of default policy.
Keep simultaneous role choices separate. Use the most specific applicable scope;
newer requests replace older choices in the same scope, not unrelated roles.
Record exact wording, model/effort, scope and expiry in checkpointed coordinator
state and relevant worker assignments; consult them before spawning and on resume.
If the requested model is unavailable or forbidden by the harness, report it and
ask before substituting, unless the user already authorized a fallback or expiry.
Run loops mean useful execution, diagnosis and iteration; pure waiting should use
process tooling, not repeated model calls. Clarify genuinely ambiguous scope.
"""
MCP = {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}


class InstallConflict(ValueError):
    """An existing user setting or changed owned block requires manual resolution."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _path(project: Path, relative: str) -> Path:
    path = project / relative
    # Refuse symlinks even when they point inside the project: ownership should
    # never silently follow another file, including a global configuration.
    for part in [path, *path.parents]:
        if part == project:
            break
        if part.is_symlink():
            raise InstallConflict(f"Refusing symlink: {part}")
    if not path.resolve().is_relative_to(project):
        raise InstallConflict(f"Path escapes project: {relative}")
    return path


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _markers(relative: str) -> tuple[str, str]:
    if relative.endswith(".toml"):
        return "# token-kit:begin", "# token-kit:end"
    return "<!-- token-kit:begin -->", "<!-- token-kit:end -->"


def _owned_text(relative: str, text: str, record: dict) -> tuple[int, int]:
    start, end = _markers(relative)
    if text.count(start) != 1 or text.count(end) != 1:
        raise InstallConflict(f"Missing or ambiguous owned block: {relative}")
    left = text.index(start)
    right = text.index(end) + len(end)
    if right <= left or _hash(text[left:right]) != record["sha256"]:
        raise InstallConflict(f"Modified owned block: {relative}")
    return left, right


def _object(text: str, relative: str) -> dict:
    value = json.loads(text) if text else {}
    if not isinstance(value, dict):
        raise InstallConflict(f"Expected JSON object: {relative}")
    if "mcpServers" in value and not isinstance(value["mcpServers"], dict):
        raise InstallConflict(f"Expected mcpServers object: {relative}")
    return value


def configure(project: Path, *, engine: str = "both", codegraph: bool = False,
              uninstall: bool = False, dry_run: bool = False) -> list[str]:
    """Serialize cooperating installers without creating a lock-file artifact.

    The project directory itself provides the POSIX advisory lock, including
    for dry runs, which remain read-only. This does not lock out editors.
    """
    project = project.resolve()
    if not project.is_dir():
        raise InstallConflict(f"Project directory does not exist: {project}")
    descriptor = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH if dry_run else fcntl.LOCK_EX)
        return _configure_locked(project, engine=engine, codegraph=codegraph,
                                 uninstall=uninstall, dry_run=dry_run)
    finally:
        os.close(descriptor)


def _configure_locked(project: Path, *, engine: str, codegraph: bool,
                      uninstall: bool, dry_run: bool) -> list[str]:
    """Preflight all edits, then atomically replace individual changed files.

    Installs are additive: installing one engine does not remove the other.
    Uninstall removes selected engine assets; shared instructions remain until
    the last installed engine is removed. Edited owned content is never adopted.
    """
    project = project.resolve()
    if not project.is_dir():
        raise InstallConflict(f"Project directory does not exist: {project}")
    if engine not in {"claude", "codex", "both"}:
        raise ValueError(f"Unknown engine: {engine}")
    manifest_path = _path(project, MANIFEST)
    old_manifest = _read(manifest_path)
    manifest = json.loads(old_manifest) if old_manifest else {
        "version": 1, "engines": [], "files": {}}
    if manifest.get("version") != 1:
        raise InstallConflict("Unsupported project install manifest version")
    records = manifest["files"]
    changes: dict[str, str] = {}
    selected = {"claude", "codex"} if engine == "both" else {engine}
    engines = set(manifest["engines"])

    # Validate every owned record before modifying any file.
    for relative, record in records.items():
        text = _read(_path(project, relative))
        if record["kind"] == "text":
            _owned_text(relative, text, record)
        else:
            value = _object(text, relative).get("mcpServers", {}).get("codegraph")
            if _hash(_json(value)) != record["sha256"]:
                raise InstallConflict(f"Modified owned CodeGraph entry: {relative}")

    def add_text(relative: str, content: str) -> None:
        text = _read(_path(project, relative))
        start, end = _markers(relative)
        block = start + "\n" + content.rstrip() + "\n" + end
        if relative in records:
            # All ownership hashes were validated above. Upgrade only exact
            # recognized historical instructions, preserving surrounding text.
            if relative == "AGENTS.md":
                left, right = _owned_text(relative, text, records[relative])
                legacy_blocks = {
                    start + "\n" + old.rstrip() + "\n" + end
                    for old in _LEGACY_INSTRUCTION_VERSIONS
                }
                if text[left:right] in legacy_blocks:
                    changes[relative] = text[:left] + block + text[right:]
                    records[relative]["sha256"] = _hash(block)
            return
        if start in text or end in text:
            raise InstallConflict(f"Unowned token-kit markers: {relative}")
        prefix = "\n\n" if text else ""
        changes[relative] = text + prefix + block + "\n"
        records[relative] = {"kind": "text", "sha256": _hash(block),
                             "prefix": prefix, "created": not (project / relative).exists()}

    if uninstall:
        engines -= selected
        remove = set()
        if "claude" in selected:
            remove.update({"CLAUDE.md", ".mcp.json"})
        if "codex" in selected:
            remove.add(".codex/config.toml")
        if not engines:
            remove.add("AGENTS.md")
        for relative in sorted(remove & records.keys()):
            record = records.pop(relative)
            text = _read(_path(project, relative))
            if record["kind"] == "text":
                left, right = _owned_text(relative, text, record)
                prefix = record["prefix"]
                if prefix and text[:left].endswith(prefix):
                    left -= len(prefix)
                if text[right:right + 1] == "\n":
                    right += 1
                changes[relative] = text[:left] + text[right:]
            else:
                value = _object(text, relative)
                del value["mcpServers"]["codegraph"]
                if not value["mcpServers"] and not record["had_servers"]:
                    del value["mcpServers"]
                changes[relative] = json.dumps(value, indent=2) + "\n"
    else:
        engines |= selected
        add_text("AGENTS.md", INSTRUCTIONS)
        if "claude" in selected:
            claude = _read(_path(project, "CLAUDE.md"))
            if not re.search(r"(?<!\S)@(?:\./)?AGENTS\.md(?=\s|$)", claude):
                add_text("CLAUDE.md", "@AGENTS.md\n")
        if codegraph and "codex" in selected:
            relative = ".codex/config.toml"
            text = _read(_path(project, relative))
            config = tomllib.loads(text)
            if "codegraph" in config.get("mcp_servers", {}) and relative not in records:
                raise InstallConflict("Existing unmanaged Codex CodeGraph configuration")
            add_text(relative, '[mcp_servers.codegraph]\ncommand = "codegraph"\nargs = ["serve", "--mcp"]\n')
            # Appending a table must remain valid for quoted/dotted user configs.
            tomllib.loads(changes.get(relative, text))
        if codegraph and "claude" in selected and ".mcp.json" not in records:
            relative = ".mcp.json"
            value = _object(_read(_path(project, relative)), relative)
            had_servers = "mcpServers" in value
            servers = value.setdefault("mcpServers", {})
            if "codegraph" in servers:
                raise InstallConflict("Existing unmanaged Claude CodeGraph configuration")
            entry = dict(MCP, alwaysLoad=True)
            servers["codegraph"] = entry
            changes[relative] = json.dumps(value, indent=2) + "\n"
            records[relative] = {"kind": "json-entry", "sha256": _hash(_json(entry)),
                                 "had_servers": had_servers}

    manifest["engines"] = sorted(engines)
    next_manifest = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if next_manifest != old_manifest and (old_manifest or not uninstall):
        changes[MANIFEST] = next_manifest
    changes = {rel: text for rel, text in changes.items()
               if text != _read(_path(project, rel))}
    if not dry_run:
        # Manifest is last so it never claims an edit before the edit is written.
        # An interrupted multi-file installation is reported as a conflict on
        # retry; we intentionally do not guess ownership of unrecorded blocks.
        for relative, text in changes.items():
            path = _path(project, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".token-kit-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(text)
                if path.exists():
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return list(changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "install", "uninstall"])
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--engine", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--codegraph", action="store_true",
                        help="Configure an already-installed codegraph executable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = configure(args.project, engine=args.engine, codegraph=args.codegraph,
                            uninstall=args.action == "uninstall", dry_run=args.dry_run)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f"token-kit project install: {exc}\n")
    print(json.dumps({"dry_run": args.dry_run, "changed": changed}))
    if args.codegraph and args.action != "uninstall":
        print("CodeGraph must be installed separately. Client trust/MCP approval may be required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
