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
from . import runtime
from .core import ledger, lifecycle
from .rollover import format_limit, parse_limit
from .timefmt import human, parse_iso


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
    if args.limit <= 0:
        raise ValueError("--limit must be positive")
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
    yolo = args.yolo if args.yolo is not None else previous.get("yolo", False)
    if not isinstance(yolo, bool):
        raise ValueError("invalid recorded permissions; specify --yolo or --no-yolo")
    if model is not None and not isinstance(model, str):
        raise ValueError("invalid recorded model; specify --model")
    command = ["token-kit", "run", "--task", task, "--engine", engine]
    if model and model != "unknown":
        command += ["--model", str(model)]
    if threshold is not None:
        command += _rollover_command(threshold)
    if yolo:
        command += ["--yolo"]
    if args.print_command:
        print(shlex.join(command))
        return 0
    print("Resuming: " + shlex.join(command), file=sys.stderr)
    return run(build_parser().parse_args(command[1:]))


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
    if args.max_rollovers < 0:
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
                          "compaction_recovery": args.max_rollovers > 0,
                          "rollover_tokens": args.rollover_tokens,
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
    startup_line("Structured compaction recovery: " +
                 (f"enabled (up to {args.max_rollovers} managed restart" +
                  ("s" if args.max_rollovers != 1 else "") + ")"
                  if args.max_rollovers else "disabled"))
    startup_line(f"Token ledger: {store.path / 'TOKEN_LEDGER.md'}")
    startup_line(f"Continue later: token-kit run --task {shlex.quote(str(store.path))} "
          f"--engine {args.engine}" + (f" --model {shlex.quote(args.model)}" if args.model else "")
          + (" --yolo" if args.yolo else "")
          + (" " + shlex.join([*_rollover_command(args.rollover_tokens),
                               "--max-rollovers", str(args.max_rollovers)])
             if args.rollover_tokens is not None else ""))
    return launch(store, "coordinator", args.engine, args.model, yolo=args.yolo,
                  rollover_tokens=args.rollover_tokens, max_rollovers=args.max_rollovers,
                  idle=idle, initial_prompt=args.prompt)


def exit_summary(store, agent, engine, model, yolo, threshold, max_rollovers, rc, control):
    """Report supervisor evidence, not a guessed explanation of client UI errors."""
    if threshold is not None:
        threshold = parse_limit(threshold)
    if control.get("phase") == "halted":
        outcome = "safety stop: " + str(control.get("reason", "inspect run records"))
    elif control.get("phase") == "ready":
        outcome = "rollover limit reached; checkpoint saved"
    elif rc == 130:
        outcome = "interrupted (130); reconcile unfinished operations"
    elif rc == 0:
        outcome = "client exited normally (0); this does not prove the task is complete"
    else:
        outcome = f"client/launcher failure ({rc}); inspect client output and run records"
    startup_line("Token Kit exit: " + outcome, heading=True)
    sample = control.get("sample") or {}
    context = sample.get("context_tokens")
    startup_line("Latest context: " + (f"{context:,} tokens" if context is not None else "unknown"))
    startup_line(f"Cumulative usage (including cached input and observed workers): {store.path / 'TOKEN_LEDGER.md'}")
    command = ["token-kit", "run" if agent == "coordinator" else "launch"]
    command += ["--task", str(store.path)] if agent == "coordinator" else [str(store.path), "--agent", agent]
    command += ["--engine", engine]
    effective_model = sample.get("current_model") or model
    if effective_model and effective_model != "unknown":
        command += ["--model", effective_model]
    if yolo:
        command += ["--yolo"]
    if threshold is not None:
        command += [*_rollover_command(threshold), "--max-rollovers", str(max_rollovers)]
    startup_line("Resume with Token Kit (checkpoints, not native transcript): " + shlex.join(command))
    return rc


