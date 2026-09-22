"""Fresh Codex sessions using portable task state supplied by the core.

Strict compaction prevention is deliberately unsupported until verified. The
installed codex-cli 0.153.4 exposes a token threshold, not a documented disable
switch. Raising that threshold cannot guarantee compaction never occurs.
See https://developers.openai.com/codex/config-sample/ .
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .base import AdapterError, LaunchPlan, LaunchRequest


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
    if request.strict_no_compaction:
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
    if not request.prompt.strip() or "\x00" in request.prompt:
        raise AdapterError("Codex prompt must be nonempty and contain no NUL characters")
    argv = [executable, "--cd", str(workspace)]
    if request.model is not None:
        if (not request.model.strip() or "\x00" in request.model
                or request.model.startswith("-")):
            raise AdapterError("Codex model must be a nonempty model name, not an option")
        argv.extend(("--model", request.model))
    # The separator prevents a checkpoint starting with '-' from becoming an
    # option. Passing one argv element preserves newlines and shell metacharacters.
    argv.extend(("--", request.prompt))
    return LaunchPlan(
        engine="codex",
        argv=tuple(argv),
        cwd=workspace,
        env=dict(os.environ if environ is None else environ),
        strict_no_compaction=False,
    )
