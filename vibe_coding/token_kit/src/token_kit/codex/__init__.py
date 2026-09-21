"""On-demand codex turns, spawned as a child process and gone when the turn ends.

No serving job, no endpoint file, no port, no ssh.  `dispatch.py` drives
`codex app-server --stdio` (JSON-RPC over the child's own pipes) with the
stdlib alone.

Import the module, not a name from this package::

    from token_kit.codex.dispatch import dispatch, EXIT_BUSY_OR_ABSENT

This file deliberately re-exports NOTHING: an eager `from .dispatch import
dispatch` here both shadows the submodule with the function of the same name
and makes `python -m token_kit.codex.dispatch` emit a runpy double-import
warning.
"""
