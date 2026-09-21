"""token-kit-supervise -- external rollover supervisor for a long-running
interactive Claude Code session.

    supervise launch [--config F] [dials] [-- <claude flags>]
    supervise watch  --session-pid N [--transcript F] [--status-override S]
    supervise once   --session-pid N [...]      one bounded pass (step mode)
    supervise status
    supervise stop

It watches the session's own transcript, asks the session to bring STATE.md
current at SOFT, lets in-flight work drain, and rolls the session over at HARD.
There is NO global token cap: this is a rollover supervisor, not a rationer.

Every state change appends a ROW to <campaign>/.ccsup/ccsup.log. Nothing here
rewrites a file in place; every hold or refusal is a row with a reason.
The seed reaches a relaunched session as ONE short CLI argument naming the seed
FILE -- never `tmux send-keys` into a prompt.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from token_kit.supervisor import config as cfgmod
from token_kit.supervisor import session as S


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def row(cfg, event: str, detail: str = "") -> None:
    """Append one tab-separated row. Append-only, one open, no rewrite."""
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    with cfg.log.open("a") as fh:
        fh.write(f"{stamp()}\t{event}\t{detail}\n")


def say(*msg) -> None:
    print("supervise:", *msg, file=sys.stderr)


def claim(marker: Path, payload: str) -> bool:
    """Claim a marker exclusively (O_EXCL). True only if WE claimed it."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{stamp()}\t{payload}\n")
    return True


# --------------------------------------------------------------------- actions
def soft_request(cfg, pid, tokens) -> None:
    """The SOFT channel: an appended request the session's own tick reads.

    Not send-keys, not a terminal write -- the session brings STATE.md current
    itself while its context is still cached.
    """
    Path(cfg.campaign_dir).mkdir(parents=True, exist_ok=True)
    with cfg.request.open("a") as fh:
        fh.write(f"## {stamp()}  SOFT threshold crossed\n")
        fh.write(f"- session pid: {pid}  context tokens: {tokens}"
                 f"  soft: {cfg.soft_tokens}  hard: {cfg.hard_tokens}\n")
        fh.write("- ACTION FOR THE SESSION: bring STATE.md current now, in this turn.\n")
        fh.write("- Rollover follows once in-flight work drains, or at HARD regardless.\n\n")
    say(f"SOFT {tokens}/{cfg.soft_tokens} -- asked session {pid} to bring STATE.md current")
    row(cfg, "soft_request",
        f"pid={pid} tokens={tokens} soft={cfg.soft_tokens} request={cfg.request}")


def _signal(cfg, pid: int, sig: str) -> None:
    if cfg.kill_cmd:
        subprocess.run([cfg.kill_cmd, sig, str(pid)], check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM if sig == "TERM" else signal.SIGKILL)
    except OSError:
        pass


def end_session(cfg, pid: int) -> bool:
    """SIGTERM, then SIGKILL after a bounded wait. Verified by START TIME.

    An exit code proves nothing here: whole jobs have ignored SIGTERM for
    minutes with every rank alive. The only evidence accepted is that the pid's
    kernel start time no longer matches the one the registry recorded.
    """
    if not S.is_live_session(cfg.claude_home, pid):
        return True
    _signal(cfg, pid, "TERM")
    waited = 0
    while waited < cfg.term_wait_secs:
        if not S.is_live_session(cfg.claude_home, pid):
            return True
        time.sleep(cfg.kill_poll_secs)
        waited += cfg.kill_poll_secs
    if S.is_live_session(cfg.claude_home, pid):
        row(cfg, "kill_escalated", f"pid={pid} after={waited}s signal=KILL")
        _signal(cfg, pid, "KILL")
        time.sleep(cfg.kill_poll_secs)
    return not S.is_live_session(cfg.claude_home, pid)


