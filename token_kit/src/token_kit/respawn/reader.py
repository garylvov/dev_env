"""The READER for the directive a lane recycler writes.

THE DEFECT IT CLOSES: a recycler appends a row to <LANE_DIR>/RESPAWN_REQUEST.md
when it recycles a lane at its call floor, and nothing ever read that file. A
stamped-but-unread directive is the same defect as a writer with no reader.

WHY NOT SubagentStop -- it is the obvious event and the WRONG one. The installed
CLI (2.1.278) describes its output as "additionalContext is non-error feedback
delivered to the SUBAGENT; the subagent continues so it can act on it", so a
SubagentStop hook restarts the dying lane instead of telling the main thread
anything. The main thread is reachable from PostToolUse and Stop.

SO IT IS TWO ENTRY POINTS, ONE READER, dispatched on hook_event_name:
  PostToolUse (matcher "Agent") -- fires in the MAIN thread when a spawn
    returns: REGISTER the lane dir named in the spawn prompt, and report any
    unconsumed respawn row. A foreground agent's row is already on disk; a
    BACKGROUND agent's tool_result comes back immediately, so registration is
    the only useful half for those -- hence:
  Stop -- fires at the end of every main-thread turn and sweeps the registry,
    which is what actually catches a background lane that died minutes later.

Append-only throughout: RESPAWN_REQUEST.md is never touched, consumption is an
appended row in a separate ledger, and the registry is an appended TSV.
Bounded at `max_respawns`: past the cap the message flips from "respawn" to
"stop, read out.md yourself".

The hook FAILS OPEN, loudly: any internal error prints nothing (the call
proceeds) and appends a row to <state>/respawn_reader_errors.log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

LANE_RE = re.compile(r"LANE_DIR:[ \t]*(/\S+)")
CALLS_RE = re.compile(r"calls=(\d+)")
AGENT_RE = re.compile(r"agent=(\S+)")


class Config:
    """Dials: a TOML file or CLI arguments, and nothing else. No env dials --
    an ambient variable would decide this reader's behaviour by who exported
    what, which is exactly the bug the workspace key below closes."""

    def __init__(self, state_root=None, max_respawns=None, config_file=None,
                 request_name=None, ledger_name=None):
        self.max_respawns = 3
        self.request_name = "RESPAWN_REQUEST.md"
        self.ledger_name = "RESPAWN_CONSUMED.md"
        from token_kit import config as config_mod
        self.state_root = str(config_mod.state_home() / "respawn")
        # The handoff nudge. Its two dials live under [supervisor] with the
        # rollover's own, because they are the same decision seen from two
        # sides: the supervisor checks the file at the ceiling, this asks for
        # it to be current long before the ceiling. Empty registry = derive it.
        self.supervise_registry = ""
        self.nudge_min_calls = 0        # 0 = take the supervisor's default
        self.nudge_every_mins = 0

        if config_file:
            import tomllib
            with open(config_file, "rb") as fh:
                table = tomllib.load(fh)
            table = table.get("respawn", table)
            for key, value in table.items():
                if not hasattr(self, key):
                    raise SystemExit(f"respawn-reader: unknown config key: {key}")
                setattr(self, key, type(getattr(self, key))(value))

        if state_root:
            self.state_root = state_root
        if max_respawns:
            self.max_respawns = int(max_respawns)
        if request_name:
            self.request_name = request_name
        if ledger_name:
            self.ledger_name = ledger_name

    @property
    def registry(self) -> Path:
        return Path(self.state_root) / "respawn_registry.tsv"

    @property
    def nudge_ledger(self) -> Path:
        """Every nudge decision, appended. A nudge that left no row would be a
        thing the machinery did to a session with nobody able to audit it."""
        return Path(self.state_root) / "nudge_ledger.tsv"

    def dials(self) -> tuple[int, int]:
        """(min calls, minutes between nudges), from [supervisor] or defaults."""
        calls, mins = self.nudge_min_calls, self.nudge_every_mins
        if calls and mins:
            return calls, mins
        try:
            from token_kit.supervisor import config as supcfg
            sup = supcfg.load(None, {})
            return calls or sup.nudge_min_calls, mins or sup.nudge_every_mins
        except Exception:                      # noqa: BLE001 -- defaults will do
            return calls or 25, mins or 20


# ------------------------------------------------------------- the handoff nudge
#: The Stop hook's BLOCK contract, verified against the installed CLI's own
#: bundle rather than from memory: stdout carries `{"decision": "block",
#: "reason": "..."}`, the reason reaches the model as "Stop hook feedback", the
#: input carries `stop_hook_active` which a hook must honour ("check
#: stop_hook_active in the input and return success while it's true"), and
#: consecutive blocks are capped (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, default 8)
#: before the CLI overrides the hook and ends the turn anyway. So: one block,
#: never a loop, and never a block while stop_hook_active is true.
BLOCK_DECISION = "block"


def state_file_for(cfg: Config, cwd: str) -> Path | None:
    """The handoff file this session is judged against, or None for silence.

    Order: the state file the SUPERVISOR is watching for this directory (its
    registry is the only thing that knows, because the run directory is keyed
    by an irreversible hash of the path), else ./STATE.md when it exists. With
    neither, there is no handoff to nudge about and the hook says nothing at
    all: a nudge about a file nobody chose is noise.
    """
    if not cwd:
        return None
    registry = Path(cfg.supervise_registry) if cfg.supervise_registry else None
    if registry is None:
        try:
            from token_kit.supervisor import config as supcfg
            registry = supcfg.Config().registry
        except Exception:                      # noqa: BLE001
            registry = None
    if registry is not None:
        try:
            rows = registry.read_text(errors="replace").splitlines()
        except OSError:
            rows = []
        for line in reversed(rows):            # newest wins
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1].rstrip("/") == cwd.rstrip("/"):
                candidate = Path(parts[2])
                if candidate.is_file():
                    return candidate
    fallback = Path(cwd) / "STATE.md"
    return fallback if fallback.is_file() else None


def tool_calls_since(transcript: str, since: float) -> int:
    """Main thread tool calls in the CLI's own transcript after `since`.

    Sidechain records are a SUBAGENT's calls and are not the main thread's to
    answer for. Streamed line by line, and a line with no `tool_use` in it is
    never parsed: this runs at the end of every turn.
    """
    from datetime import timezone

    if not transcript:
        return 0
    cutoff = datetime.fromtimestamp(since, tz=timezone.utc)
    count = 0
    try:
        with open(transcript, "r", errors="replace") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                when = rec.get("timestamp")
                try:
                    at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    continue
                if at <= cutoff:
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    count += sum(1 for b in content if isinstance(b, dict)
                                 and b.get("type") == "tool_use")
    except OSError:
        return 0
    return count


def last_nudge(cfg: Config, session: str) -> float:
    """When this session was last BLOCKED, as a POSIX time, or 0."""
    try:
        rows = cfg.nudge_ledger.read_text(errors="replace").splitlines()
    except OSError:
        return 0.0
    for line in reversed(rows):
        parts = line.split("\t")
        if len(parts) >= 6 and parts[1] == session and parts[5] == "blocked":
            try:
                return datetime.fromisoformat(parts[0]).timestamp()
            except ValueError:
                return 0.0
    return 0.0


def nudge_row(cfg: Config, session, cwd, state, calls, decision) -> None:
    try:
        cfg.nudge_ledger.parent.mkdir(parents=True, exist_ok=True)
        with cfg.nudge_ledger.open("a") as fh:
            fh.write(f"{stamp()}\t{session}\t{cwd}\t{state}\t{calls}\t{decision}\n")
    except OSError:
        pass


NUDGE_TEXT = (
    "Bring the handoff file current before you stop: {state} was last written "
    "{minutes} minutes ago and you have made {calls} tool calls since. Write "
    "what you DECIDED, what is IN FLIGHT, and what to do NEXT. If its title or "
    "Summary no longer describes the work, run token-kit-task retitle. Then "
    "stop.")


def nudge(cfg: Config, event_json: dict) -> str:
    """The reason to block the stop with, or "" for silence.

    THE DEFECT IT CLOSES: nothing checked that the handoff was current between
    rollovers. The supervisor only ASKS at the soft ceiling, which is hours of
    work later and may be the first time anyone looked.
    """
    from token_kit.core.context import shared_workflow
    if shared_workflow(event_json.get("cwd")):
        return ""
    if event_json.get("stop_hook_active"):
        return ""                              # honour the CLI's loop guard
    cwd = str(event_json.get("cwd") or "")
    state = state_file_for(cfg, cwd)
    if state is None:
        return ""                              # no handoff chosen: say nothing
    session = str(event_json.get("session_id") or "none")
    min_calls, every_mins = cfg.dials()
    try:
        mtime = state.stat().st_mtime
    except OSError:
        return ""
    calls = tool_calls_since(str(event_json.get("transcript_path") or ""), mtime)
    now = time.time()
    if calls < min_calls:
        nudge_row(cfg, session, cwd, state, calls, "below_call_floor")
        return ""
    since_last = now - last_nudge(cfg, session)
    if since_last < every_mins * 60:
        nudge_row(cfg, session, cwd, state, calls, "rate_limited")
        return ""
    nudge_row(cfg, session, cwd, state, calls, "blocked")
    return NUDGE_TEXT.format(state=state, calls=calls,
                             minutes=max(0, int((now - mtime) // 60)))


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rows_of(path: Path) -> int:
    """Non-blank rows, 0 if absent."""
    try:
        return sum(1 for line in path.read_text(errors="replace").splitlines()
                   if line.strip())
    except OSError:
        return 0


def lane_from_prompt(prompt: str) -> str:
    match = LANE_RE.search(prompt or "")
    return match.group(1) if match else ""


def workspace_of(event_json: dict) -> str:
    """The WORK this session belongs to, which outlives the session.

    THE DEFECT THIS CLOSES: the sweep used to key on `session_id` alone. A
    session that rolls over gets a NEW session id, so every lane it spawned
    before the rollover fell out of the sweep and a background lane that died
    afterwards was never reported -- silently, because the registry rows were
    still there and simply did not match. Registration therefore records the
    workspace as well, and the sweep matches EITHER key.

    The key is the event's own `cwd`, which every hook event carries and which
    a rollover preserves (the supervisor relaunches in the same directory).
    It is deliberately NOT an environment variable: an ambient one would make
    two unrelated sessions on one machine sweep each other's lanes, and would
    make this reader's behaviour depend on who exported what.
    Empty means "no workspace key", and an empty key never matches anything.
    """
    return str(event_json.get("cwd") or "").rstrip("/")


def register(cfg: Config, session: str, lane: str, workspace: str = "") -> None:
    """Append-only; duplicates are fine, reads dedup."""
    if not lane or not Path(lane).is_dir():
        return
    cfg.registry.parent.mkdir(parents=True, exist_ok=True)
    with cfg.registry.open("a") as fh:
        fh.write(f"{stamp()}\t{session}\t{lane}\t{workspace}\n")


def registered_lanes(cfg: Config, session: str, workspace: str = "") -> list[str]:
    """Lane dirs to sweep, in order, deduplicated.

    A row matches on its session id OR on its workspace, so the sweep survives
    a rollover. Rows written before this column existed have three fields and
    still match on the session. Registry rows only: no directory walk, no
    process scan."""
    out: list[str] = []
    try:
        lines = cfg.registry.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        row_workspace = parts[3].rstrip("/") if len(parts) > 3 else ""
        same_session = parts[1] == session
        same_workspace = bool(workspace) and row_workspace == workspace
        if (same_session or same_workspace) and parts[2] not in out:
            out.append(parts[2])
    return out


def report_lane(cfg: Config, event: str, session: str, lane: str) -> str:
    """The message for ONE lane with an unconsumed row, or "".

    Consumption is an APPENDED ledger row, never a rewrite of the request file,
    so the same row is reported exactly once however often Stop fires (it fired
    TWICE in one measured `-p` run).
    """
    request = Path(lane) / cfg.request_name
    ledger = Path(lane) / cfg.ledger_name
    if not request.is_file():
        return ""
    n_request, n_consumed = rows_of(request), rows_of(ledger)
    if n_request <= n_consumed:
        return ""

    last = [l for l in request.read_text(errors="replace").splitlines() if l.strip()][-1]
    calls = CALLS_RE.search(last)
    agent = AGENT_RE.search(last)
    number = n_consumed + 1
    with ledger.open("a") as fh:
        fh.write(f"{stamp()}\tconsumed_by={event}\tsession={session}"
                 f"\trespawn_no={number}\n")

    if number > cfg.max_respawns:
        return (f"STOP: lane {lane} has been recycled {number} times "
                f"(cap {cfg.max_respawns}). Do NOT respawn it again. Read "
                f"{lane}/out.md yourself and decide what to do; the lane is "
                "not converging.")
    return (f"lane {lane} was recycled at {calls.group(1) if calls else '?'} calls "
            f"(agent {agent.group(1) if agent else '?'}; respawn {number} of "
            f"{cfg.max_respawns}); respawn a fresh agent on the same in.md; "
            f"{lane}/in.md; its out.md holds the RESUME block, whose first line "
            "is the single next command.")


def handle(cfg: Config, event_json: dict) -> str:
    """The whole decision. Returns the additionalContext text, or "" for silence."""
    from token_kit.core.context import shared_workflow
    if shared_workflow(event_json.get("cwd")):
        return ""
    event = event_json.get("hook_event_name") or ""
    if not event:
        return ""
    # A Stop hook that speaks while it is already re-running is how a turn loops
    # forever. One pass only.
    if event_json.get("stop_hook_active"):
        return ""
    session = event_json.get("session_id") or "none"
    workspace = workspace_of(event_json)
    Path(cfg.state_root).mkdir(parents=True, exist_ok=True)

    messages: list[str] = []
    if event == "PostToolUse":
        if event_json.get("tool_name") not in ("Agent", "Task"):
            return ""
        lane = lane_from_prompt((event_json.get("tool_input") or {}).get("prompt", ""))
        register(cfg, session, lane, workspace)
        if lane:
            messages.append(report_lane(cfg, event, session, lane))
    elif event == "Stop":
        for lane in registered_lanes(cfg, session, workspace):
            messages.append(report_lane(cfg, event, session, lane))
    else:
        # SubagentStop lands here on purpose: its additionalContext reaches the
        # SUBAGENT, so the reader must never speak there.
        return ""
    return "\n".join(m for m in messages if m)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    import argparse
    parser = argparse.ArgumentParser(prog="respawn-reader", description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--state-root", default=None)
    parser.add_argument("--max", dest="max_respawns", default=None)
    args = parser.parse_args(argv)

    cfg = None
    try:
        cfg = Config(state_root=args.state_root, max_respawns=args.max_respawns,
                     config_file=args.config)
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        text = handle(cfg, payload)
        event = payload.get("hook_event_name")
        reason = nudge(cfg, payload) if event == "Stop" else ""
        if reason:
            # A BLOCK, not additionalContext: the whole point is that the turn
            # does not end until the handoff is current. One block only, and
            # never while stop_hook_active is true, so no turn can loop here.
            print(json.dumps({"decision": BLOCK_DECISION,
                              "reason": "\n".join([t for t in (text, reason) if t])}))
            return 0
        if not text:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": text}}))
        return 0
    except Exception as exc:                      # FAIL OPEN, LOUDLY
        try:
            from token_kit import config as config_mod
            root = Path(cfg.state_root if cfg else config_mod.state_home() / "respawn")
            root.mkdir(parents=True, exist_ok=True)
            with (root / "respawn_reader_errors.log").open("a") as fh:
                fh.write(f"{stamp()}\terror\t{type(exc).__name__}: {exc}\n")
        except OSError:
            pass
        print(f"respawn-reader: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
