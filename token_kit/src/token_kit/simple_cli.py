"""Start/resume ordinary task folders; process details belong to the launcher."""
from __future__ import annotations

import argparse
import fcntl
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
  token-kit run \"Title\"             Start a named session
  token-kit continue NAME_OR_PATH    Resume a task
  token-kit status PATH              Show saved state paths

Sessions start idle; --prompt TEXT starts work immediately.
Rollover defaults to 80%, unlimited. --no-rollover disables automatic restarts.
Use COMMAND --help for launch options. Files can be edited normally.
"""


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
    parser.add_argument("--engine", choices=("claude", "codex"), default=argparse.SUPPRESS)
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
    parser.add_argument("--dry-run", "--print", dest="preview", action="store_true")
    parser.add_argument("--prompt")


def parser_for(command: str):
    parser = argparse.ArgumentParser(prog="token-kit " + command)
    if command in ("run", "new"):
        parser.add_argument("title", nargs="?")
        parser.add_argument("--task", type=Path)
    elif command in ("continue", "pick", "find", "list"):
        parser.add_argument("words", nargs="*")
        parser.add_argument("--task", type=Path)
        parser.add_argument("--select", type=int)
        parser.add_argument("--limit", type=int, default=20)
    else:
        parser.add_argument("task", type=Path)
    parser.add_argument("--root", type=Path, default=default_root())
    if command in ("run", "new", "continue", "pick", "launch"):
        _launch_options(parser)
    elif command in ("status", "resume"):
        parser.add_argument("--agent", default="coordinator")
        parser.add_argument("--full", action="store_true")
    elif command == "send":
        parser.add_argument("text")
        parser.add_argument("--agent", default="coordinator")
    elif command == "retitle":
        parser.add_argument("title")
    elif command == "checkpoint":
        parser.add_argument("--agent", default="coordinator")
    return parser


def resolve_options(args, saved: dict | None = None) -> LaunchOptions:
    values = dict(saved or {})
    if "rollover" not in values and values.get("rollover_tokens") is not None:
        values["rollover"] = values["rollover_tokens"]
    if "engine" in vars(args) and values.get("engine") != args.engine:
        # Provider-specific choices cannot be silently translated.
        values.pop("model", None)
        values.pop("effort", None)
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
    options = LaunchOptions(**result)
    if options.engine not in ("claude", "codex"):
        raise ValueError("Saved engine is invalid; select --engine claude or codex")
    if type(options.yolo) is not bool:
        raise ValueError("Saved permission setting is invalid; select --yolo or --no-yolo")
    if options.max_rollovers is not None and (type(options.max_rollovers) is not int or options.max_rollovers < 0):
        raise ValueError("Saved restart limit is invalid; select --max-rollovers")
    if options.rollover is not None:
        options = replace(options, rollover=parse_limit(options.rollover))
    if options.context_window is not None:
        options = replace(options, context_window=_window(str(options.context_window)))
    return options


def task_candidates(root: Path, words=(), limit=20) -> list[TaskView]:
    from .task_files import load_task
    if not root.is_dir():
        return []
    query = [word.casefold() for phrase in words for word in phrase.split()]
    rows = []
    # One explicit root, no recursive filesystem discovery.
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 10000:
                raise ValueError("Task root exceeds discovery limit; use an exact task path")
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if not any((path / name).exists() for name in ("task.md", "task.json", "STATE.md")):
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
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in rows[:limit]]


def _show_candidates(rows):
    for index, view in enumerate(rows, 1):
        stamp = datetime.fromtimestamp(view.root.stat().st_mtime).strftime("%b %d %H:%M")
        print(f"{index}. {view.title} — {stamp}\n   {view.root}")


def select_task(args) -> Path:
    if args.task is not None:
        return args.task
    words = args.words
    if len(words) == 1:
        candidate = Path(words[0]).expanduser()
        if candidate.is_dir():
            return candidate
    rows = task_candidates(args.root, words, max(1, args.limit))
    if not rows:
        raise ValueError("No matching task. Use token-kit run \"Title\" to create one.")
    if args.select is not None:
        if not 1 <= args.select <= len(rows):
            raise ValueError("Selection is outside the displayed candidates")
        return rows[args.select - 1].root
    if len(rows) == 1:
        return rows[0].root
    _show_candidates(rows)
    if not sys.stdin.isatty():
        raise ValueError("Several tasks match; use an exact path or --select N")
    choice = input("Task number: ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(rows):
        raise ValueError("No task selected")
    return rows[int(choice) - 1].root


def _preview(view, options, prompt):
    from .simple_adapters import prepare
    # A rollback recipe uses native launch without Token Kit hooks or ownership.
    native = replace(options, rollover=None, max_rollovers=0)
    plan = prepare(view, native, prompt, view.root / ".token-kit" / "preview")
    print(json.dumps({"task": str(view.root), "workspace": str(view.workspace) if view.workspace else None,
                      "state": str(view.state), "assignment": str(view.assignment),
                      "settings": asdict(options), "native_recovery": {"argv": list(plan.argv),
                      "cwd": str(plan.cwd)}, "note": "Preview only; no files written or clients launched."}, indent=2))


def _launch(args, path: Path | None, *, create=False, only_create=False):
    from .task_files import create_task, load_task, load_settings, recovery_input
    workspace = args.workspace.expanduser().resolve() if args.workspace else None
    if create:
        if not args.title or not args.title.strip():
            raise ValueError("Provide a title for the new task")
        workspace = workspace or Path.cwd()
        options = resolve_options(args)
        if args.preview:
            print(json.dumps({"create": args.title, "root": str(args.root), "workspace": str(workspace),
                              "settings": asdict(options), "prompt": args.prompt}, indent=2))
            return 0
        view = create_task(args.root, args.title, workspace, assignment=args.prompt)
    else:
        view = load_task(path, workspace=workspace, agent=args.agent)
        options = resolve_options(args, load_settings(view))
    if only_create:
        print(view.root)
        return 0
    for warning in view.diagnostics:
        print("Token Kit: " + warning, file=sys.stderr)
    if view.workspace is None or not view.workspace.is_dir():
        raise ValueError("Workspace is unavailable; supply --workspace PATH")
    if args.prompt:
        prompt = args.prompt
    elif create:
        prompt = None
    else:
        prompt = recovery_input(view, agent=args.agent).text or None
    if args.preview:
        _preview(view, options, prompt)
        return 0
    from .simple_runner import run_session
    return run_session(view, options, prompt=prompt, agent=args.agent, resume=not create)


def _picker():
    rows = task_candidates(default_root())
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
        command = args.pop(0)
        if command in ("worker", "close-run", "agent", "migrate"):
            raise ValueError(f"'{command}' is no longer required. Edit task/agent files directly and use continue to resume. No records changed.")
        if command not in ("run", "new", "continue", "pick", "launch", "status", "resume", "list", "find", "send", "checkpoint", "done", "reopen", "retitle"):
            raise ValueError(f"Unknown command: {command}. Use token-kit --help.")
        parsed = parser_for(command).parse_args(args)
        if command in ("run", "new"):
            if parsed.task and parsed.title:
                raise ValueError("Use either a new title or --task PATH")
            return _launch(parsed, parsed.task, create=parsed.task is None, only_create=command == "new")
        if command in ("continue", "pick"):
            return _launch(parsed, select_task(parsed))
        if command == "launch":
            return _launch(parsed, parsed.task)
        if command in ("list", "find"):
            _show_candidates(task_candidates(parsed.root, parsed.words, max(1, parsed.limit)))
            return 0
        from .task_files import load_task, recovery_input, safe_task_path
        view = load_task(parsed.task, agent=getattr(parsed, "agent", "coordinator"))
        if command in ("status", "resume"):
            print(f"{view.title}\nWorkspace: {view.workspace or 'unknown'}\nState: {view.state}\nAssignment: {view.assignment}")
            if command == "resume" or parsed.full:
                print(recovery_input(view, agent=parsed.agent).text)
            return 0
        if command == "checkpoint":
            # Optional convenience; cannot gate recovery or acknowledge messages.
            from .core.store import atomic_text
            state = view.state.read_text() if view.state.is_file() else ""
            target = view.root / ".token-kit" / "snapshots" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".md")
            safe_task_path(view, target)
            atomic_text(target, state)
            print(target)
            return 0
        if command == "send":
            from .core.store import atomic_text
            import uuid
            text = sys.stdin.read(65537) if parsed.text == "-" else parsed.text
            if not text.strip() or len(text.encode()) > 65536:
                raise ValueError("Message must be nonempty and at most 64 KiB")
            # Legacy inbox format stays readable without any checkpoint requirement.
            recipient = view.state.parent
            if parsed.agent == "coordinator" and not view.legacy:
                recipient = view.root
            target = recipient / "messages" / (uuid.uuid4().hex + ".json")
            safe_task_path(view, target)
            atomic_text(target, json.dumps({"message_id": target.stem, "text": text,
                        "created_at": datetime.now().astimezone().isoformat(), "source": "user"}))
            print(target)
            return 0
        from .core.store import atomic_text
        target = safe_task_path(view, view.root / ".token-kit" / "labels.json")
        lock = safe_task_path(view, target.parent / "metadata.lock")
        lock.parent.mkdir(exist_ok=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            labels = json.loads(target.read_text()) if target.exists() else {}
            if not isinstance(labels, dict):
                raise ValueError("Saved labels must be an object; existing bytes were preserved")
            labels.update({"title": parsed.title} if command == "retitle" else {"status": "done" if command == "done" else "open"})
            atomic_text(target, json.dumps(labels, indent=2) + "\n")
        return 0
    except (OSError, ValueError) as exc:
        print("Token Kit: " + str(exc), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