def rollover(cfg, pid, tokens, reason) -> bool:
    if not claim(cfg.run_dir / f"rolled.{pid}.marker", f"reason={reason} tokens={tokens}"):
        row(cfg, "rollover_skipped", f"pid={pid} reason=already_rolled")
        return True
    row(cfg, "rollover_begin", f"pid={pid} tokens={tokens} reason={reason}")
    if cfg.rollover_cmd:
        rc = subprocess.run([cfg.rollover_cmd, str(pid), str(tokens), str(reason)],
                            check=False).returncode
        row(cfg, "rollover_stub", f"pid={pid} rc={rc} cmd={cfg.rollover_cmd}")
        return True
    if not end_session(cfg, int(pid)):
        row(cfg, "rollover_failed", f"pid={pid} reason=session_still_alive_after_sigkill")
        say(f"ROLLOVER FAILED: pid {pid} still alive after SIGKILL")
        return False
    row(cfg, "rollover_ended", f"pid={pid}")
    # Release our watcher lock BEFORE relaunching, or the watcher we are about
    # to spawn either refuses (our heartbeat is still fresh) or has its lock
    # removed out from under it by our own exit.
    release_lock(cfg)
    launch_from_file(cfg)
    return True


# --------------------------------------------------------------------- launch
def seed_arg(seed: Path) -> str:
    """The initial prompt, as ONE short CLI argument pointing at the seed FILE.

    argv is not a place for a multi-line handoff, and send-keys is forbidden.
    """
    return (f"Rollover seed: read the file {seed} and do exactly what it says, "
            "starting now. Treat every claim in it as a lead to verify.")


def seed_text(cfg) -> str:
    c = cfg.campaign_dir
    return ("Rollover: previous session ended at context limit. Read, in order:\n"
            f"- {c}/AGENTS.md\n"
            f"- {c}/STATE.md   (the handoff)\n"
            f"- tail of {c}/PROMPTS.log\n"
            "Then continue from STATE.md. Treat every claim in it as a lead to verify.\n")


def resolve_session_pid(cfg, tmux_name: str) -> int | None:
    """The new session's pid: descend from the tmux pane pid, confirm in the registry."""
    deadline = time.time() + cfg.launch_pid_wait_secs
    while True:
        panes = subprocess.run(
            [cfg.tmux_bin, "list-panes", "-t", tmux_name, "-F", "#{pane_pid}"],
            capture_output=True, text=True, check=False).stdout.split()
        if panes:
            for pid in S.subtree(panes[0]):
                if S.is_live_session(cfg.claude_home, pid):
                    return pid
        if time.time() >= deadline:
            return None
        time.sleep(1)


def launch_from_file(cfg) -> int | None:
    flags = ""
    if cfg.flags_file.is_file():
        flags = cfg.flags_file.read_text().strip()
    name = f"{cfg.tmux_prefix}-{datetime.now().strftime('%H%M%S')}"
    seed = cfg.run_dir / f"seed.{name}.md"
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    seed.write_text(seed_text(cfg))

    cmd = f"{cfg.claude_bin} {flags}".strip()
    if cfg.seed_as_arg:
        cmd = f"{cmd} {shlex.quote(seed_arg(seed))}"
    # Detached tmux: the server is reparented to init, so it outlives us.
    rc = subprocess.run([cfg.tmux_bin, "new-session", "-d", "-s", name, cmd],
                        check=False).returncode
    if rc != 0:
        row(cfg, "launch_failed", f"name={name} rc={rc}")
        say(f"tmux new-session failed (rc {rc})")
        return None
    row(cfg, "launch",
        f"tmux={name} flags=[{flags}] seed={seed} seed_as_arg={int(cfg.seed_as_arg)}")
    say(f"launched tmux session {name} (flags: {flags}); seed: {seed}")

    pid = resolve_session_pid(cfg, name)
    if pid is None:
        row(cfg, "launch_pid_unresolved", f"tmux={name} waited={cfg.launch_pid_wait_secs}s")
        say("could not resolve the new session's pid; start the watcher by hand")
        return None
    cfg.pid_file.write_text(f"{pid}\n")
    row(cfg, "launch_pid", f"tmux={name} pid={pid}")
    if cfg.autowatch:
        out = (cfg.run_dir / "watch.out").open("a")
        cmdline = [cfg.supervisor_cmd, "watch", "--session-pid", str(pid)]
        if getattr(cfg, "config_file", ""):
            cmdline += ["--config", str(cfg.config_file)]
        child = subprocess.Popen(
            cmdline, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
            start_new_session=True)
        row(cfg, "watch_spawned", f"pid={pid} watcher={child.pid}")
        say(f"watcher spawned for pid {pid}")
    else:
        say(f"start the watcher with: {cfg.supervisor_cmd} watch --session-pid {pid}")
    return pid


