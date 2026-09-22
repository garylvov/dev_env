"""Token Kit shared task lifecycle and explicit fresh-session launches.

Opt-in rollover uses lifecycle hooks and fresh checkpoints, never a summarizer.
Provider failover is not implemented. Hook policy requests are not certification
of live client behavior or a hard context cap.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import termios
from pathlib import Path

from .adapters.base import LaunchRequest
from .core.store import Store, atomic_text, now, process_identity, read_json, write_json
from . import runtime
from .core import ledger


def default_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "token_kit"


def task_rows(root: Path, words: list[str] | None = None, only_open: bool = False) -> list[dict]:
    """Inspect only immediate task folders under the explicit task root."""
    result = []
    if not root.is_dir():
        return result
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not (path / "task.json").is_file():
            continue
        store = Store(path)
        metadata = read_json(store.safe(store.path / "task.json"))
        if only_open and metadata.get("status", "open") != "open":
            continue
        text = " ".join(str(metadata.get(key, "")) for key in ("title", "summary", "task_id", "workspace")).lower()
        if words and not all(word.lower() in text for word in words):
            continue
        result.append({**metadata, "status": metadata.get("status", "open"), "path": str(store.path)})
    return result


def stop_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run(args) -> int:
    """Create or recover a task and launch; project installation is opt-in."""
    from .project_install import configure

    if args.task and (args.title or args.workspace or args.root):
        raise ValueError("--task cannot be combined with a new title, --workspace, or --root")
    if args.codegraph and not args.install_project:
        raise ValueError("--codegraph writes project settings; add --install-project to opt in")
    store = Store(args.task) if args.task else None
    workspace = store.workspace if store else (args.workspace or Path.cwd()).resolve()
    title = args.title or workspace.name
    # Reject unsupported clients and missing binaries before creating anything.
    adapter = importlib.import_module(f"token_kit.adapters.{args.engine}")
    plan = adapter.prepare_launch(LaunchRequest(workspace, "Continue the task", True,
                                               args.model, yolo=args.yolo,
                                               managed_hooks=bool(args.rollover_tokens)))
    if not args.dry_run and shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    if args.max_rollovers < 0:
        raise ValueError("--max-rollovers must be nonnegative")
    if args.engine == "codex" and args.rollover_tokens and not args.dry_run:
        runtime.verify_codex(plan.argv[0], workspace)
    if store:
        store.resume_bundle("coordinator")  # validate recovery before changing project files
    changes = configure(workspace, engine="both", codegraph=args.codegraph, dry_run=True) if args.install_project else []
    if args.dry_run:
        print(json.dumps({"dry_run": True, "engine": args.engine, "workspace": str(workspace),
                          "task": str(store.path) if store else None, "title": title,
                          "root": str(args.root or default_root()), "project_changes": changes,
                          "yolo": args.yolo, "install_project": args.install_project,
                          "automatic_rollover": bool(args.rollover_tokens),
                          "rollover_tokens": args.rollover_tokens,
                          "max_rollovers": args.max_rollovers}, indent=2))
        return 0
    if args.install_project:
        configure(workspace, engine="both", codegraph=args.codegraph)
    store = store or Store.create(args.root or default_root(), title, workspace)
    print(f"Token Kit | {args.engine} | task: {store.path}", file=sys.stderr)
    print("Guidance: session-only; existing project settings are preserved" if not args.install_project
          else "Guidance: installed in project", file=sys.stderr)
    print("Checkpoints: agent-maintained | automatic rollover: " +
          (f"{args.rollover_tokens:,} context tokens (turn boundaries)" if args.rollover_tokens else "disabled"), file=sys.stderr)
    print(f"Token ledger: {store.path / 'TOKEN_LEDGER.md'}", file=sys.stderr)
    print(f"Continue later: token-kit run --task {shlex.quote(str(store.path))} "
          f"--engine {args.engine}" + (f" --model {shlex.quote(args.model)}" if args.model else "")
          + (" --yolo" if args.yolo else "")
          + (f" --rollover-tokens {args.rollover_tokens} --max-rollovers {args.max_rollovers}"
             if args.rollover_tokens else ""), file=sys.stderr)
    return launch(store, "coordinator", args.engine, args.model, yolo=args.yolo,
                  rollover_tokens=args.rollover_tokens, max_rollovers=args.max_rollovers)


def launch(store: Store, agent: str, engine: str, model: str | None = None,
           dry_run: bool = False, yolo: bool = False, rollover_tokens: int | None = None,
           max_rollovers: int = 10) -> int:
    if max_rollovers < 0 or (rollover_tokens is not None and rollover_tokens <= 0):
        raise ValueError("Invalid rollover limits")
    if engine == "codex" and rollover_tokens and not dry_run:
        runtime.verify_codex(workspace=store.workspace)
    for segment in range(max_rollovers + 1):
        rc, control = _launch_segment(store, agent, engine, model, dry_run, yolo, rollover_tokens)
        if control.get("phase") != "ready":
            return rc
        if segment == max_rollovers:
            print("Token Kit: rollover limit reached; checkpoint saved. Resume manually.", file=sys.stderr)
            return 75
        observed_model = (control.get("sample") or {}).get("current_model")
        if observed_model and observed_model != "unknown":
            model = observed_model
        print(f"Token Kit: checkpoint saved; restarting {engine} ({segment + 1}/{max_rollovers}).", file=sys.stderr)
    return 75


def _launch_segment(store: Store, agent: str, engine: str, model: str | None,
                    dry_run: bool, yolo: bool, rollover_tokens: int | None) -> tuple[int, dict]:
    bundle = store.resume_bundle(agent)
    resume_command = shlex.join(["token-kit", "resume", str(store.path), "--agent", agent])
    checkpoint_command = shlex.join(["token-kit", "checkpoint", str(store.path), "--agent", agent])
    prompt = (
        f"Continue logical agent {agent} in task {store.path}. "
        f"Read assignment {bundle['assignment']} and committed state "
        f"{bundle['checkpoint']}/STATE.md. The workspace is {store.workspace}. "
        f"Treat checkpoint claims as evidence to verify. Run {resume_command} to see pending "
        "messages, changed evidence, and prior run records. Before changing files, reconcile any changes or "
        "unfinished operations since the checkpoint. Do not blindly repeat an interrupted command. "
        "Do not compact or use a transcript summarizer. Maintain the agent's STATE.md with "
        "Objective, Completed, Evidence, Unresolved, and Next sections, and commit checkpoints "
        f"with {checkpoint_command}; add --evidence for relevant changed files and --incorporated "
        "for each message ID addressed. Write out.md when the assignment is complete."
    )
    from .project_install import INSTRUCTIONS
    from .worker_policy import brief
    if agent == "coordinator":
        prompt += "\n\nToken Kit guidance for this session:\n" + INSTRUCTIONS.rstrip()
    else:
        prompt = brief(prompt, str(store.path), agent)
    adapter = importlib.import_module(f"token_kit.adapters.{engine}")
    plan = adapter.prepare_launch(LaunchRequest(store.workspace, prompt, True, model, yolo=yolo,
                                               worker_task=str(store.path),
                                               managed_hooks=engine == "claude" or bool(rollover_tokens)))
    if dry_run:
        # Never print inherited auth-bearing environment values.
        print(json.dumps({"engine": engine, "argv": plan.argv, "cwd": str(plan.cwd),
                          "strict_no_compaction_requested": True, "yolo": yolo, "resume": bundle,
                          "rollover_tokens": rollover_tokens}, indent=2))
        return 0, {}
    if shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    run = store.claim_run(agent, engine, True)
    store.update_run(agent, run.name, yolo=yolo, model=model, rollover_tokens=rollover_tokens)
    runtime.initialize(store, agent, run, engine, rollover_tokens)
    write_json(run / "resume.json", bundle)
    atomic_text(run / "prompt.md", prompt + "\n")
    child = None
    terminal = None
    try:
        if sys.stdin.isatty():
            try:
                terminal = (sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno()))
            except (OSError, termios.error):
                pass
        environment = dict(plan.env)
        environment["TOKEN_KIT_TASK"] = str(store.path)
        environment["TOKEN_KIT_AGENT"] = agent
        environment["TOKEN_KIT_RUN"] = run.name
        bin_directory = str(Path(__file__).resolve().parent / "bin")
        environment["PATH"] = bin_directory + os.pathsep + environment.get("PATH", os.defpath)
        child = subprocess.Popen(plan.argv, cwd=plan.cwd, env=environment)
        store.update_run(agent, run.name, status="running", child_pid=child.pid,
                         child_identity=process_identity(child.pid))
        if rollover_tokens:
            rc, control = runtime.wait_segment(child, store, agent, run, stop_child)
        else:
            rc, control = child.wait(), {}
        if control.get("phase") == "halted":
            store.update_run(agent, run.name, status="interrupted", ended_at=now(),
                             rollover_error=control.get("reason"))
            print(f"Token Kit: {control.get('reason')}", file=sys.stderr)
            return 75, control
        ready = control.get("phase") == "ready"
        # An exited process does not prove that its external jobs finished.
        store.update_run(agent, run.name, status="exited" if rc == 0 or ready else "interrupted",
                         exit_code=rc, ended_at=now(), rollover_checkpoint=control.get("checkpoint"))
        ledger.refresh(store)
        return (0 if ready else rc if rc >= 0 else 128 - rc), control
    except KeyboardInterrupt:
        if child is not None:
            stop_child(child)
        store.update_run(agent, run.name, status="interrupted", ended_at=now())
        return 130, {}
    except (OSError, ValueError):
        if child is not None:
            stop_child(child)
        try:
            store.update_run(agent, run.name, status="interrupted", ended_at=now())
        except OSError:
            pass  # The original starting/running record still blocks another run.
        raise
    finally:
        if terminal is not None:
            try:
                termios.tcsetattr(terminal[0], termios.TCSANOW, terminal[1])
            except (OSError, termios.error):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="token-kit", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("hooks", help="open Codex to review session-local lifecycle hooks (no task/model prompt)")
    review.add_argument("--engine", choices=("codex",), default="codex")
    start = commands.add_parser("run", help="create or resume a task and launch with session-only guidance")
    start.add_argument("title", nargs="?")
    start.add_argument("--task", type=Path, help="continue an existing task instead of creating one")
    start.add_argument("--workspace", type=Path, help="source directory (default: current directory)")
    start.add_argument("--root", type=Path, help="session root (default: ~/.config/token_kit; honors XDG_CONFIG_HOME)")
    start.add_argument("--engine", choices=("claude", "codex"), default="claude")
    start.add_argument("--model")
    start.add_argument("--install-project", action="store_true", help="persist shared project instructions (opt-in)")
    start.add_argument("--codegraph", action="store_true", help="configure an already installed CodeGraph")
    start.add_argument("--yolo", action="store_true", help="bypass client permission checks")
    start.add_argument("--dry-run", action="store_true", help="preview without writing or launching")
    start.add_argument("--rollover-tokens", type=runtime.token_limit, help="restart from a checkpoint at a turn boundary (e.g. 500k)")
    start.add_argument("--max-rollovers", type=int, default=10, help="maximum automatic restarts (default: 10)")
    new = commands.add_parser("new", help="create a shared task and coordinator checkpoint")
    new.add_argument("title")
    new.add_argument("--root", type=Path, default=default_root())
    new.add_argument("--workspace", type=Path, default=Path.cwd())
    new.add_argument("--summary", default="")
    for name in ("list", "find"):
        command = commands.add_parser(name, help="locate tasks under a shared root")
        command.add_argument("--root", type=Path, default=default_root())
        command.add_argument("--open", action="store_true")
        if name == "find":
            command.add_argument("words", nargs="+")
    migrate = commands.add_parser("migrate", help="import a legacy task without modifying it")
    migrate.add_argument("source", type=Path)
    migrate.add_argument("--root", type=Path, default=default_root())
    for name in ("done", "reopen", "retitle"):
        command = commands.add_parser(name, help="update task metadata without changing its identity")
        command.add_argument("task", type=Path)
        if name == "retitle":
            command.add_argument("title")
            command.add_argument("--summary")
    agent = commands.add_parser("agent", help="create a stable logical worker")
    agent.add_argument("task", type=Path)
    agent.add_argument("name")
    agent.add_argument("--assignment-file", required=True, type=Path)
    for name, help_text in (("checkpoint", "commit the working STATE.md"),
                            ("resume", "export engine-independent recovery information"),
                            ("launch", "launch a fresh native session with strict compaction controls"),
                            ("send", "queue a durable message for the next resume"),
                            ("close-run", "record manual reconciliation after a dead run")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("task", type=Path)
        command.add_argument("--agent", default="coordinator")
        if name == "checkpoint":
            command.add_argument("--evidence", action="append", default=None)
            command.add_argument("--incorporated", action="append", default=[])
        elif name == "launch":
            command.add_argument("--engine", choices=("claude", "codex"), required=True)
            command.add_argument("--model")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--yolo", action="store_true",
                                 help="bypass client permission checks (Codex also disables sandboxing)")
            command.add_argument("--rollover-tokens", type=runtime.token_limit)
            command.add_argument("--max-rollovers", type=int, default=10)
        elif name == "send":
            command.add_argument("text", help="literal text, or - to read stdin")
        elif name == "close-run":
            command.add_argument("run_id")
            command.add_argument("--note", required=True)
    status = commands.add_parser("status", help="show agents and their runs without loading transcripts")
    status.add_argument("task", type=Path)
    report = commands.add_parser("ledger", help="show reported token accounting without loading transcripts")
    report.add_argument("task", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "hooks":
            return runtime.review_hooks()
        if args.command == "run":
            return run(args)
        if args.command == "new":
            store = Store.create(args.root, args.title, args.workspace)
            if args.summary:
                store.update_task(summary=args.summary)
            print(store.path)
            return 0
        if args.command in ("list", "find"):
            print(json.dumps(task_rows(args.root, getattr(args, "words", None), args.open), indent=2))
            return 0
        if args.command == "migrate":
            from .core.migrate import migrate_task
            print(migrate_task(args.source, args.root).path)
            return 0
        if not (args.task / "task.json").exists() and (args.task / "STATE.md").is_file():
            raise ValueError(f"Legacy task folder: first run token-kit migrate {shlex.quote(str(args.task))}; the original will be preserved")
        store = Store(args.task)
        if args.command in ("done", "reopen"):
            store.update_task(status="done" if args.command == "done" else "open")
        elif args.command == "retitle":
            fields = {"title": args.title}
            if args.summary is not None:
                fields["summary"] = args.summary
            store.update_task(**fields)
        elif args.command == "agent":
            print(store.add_agent(args.name, args.assignment_file.read_text(encoding="utf-8")))
        elif args.command == "checkpoint":
            print(store.checkpoint(args.agent, args.evidence, args.incorporated))
        elif args.command == "resume":
            print(json.dumps(store.resume_bundle(args.agent), indent=2))
        elif args.command == "send":
            print(store.send(args.agent, sys.stdin.read() if args.text == "-" else args.text))
        elif args.command == "close-run":
            store.close_run(args.agent, args.run_id, args.note)
        elif args.command == "launch":
            return launch(store, args.agent, args.engine, args.model, args.dry_run, args.yolo,
                          args.rollover_tokens, args.max_rollovers)
        elif args.command == "ledger":
            print(ledger.refresh(store))
        elif args.command == "status":
            agents = []
            for path in sorted((store.path / "agents").iterdir()):
                if (path / "agent.json").is_file():
                    agents.append({"agent": read_json(path / "agent.json"),
                                   "runs": [read_json(p) for p in sorted((path / "runs").glob("*/run.json"))]})
            print(json.dumps({"task": read_json(store.path / "task.json"), "agents": agents}, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"token-kit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
