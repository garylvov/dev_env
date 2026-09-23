"""Fresh Codex sessions using portable task state supplied by the core.

Standalone strict launches are unsupported. Managed rollover installs the
documented PreCompact veto and requires a lifecycle-hook startup handshake.
See https://learn.chatgpt.com/docs/hooks#precompact . No runtime certification
is implied; a disabled/bypassed hook cannot provide a compaction guarantee.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .base import AdapterError, LaunchPlan, LaunchRequest, default_effort


def prepare_launch(
    request: LaunchRequest,
    *,
    executable: str = "codex",
    environ: Mapping[str, str] | None = None,
) -> LaunchPlan:
    """Build an interactive argv without starting Codex or changing its config.

    The caller owns any node-local CODEX_HOME preparation and passes the result
    through ``environ``. Existing authentication and permission settings remain
    the client's responsibility. No native thread history is resumed.
    """
    if request.strict_no_compaction and not request.managed_hooks:
        raise AdapterError(
            "Strict no-compaction is not verified for Codex; refusing to launch. "
            "Codex 0.153.4 exposes an automatic-compaction token threshold, "
            "but no verified disable-all-compaction control. Explicitly allow "
            "compaction to launch a fresh Codex session from portable state."
        )
    if not executable or "\x00" in executable or executable.startswith("-"):
        raise AdapterError("Codex executable must be a nonempty path or command name")
    workspace = request.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise AdapterError(f"Codex workspace is not a directory: {workspace}")
    if request.prompt is not None and (not request.prompt.strip() or "\x00" in request.prompt):
        raise AdapterError("Codex prompt must be nonempty and contain no NUL characters")
    argv = [executable, "--cd", str(workspace)]
    if request.yolo:
        argv.append("--yolo")
    if request.model is not None:
        if (not request.model.strip() or "\x00" in request.model
                or request.model.startswith("-")):
            raise AdapterError("Codex model must be a nonempty model name, not an option")
        argv.extend(("--model", request.model))
    # The separator prevents a checkpoint starting with '-' from becoming an
    # option. Passing one argv element preserves newlines and shell metacharacters.
    effort = default_effort(request.model)
    argv.extend(("-c", f'model_reasoning_effort="{effort}"',
                 "-c", f'plan_mode_reasoning_effort="{effort}"'))
    if request.managed_hooks:
        from ..runtime import codex_config
        argv.extend(codex_config())
    if request.worker_task is not None:
        argv.extend(("--add-dir", request.worker_task))
    if request.prompt is not None:
        argv.extend(("--", request.prompt))
    return LaunchPlan(
        engine="codex",
        argv=tuple(argv),
        cwd=workspace,
        env=dict(os.environ if environ is None else environ),
        strict_no_compaction=request.strict_no_compaction,
    )
