"""On-demand codex turns, spawned as a child process and gone when the turn ends.

No serving job, no endpoint file, no port, no ssh.  `dispatch` drives
`codex app-server --stdio` (JSON-RPC over the child's own pipes) with the
stdlib alone.
"""

from .dispatch import (  # noqa: F401
    EXIT_BUSY_OR_ABSENT,
    EXIT_CODEX_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    MIN_TOKIO_WORKER_THREADS,
    CodexUnavailable,
    DispatchResult,
    dispatch,
    main,
)
