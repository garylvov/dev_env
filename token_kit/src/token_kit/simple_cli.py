"""Start/resume ordinary task folders; process details belong to the launcher."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import sys

from .rollover import parse_limit
from .simple_types import LaunchOptions, TaskView

HELP = """Token Kit — folders and automatic session rollover.

  token-kit                         Choose a task or create one
  tk NAME [WORDS...]                 Continue the task with that name, or start it if new (no quotes needed)
  token-kit run \"Title\"             Start a named session (short: tk r, c, s, p, l)
  token-kit continue NAME_OR_PATH    Resume a task
  token-kit status NAME_OR_PATH      Show saved state paths
  token-kit codegraph-hooks MAIN     Print git hook lines that keep CodeGraph indexes fresh

Inside git, tasks live in <repo>/agent_tasks/; continue searches this repo, its
worktrees, then ~/.config/token_kit (--root DIR searches only DIR).
New tasks open with a short setup overview; --prompt TEXT then starts work right after it.
Rollover defaults to 80%, unlimited. --no-rollover disables automatic restarts.
Use COMMAND --help for launch options. Files can be edited normally.
"""



# [1m] gives the 1M window; plain "opus" compacts near 200K.
OPERATOR_DEFAULTS = {"model": "opus[1m]", "codegraph": True}

def default_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "token_kit"


def _percent(value: str) -> str:
    result = parse_limit(value if value.endswith("%") else value + "%")
    if not isinstance(result, str):
        raise argparse.ArgumentTypeError("Use a percentage from 1 to 99")
    return result


def _window(value: str) -> int:
    result = parse_limit(value)
    if not isinstance(result, int):
        raise argparse.ArgumentTypeError("Use a token count, not a percentage")
    return result


def _budget(value: str) -> int | None:
    if value == "unlimited":
        return None
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("Use a nonnegative number or unlimited")
    return int(value)


def _launch_options(parser):
    # SUPPRESS distinguishes an absent flag from explicit off/zero/unlimited.
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--effort", default=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--agent", default="coordinator", help=argparse.SUPPRESS)
    parser.add_argument("--yolo", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
    threshold = parser.add_mutually_exclusive_group()
    threshold.add_argument("--rollover-perc", dest="rollover", type=_percent, default=argparse.SUPPRESS)
    threshold.add_argument("--rollover-tokens", "--rollover-at", dest="rollover", type=parse_limit,
                           default=argparse.SUPPRESS)
    threshold.add_argument("--no-rollover", dest="rollover", action="store_const", const=None,
                           default=argparse.SUPPRESS)
    parser.add_argument("--max-rollovers", type=_budget, default=argparse.SUPPRESS)
    parser.add_argument("--context-window", type=_window, default=argparse.SUPPRESS)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
    parser.add_argument("--codegraph", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="Attach CodeGraph MCP to this session (default: when an index exists)")
    parser.add_argument("--dry-run", "--print", dest="preview", action="store_true")
    parser.add_argument("--prompt")
    parser.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)


def parser_for(command: str):
    parser = argparse.ArgumentParser(prog="token-kit " + command)
    if command == "run":
        parser.add_argument("title", nargs="*")  # unquoted words form one title
        parser.add_argument("--task", type=Path)
    elif command in ("continue", "pick", "find", "list", "status"):
        parser.add_argument("words", nargs="*")
        parser.add_argument("--task", type=Path)
        parser.add_argument("--select", type=int)
        parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--root", type=Path, help="Task folder root (default: <repo>/agent_tasks inside git)")
    if command in ("run", "continue", "pick"):
        _launch_options(parser)
    elif command == "status":
        parser.add_argument("--agent", default="coordinator")
        parser.add_argument("--full", action="store_true")
    return parser


def resolve_options(args, saved: dict | None = None) -> LaunchOptions:
    values = dict(saved or {})
    if "rollover" not in values and values.get("rollover_tokens") is not None:
        values["rollover"] = values["rollover_tokens"]
    if values.get("engine", "claude") != "claude":
        # Codex-era choices cannot be silently translated to Claude.
        for key in ("model", "effort", "context_window", "sandbox"):
            values.pop(key, None)
        values["yolo"] = False
        print("Token Kit: this task was saved for Codex and now continues on Claude "
              "(model, effort and --yolo reset); GPT models are Shunt subagents.", file=sys.stderr)
    supplied = vars(args)
    if supplied.get("rollover", "absent") is None and supplied.get("max_rollovers") not in (None, 0):
        raise ValueError("--no-rollover cannot be combined with a positive restart limit")
    fields = LaunchOptions.__dataclass_fields__
    result = {key: value for key, value in values.items() if key in fields and key != "executable"}
    result.update({key: value for key, value in supplied.items() if key in fields})
    if "rollover" in supplied and supplied["rollover"] is not None and values.get("rollover") is None:
        # An explicit threshold re-enables rollover, unless the user separately set zero.
        if "max_rollovers" not in supplied and values.get("max_rollovers") == 0:
            result["max_rollovers"] = None
    if "headless" in supplied:
        result["non_interactive"] = False
    # Operator defaults for tasks that never chose: Opus main session, CodeGraph on.
    result.setdefault("model", OPERATOR_DEFAULTS["model"])
    result.setdefault("codegraph", OPERATOR_DEFAULTS["codegraph"])
    options = LaunchOptions(**result)
    if type(options.headless) is not bool or type(options.non_interactive) is not bool:
        raise ValueError("Saved headless setting is invalid; select --headless or --no-headless")
    if options.codegraph is not None and type(options.codegraph) is not bool:
        raise ValueError("Saved CodeGraph setting is invalid; select --codegraph or --no-codegraph")
    if type(options.yolo) is not bool:
        raise ValueError("Saved permission setting is invalid; select --yolo or --no-yolo")
    if options.max_rollovers is not None and (type(options.max_rollovers) is not int or options.max_rollovers < 0):
        raise ValueError("Saved restart limit is invalid; select --max-rollovers")
    if options.rollover is not None:
        options = replace(options, rollover=parse_limit(options.rollover))
    if options.context_window is not None:
        options = replace(options, context_window=_window(str(options.context_window)))
    return options


def search_roots(args) -> list[Path]:
    if args.root is not None:
        return [args.root]
    from .simple_task_roots import search_roots as roots
    return roots(Path.cwd(), default_root())


def task_candidates(roots: list[Path], words=(), limit=20) -> list[TaskView]:
    query = [word.casefold() for phrase in words for word in phrase.split()]
    rows = [row for root in roots for row in _scan(root, query)]
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    # Deduplicate before the limit so a copy in the current checkout is never cut off.
    return _unique([row[2] for row in rows], roots)[:limit]


def _scan(root: Path, query: list[str]) -> list:
    from .task_files import load_task
    if not root.is_dir():
        return []
    rows = []
    # Explicit roots only, no recursive filesystem discovery.
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 10000:
                raise ValueError("Task root exceeds discovery limit; use an exact task path")
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if not any((path / name).exists() for name in ("task.md", "task.json", "STATE.md", "in.md", f"{path.name}_in.md", f"{path.name}_out.md")):
                continue
            try:
                view = load_task(path)
                text = f"{view.title} {path.name}".casefold()
                tokens = re.findall(r"\w+", text)
                scores = [1.0 if word in text else max(
                    (SequenceMatcher(None, word, token).ratio() for token in tokens), default=0.0)
                    for word in query]
                if scores and min(scores) < .72:
                    continue
                rows.append((sum(scores) / len(scores) if scores else 1, path.stat().st_mtime, view))
            except (OSError, ValueError):
                continue
    return rows


def _unique(rows: list[TaskView], roots: list[Path]) -> list[TaskView]:
    """One row per task name: the current checkout's copy wins, otherwise the first copy stands for all."""
    first = roots[0].resolve()
    kept, seen = [], set()
    for view in rows:
        if view.root.name in seen:
            continue
        seen.add(view.root.name)
        copies = [other for other in rows if other.root.name == view.root.name]
        local = [other for other in copies if other.root.parent.resolve() == first]
        kept.append(local[0] if local else view)
    return kept