# ---------------------------------------------------------------------- watch
def acquire_lock(cfg, pid) -> bool:
    """One watcher per campaign. An atomic mkdir is the lock; a stale heartbeat
    is a takeover, and both outcomes are a ledger row with a reason."""
    try:
        cfg.lock_dir.mkdir(parents=True)
        return True
    except FileExistsError:
        pass
    if cfg.heartbeat.is_file():
        age = time.time() - cfg.heartbeat.stat().st_mtime
        if age < cfg.heartbeat_stale_secs:
            row(cfg, "watch_refused",
                f"pid={pid} reason=live_watcher_holds_lock heartbeat_age={int(age)}s"
                f" release=heartbeat_older_than_{cfg.heartbeat_stale_secs}s")
            return False
    row(cfg, "watch_takeover", f"pid={pid} reason=stale_heartbeat")
    return True


def release_lock(cfg) -> None:
    try:
        cfg.lock_dir.rmdir()
    except OSError:
        pass
    try:
        cfg.heartbeat.unlink()
    except OSError:
        pass


def one_pass(cfg, pid, transcript, status_override="") -> str:
    """ONE bounded pass. Returns the decision word: below|soft|drain|rolled|failed."""
    cfg.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat.touch()
    tokens = S.context_tokens(transcript)
    status = status_override or S.session_status(cfg.claude_home, pid)
    row(cfg, "poll", f"pid={pid} tokens={tokens} status={status}")
    if tokens >= cfg.hard_tokens:
        return "rolled" if rollover(cfg, pid, tokens, "hard") else "failed"
    if tokens < cfg.soft_tokens:
        return "below"
    marker = cfg.run_dir / f"soft.{pid}.marker"
    if claim(marker, f"tokens={tokens}"):
        soft_request(cfg, pid, tokens)
        return "soft"
    age = int(time.time() - marker.stat().st_mtime)
    if status == "idle":
        row(cfg, "drain_done", f"pid={pid} reason=session_idle age={age}")
        return "rolled" if rollover(cfg, pid, tokens, "drain_idle") else "failed"
    if age >= cfg.drain_wait_secs:
        row(cfg, "drain_done", f"pid={pid} reason=drain_wait_expired age={age}")
        return "rolled" if rollover(cfg, pid, tokens, "drain_timeout") else "failed"
    row(cfg, "drain_hold", f"pid={pid} reason=work_in_flight status={status} age={age}"
                           f" release=idle_or_age_ge_{cfg.drain_wait_secs}s")
    return "drain"


def cmd_watch(cfg, pid, transcript, status_override, once) -> int:
    if not transcript:
        resolved = S.transcript_of_pid(cfg.claude_home, pid)
        if resolved is None:
            row(cfg, "watch_failed", f"pid={pid} reason=no_transcript_in_registry")
            say(f"cannot resolve transcript for pid {pid}")
            return 1
        transcript = str(resolved)
    if not acquire_lock(cfg, pid):
        say("another watcher holds the lock and its heartbeat is fresh")
        return 1
    # watcher_pid makes the chain auditable after the fact: a `watch_start` row
    # whose watcher_pid equals the `watcher=` of an earlier `watch_spawned` row
    # proves the pass that follows was driven by the spawned watcher, not by an
    # operator running `once` by hand.
    row(cfg, "watch_start",
        f"pid={pid} watcher_pid={os.getpid()} transcript={transcript} soft={cfg.soft_tokens}"
        f" hard={cfg.hard_tokens} poll={cfg.poll_secs} once={int(once)}")
    try:
        while True:
            verdict = one_pass(cfg, pid, transcript, status_override)
            if verdict in ("rolled", "failed") or once:
                return 0 if verdict != "failed" else 1
            time.sleep(cfg.poll_secs)
    finally:
        # A rollover releases the lock itself before relaunching; rmdir of an
        # absent dir is harmless.
        try:
            cfg.lock_dir.rmdir()
        except OSError:
            pass


