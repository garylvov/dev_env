"""token_kit.router -- the agent_trigger_matrix reader and the per-lane call cap.

`cli.py hook` imports exactly one name from here, `handle`, and hands it the
parsed hook event.  Everything else is imported lazily inside it: this package
is on the path of EVERY tool call, so an import it does not need is a cost paid
thousands of times a day.
"""

from __future__ import annotations

__all__ = ["handle", "main"]


def handle(payload: dict) -> int:
    """The hook's entry point.

    The cheapest branch is answered HERE, before a single further import: a
    main-thread tool call that is not a spawn has no opinion to give, and it
    is by far the commonest event there is.  Measured on the login node, the
    difference between answering it here and answering it in hook.py is about
    30 ms on every tool call the operator makes.
    """
    if (isinstance(payload, dict)
            and payload.get("tool_name") != "Agent"
            and not payload.get("agent_id")
            and payload.get("hook_event_name") != "SessionStart"):
        return 0
    from .hook import handle as _handle
    return _handle(payload)


def main(argv: list[str] | None = None) -> int:
    from .hook import main as _main
    return _main(argv)