def launch(store: Store, agent: str, engine: str, model: str | None = None,
           dry_run: bool = False, yolo: bool = False, rollover_tokens: int | str | None = None,
           max_rollovers: int = 10, *, idle: bool = False, initial_prompt: str | None = None) -> int:
    if rollover_tokens is not None:
        rollover_tokens = parse_limit(rollover_tokens)
    if max_rollovers < 0:
        raise ValueError("Invalid rollover limits")
    if engine == "codex" and not dry_run:
        runtime.ensure_codex_hooks(workspace=store.workspace)
    for segment in range(max_rollovers + 1):
        try:
            rc, control = _launch_segment(store, agent, engine, model, dry_run, yolo, rollover_tokens,
                                         idle=idle if segment == 0 else False,
                                         initial_prompt=initial_prompt if segment == 0 else None,
                                         allow_recovery=max_rollovers > 0)
        except (OSError, ValueError):
            if not dry_run:
                exit_summary(store, agent, engine, model, yolo, rollover_tokens, max_rollovers, 2, {})
            raise
        if control.get("phase") == "halted" and control.get("halt_kind") == "compaction":
            # A structured parent compaction halt is the sole halt eligible
            # for an automatic recovery segment.  It consumes the same
            # restart budget as a normal threshold rollover.
            if segment == max_rollovers:
                return exit_summary(store, agent, engine, model, yolo, rollover_tokens,
                                    max_rollovers, 75, control)
            print(f"Token Kit: compaction halted the session; starting recovery "
                  f"({segment + 1}/{max_rollovers}).", file=sys.stderr)
            continue
        if control.get("phase") != "ready":
            return rc if dry_run else exit_summary(store, agent, engine, model, yolo,
                                                   rollover_tokens, max_rollovers, rc, control)
        if segment == max_rollovers:
            print("Token Kit: rollover limit reached; checkpoint saved. Resume manually.", file=sys.stderr)
            return exit_summary(store, agent, engine, model, yolo, rollover_tokens, max_rollovers, 75, control)
        observed_model = (control.get("sample") or {}).get("current_model")
        if observed_model and observed_model != "unknown":
            model = observed_model
        print(f"Token Kit: checkpoint saved; restarting {engine} ({segment + 1}/{max_rollovers}).", file=sys.stderr)
    return 75


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
                    allow_recovery: bool = True) -> tuple[int, dict]:
    if rollover_tokens is not None:
        rollover_tokens = parse_limit(rollover_tokens)
    # A real managed launch may seed an old task's missing snapshot. Dry runs
    # remain read-only and use the repository fallback through resume_bundle.
    if not dry_run:
        store.trigger_pyramid(seed=True)
    bundle = store.resume_bundle(agent)
    recovery_from = None
    recovery_state = None
    if not dry_run and allow_recovery:
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
        close_command = shlex.join(["token-kit", "close-run", str(store.path), "--agent", agent,
                                    recovery_from, "--note",
                                    "Verified predecessor process, children, and external operations; recovery state reconciled."])
        prompt = (
            f"This is structured recovery of predecessor run {recovery_from}. Read the exact pre-recovery "
            f"working STATE at {store.agent_path(agent) / 'STATE.md'} captured in the successor run's "
            "recovery-input.md before editing "
            "STATE.md, and compare it with the "
            f"committed snapshot {bundle['checkpoint']}/STATE.md. Read the predecessor run record at "
            f"{predecessor_path}, prior run metadata ({compact_text(prior_runs, 3000)}), child lifecycle records ({compact_text(children, 2000)}), "
            f"and the bounded external-operation summary ({compact_text(external, 2000)}).{historical_hint} Verify every native "
            "child and external operation before doing ordinary product work. Do not resubmit an external job, "
            "replay an uncertain operation, or replace an unknown native worker. A bounded recovery-only audit "
            "worker with a fresh logical ID is allowed if needed for verification. Reconciliation is the only "
            "allowed work until verification is complete. Update STATE.md, commit a fresh checkpoint, then run "
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
    prompt = brief(prompt, str(store.path), agent, pyramid=bundle["trigger_pyramid"])
    adapter = importlib.import_module(f"token_kit.adapters.{engine}")
    plan = adapter.prepare_launch(LaunchRequest(store.workspace, prompt, True, model, yolo=yolo,
                                               worker_task=str(store.path),
                                               managed_hooks=True))
    if dry_run:
        # Never print inherited auth-bearing environment values.
        print(json.dumps({"engine": engine, "argv": plan.argv, "cwd": str(plan.cwd),
                          "strict_no_compaction_requested": True, "yolo": yolo, "resume": bundle,
                          "startup": "idle" if idle else "prompt" if initial_prompt is not None else "resume",
                          "rollover_tokens": rollover_tokens}, indent=2))
        return 0, {}
    if shutil.which(plan.argv[0], path=plan.env.get("PATH")) is None:
        raise ValueError(f"Executable not found: {plan.argv[0]}")
    run = store.claim_run(agent, engine, True, recovery_from=recovery_from)
    store.update_run(agent, run.name, yolo=yolo, model=model, rollover_tokens=rollover_tokens,
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
                                                   worker_task=str(store.path), managed_hooks=True))
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
        # Lifecycle hooks supervise every managed session.  A missing
        # threshold only disables token rollover; it does not disable startup,
        # compaction, or stop-hook supervision.
        rc, control = runtime.wait_segment(child, store, agent, run, stop_child)
        if control.get("phase") == "halted":
            store.update_run(agent, run.name, status="interrupted", ended_at=now(),
                             rollover_error=control.get("reason"),
                             halt_kind=control.get("halt_kind"))
            print(f"Token Kit: {control.get('reason')}", file=sys.stderr)
            return 75, control
        ready = control.get("phase") == "ready"
        if recovery_from is not None and (rc == 0 or ready):
            predecessor_path = store.safe(store.agent_path(agent) / "runs" / recovery_from / "run.json")
            predecessor_record = read_json(predecessor_path)
            successor_record = read_json(store.safe(run / "run.json"))
            if (predecessor_record.get("status") != "reconciled"
                    or successor_record.get("recovery_completed") is not True):
                reason = (f"Recovery client exited successfully, but predecessor run {recovery_from} "
                          "is still not reconciled; refusing a false success")
                store.update_run(agent, run.name, status="interrupted", ended_at=now(),
                                 recovery_error=reason, rollover_error=reason)
                control = {"phase": "halted", "halt_kind": "recovery_unreconciled",
                           "reason": reason, "recovery_from": recovery_from}
                print(f"Token Kit: {reason}", file=sys.stderr)
                return 75, control
        # An exited process does not prove that its external jobs finished.
        store.update_run(agent, run.name, status="exited" if rc == 0 or ready else "interrupted",
                         exit_code=rc, ended_at=now(), rollover_checkpoint=control.get("checkpoint"),
                         recovery_from=recovery_from)
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
    for action in ("prepare", "bind", "request-rollover", "stopped", "complete", "status"):
        command = actions.add_parser(action)
        command.add_argument("task", type=Path)
        command.add_argument("--agent", required=True)
        if action in ("bind", "request-rollover", "stopped", "complete"):
            command.add_argument("--ticket", required=True)
        if action == "prepare":
            assignment = command.add_mutually_exclusive_group()
            assignment.add_argument("--brief", help="create a new logical worker with this assignment")
            assignment.add_argument("--assignment-file", type=Path,
                                    help="create a new logical worker from this assignment file")
            command.add_argument("--parent", help="parent for a new logical worker (default: coordinator)")
            command.add_argument("--engine", choices=("claude", "codex"))
            command.add_argument("--model")
            command.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                                 help="rollover target as an integer percentage from 1 through 99")
            command.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
            command.add_argument("--owner-agent", default=os.environ.get("TOKEN_KIT_AGENT"))
            command.add_argument("--owner-run", default=os.environ.get("TOKEN_KIT_RUN"))
        elif action == "bind":
            command.add_argument("--native-id", required=True)
        elif action == "request-rollover":
            command.add_argument("--reason", required=True)
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
        elif name == "launch":
            command.add_argument("--engine", choices=("claude", "codex"), required=True)
            command.add_argument("--model")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--yolo", action="store_true",
                                 help="bypass client permission checks (Codex also disables sandboxing)")
            command.add_argument("--rollover-perc", dest="rollover_tokens", type=parse_rollover_percentage,
                                 help="rollover target as an integer percentage from 1 through 99")
            command.add_argument("--rollover-tokens", "--rollover-at", dest="rollover_tokens", type=parse_limit)
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
        if args.command == "pick":
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
                                           owner_run=args.owner_run)
            elif action == "bind":
                result = lifecycle.bind(store, args.agent, args.ticket, args.native_id)
            elif action == "request-rollover":
                result = lifecycle.request(store, args.agent, args.ticket, args.reason)
            elif action == "complete":
                result = lifecycle.request(store, args.agent, args.ticket, "Assignment complete", complete=True)
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
                    worker = lifecycle.inspect(store, path.name)
                    agents.append({"agent": read_json(path / "agent.json"),
                                   "worker": {key: value for key, value in worker.items()
                                              if key not in ("events", "history")} if worker else None,
                                   "runs": [read_json(p) for p in sorted((path / "runs").glob("*/run.json"))]})
            print(json.dumps({"task": read_json(store.path / "task.json"), "agents": agents}, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"token-kit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
