"""Fresh Claude launches, without the legacy worker lifecycle.

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
from .simple_guidance import CONCURRENCY, REPO_DOCS, TASK_RULES, load_dispatch_guide


@dataclass(frozen=True)
class Capabilities:
    managed_hooks: bool
    diagnostics: tuple[str, ...]
    names: bool = False  # client accepts --name, so successors keep the task's session name


def capabilities(options: LaunchOptions) -> Capabilities:
    """Bounded local help only: no model requests or configuration writes."""
    executable = options.executable or "claude"
    _argument(executable, "Executable")
    try:
        result = subprocess.run([executable, "--help"], capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Capabilities(False, (f"Rollover capability probe failed: {exc}",))
    if result.returncode or not all(flag in result.stdout for flag in ("--session-id", "--settings")):
        return Capabilities(False, ("Client help does not confirm session identity and settings flags; "
                                    "automatic rollover is unavailable.",))
    return Capabilities(True, (), "--name" in result.stdout)


def _argument(value: str, label: str) -> None:
    if not value or not value.strip() or "\0" in value or value.startswith("-"):
        raise AdapterError(f"{label} must be nonempty and cannot be an option")


def _settings_paths(workspace: Path, env: dict[str, str]) -> list[Path]:
    """Inspect only known configuration locations; never scan session history.

    Detection is deliberately conservative. No settings are removed or rewritten.
    """
    home = Path(env.get("CLAUDE_CONFIG_DIR", str(Path(env.get("HOME", "~")).expanduser() / ".claude")))
    paths = [home / "settings.json", home / "settings.local.json"]
    for directory in (workspace, *workspace.parents):
        paths.extend((directory / ".claude/settings.json", directory / ".claude/settings.local.json"))
    paths.append(Path("/etc/claude-code/managed-settings.json"))
    return list(dict.fromkeys(paths))


def _legacy_conflicts(workspace: Path, env: dict[str, str]) -> list[str]:
    conflicts = []
    for path in _settings_paths(workspace, env):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 2_000_000:
                conflicts.append(f"Cannot inspect oversized settings: {path}")
                continue
            content = json.dumps(json.loads(path.read_text()).get("hooks", {}))
            if re.search(r"(?:token_kit[/\\]+(?:runtime|worker_policy)\.py|token[_-]kit[/\\]+(?:router|respawn)[/\\]+(?:hook|hooks)\.py|token-kit\s+(?:hook|worker-hook)\b)", content):
                conflicts.append(str(path))
        except (OSError, ValueError, AttributeError) as exc:
            conflicts.append(f"Cannot inspect settings {path}: {exc}")
    return conflicts


LEGACY_LAYOUT = "Legacy task.md/in.md, STATE.md/out.md: apply these rules in place.\n"


def _legacy_layout(view: TaskView) -> bool:
    return view.legacy or view.assignment.name in ("task.md", "in.md") or view.state.name == "STATE.md"


def folder_guidance(view: TaskView, repo_notes: str = "") -> str:
    from .simple_task_roots import committable
    commit = " (in the repo: commit it with your work commits)" if committable(view.root.resolve()) else ""
    return (f"Task folder: {view.root.resolve()}{commit}\n"
            f"Input (objective and asks): {view.assignment.resolve()}\n"
            f"Asks (every request, open first; update each turn with a user message): {view.root.resolve() / 'asks.md'}\n"
            f"Output (running state and results): {view.output.resolve()}\n"
            f"Continuation notes: {view.state.resolve()}\n"
            + TASK_RULES + (LEGACY_LAYOUT if _legacy_layout(view) else "")
            + REPO_DOCS + repo_notes + load_dispatch_guide()
            + (f"Optional task preferences: {view.preferences}\n" if view.preferences else "")
            + CONCURRENCY)


def prepare(view: TaskView, options: LaunchOptions, prompt: str | None,
            run_dir: Path, agent: str = "coordinator", greet: bool = False) -> LaunchPlan:
    """Build argv for direct execution (shell=False), never execute the client."""
    workspace = Path(view.workspace or view.root).expanduser().resolve()
    if not workspace.is_dir():
        raise AdapterError(f"Workspace is not a directory: {workspace}")
    headless = options.headless or options.non_interactive
    executable = options.executable or "claude"
    _argument(executable, "Executable")
    for value, label in ((options.model, "Model"), (options.effort, "Effort")):
        if value is not None:
            _argument(value, label)
    if prompt is not None and (not prompt.strip() or "\0" in prompt):
        raise AdapterError("Prompt must be nonempty and contain no NUL bytes")
    if headless and prompt is None:
        raise AdapterError("Headless mode requires a prompt or saved task context")
    # A nested launch must never inherit its parent's control nonce or identity.
    env = {key: value for key, value in os.environ.items() if not key.startswith("TOKEN_KIT_")}
    conflicts = _legacy_conflicts(workspace, env)
    if conflicts:
        raise AdapterError("Legacy or unreadable hook configuration prevents a clean launch: "
                           + "; ".join(conflicts) + ". Inspect Token Kit installation cleanup first; "
                           "unrelated hooks must be preserved.")
    env["TOKEN_KIT_SIMPLE_RUN"] = str(Path(run_dir).resolve())
    env["TOKEN_KIT_TASK"] = str(view.root.resolve())
    env["TOKEN_KIT_AGENT"] = agent
    from .simple_codegraph import attach, ENV as codegraph_env
    from .simple_repo_docs import kickoff_note
    codegraph = attach(workspace, options.codegraph)
    from .simple_records import experiments_note
    guidance = folder_guidance(view, kickoff_note(workspace) + experiments_note(view.root) + codegraph.guidance)
    env["TOKEN_KIT_SIMPLE_GUIDANCE"] = guidance
    env.update(codegraph_env)  # telemetry off for codegraph runs the session starts itself
    # Hooks record asks, workers and experiments even when rollover is off.
    cap = capabilities(options)
    from .simple_codegraph import BUILD_GUIDANCE
    graph = ("off" if not codegraph.argv else "no index yet (Claude builds it once it knows the repo)"
             if codegraph.guidance == BUILD_GUIDANCE else "main checkout's index"
             if "answers from" in codegraph.guidance else "on")
    rollover = "off" if options.rollover in (None, 0, "off") else f"at {options.rollover}"
    greet = greet and not headless
    banner = (f"{view.title}: folder {view.root.resolve()}; rollover {rollover}; CodeGraph {graph}. "
              "Token Kit guidance is in its system prompt; "
              + (("Claude first summarizes the setup, then " + ("waits for your first message." if prompt is None
                  else "starts on your prompt.")) if greet else "without --prompt it waits for your first message."))
    if greet:
        from .simple_guidance import greeting
        prompt = greeting(view.root.resolve(), rollover, graph, then=prompt)
    env["TOKEN_KIT_SIMPLE_CAPABILITIES"] = json.dumps({
        "managed_hooks": cap.managed_hooks,
        "diagnostics": (banner,) + cap.diagnostics + codegraph.diagnostics})
    argv = [executable, "--add-dir", str(view.root.resolve()), "--append-system-prompt", guidance]
    if headless:
        argv.append("--print")
    if options.yolo:
        argv.append("--dangerously-skip-permissions")
    if options.effort is not None:
        argv.extend(("--effort", options.effort))
    if cap.managed_hooks:
        from .simple_runtime import hooks
        session_id = str(uuid.uuid4())
        env["TOKEN_KIT_SIMPLE_SESSION_ID"] = session_id
        argv.extend(("--session-id", session_id, "--settings", json.dumps({"hooks": hooks(), "idleCompaction": False})))
    if options.model is not None:
        argv.extend(("--model", options.model))
    if cap.names and view.title.strip() and not view.title.startswith("-"):
        argv.extend(("--name", view.title))
    argv.extend(codegraph.argv)
    if prompt is not None:
        argv.extend(("--", prompt))
    return LaunchPlan("claude", tuple(argv), workspace, env, False)


