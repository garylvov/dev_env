"""Fresh client launches, without the legacy worker lifecycle.

A help probe establishes syntax only. Hook trust and functioning telemetry still
need the runner's exact-parent handshake; these adapters do not certify either.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from .adapters.base import AdapterError, LaunchPlan
from .simple_types import LaunchOptions, TaskView


@dataclass(frozen=True)
class Capabilities:
    managed_hooks: bool
    diagnostics: tuple[str, ...]
    hook_trust: str = "unverified"


def capabilities(options: LaunchOptions) -> Capabilities:
    """Bounded local help only: no model requests or configuration writes."""
    if options.engine not in ("claude", "codex"):
        raise AdapterError(f"Unknown engine: {options.engine}")
    executable = options.executable or options.engine
    _argument(executable, "Executable")
    try:
        result = subprocess.run([executable, "--help"], capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Capabilities(False, (f"Rollover capability probe failed: {exc}",))
    required = (("--session-id", "--settings") if options.engine == "claude"
                else ("--no-daemon", "--config", "--enable"))
    if result.returncode or not all(flag in result.stdout for flag in required):
        return Capabilities(False, ("Client help does not confirm session identity and settings flags; "
                                    "automatic rollover is unavailable.",))
    return Capabilities(True, ("Rollover hooks are candidates; hook support, trust, parent identity and telemetry remain "
                               "unverified until the expected parent session reports them.",))


def _argument(value: str, label: str) -> None:
    if not value or not value.strip() or "\0" in value or value.startswith("-"):
        raise AdapterError(f"{label} must be nonempty and cannot be an option")


def _legacy_conflicts(workspace: Path, engine: str, env: dict[str, str]) -> list[str]:
    """Inspect only known configuration locations; never scan session history.

    Detection is deliberately conservative. No settings are removed or rewritten.
    """
    if engine == "claude":
        home = Path(env.get("CLAUDE_CONFIG_DIR", str(Path(env.get("HOME", "~")).expanduser() / ".claude")))
        paths = [home / "settings.json", home / "settings.local.json"]
        for directory in (workspace, *workspace.parents):
            paths.extend((directory / ".claude/settings.json", directory / ".claude/settings.local.json"))
        paths.append(Path("/etc/claude-code/managed-settings.json"))
    else:
        home = Path(env.get("CODEX_HOME", str(Path(env.get("HOME", "~")).expanduser() / ".codex")))
        paths = [home / "config.toml"]
        paths.extend(directory / ".codex/config.toml" for directory in (workspace, *workspace.parents))
    conflicts = []
    for path in dict.fromkeys(paths):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 2_000_000:
                conflicts.append(f"Cannot inspect oversized settings: {path}")
                continue
            content = path.read_text()
            if path.suffix == ".json":
                content = json.dumps(json.loads(content).get("hooks", {}))
            else:
                import tomllib
                config = tomllib.loads(content)
                content = json.dumps(config.get("hooks", {}))
            if re.search(r"token[_-]kit|(?:router|respawn)[/\\]+(?:hook|hooks)\.py", content):
                conflicts.append(str(path))
        except (OSError, ValueError, AttributeError) as exc:
            conflicts.append(f"Cannot inspect settings {path}: {exc}")
    return conflicts


def prepare(view: TaskView, options: LaunchOptions, prompt: str | None,
            run_dir: Path, agent: str = "coordinator") -> LaunchPlan:
    """Build argv for direct execution (shell=False), never execute the client."""
    workspace = Path(view.workspace or view.root).expanduser().resolve()
    if not workspace.is_dir():
        raise AdapterError(f"Workspace is not a directory: {workspace}")
    if options.engine not in ("claude", "codex"):
        raise AdapterError(f"Unknown engine: {options.engine}")
    if options.engine == "codex" and options.non_interactive:
        raise AdapterError("Codex print mode is not supported by the simplified launcher")
    executable = options.executable or options.engine
    _argument(executable, "Executable")
    for value, label in ((options.model, "Model"), (options.effort, "Effort")):
        if value is not None:
            _argument(value, label)
    if prompt is not None and (not prompt.strip() or "\0" in prompt):
        raise AdapterError("Prompt must be nonempty and contain no NUL bytes")
    if options.non_interactive and prompt is None:
        raise AdapterError("Print mode requires a prompt")
    # A nested launch must never inherit its parent's control nonce or identity.
    env = {key: value for key, value in os.environ.items() if not key.startswith("TOKEN_KIT_")}
    conflicts = _legacy_conflicts(workspace, options.engine, env)
    if conflicts:
        raise AdapterError("Legacy or unreadable hook configuration prevents a clean launch: "
                           + "; ".join(conflicts) + ". Inspect Token Kit installation cleanup first; "
                           "unrelated hooks must be preserved.")
    env["TOKEN_KIT_SIMPLE_RUN"] = str(Path(run_dir).resolve())
    env["TOKEN_KIT_TASK"] = str(view.root.resolve())
    env["TOKEN_KIT_AGENT"] = agent
    enabled = options.rollover not in (None, 0, "off")
    cap = capabilities(options) if enabled else Capabilities(False, ())
    env["TOKEN_KIT_SIMPLE_CAPABILITIES"] = json.dumps({
        "managed_hooks": cap.managed_hooks, "hook_trust": cap.hook_trust,
        "diagnostics": cap.diagnostics})
    argv = [executable]
    if options.engine == "claude":
        if options.non_interactive:
            argv.append("--print")
        if options.yolo:
            argv.append("--dangerously-skip-permissions")
        if options.effort is not None:
            argv.extend(("--effort", options.effort))
        if cap.managed_hooks:
            from .simple_runtime import hooks
            session_id = str(uuid.uuid4())
            env["TOKEN_KIT_SIMPLE_SESSION_ID"] = session_id
            argv.extend(("--session-id", session_id, "--settings", json.dumps({"hooks": hooks()})))
    else:
        argv.extend(("--cd", str(workspace)))
        if cap.managed_hooks:
            from .simple_runtime import codex_config
            argv.append("--no-daemon")
            argv.extend(codex_config())
        if options.yolo:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        if options.effort is not None:
            argv.extend(("-c", "model_reasoning_effort=" + json.dumps(options.effort)))
    if options.model is not None:
        argv.extend(("--model", options.model))
    if prompt is not None:
        argv.extend(("--", prompt))
    return LaunchPlan(options.engine, tuple(argv), workspace, env, False)


def codex_root_session(payload: dict) -> str | None:
    """Metadata evidence only; caller MUST also prove managed process ancestry.

    Native children carry a structured source, not the root CLI source. An
    independently launched nested CLI can still have source=cli, hence this
    function alone never authorizes a hook or establishes process ownership.
    """
    session = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if (not isinstance(session, str) or not session or not isinstance(transcript, str)
            or any(payload.get(key) for key in ("agent_id", "agent_type",
                                                 "parent_thread_id", "parent_session_id"))):
        return None
    path = Path(transcript)
    if not path.is_absolute():
        return None
    try:
        with path.open(encoding="utf-8") as stream:
            line = stream.readline(262145)
        if len(line) > 262144:
            return None
        row = json.loads(line)
        meta = row.get("payload", {})
        if (row.get("type") != "session_meta" or meta.get("source") != "cli"
                or meta.get("id") != session
                or any(meta.get(key) for key in ("parent_thread_id", "parent_session_id",
                                                 "forked_from_id", "agent_path"))):
            return None
        if meta.get("session_id") not in (None, session):
            return None
        return session
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def codex_usage(transcript: str | Path) -> tuple[int | None, int | None]:
    """Read the last bounded token_count sample, never cumulative usage totals."""
    try:
        with Path(transcript).open('rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 1_048_576)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read(1_048_576).splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = row.get('payload', {})
            if row.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            info = payload.get('info') or {}
            used = (info.get('last_token_usage') or {}).get('total_tokens')
            window = info.get('model_context_window')
            return (used if type(used) is int and used >= 0 else None,
                    window if type(window) is int and window > 0 else None)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None, None


def codex_usage_sample(transcript: Path) -> dict:
    used, window = codex_usage(transcript)
    return {"context_tokens": used, "context_window": window}