def cmd_status(cfg) -> int:
    state, age = "DEAD", "-"
    if cfg.heartbeat.is_file():
        age = int(time.time() - cfg.heartbeat.stat().st_mtime)
        state = "ALIVE" if age < cfg.heartbeat_stale_secs else "DEAD"
    print(f"watcher={state} heartbeat={age} stale_after={cfg.heartbeat_stale_secs}s "
          f"lock={'held' if cfg.lock_dir.is_dir() else 'free'}")
    print(f"campaign={cfg.campaign_dir} soft={cfg.soft_tokens} hard={cfg.hard_tokens} "
          f"poll={cfg.poll_secs} drain={cfg.drain_wait_secs}")
    if cfg.pid_file.is_file():
        pid = cfg.pid_file.read_text().strip()
        print(f"session_pid={pid} live={S.is_live_session(cfg.claude_home, pid)}")
    if cfg.log.is_file():
        print("last rows:")
        for line in cfg.log.read_text().splitlines()[-3:]:
            print(line)
    return 0 if state == "ALIVE" else 1


def cmd_stop(cfg) -> int:
    row(cfg, "watch_stop_requested", "reason=operator release=next_poll")
    release_lock(cfg)
    say("lock released; a running watcher exits at its next poll")
    return 0


# ------------------------------------------------------------------------ cli
DIALS = ("soft_tokens", "hard_tokens", "drain_wait_secs", "poll_secs",
         "campaign_dir", "claude_home", "claude_bin", "claude_flags",
         "tmux_bin", "tmux_prefix", "seed_as_arg", "autowatch",
         "launch_pid_wait_secs", "supervisor_cmd", "term_wait_secs",
         "kill_poll_secs", "heartbeat_stale_secs", "rollover_cmd", "kill_cmd")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="supervise", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["launch", "watch", "once", "status", "stop"])
    p.add_argument("--config", default="")
    p.add_argument("--profile", default=None)
    p.add_argument("--session-pid", default=None)
    p.add_argument("--transcript", default="")
    p.add_argument("--status-override", default="")
    for dial in DIALS:                      # every dial is also a CLI argument
        p.add_argument(f"--{dial.replace('_', '-')}", dest=dial, default=None)
    p.add_argument("flags", nargs="*", help="after --, the claude flags to record")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, passthrough = argv[:cut], argv[cut + 1:]
    args = build_parser().parse_args(argv)
    cfg = cfgmod.load(args.config or None,
                      {d: getattr(args, d) for d in DIALS}, args.profile)
    cfg.config_file = args.config

    if args.command == "launch":
        cfg.run_dir.mkdir(parents=True, exist_ok=True)
        flags = " ".join(passthrough) or " ".join(args.flags) or cfg.claude_flags
        cfg.flags_file.write_text(flags + "\n")
        return 0 if launch_from_file(cfg) is not None else 1
    if args.command in ("watch", "once"):
        if not args.session_pid:
            raise SystemExit("supervise: --session-pid is required")
        return cmd_watch(cfg, args.session_pid, args.transcript,
                         args.status_override, once=(args.command == "once"))
    if args.command == "status":
        return cmd_status(cfg)
    return cmd_stop(cfg)


if __name__ == "__main__":
    sys.exit(main())
