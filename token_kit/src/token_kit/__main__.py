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
    from .simple_cli import main as launch
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