def _unambiguous(path: Path, roots: list[Path]) -> Path:
    """A chosen name found in several places outside the current checkout needs --root."""
    if len(roots) < 2 or path.parent.resolve() == roots[0].resolve():
        return path
    copies = [root for root in roots if (root / path.name).is_dir()]
    if len(copies) > 1:
        raise ValueError(f"Task {path.name} exists in several places; choose one with --root DIR:\n"
                         + "\n".join(f"  {root}" for root in copies))
    return path


def _show_candidates(rows):
    for index, view in enumerate(rows, 1):
        stamp = datetime.fromtimestamp(view.root.stat().st_mtime).strftime("%b %d %H:%M")
        print(f"{index}. {view.title} — {stamp}\n   {view.root}")


def select_task(args) -> Path:
    if args.task is not None:
        return args.task
    words = args.words
    candidate = Path(words[0]).expanduser() if len(words) == 1 else None
    # A bare name is a search query, even when a project directory shares it.
    explicit_path = len(words) == 1 and ("/" in words[0] or words[0] in (".", "..") or words[0].startswith("~"))
    if explicit_path:
        if not candidate.is_dir():
            raise ValueError(f"Task folder does not exist: {candidate}")
        return candidate
    roots = search_roots(args)
    rows = task_candidates(roots, words, max(1, args.limit))
    if not rows and candidate is not None and candidate.is_dir():
        return candidate
    if not rows:
        raise ValueError("No matching task. Use token-kit run \"Title\" to create one.")
    if args.select is not None:
        if not 1 <= args.select <= len(rows):
            raise ValueError("Selection is outside the displayed candidates")
        return _unambiguous(rows[args.select - 1].root, roots)
    if len(rows) == 1:
        return _unambiguous(rows[0].root, roots)
    _show_candidates(rows)
    if not sys.stdin.isatty():
        raise ValueError("Several tasks match; use an exact path or --select N")
    choice = input("Task number: ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(rows):
        raise ValueError("No task selected")
    return _unambiguous(rows[int(choice) - 1].root, roots)


def _preview(view, options, prompt):
    from .simple_recovery import native_recipe
    print(json.dumps({"task": str(view.root), "workspace": str(view.workspace) if view.workspace else None,
                      "state": str(view.state), "assignment": str(view.assignment),
                      "settings": asdict(options), "native_recovery": native_recipe(view, options),
                      "note": "Preview only; no files written or clients launched."}, indent=2))


def _launch(args, path: Path | None, *, create=False):
    from .task_files import create_task, load_task, load_settings, recovery_input
    workspace = args.workspace.expanduser().resolve() if args.workspace else None
    if create:
        if not args.title or not args.title.strip():
            raise ValueError("Provide a title for the new task")
        workspace = workspace or Path.cwd()
        options = resolve_options(args)
        from .simple_task_roots import creation_root, protect
        root = args.root if args.root is not None else creation_root(workspace, default_root())
        if args.preview:
            print(json.dumps({"create": args.title, "root": str(root), "workspace": str(workspace),
                              "settings": asdict(options), "prompt": args.prompt}, indent=2))
            return 0
        view = create_task(root, args.title, workspace, assignment=args.prompt)
        for note in protect(view.root):
            print("Token Kit: " + note, file=sys.stderr)
    else:
        view = load_task(path, workspace=workspace, agent=args.agent)
        options = resolve_options(args, load_settings(view))
    for warning in view.diagnostics:
        print("Token Kit: " + warning, file=sys.stderr)
    if view.workspace is None or not view.workspace.is_dir():
        raise ValueError("Workspace is unavailable; supply --workspace PATH")
    if args.prompt:
        prompt = args.prompt
    elif create or not args.preview:
        prompt = None
    else:
        prompt = recovery_input(view, agent=args.agent, paths_only=True).text or None
    if args.preview:
        _preview(view, options, prompt)
        return 0
    from .simple_task_roots import exclude_runtime
    exclude_runtime(view.root)
    from .simple_runner import run_session
    return run_session(view, options, prompt=prompt, agent=args.agent, resume=not create)


def _picker():
    from .simple_task_roots import search_roots as roots
    rows = task_candidates(roots(Path.cwd(), default_root()))
    _show_candidates(rows)
    if not sys.stdin.isatty():
        print(HELP)
        return 0
    answer = input("Task number, or n for a new task: ").strip()
    if answer.casefold() == "n":
        title = input("Title: ").strip()
        return main(["run", title])
    if answer.isdecimal() and 1 <= int(answer) <= len(rows):
        return main(["continue", str(rows[int(answer) - 1].root)])
    raise ValueError("No task selected")


# Removed commands must not silently become new task names.
REMOVED = ("send", "checkpoint", "done", "reopen", "retitle", "resume", "launch", "new")
ALIASES = {"r": "run", "c": "continue", "s": "status", "p": "pick", "l": "list"}
COMMANDS = ("run", "continue", "pick", "status", "list", "find", "codegraph-hooks",
            "worker", "close-run", "agent", "migrate", "install", "init", "uninstall")


def _open_or_create(args: list[str]) -> int:
    """`tk NAME WORDS... [options]`: continue the task whose folder is named NAME, else start one titled NAME."""
    # Leading words up to the first option form the name, so quotes are optional.
    count = next((i for i, word in enumerate(args) if word.startswith("-")), len(args))
    name, rest = " ".join(args[:count]), args[count:]
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:64]
    probe = parser_for("continue").parse_args([name, *rest])
    roots = search_roots(probe)
    matches = [row for row in task_candidates(roots, [name], max(1, probe.limit))
               if row.root.name == slug or re.fullmatch(re.escape(slug) + r"_\d{8}_\d{4}_[0-9a-f]{8}", row.root.name)]
    if not matches:
        print(f"Token Kit: starting new task {name!r}", file=sys.stderr)
        parsed = parser_for("run").parse_args([name, *rest])
        parsed.title = " ".join(parsed.title) or None
        return _launch(parsed, None, create=True)
    if len(matches) > 1:
        _show_candidates(matches)
        raise ValueError(f"Several tasks are named {name!r}; use tk c {name} --select N")
    print(f"Token Kit: continuing {matches[0].root}", file=sys.stderr)
    return _launch(probe, _unambiguous(matches[0].root, roots))


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(HELP)
        return 0
    if args and args[0] in ("task", "workflow"):
        return main(args[1:])
    try:
        if not args:
            return _picker()
        command = ALIASES.get(args[0], args[0])
        if command in REMOVED:
            raise ValueError(f"Unknown command: {command}. Use token-kit --help.")
        if command not in COMMANDS and not command.startswith("-"):
            return _open_or_create(args)
        args.pop(0)
        if command in ("worker", "close-run", "agent", "migrate", "install", "init", "uninstall"):
            raise ValueError(f"'{command}' is no longer required. Edit task/agent files directly and use continue to resume. No records changed.")
        if command == "codegraph-hooks":
            from .simple_codegraph import hook_lines
            if len(args) != 1 or args[0].startswith("-"):
                raise ValueError("Usage: token-kit codegraph-hooks MAIN_CHECKOUT")
            print(hook_lines(Path(args[0]).expanduser().resolve()), end="")
            return 0
        if command not in COMMANDS:
            raise ValueError(f"Unknown command: {command}. Use token-kit --help.")
        parsed = parser_for(command).parse_args(args)
        if command == "run":
            parsed.title = " ".join(parsed.title) or None
            if parsed.task and parsed.title:
                raise ValueError("Use either a new title or --task PATH")
            return _launch(parsed, parsed.task, create=parsed.task is None)
        if command in ("continue", "pick"):
            return _launch(parsed, select_task(parsed))
        if command in ("list", "find"):
            _show_candidates(task_candidates(search_roots(parsed), parsed.words, max(1, parsed.limit)))
            return 0
        from .task_files import load_task, recovery_input
        view = load_task(select_task(parsed), agent=parsed.agent)
        print(f"{view.title}\nWorkspace: {view.workspace or 'unknown'}\nState: {view.state}\nAssignment: {view.assignment}")
        if parsed.full:
            print(recovery_input(view, agent=parsed.agent).text)
        return 0
    except (OSError, ValueError) as exc:
        print("Token Kit: " + str(exc), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
