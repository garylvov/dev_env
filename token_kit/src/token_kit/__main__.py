"""The small public launcher; old installed hook utilities retain explicit entrypoints."""
from __future__ import annotations
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "legacy":
        from .cli import legacy_main
        return legacy_main(args[1:])
    if args and args[0] in ("hook", "config", "census", "probe", "prompts"):
        # Captured global hook shims remain resolvable until explicit cleanup.
        from .cli import legacy_main
        return legacy_main(args)
    if args and args[0] in ("install", "init", "uninstall"):
        import argparse
        from pathlib import Path
        from .simple_install import preview
        import json
        parser = argparse.ArgumentParser(prog="token-kit " + args[0])
        parser.add_argument("--project", type=Path, default=Path.cwd())
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--engine", choices=("both", "claude", "codex"), default="both")
        parsed = parser.parse_args(args[1:])
        if not parsed.dry_run:
            print("Token Kit needs no project install. Existing configuration cleanup is release-specific; use --dry-run to inspect owned guidance without changing it.", file=sys.stderr)
            return 2
        print(json.dumps(preview(parsed.project).as_dict(), indent=2))
        return 0
    from .simple_cli import main as launch
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
