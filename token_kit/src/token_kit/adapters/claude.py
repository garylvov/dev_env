"""Build fresh interactive Claude Code launches without executing them.

Compaction controls follow https://code.claude.com/docs/en/env-vars and
https://code.claude.com/docs/en/cli-reference. The inline settings protect the
requested policy from user/project settings overriding the inherited environment.
Managed settings still take precedence. A strict plan describes requested
configuration, not certification of an installed client's behavior.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from .base import AdapterError, LaunchPlan, LaunchRequest, default_effort


def prepare_launch(
    request: LaunchRequest,
    *,
    executable: str = "claude",
    environ: Mapping[str, str] | None = None,
) -> LaunchPlan:
    """Prepare a fresh session; checking binary availability is the caller's job.

    The caller must execute ``argv`` directly with ``shell=False`` and the plan's
    cwd/environment. Permissions are inherited unless yolo is explicitly requested.
    """
    workspace = Path(request.workspace).resolve()
    if not workspace.is_dir():
        raise AdapterError(f"Workspace is not a directory: {workspace}")
    if not executable or "\0" in executable or executable.startswith("-"):
        raise AdapterError("Claude executable must be a nonempty command or path")
    if not request.prompt.strip() or "\0" in request.prompt:
        raise AdapterError("Prompt must be nonempty and contain no NUL bytes")
    if request.model is not None and (
        not request.model.strip() or "\0" in request.model or request.model.startswith("-")
    ):
        raise AdapterError("Model must be a nonempty model name, not an option")

    env = dict(os.environ if environ is None else environ)
    argv = [executable, "--effort", default_effort(request.model)]
    if request.yolo:
        argv.append("--dangerously-skip-permissions")
    settings = {}
    if request.strict_no_compaction:
        env["DISABLE_COMPACT"] = "1"
        settings["env"] = {"DISABLE_COMPACT": "1"}
    if request.worker_task is not None:
        from ..worker_policy import hook_command
        argv.extend(("--add-dir", request.worker_task))
        settings["hooks"] = {"PreToolUse": [{"matcher": "^(Agent|Task)$", "hooks": [
            {"type": "command", "command": hook_command(request.worker_task), "timeout": 5}
        ]}]}
    if request.managed_hooks:
        from ..runtime import hooks
        settings.setdefault("hooks", {}).update(hooks())
    if settings:
        argv.extend(("--settings", json.dumps(settings)))
    if request.model is not None:
        argv.extend(("--model", request.model))
    # The separator also keeps a prompt beginning with '-' from becoming flags.
    argv.extend(("--", request.prompt))
    return LaunchPlan(
        engine="claude",
        argv=tuple(argv),
        cwd=workspace,
        env=env,
        strict_no_compaction=request.strict_no_compaction,
    )
