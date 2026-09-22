"""Portable task checkpoints and explicit fresh-session launches.

This first milestone does not supervise context, intercept tools, or automatically
fail over providers. Strict Codex launch is unavailable until its control is verified.
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
from pathlib import Path

from .adapters.base import LaunchRequest
from .core.store import Store, atomic_text, now, process_identity, read_json, write_json


def stop_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def launch(store: Store, agent: str, engine: str, model: str | None = None,
           dry_run: bool = False) -> int:
    bundle = store.resume_bundle(agent)
    resume_command = shlex.join(["token-kit-workflow", "resume", str(store.path), "--agent", agent])
    checkpoint_command = shlex.join(["token-kit-workflow", "checkpoint", str(store.path), "--agent", agent])
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
    adapter = importlib.import_module(f"token_kit.adapters.{engine}")
    plan = adapter.prepare_launch(LaunchRequest(store.workspace, prompt, True, model))
    if dry_run:
        # Never print inherited auth-bearing environment values.
        print(json.dumps({"engine": engine, "argv": plan.argv, "cwd": str(plan.cwd),
                          "strict_no_compaction_requested": True, "resume": bundle}, indent=2))
        return 0
    if shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    run = store.claim_run(agent, engine, True)
    write_json(run / "resume.json", bundle)
    atomic_text(run / "prompt.md", prompt + "\n")
    child = None
    try:
        environment = dict(plan.env)
        bin_directory = str(Path(__file__).resolve().parent / "bin")
        environment["PATH"] = bin_directory + os.pathsep + environment.get("PATH", os.defpath)
        child = subprocess.Popen(plan.argv, cwd=plan.cwd, env=environment)
        store.update_run(agent, run.name, status="running", child_pid=child.pid,
                         child_identity=process_identity(child.pid))
        rc = child.wait()
        # An exited process does not prove that its external jobs finished.
        store.update_run(agent, run.name, status="exited" if rc == 0 else "interrupted",
                         exit_code=rc, ended_at=now())
        return rc if rc >= 0 else 128 - rc
    except KeyboardInterrupt:
        if child is not None:
            stop_child(child)
        store.update_run(agent, run.name, status="interrupted", ended_at=now())
        return 130
    except (OSError, ValueError):
        if child is not None:
            stop_child(child)
        try:
            store.update_run(agent, run.name, status="interrupted", ended_at=now())
        except OSError:
            pass  # The original starting/running record still blocks another run.
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="token-kit-workflow", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="create a portable task and coordinator checkpoint")
    new.add_argument("title")
    new.add_argument("--root", type=Path, default=Path("tasks"))
    new.add_argument("--workspace", type=Path, default=Path.cwd())
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
        elif name == "send":
            command.add_argument("text", help="literal text, or - to read stdin")
        elif name == "close-run":
            command.add_argument("run_id")
            command.add_argument("--note", required=True)
    status = commands.add_parser("status", help="show agents and their runs without loading transcripts")
    status.add_argument("task", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "new":
            print(Store.create(args.root, args.title, args.workspace).path)
            return 0
        store = Store(args.task)
        if args.command == "agent":
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
            return launch(store, args.agent, args.engine, args.model, args.dry_run)
        elif args.command == "status":
            agents = []
            for path in sorted((store.path / "agents").iterdir()):
                if (path / "agent.json").is_file():
                    agents.append({"agent": read_json(path / "agent.json"),
                                   "runs": [read_json(p) for p in sorted((path / "runs").glob("*/run.json"))]})
            print(json.dumps({"task": read_json(store.path / "task.json"), "agents": agents}, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"token-kit-workflow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
