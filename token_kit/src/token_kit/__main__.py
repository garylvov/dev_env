"""One Token Kit command surface for both coding clients."""
from __future__ import annotations

import sys

HELP = """usage: token-kit COMMAND [OPTIONS]

Shared tasks and checkpoints for Claude Code and Codex.

Project setup:
  install, uninstall       Project instructions and optional CodeGraph configuration

Work:
  run                     Open an idle named session; --prompt starts work, --task resumes
  new, list, find          Create and locate tasks
  pick                    Fuzzy-select and resume a task (--print for command only)
  agent                   Add a logical agent with an assignment
  worker                  Reserve, bind, hand off and reconcile durable worker attempts
  checkpoint, resume      Commit state or inspect recovery information
  launch                  Start a fresh client session (--engine claude|codex)
  send, status            Queue messages or inspect tasks and runs
  ledger                  Show reported token usage by agent, run and model
  hooks                   Open Codex's lifecycle-hook review (no task launch)
  done, reopen, retitle    Update task status or display title
  close-run               Reconcile an interrupted execution
  migrate                 Import an old task folder without changing the original

Compatibility:
  task, workflow          Aliases for this same task interface
  legacy                  Explicit access to the old global hook installer and task tools

Use token-kit COMMAND --help for command options.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(HELP)
        return 0
    command = args[0]
    if command in ("task", "workflow"):
        return main(args[1:])
    if command in ("install", "init", "uninstall"):
        from .project_install import main as install
        return install(args)
    if command == "legacy":
        from .cli import legacy_main
        return legacy_main(args[1:])
    if command in ("hook", "config", "census", "probe", "prompts"):
        # Existing installed utility/hook shims still call these paths.
        from .cli import legacy_main
        return legacy_main(args)
    from .workflow import main as workflow
    return workflow(args)


if __name__ == "__main__":
    raise SystemExit(main())
