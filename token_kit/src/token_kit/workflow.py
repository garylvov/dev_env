"""Token Kit shared task lifecycle and explicit fresh-session launches.

Opt-in rollover uses lifecycle hooks and fresh checkpoints, never a summarizer.
Provider failover is not implemented. Hook policy requests are not certification
of live client behavior or a hard context cap.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from difflib import SequenceMatcher
import importlib
from itertools import count
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
from pathlib import Path

from .adapters.base import LaunchRequest
from .core.store import Store, atomic_bytes, atomic_text, now, process_identity, read_json, write_json
from . import runtime, display
from .core import ledger, lifecycle
from .rollover import format_limit, parse_limit
from .timefmt import human, parse_iso


def parse_context_window(value: object) -> int:
    parsed = parse_limit(value)
    if type(parsed) is not int or parsed <= 0:
        raise ValueError("context window must be a positive token count")
    return parsed


def parse_restart_budget(value: str) -> int | None:
    if value.lower() in ("unlimited", "infinite"):
        return None
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("maximum rollovers must be nonnegative or unlimited")
    return int(value)


class RestartBudgetAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.max_rollovers_explicit = True


def add_restart_budget(parser):
    parser.set_defaults(max_rollovers=None, max_rollovers_explicit=False)
    parser.add_argument("--max-rollovers", type=parse_restart_budget, action=RestartBudgetAction,
                        help="maximum automatic restarts: nonnegative integer or unlimited (default)")


def format_restart_budget(value):
    return "unlimited" if value is None else str(value)


def inherited_restart_budget(previous):
    budget = previous.get("max_rollovers")
    explicit = previous.get("max_rollovers_explicit")
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("invalid recorded restart budget; specify --max-rollovers")
    if explicit is not None and type(explicit) is not bool:
        raise ValueError("invalid recorded restart budget provenance; specify --max-rollovers")
    if explicit is None and budget == 10:
        return None, False  # Previous releases recorded their default as an ordinary cap.
    return budget, explicit if explicit is not None else budget is not None


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


def ranked_tasks(root: Path, words: list[str]) -> list[dict]:
    """Rank metadata only: substring matches precede conservative typo matches."""
    query = [word.casefold() for phrase in words for word in phrase.split()]
    ranked = []
    for row in task_rows(root):
        text = " ".join(str(row.get(key, "")) for key in
                        ("title", "task_id", "summary", "workspace")).casefold()
        tokens = re.findall(r"\w+", text)
        scores = [1.0 if word in text else max(
            (SequenceMatcher(None, word, token).ratio() for token in tokens), default=0.0)
            if len(word) >= 3 else 0.0 for word in query]
        if any(score < 0.72 for score in scores):
            continue
        try:
            created = datetime.fromisoformat(str(row.get("created_at", ""))).timestamp()
        except (ValueError, OverflowError):
            created = 0.0
        ranked.append((sum(scores) / len(scores) if scores else 1.0, created, row))
    ranked.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("task_id", ""))))
    return [row for _, _, row in ranked]


def compact_text(value, limit: int) -> str:
    text = " ".join("".join(c for c in str(value) if c.isprintable() or c.isspace()).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def task_next_preview(row: dict) -> str:
    """Read at most 32 KiB of current coordinator state; never walk history."""
    try:
        store = Store(row["path"])
        path = store.safe(store.path / "agents" / "coordinator" / "STATE.md")
        with path.open("rb") as handle:
            state = handle.read(32768).decode("utf-8", errors="replace")
        match = re.search(r"^## Next\s*\n(.*?)(?=^## |\Z)", state, re.MULTILINE | re.DOTALL)
        return compact_text(match.group(1), 100) if match else "unavailable"
    except (OSError, ValueError, KeyError):
        return "unavailable"


def picker_created(value) -> str:
    when = parse_iso(value)
    if when is None:
        return "Unknown date"
    if when.tzinfo is None:
        return human(when) + " (timezone unknown)"
    try:
        local = when.astimezone()
    except (ValueError, OverflowError, OSError):
        return "Unknown date"
    return human(local) + " " + (local.tzname() or "(timezone unknown)")


def latest_picker_run(task: str) -> dict:
    """Inspect only bounded immediate coordinator run metadata for one selection."""
    store = Store(task)
    root = store.safe(store.path / "agents" / "coordinator" / "runs")
    if not root.exists():
        return {}
    records = []
    for number, path in enumerate(root.iterdir(), 1):
        if number > 256:
            raise ValueError("too many coordinator runs to infer resume settings safely (limit: 256)")
        record_path = store.safe(path / "run.json")
        if not record_path.is_file():
            continue
        with record_path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("run metadata exceeds 64 KiB; cannot infer resume settings safely")
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError(f"Expected run metadata object: {record_path}")
        try:
            created = datetime.fromisoformat(str(record.get("created_at", ""))).timestamp()
        except (ValueError, OverflowError):
            created = 0.0
        records.append((created, path.name, record))
    return max(records, key=lambda item: (item[0], item[1]))[2] if records else {}


def parse_rollover_percentage(value: object) -> str:
    """Normalize the CLI percentage spelling to the internal ``N%`` form."""
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("--rollover-perc expects an integer from 1 through 99 without '%'")
    percentage = int(text)
    if not 1 <= percentage <= 99:
        raise ValueError("--rollover-perc expects an integer from 1 through 99")
    return parse_limit(f"{percentage}%")


def _rollover_command(spec) -> list[str]:
    """Return the stable CLI spelling for a requested rollover target."""
    normalized = parse_limit(spec)
    if isinstance(normalized, str):
        return ["--rollover-perc", normalized[:-1]]
    return ["--rollover-tokens", str(normalized)]


def pick(args) -> int:
    continuing = args.command == "continue"
    if args.limit <= 0:
        raise ValueError("--limit must be positive")
    exact = getattr(args, "task", None)
    if exact and args.words:
        raise ValueError("--task cannot be combined with a query")
    if continuing and not exact and len(args.words) == 1:
        candidate = Path(args.words[0]).expanduser()
        if (candidate / "task.json").is_file():
            exact = candidate
    if exact:
        store = Store(exact)
        rows = [{**read_json(store.path / "task.json"), "path": str(store.path)}]
    else:
        rows = ranked_tasks(args.root, args.words)
    if not rows:
        print("token-kit: no matching tasks", file=sys.stderr)
        return 1
    omitted = max(0, len(rows) - args.limit)
    rows = rows[:args.limit]
    for number, row in enumerate(rows, 1):
        status = {"open": "Unfinished", "done": "Finished"}.get(row.get("status"), "Unknown status")
        print(f"{number:>2}. {compact_text(row.get('title', ''), 64)} "
              f"[{status}] Created {picker_created(row.get('created_at'))}\n"
              f"    {compact_text(row.get('task_id', ''), 128)} | Next: {task_next_preview(row)}",
              file=sys.stderr)
    if omitted:
        print(f"{omitted} more matching tasks; narrow your query or increase --limit.", file=sys.stderr)
    selected = args.select
    if continuing and len(rows) == 1 and not omitted and selected is None:
        selected = 1
    if selected is None:
        if not sys.stdin.isatty():
            print("token-kit: selection required; rerun with --select N", file=sys.stderr)
            return 2
        while selected is None:
            print("Choose task number (Enter/q cancels): ", end="", file=sys.stderr, flush=True)
            try:
                answer = input().strip().casefold()
            except (EOFError, KeyboardInterrupt):
                print("\ntoken-kit: cancelled", file=sys.stderr)
                return 1
            if not answer or answer == "quit" or set(answer) == {"q"}:
                print("token-kit: cancelled", file=sys.stderr)
                return 1
            try:
                candidate = int(answer)
            except ValueError:
                candidate = 0
            if 1 <= candidate <= len(rows):
                selected = candidate
            else:
                print(f"Choose a task number between 1 and {len(rows)}, or q to cancel.", file=sys.stderr)
    if not 1 <= selected <= len(rows):
        raise ValueError(f"selection must be between 1 and {len(rows)}")
    task = rows[selected - 1]["path"]
    previous = latest_picker_run(task)
    engine = args.engine or previous.get("engine") or "claude"
    if engine not in ("codex", "claude"):
        raise ValueError("unknown recorded engine; specify --engine")
    model = args.model
    if model is None and engine == previous.get("engine"):
        usage = previous.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("invalid recorded usage; specify --model")
        observed = usage.get("current_model")
        model = observed if observed and observed != "unknown" else previous.get("model")
    threshold = args.rollover_tokens if args.rollover_tokens is not None else previous.get("rollover_tokens")
    if threshold is not None:
        threshold = parse_limit(threshold)
    context_window = getattr(args, "context_window", None)
    if (context_window is None and engine == previous.get("engine")
            and (args.model is None or args.model == previous.get("model"))):
        context_window = previous.get("context_window")
    if context_window is not None:
        context_window = parse_context_window(context_window)
    yolo = args.yolo if args.yolo is not None else previous.get("yolo", False)
    if not isinstance(yolo, bool):
        raise ValueError("invalid recorded permissions; specify --yolo or --no-yolo")
    if model is not None and not isinstance(model, str):
        raise ValueError("invalid recorded model; specify --model")
    budget = getattr(args, "max_rollovers", None)
    budget_explicit = getattr(args, "max_rollovers_explicit", False)
    if continuing and not budget_explicit:
        budget, budget_explicit = inherited_restart_budget(previous)
    command = ["token-kit", "continue" if continuing else "run", "--task", task, "--engine", engine]
    if model and model != "unknown":
        command += ["--model", str(model)]
    if threshold is not None:
        command += _rollover_command(threshold)
    if context_window is not None:
        command += ["--context-window", str(context_window)]
    if yolo:
        command += ["--yolo"]
    if continuing and not yolo:
        command += ["--no-yolo"]
    if continuing or budget_explicit:
        command += ["--max-rollovers", format_restart_budget(budget)]
    if args.print_command:
        print(shlex.join(command))
        return 0
    print("Resuming: " + shlex.join(command), file=sys.stderr)
    launch_args = build_parser().parse_args(["run", *[part for part in command[2:] if part != "--no-yolo"]])
    launch_args.max_rollovers_explicit = budget_explicit
    launch_args.continue_session = continuing
    return run(launch_args)


def stop_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def startup_line(text: str, *, heading: bool = False) -> None:
    """Purple terminal accents without escape codes in logs or NO_COLOR output."""
    if sys.stderr.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ:
        if heading:
            text = "\033[1;35m" + text + "\033[0m"
        else:
            label, separator, value = text.partition(": ")
            text = "\033[35m" + label + "\033[0m" + separator + value
    print(text, file=sys.stderr)


def run(args) -> int:
    """Create or recover a task and launch; project installation is opt-in."""
    from .project_install import configure

    if args.rollover_tokens is not None:
        args.rollover_tokens = parse_limit(args.rollover_tokens)

    if args.task and (args.title or args.workspace or args.root or args.prompt is not None):
        raise ValueError("--task cannot be combined with a new title, --workspace, --root, or --prompt")
    if args.prompt is not None and (not args.prompt.strip() or "\0" in args.prompt):
        raise ValueError("--prompt must be nonempty and contain no NUL characters")
    idle = not args.task and args.prompt is None
    if args.codegraph and not args.install_project:
        raise ValueError("--codegraph writes project settings; add --install-project to opt in")
    store = Store(args.task) if args.task else None
    workspace = store.workspace if store else (args.workspace or Path.cwd()).resolve()
    title = args.title or workspace.name
    # Reject unsupported clients and missing binaries before creating anything.
    adapter = importlib.import_module(f"token_kit.adapters.{args.engine}")
    # Every managed session needs the lifecycle handshake.  The threshold
    # still controls whether usage-based rollover is active; it does not
    # control whether compaction and startup events are supervised.
    plan = adapter.prepare_launch(LaunchRequest(workspace, "Continue the task", True,
                                               args.model, yolo=args.yolo,
                                               managed_hooks=True))
    if not args.dry_run and shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    if args.max_rollovers is not None and (type(args.max_rollovers) is not int or args.max_rollovers < 0):
        raise ValueError("--max-rollovers must be nonnegative")
    if args.engine == "codex" and not args.dry_run:
        runtime.ensure_codex_hooks(plan.argv[0], workspace)
    if store:
        store.resume_bundle("coordinator")  # validate recovery before changing project files
    changes = configure(workspace, engine="both", codegraph=args.codegraph, dry_run=True) if args.install_project else []
    if args.dry_run:
        print(json.dumps({"dry_run": True, "engine": args.engine, "workspace": str(workspace),
                          "task": str(store.path) if store else None, "title": title,
                          "root": str(args.root or default_root()), "project_changes": changes,
                          "yolo": args.yolo, "install_project": args.install_project,
                          "startup": "idle" if idle else "prompt" if args.prompt is not None else "resume",
                          "initial_prompt": args.prompt,
                          "automatic_rollover": bool(args.rollover_tokens),
                          "compaction_recovery": args.max_rollovers != 0,
                          "rollover_tokens": args.rollover_tokens,
                          "context_window": getattr(args, "context_window", None),
                          "max_rollovers": args.max_rollovers}, indent=2))
        return 0
    if args.install_project:
        configure(workspace, engine="both", codegraph=args.codegraph)
    assignment = args.prompt if args.prompt is not None else (
        "No task assigned. Wait for the user's first instruction. The session title is only a label, "
        "not authorization to investigate files or perform work. Record the user's actual objective "
        "and scope in STATE.md after receiving an instruction.")
    store = store or Store.create(args.root or default_root(), title, workspace, assignment=assignment)
    display_title = read_json(store.path / "task.json")["title"]
    display_title = " ".join("".join(c for c in display_title if c.isprintable() or c.isspace()).split())
    startup_line(f"Token Kit | {args.engine} | {display_title}", heading=True)
    startup_line(f"Task: {store.path}")
    if idle:
        startup_line("New session: loading trigger matrix, then waiting for your instruction; the title is only a label.")
    startup_line("Guidance: session-only; existing project settings are preserved" if not args.install_project
                 else "Guidance: installed in project")
    if args.rollover_tokens is None:
        rollover_display = "disabled"
    elif isinstance(args.rollover_tokens, str):
        rollover_display = f"{format_limit(args.rollover_tokens)} context (turn boundaries)"
    else:
        rollover_display = f"{format_limit(args.rollover_tokens)} context tokens (turn boundaries)"
    startup_line("Checkpoints: agent-maintained | automatic rollover: " + rollover_display)
    if args.max_rollovers is None:
        recovery_display = "enabled (unlimited managed restarts)"
    elif args.max_rollovers == 0:
        recovery_display = "disabled"
    else:
        recovery_display = (f"enabled (up to {args.max_rollovers} managed restart" +
                            ("s" if args.max_rollovers != 1 else "") + ")")
    startup_line("Structured compaction recovery: " + recovery_display)
    startup_line(f"Token ledger: {store.path / 'TOKEN_LEDGER.md'}")
    command = ["token-kit", "continue", "--task", str(store.path), "--engine", args.engine,
               "--max-rollovers", format_restart_budget(args.max_rollovers)]
    if args.model:
        command += ["--model", args.model]
    if getattr(args, "context_window", None) is not None:
        command += ["--context-window", str(args.context_window)]
    command += ["--yolo" if args.yolo else "--no-yolo"]
    if args.rollover_tokens is not None:
        command += _rollover_command(args.rollover_tokens)
    startup_line("Continue later: " + shlex.join(command))
    options = dict(yolo=args.yolo, rollover_tokens=args.rollover_tokens,
                   max_rollovers=args.max_rollovers,
                   max_rollovers_explicit=getattr(args, "max_rollovers_explicit", False),
                   idle=idle, initial_prompt=args.prompt)
    if getattr(args, "context_window", None) is not None:
        options["context_window"] = args.context_window
    if getattr(args, "continue_session", False):
        from .continuation import handoff
        with handoff(store, "coordinator", args.engine) as continuation:
            return launch(store, "coordinator", args.engine, args.model,
                          continuation=continuation, **options)
    return launch(store, "coordinator", args.engine, args.model, **options)



def exit_summary(store, agent, engine, model, yolo, threshold, max_rollovers, rc, control, *, worker_ticket=None, context_window=None):
    """Report supervisor evidence, not a guessed explanation of client UI errors."""
    if threshold is not None:
        threshold = parse_limit(threshold)
    if control.get("phase") == "halted":
        outcome = "safety stop: " + str(control.get("reason", "inspect run records"))
    elif control.get("phase") == "ready":
        outcome = "rollover limit reached; checkpoint saved"
    elif rc == 130:
        outcome = "interrupted (130); reconcile unfinished operations"
    elif worker_ticket is not None and control.get("phase") == "completed":
        outcome = "worker finished; latest result recorded"
    elif rc == 0:
        outcome = "client exited normally (0); this does not prove the task is complete"
    else:
        outcome = f"client/launcher failure ({rc}); inspect client output and run records"
    startup_line("Token Kit exit: " + outcome, heading=True)
    sample = control.get("sample") or {}
    context = sample.get("context_tokens")
    startup_line("Latest context: " + (f"{context:,} tokens" if context is not None else "unknown"))
    startup_line(f"Cumulative usage (including cached input and observed workers): {store.path / 'TOKEN_LEDGER.md'}")
    command = ["token-kit", "continue" if agent == "coordinator" else "launch"]
    command += ["--task", str(store.path)] if agent == "coordinator" else [str(store.path), "--agent", agent]
    command += ["--engine", engine]
    effective_model = model if worker_ticket is not None else sample.get("current_model") or model
    if effective_model and effective_model != "unknown":
        command += ["--model", effective_model]
    if context_window is not None:
        command += ["--context-window", str(context_window)]
    if yolo:
        command += ["--yolo"]
    if agent == "coordinator" and not yolo:
        command += ["--no-yolo"]
    if threshold is not None:
        command += _rollover_command(threshold)
    command += ["--max-rollovers", format_restart_budget(max_rollovers)]
    if worker_ticket is not None:
        command += ["--ticket", worker_ticket]
        if control.get("phase") == "completed":
            startup_line("Worker completion recorded and delivered to parent.")
            return rc
        if control.get("phase") == "stopped":
            startup_line("Worker stopped; bookkeeping notes saved. Continue authorized work.")
            return rc
        if control.get("phase") != "ready":
            startup_line("Worker needs reconciliation; inspect " + shlex.join([
                "token-kit", "resume", str(store.path), "--agent", agent]))
            return rc
    startup_line("Resume with Token Kit (checkpoints, not native transcript): " + shlex.join(command))
    return rc


def launch(store: Store, agent: str, engine: str, model: str | None = None,
           dry_run: bool = False, yolo: bool = False, rollover_tokens: int | str | None = None,
           max_rollovers: int | None = None, *, max_rollovers_explicit: bool | None = None,
           idle: bool = False, initial_prompt: str | None = None, continuation=None,
           worker_ticket: str | None = None, context_window: int | None = None) -> int:
    if worker_ticket is not None:
        if idle or initial_prompt is not None or continuation is not None:
            raise ValueError("Ticketed workers must launch their reserved assignment")
        with store.locked():
            reservation = lifecycle.current_locked(store, agent, worker_ticket)
            if reservation.get("engine") != engine:
                raise ValueError("Managed worker engine must match its reservation")
            reserved_model = reservation.get("model")
            if model is not None and model != reserved_model:
                raise ValueError("Managed worker model must match its reservation")
            model = reserved_model
            if rollover_tokens is None:
                rollover_tokens = reservation.get("rollover_tokens")
            if context_window is None:
                context_window = reservation.get("context_window")
        if engine != "claude":
            raise ValueError("Ticketed managed launch currently supports Claude workers only")
    if rollover_tokens is not None:
        rollover_tokens = parse_limit(rollover_tokens)
    if context_window is not None:
        context_window = parse_context_window(context_window)
    if max_rollovers is not None and (type(max_rollovers) is not int or max_rollovers < 0):
        raise ValueError("Invalid rollover limits")
    if max_rollovers_explicit is None:
        max_rollovers_explicit = max_rollovers is not None
    if engine == "codex" and not dry_run:
        runtime.ensure_codex_hooks(workspace=store.workspace)
    launch_options = {}
    if worker_ticket is not None:
        launch_options["worker_ticket"] = worker_ticket
    if context_window is not None:
        launch_options["context_window"] = context_window
    for segment in count():
        try:
            rc, control = _launch_segment(store, agent, engine, model, dry_run, yolo, rollover_tokens,
                                         idle=idle if segment == 0 else False,
                                         initial_prompt=initial_prompt if segment == 0 else None,
                                         allow_recovery=max_rollovers != 0,
                                         max_rollovers=max_rollovers,
                                         max_rollovers_explicit=max_rollovers_explicit,
                                         continuation=continuation if segment == 0 else None,
                                         **launch_options)
        except (OSError, ValueError):
            if not dry_run:
                exit_summary(store, agent, engine, model, yolo, rollover_tokens,
                             max_rollovers, 2, {}, **launch_options)
            raise
        progress = str(segment + 1) if max_rollovers is None else f"{segment + 1}/{max_rollovers}"
        if control.get("phase") == "halted" and control.get("halt_kind") == "compaction":
            # A structured parent compaction halt is the sole halt eligible
            # for an automatic recovery segment.  It consumes the same
            # restart budget as a normal threshold rollover.
            if segment == max_rollovers:
                return exit_summary(store, agent, engine, model, yolo, rollover_tokens,
                                    max_rollovers, 75, control, **launch_options)
            print(f"Token Kit: compaction halted the session; starting recovery "
                  f"({progress}).", file=sys.stderr)
            continue
        if control.get("phase") != "ready":
            return rc if dry_run else exit_summary(store, agent, engine, model, yolo,
                                                   rollover_tokens, max_rollovers, rc, control, **launch_options)
        if segment == max_rollovers:
            print("Token Kit: rollover limit reached; checkpoint saved. Resume manually.", file=sys.stderr)
            return exit_summary(store, agent, engine, model, yolo, rollover_tokens,
                                max_rollovers, 75, control, **launch_options)
        observed_model = (control.get("sample") or {}).get("current_model")
        if worker_ticket is None and observed_model and observed_model != "unknown":
            model = observed_model
        print(f"Token Kit: checkpoint saved; restarting {engine} ({progress}).", file=sys.stderr)


def _recovery_record(store: Store, agent: str, candidate: str, bundle: dict) -> dict:
    """Load and validate the predecessor selected by the store gate."""
    if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate):
        raise ValueError("Recovery candidate is not a valid run ID")
    for record in bundle.get("runs", []):
        if isinstance(record, dict) and record.get("run_id") == candidate:
            return record
    path = store.safe(store.agent_path(agent) / "runs" / candidate / "run.json")
    if not path.is_file():
        raise ValueError("Recovery candidate run record is missing")
    record = read_json(path)
    if record.get("run_id") != candidate:
        raise ValueError("Recovery candidate run identity mismatch")
    return record


def _working_state_for_recovery(store: Store, agent: str, bundle: dict) -> bytes:
    """Capture the exact current STATE.md after accepting only a safe path."""
    expected = store.agent_path(agent) / "STATE.md"
    supplied = bundle.get("working_state", bundle.get("working_state_path"))
    path = expected
    if isinstance(supplied, str):
        candidate = Path(supplied)
        try:
            candidate = candidate.resolve()
            if candidate == expected.resolve() and candidate.is_file():
                path = candidate
        except OSError:
            pass
    return path.read_bytes()


def _recovery_candidate(store: Store, agent: str, engine: str, bundle: dict,
                        *, idle: bool, initial_prompt: str | None) -> tuple[str | None, dict | None]:
    """Ask the store for recovery only on an ordinary resume segment."""
    if idle or initial_prompt is not None:
        return None, None
    candidate = store.recovery_candidate(agent)
    if candidate is None:
        return None, None
    record = _recovery_record(store, agent, candidate, bundle)
    if record.get("engine") != engine:
        raise ValueError("Recovery requires the predecessor's same engine")
    return candidate, record


def _launch_segment(store: Store, agent: str, engine: str, model: str | None,
                    dry_run: bool, yolo: bool, rollover_tokens: int | str | None,
                    *, idle: bool = False, initial_prompt: str | None = None,
                    allow_recovery: bool = True, max_rollovers: int | None = None,
                    max_rollovers_explicit: bool | None = None,
                    continuation=None, worker_ticket: str | None = None,
                    context_window: int | None = None) -> tuple[int, dict]:
    if rollover_tokens is not None:
        rollover_tokens = parse_limit(rollover_tokens)
    # A real managed launch may seed an old task's missing snapshot. Dry runs
    # remain read-only and use the repository fallback through resume_bundle.
    if not dry_run:
        store.trigger_pyramid(seed=True)
    bundle = store.resume_bundle(agent)
    recovery_from = None
    recovery_state = None
    if not dry_run and continuation is not None:
        recovery_from = continuation.recovery_from
        if recovery_from is not None:
            recovery_state = _working_state_for_recovery(store, agent, bundle)
    elif not dry_run and allow_recovery:
        recovery_from, _ = _recovery_candidate(
            store, agent, engine, bundle, idle=idle, initial_prompt=initial_prompt)
        if recovery_from is not None:
            recovery_state = _working_state_for_recovery(store, agent, bundle)
    resume_command = shlex.join(["token-kit", "resume", str(store.path), "--agent", agent])
    checkpoint_command = shlex.join(["token-kit", "checkpoint", str(store.path), "--agent", agent])
    prompt = (
        f"Continue logical agent {agent} in task {store.path}. "
        f"Read assignment {bundle['assignment']}, current working state "
        f"{store.agent_path(agent) / 'STATE.md'}, and committed state "
        f"{bundle['checkpoint']}/STATE.md. The workspace is {store.workspace}. "
        f"Treat checkpoint claims as evidence to verify. Run {resume_command} to see pending "
        "messages, unresolved children, changed evidence, and prior run records. Before changing files, reconcile any changes or "
        "unfinished operations since the checkpoint. Do not blindly repeat an interrupted command. "
        "Do not compact or use a transcript summarizer. Maintain the agent's STATE.md with "
        "Objective, Completed, Evidence, Unresolved, and Next sections, and commit checkpoints "
        f"with {checkpoint_command}; add --evidence for relevant changed files and --incorporated "
        "for each message ID addressed. Write out.md when the assignment is complete."
    )
    if recovery_from is not None:
        predecessor_path = store.safe(store.agent_path(agent) / "runs" / recovery_from / "run.json")
        children = bundle.get("children", [])
        external = bundle.get("external_operations", bundle.get("external_jobs", []))
        prior_runs = bundle.get("runs", [])
        historical = bundle.get("historical_state")
        historical_hint = (f" Historical detail is available at {historical}; read it selectively only if "
                           "needed to understand a compressed STATE." if historical else "")
        ancestors = [*store.recovery_ancestors(agent, recovery_from), read_json(predecessor_path)]
        unresolved = [record for record in ancestors if record.get("status") != "reconciled"]
        close_command = " && ".join(shlex.join([
            "token-kit", "close-run", str(store.path), "--agent", agent,
            record["run_id"], "--note",
            "Verified predecessor process, children, and external operations; recovery state reconciled."])
            for record in unresolved)

        prompt = (
            f"This is structured recovery of predecessor run {recovery_from}. "
            f"Unresolved ancestors, oldest first: {[record['run_id'] for record in unresolved]}. "
            "Audit workers and external operations across every ancestor; close them oldest first. "
            "Read the exact pre-recovery "
            f"working STATE at {store.agent_path(agent) / 'STATE.md'} captured in the successor run's "
            "recovery-input.md before editing "
            "STATE.md, and compare it with the "
            f"committed snapshot {bundle['checkpoint']}/STATE.md. Read the predecessor run record at "
            f"{predecessor_path}, prior run metadata ({compact_text(prior_runs, 3000)}), child lifecycle records ({compact_text(children, 2000)}), "
            f"and the bounded external-operation summary ({compact_text(external, 2000)}).{historical_hint} Verify every native "
            "child and external operation before doing ordinary product work. Do not resubmit an external job, "
            "replay an uncertain operation, or replace an unknown native worker. A bounded recovery-only audit "
            "worker with a fresh logical ID is allowed if needed for verification. Reconciliation is the only "
            "allowed work until verification is complete. For orphan workers whose recorded owner process is dead, "
            "inspect external operations, preserve partial/failed outcomes in a fresh worker checkpoint, then use "
            "token-kit worker retire TASK --agent ID --ticket T --note TEXT --operations-reconciled. "
            "The recovery identity comes from TOKEN_KIT_AGENT/RUN. This retires execution authority without "
            "claiming native closure or success; do not require confirmation from a dead runner. "
            "If any operation remains live or uncertain, report the blocker without repeated checking. "
            "Replace a retired worker only if work remains and authority permits. "
            "Update STATE.md, commit a fresh checkpoint, then run "
            f"{close_command}. "
            "Only after that may you continue the assigned task."
            f"\n\nThe normal recovery bundle remains: {resume_command}."
        )
    from .worker_policy import brief
    if idle:
        matrix_path = Path(__file__).resolve().parents[2] / "agent_trigger_matrix.md"
        matrix = matrix_path.read_text(encoding="utf-8")
        prompt = (f"Token Kit agent trigger matrix, loaded from {matrix_path}:\n\n"
                  f"{matrix}\n\nEND OF TRIGGER MATRIX\n\n"
                  "Apply the shared-session delegation guidance to future work, subject to applicable "
                  "repository instructions and user overrides. Legacy-only examples are not active commands "
                  "or guarantees for this shared session.\n\n"
                  "New idle Token Kit session. No task has been submitted. The session title is a label, "
                  "not an instruction. Do not investigate, recover prior work, or execute tools until "
                  "the user gives an instruction. Do not compact. Then record their objective/scope in your STATE.md. "
                  f"Your workspace is {store.workspace}. Recovery command, only when needed: {resume_command}. "
                  "For this startup turn only, do not run tools, write checkpoints, delegate, or start any task. "
                  "Reply briefly: 'Token Kit trigger matrix loaded. Waiting for your instructions.' Then wait.")
    elif initial_prompt is not None:
        prompt = f"User's explicit task:\n{initial_prompt}\n\nDo not compact. Token Kit record: {resume_command}."
    if worker_ticket is not None:
        complete_command = shlex.join(["token-kit", "worker", "complete", str(store.path),
                                       "--agent", agent, "--ticket", worker_ticket])
        rollover_command = shlex.join(["token-kit", "worker", "request-rollover", str(store.path),
                                       "--agent", agent, "--ticket", worker_ticket,
                                       "--reason", "Context budget reached"])
        prompt += (f"\n\nThis managed worker owns ticket {worker_ticket}. "
                   "When finished, write out.md, checkpoint with evidence, then run "
                   f"{complete_command} and return. Before exhausting context, checkpoint, run "
                   f"{rollover_command}, and return. Never launch your own replacement. "
                   "The managed launcher confirms process exit and delivers your result to the parent.")
    prompt = brief(prompt, str(store.path), agent, pyramid=bundle["trigger_pyramid"])
    adapter = importlib.import_module(f"token_kit.adapters.{engine}")
    plan = adapter.prepare_launch(LaunchRequest(store.workspace, prompt, True, model, yolo=yolo,
                                               worker_task=str(store.path),
                                               managed_hooks=True,
                                               **({"non_interactive": True} if worker_ticket else {})))
    if dry_run:
        # Never print inherited auth-bearing environment values.
        print(json.dumps({"engine": engine, "argv": plan.argv, "cwd": str(plan.cwd),
                          "strict_no_compaction_requested": True, "yolo": yolo, "resume": bundle,
                          "startup": "idle" if idle else "prompt" if initial_prompt is not None else "resume",
                          "rollover_tokens": rollover_tokens, "context_window": context_window}, indent=2))
        return 0, {}
    if shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    claim_options = {"recovery_from": recovery_from}
    if continuation is not None:
        claim_options["continuation"] = True
    if worker_ticket is not None:
        claim_options.update(worker_ticket=worker_ticket, worker_model=model)
    run = store.claim_run(agent, engine, True, **claim_options)
    if continuation is not None:
        continuation.release()
    store.update_run(agent, run.name, yolo=yolo, model=model, rollover_tokens=rollover_tokens,
                     context_window=context_window,
                     max_rollovers=max_rollovers,
                     max_rollovers_explicit=(max_rollovers is not None if max_rollovers_explicit is None
                                            else max_rollovers_explicit),
                     startup="idle" if idle else "prompt" if initial_prompt is not None else "resume")
    if recovery_from is not None:
        store.update_run(agent, run.name, recovery_from=recovery_from,
                         recovery_status="reconciliation_required")
        atomic_bytes(run / "recovery-input.md", recovery_state)
        # The successor ID is only allocated by claim_run.  Rebuild the
        # recovery launch once so the client receives the exact validated
        # artifact path instead of a symbolic placeholder.
        prompt = prompt.replace(
            "the successor run's recovery-input.md",
            f"{run / 'recovery-input.md'}")
        plan = adapter.prepare_launch(LaunchRequest(store.workspace, prompt, True, model, yolo=yolo,
                                                   worker_task=str(store.path), managed_hooks=True,
                                                   **({"non_interactive": True} if worker_ticket else {})))
    runtime.initialize(store, agent, run, engine, rollover_tokens,
                       **({"context_window": context_window} if context_window is not None else {}))
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
        child = subprocess.Popen(plan.argv, cwd=plan.cwd, env=environment,
                                 **({"stdin": subprocess.DEVNULL} if worker_ticket else {}))
        store.update_run(agent, run.name, status="running", child_pid=child.pid,
                         child_identity=process_identity(child.pid))
        # Lifecycle hooks supervise every managed session.  A missing
        # threshold only disables token rollover; it does not disable startup,
        # compaction, or stop-hook supervision.
        rc, control = runtime.wait_segment(child, store, agent, run, stop_child)
        if worker_ticket is not None and not control.get("session_id"):
            control = {**control, "phase": "halted", "halt_kind": "hook_unconfirmed",
                       "reason": "Managed worker exited without a confirmed lifecycle handshake"}
        if control.get("phase") == "halted":
            store.update_run(agent, run.name, status="interrupted", ended_at=now(),
                             rollover_error=control.get("reason"),
                             halt_kind=control.get("halt_kind"))
            if worker_ticket is not None:
                lifecycle.finish_managed(store, agent, worker_ticket, run.name, returncode=rc)
            print(f"Token Kit: {control.get('reason')}", file=sys.stderr)
            return 75, control
        ready = control.get("phase") == "ready"
        if recovery_from is not None and (rc == 0 or ready):
            predecessor_path = store.safe(store.agent_path(agent) / "runs" / recovery_from / "run.json")
            predecessor_record = read_json(predecessor_path)
            successor_record = read_json(store.safe(run / "run.json"))
            if (any(record.get("status") != "reconciled"
                    for record in store.recovery_ancestors(agent, run.name))
                    or successor_record.get("recovery_completed") is not True):
                reason = (f"Recovery client exited successfully, but predecessor run {recovery_from} "
                          "is still not reconciled; refusing a false success")
                store.update_run(agent, run.name, status="interrupted", ended_at=now(),
                                 recovery_error=reason, rollover_error=reason)
                control = {"phase": "halted", "halt_kind": "recovery_unreconciled",
                           "reason": reason, "recovery_from": recovery_from}
                if worker_ticket is not None:
                    lifecycle.finish_managed(store, agent, worker_ticket, run.name, returncode=rc)
                print(f"Token Kit: {reason}", file=sys.stderr)
                return 75, control
        # An exited process does not prove that its external jobs finished.
        store.update_run(agent, run.name, status="exited" if rc == 0 or ready else "interrupted",
                         exit_code=rc, ended_at=now(), rollover_checkpoint=control.get("checkpoint"),
                         recovery_from=recovery_from)
        if ready and worker_ticket is None:
            store.reconcile_planned_rollover(agent, run.name)
        if worker_ticket is not None:
            worker = lifecycle.finish_managed(store, agent, worker_ticket, run.name,
                                               returncode=rc, rollover_ready=ready)
            if worker.get("phase") == "completed":
                control = {**control, "phase": "completed"}
            elif worker.get("phase") == "stopped" and rc == 0 and not ready:
                control = {**control, "phase": "stopped"}
                ledger.refresh(store)
                return 0, control
            elif worker.get("segment_ready") is True:
                control = {**control, "phase": "ready", "checkpoint": worker["checkpoint"]}
                ready = True
            if worker.get("phase") != "completed" and worker.get("segment_ready") is not True:
                control = {**control, "phase": "halted", "halt_kind": "worker_incomplete",
                           "reason": "Managed worker exited without a verified completed handoff"}
                ledger.refresh(store)
                return 75, control
        ledger.refresh(store)
        return (0 if ready else rc if rc >= 0 else 128 - rc), control
    except KeyboardInterrupt:
        if child is not None:
            stop_child(child)
        store.update_run(agent, run.name, status="interrupted", ended_at=now())
        if worker_ticket is not None:
            lifecycle.finish_managed(store, agent, worker_ticket, run.name, returncode=130)
        return 130, {}
    except (OSError, ValueError):
        if child is not None:
            stop_child(child)
        try:
            store.update_run(agent, run.name, status="interrupted", ended_at=now())
            if worker_ticket is not None:
                lifecycle.finish_managed(store, agent, worker_ticket, run.name, returncode=2)
        except (OSError, ValueError):
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
    start.add_argument("--prompt", help="explicit initial task; otherwise a new named session opens idle")
    start.add_argument("--task", type=Path, help="continue an existing task instead of creating one")
    start.add_argument("--workspace", type=Path, help="source directory (default: current directory)")
    start.add_argument("--root", type=Path, help="session root (default: ~/.config/token_kit; honors XDG_CONFIG_HOME)")
    start.add_argument("--engine", choices=("claude", "codex"), default="claude")
    start.add_argument("--model")
    start.add_argument("--install-project", action="store_true", help="persist shared project instructions (opt-in)")
    start.add_argument("--codegraph", action="store_true", help="configure an already installed CodeGraph")
    start.add_argument("--yolo", action="store_true", help="bypass client permission checks")
    start.add_argument("--dry-run", action="store_true", help="preview without writing or launching")
    start.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                       help="rollover target as an integer percentage from 1 through 99")
    start.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit,
                       help="rollover target: absolute tokens (e.g. 500k) or 1-99 percent of the reported context window; default: disabled")
    start.add_argument("--context-window", type=parse_context_window, help="override context window in tokens (e.g. 200k)")
    add_restart_budget(start)
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
    picker = commands.add_parser("pick", help="fuzzy task picker; resume the selected task")
    picker.add_argument("--print", dest="print_command", action="store_true",
                        help="print the resume command without launching")
    picker.add_argument("words", nargs="*")
    picker.add_argument("--root", type=Path, default=default_root())
    picker.add_argument("--engine", choices=("codex", "claude"), help="default: latest run engine, or claude")
    picker.add_argument("--model", help="override the latest run's model")
    picker.add_argument("--select", type=int, help="explicit candidate number (required without a terminal)")
    picker.add_argument("--limit", type=int, default=20, help="maximum candidates shown (default: 20)")
    picker.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                        help="rollover target as an integer percentage from 1 through 99")
    picker.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
    picker.add_argument("--yolo", action=argparse.BooleanOptionalAction, default=None,
                        help="override the latest run's permission setting")
    picker.add_argument("--context-window", type=parse_context_window, help="override context window in tokens (e.g. 200k)")
    add_restart_budget(picker)
    continuation = commands.add_parser("continue", help="safely hand off and continue a saved task")
    continuation.add_argument("words", nargs="*")
    continuation.add_argument("--task", type=Path, help="exact saved task path")
    continuation.add_argument("--root", type=Path, default=default_root())
    continuation.add_argument("--print", dest="print_command", action="store_true")
    continuation.add_argument("--select", type=int)
    continuation.add_argument("--limit", type=int, default=20)
    continuation.add_argument("--engine", choices=("codex", "claude"))
    continuation.add_argument("--model")
    continuation.add_argument("--yolo", action=argparse.BooleanOptionalAction, default=None)
    continuation.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage)
    continuation.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
    continuation.add_argument("--context-window", type=parse_context_window, help="override context window in tokens (e.g. 200k)")
    add_restart_budget(continuation)
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
    agent.add_argument("--parent", default="coordinator", help="logical parent (default: coordinator)")
    worker = commands.add_parser("worker", help="durable worker attempts: prepare, bind, request, reconcile")
    actions = worker.add_subparsers(dest="worker_action", required=True)
    for action in ("prepare", "bind", "request-rollover", "stopped", "retire", "complete", "status"):
        command = actions.add_parser(action)
        command.add_argument("task", type=Path)
        command.add_argument("--agent", required=True)
        if action in ("bind", "request-rollover", "stopped", "retire", "complete"):
            command.add_argument("--ticket", required=True)
        if action == "prepare":
            assignment = command.add_mutually_exclusive_group()
            assignment.add_argument("--brief", help="create a new logical worker with this assignment")
            assignment.add_argument("--assignment-file", type=Path,
                                    help="create a new logical worker from this assignment file")
            command.add_argument("--parent", help="parent for a new logical worker (default: coordinator)")
            command.add_argument("--engine", choices=("claude", "codex"))
            command.add_argument("--model")
            command.add_argument("--context-window", type=parse_context_window, help="override context window in tokens")
            command.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                                 help="rollover target as an integer percentage from 1 through 99")
            command.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
            command.add_argument("--owner-agent", default=os.environ.get("TOKEN_KIT_AGENT"))
            command.add_argument("--owner-run", default=os.environ.get("TOKEN_KIT_RUN"))
        elif action == "bind":
            command.add_argument("--native-id", required=True)
        elif action == "request-rollover":
            command.add_argument("--reason", required=True)
        elif action == "retire":
            command.add_argument("--note", required=True, help="record inspected operations, outcomes, and no live or uncertain operations")
            command.add_argument("--operations-reconciled", action="store_true")
            command.add_argument("--recovery-agent", default=os.environ.get("TOKEN_KIT_AGENT"))
            command.add_argument("--recovery-run", default=os.environ.get("TOKEN_KIT_RUN"))
        elif action == "stopped":
            command.add_argument("--note", required=True, help="confirm native closure and reconcile external operations; does not kill anything")
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
        elif name == "resume":
            command.add_argument("--full", action="store_true", help="include full recovery records")
        elif name == "launch":
            command.add_argument("--ticket", help="consume a reserved worker attempt for a managed Claude launch")
            command.add_argument("--engine", choices=("claude", "codex"), required=True)
            command.add_argument("--model")
            command.add_argument("--context-window", type=parse_context_window, help="override context window in tokens")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--yolo", action="store_true",
                                 help="bypass client permission checks (Codex also disables sandboxing)")
            command.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                                 help="rollover target as an integer percentage from 1 through 99")
            command.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
            add_restart_budget(command)
        elif name == "send":
            command.add_argument("text", help="literal text, or - to read stdin")
        elif name == "close-run":
            command.add_argument("run_id")
            command.add_argument("--note", required=True)
    status = commands.add_parser("status", help="show agents and their runs without loading transcripts")
    status.add_argument("task", type=Path)
    status.add_argument("--full", action="store_true", help="include full run and worker history")
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
        if args.command in ("pick", "continue"):
            return pick(args)
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
            print(store.add_agent(args.name, args.assignment_file.read_text(encoding="utf-8"), args.parent))
        elif args.command == "worker":
            action = args.worker_action
            if action == "prepare":
                assignment = args.brief
                if args.assignment_file is not None:
                    assignment = args.assignment_file.read_text(encoding="utf-8")
                if assignment is not None:
                    try:
                        store.add_agent(args.agent, assignment, args.parent)
                    except FileExistsError:
                        raise ValueError(f"Agent {args.agent} already exists; omit --brief/--assignment-file to prepare it") from None
                elif args.parent is not None:
                    raise ValueError("--parent requires --brief or --assignment-file to create a new worker")
                result = lifecycle.prepare(store, args.agent, engine=args.engine, model=args.model,
                                           threshold=args.rollover_tokens, owner_agent=args.owner_agent,
                                           owner_run=args.owner_run,
                                           **({"context_window": args.context_window} if args.context_window is not None else {}))
            elif action == "bind":
                result = lifecycle.bind(store, args.agent, args.ticket, args.native_id)
            elif action == "request-rollover":
                result = lifecycle.request(store, args.agent, args.ticket, args.reason)
            elif action == "complete":
                result = lifecycle.request(store, args.agent, args.ticket, "Assignment complete", complete=True)
            elif action == "retire":
                result = lifecycle.retire(store, args.agent, args.ticket, args.note,
                                          operations_reconciled=args.operations_reconciled,
                                          recovery_agent=args.recovery_agent, recovery_run=args.recovery_run)
            elif action == "stopped":
                result = lifecycle.stopped(store, args.agent, args.ticket, args.note)
            else:
                result = lifecycle.inspect(store, args.agent)
            # Keep historical events on disk, not in every coordinator prompt.
            if result and "worker" in result:
                result["worker"] = {key: value for key, value in result["worker"].items() if key not in ("events", "history")}
            elif result:
                result = {key: value for key, value in result.items() if key not in ("events", "history")}
            print(json.dumps(result, indent=2))
        elif args.command == "checkpoint":
            print(store.checkpoint(args.agent, args.evidence, args.incorporated))
        elif args.command == "resume":
            bundle = store.resume_bundle(args.agent)
            print(json.dumps(bundle, indent=2) if args.full else display.dumps(display.resume_view(bundle, store.path)))
        elif args.command == "send":
            print(store.send(args.agent, sys.stdin.read() if args.text == "-" else args.text))
        elif args.command == "close-run":
            store.close_run(args.agent, args.run_id, args.note)
        elif args.command == "launch":
            return launch(store, args.agent, args.engine, args.model, args.dry_run, args.yolo,
                          args.rollover_tokens, args.max_rollovers,
                          max_rollovers_explicit=args.max_rollovers_explicit, worker_ticket=args.ticket,
                          **({"context_window": args.context_window} if args.context_window is not None else {}))
        elif args.command == "ledger":
            print(ledger.refresh(store))
        elif args.command == "status":
            agents = []
            for path in sorted((store.path / "agents").iterdir()):
                if (path / "agent.json").is_file():
                    worker = lifecycle.inspect(store, path.name)
                    agents.append({"agent": read_json(path / "agent.json"),
                                   "worker": worker,
                                   "runs": [read_json(p) for p in sorted((path / "runs").glob("*/run.json"))]})
            bundle = {"task": read_json(store.path / "task.json"), "agents": agents}
            print(json.dumps(bundle, indent=2) if args.full else display.dumps(display.status_view(bundle, store.path)))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"token-kit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
