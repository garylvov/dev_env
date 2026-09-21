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
        return (f"STOP — lane {lane} has been recycled {number} times "
                f"(cap {cfg.max_respawns}). Do NOT respawn it again. Read "
                f"{lane}/out.md yourself and decide what to do; the lane is "
                "not converging.")
    return (f"lane {lane} was recycled at {calls.group(1) if calls else '?'} calls "
            f"(agent {agent.group(1) if agent else '?'}; respawn {number} of "
            f"{cfg.max_respawns}); respawn a fresh agent on the same in.md — "
            f"{lane}/in.md; its out.md holds the RESUME block, whose first line "
            "is the single next command.")


def handle(cfg: Config, event_json: dict) -> str:
    """The whole decision. Returns the additionalContext text, or "" for silence."""
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
        text = handle(cfg, json.loads(raw))
        if not text:
            return 0
        event = json.loads(raw).get("hook_event_name")
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
