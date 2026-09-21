"""job.py -- a codex job you can start in the background and then MESSAGE.

    codex-job start  --model M --effort E --cwd D --task-file F [--name N]
    codex-job send   <job> "<text>"
    codex-job status <job>
    codex-job list
    codex-job stop   <job> [--force]
    codex-job wait   <job> [--timeout-s S]

`dispatch.py` is one turn and exit.  This is the same wire, kept OPEN: `start`
prints a job id immediately and leaves exactly ONE owner process holding the
`codex app-server --stdio` connection, so a later `send` can reach the turn
that is running right now.

WHY AN OWNER PROCESS AND NOT A DAEMON
    `turn/steer` only works on the connection that owns the turn (measured:
    a steer from a second process cannot see the turn; `codex queue` accepts a
    message cross-process but it was never delivered on the next turn).  So the
    one thing that must persist is the connection, and it persists for the life
    of the job -- not as a service.  The owner exits on its own after
    `--linger-s` idle seconds; after that a `send` resumes the thread by id in
    a NEW owner, which is the same conversation, one turn later.

THE THREE DELIVERY PATHS (every message gets exactly one row)
    steered           the owner was mid-turn: `turn/steer {threadId,
                      expectedTurnId, input}` accepted into the live turn.
    queued-next-turn  no steerable turn: the text becomes the next
                      `turn/start` on the SAME thread, same owner.
    resumed           no owner was alive: `send` started one, which did
                      `thread/resume {threadId}` + `turn/start`.
    queued-remote(H)  the owner is LIVE on another node H: the file is in the
                      shared inbox and H's owner lists that directory, so the
                      message arrives.  No second owner is started here.
    failed(remote-owner-gone)
                      the owner on H has exited and the thread's rollout file
                      is in H's node-local CODEX_HOME, so nothing on this node
                      can resume it.  The message is moved to undelivered/ and
                      the refusal is loud (rc 2) -- never a silent drop.
    failed(<why>)     the turn itself could not be started.  The message file
                      STAYS in inbox/ so the next owner delivers it.

ONE JOB DIR, MANY NODES
    The job dir is on the shared filesystem, so `send/status/stop/wait` may run
    anywhere.  The owner process, its pid, its /proc start time and the codex
    thread all live on ONE node, named in `meta.json` (`host`) and in the lock
    (`owner.lock/host`).  Every verb compares hosts BEFORE it reads a pid.

NO MESSAGE IS EVER LOST (the interesting invariant)
    `send` writes its file into `inbox/` BEFORE it looks at ownership; the
    owner rescans `inbox/` AFTER it drops `owner.lock`.  So for any
    interleaving: if the write landed before the owner's last scan the owner
    keeps serving; if it landed after, the owner has already released the lock,
    `send`'s `mkdir` therefore succeeds and `send` starts the next owner.  A
    file leaves `inbox/` only when its delivery row is written.

EXIT CODES (the owner's rc, and what `wait` returns)
    0   the job's last turn answered
    2   usage
    3   codex ran and a turn failed
    42  codex unavailable -- reason=absent|auth|busy|protocol|quota.
        `quota` boards the cooldown marker (see errors.board_quota).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from token_kit.codex import errors
from token_kit.codex import launcher as launcher_mod
from token_kit.codex.dispatch import (
    EXIT_BUSY_OR_ABSENT,
    EXIT_CODEX_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    INIT_DEADLINE_S,
    SANDBOX_MODES,
    AppServerClient,
    CodexUnavailable,
    _AUTH_MARKERS,
    cap_bytes,
    child_env,
)

#: How long an idle owner keeps the connection (and the chance to steer) alive.
DEFAULT_LINGER_S = 120.0
#: Owner loop tick.  It is the select() timeout on codex's stdout, so the loop
#: is blocked on the wire, not spinning: one inbox readdir per tick.
TICK_S = 0.2
DEFAULT_ANSWER_TAIL = 2000
#: Only a test sets this: seconds to sit between the owner's last inbox scan
#: and dropping `owner.lock`, which is the exact window a lost message needs.
EXIT_RACE_ENV = "TOKEN_KIT_CODEX_EXIT_RACE_S"
DEFAULT_WAIT_TIMEOUT_S = 3600.0

STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_DONE = "done"


# --------------------------------------------------------------------------
# the job directory
# --------------------------------------------------------------------------

@dataclass
class Job:
    root: Path

    @property
    def job_id(self) -> str:
        return self.root.name

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def delivered(self) -> Path:
        return self.root / "delivered"

    @property
    def lock(self) -> Path:
        return self.root / "owner.lock"

    @property
    def done_file(self) -> Path:
        return self.root / "done"

    def meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def thread_id(self) -> str:
        try:
            return (self.root / "thread").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def rc(self) -> int | None:
        try:
            return int((self.root / "rc").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # -- append-only rows -------------------------------------------------
    def row(self, state: str, detail: str = "") -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\t{state}\t{detail}".rstrip()
        with (self.root / "status").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def event(self, kind: str, payload: Any) -> None:
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "kind": kind, "msg": payload}) + "\n")
            handle.flush()

    def delivery(self, message_id: str, disposition: str, detail: str = "") -> None:
        self.event("delivery", {"message": message_id, "disposition": disposition,
                                "detail": detail})
        self.row("delivery", f"{message_id} {disposition} {detail}".rstrip())

    def deliveries(self) -> list[dict[str, Any]]:
        out = []
        try:
            text = (self.root / "events.jsonl").read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") == "delivery":
                out.append(obj.get("msg") or {})
        return out

    def last_status(self) -> str:
        try:
            rows = (self.root / "status").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return rows[-1] if rows else ""

    def answer_tail(self, nbytes: int) -> str:
        path = self.root / "answer.md"
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        return raw[-nbytes:].decode("utf-8", "replace")

    def last_answer(self) -> str:
        """Only the most recent turn's answer.

        `answer.md` is append-only (every turn is kept), so a plain tail of it
        shows the PREVIOUS turn when the newest answer is short -- which is
        exactly what a follow-up answer usually is.
        """
        try:
            text = (self.root / "answer.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        marker = "\n## turn "
        if marker in text:
            tail = text.rsplit(marker, 1)[1]
            return tail.split("\n", 1)[1].strip() if "\n" in tail else ""
        return text.strip()

    # -- which HOST does this job live on? --------------------------------
    #
    # The job dir is on a SHARED filesystem: every login and compute node sees
    # it.  The owner process, its pid, its /proc start time and the codex
    # thread's rollout file are all on ONE node.  So a pid read from here means
    # nothing until the host matches -- on another node that pid is absent
    # (the job reads as "orphan" and a second owner starts on a thread that is
    # not there) or, worse, alive and somebody else's.  Every verb compares
    # hosts BEFORE it looks at a pid.
    def host(self) -> str:
        """The host the job was started on."""
        return str(self.meta().get("host", ""))

    def owner_host(self) -> str:
        """The host of the CURRENT owner: the lock's, else the job's."""
        try:
            recorded = (self.lock / "host").read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        return recorded or self.host()

    def is_remote(self) -> bool:
        """True when this job's owner lives on another node."""
        owner = self.owner_host()
        return bool(owner) and owner != launcher_mod.this_host()

    # -- ownership --------------------------------------------------------
    def owner_pid(self) -> int | None:
        try:
            return int((self.lock / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def owner_start_time(self) -> str:
        try:
            return (self.lock / "starttime").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def owner_alive(self) -> bool:
        """LOCAL liveness only.  False on another node is "cannot see", not
        "gone" -- callers ask `is_remote()` first and never conclude from this.
        """
        if self.is_remote():
            return False
        pid = self.owner_pid()
        if pid is None:
            return False
        if not pid_alive(pid):
            return False
        recorded = self.owner_start_time()
        return not recorded or recorded == pid_start_time(pid)

    def claim(self) -> bool:
        """Atomically become the owner.  False when a LIVE owner holds it."""
        try:
            self.lock.mkdir()
            return True
        except FileExistsError:
            if self.owner_alive():
                return False
            shutil.rmtree(self.lock, ignore_errors=True)
            try:
                self.lock.mkdir()
                return True
            except FileExistsError:
                return False

    def write_owner(self, pid: int) -> None:
        self.lock.mkdir(parents=True, exist_ok=True)
        (self.lock / "pid").write_text(f"{pid}\n", encoding="utf-8")
        (self.lock / "starttime").write_text(pid_start_time(pid) + "\n", encoding="utf-8")
        # The host goes in with the pid: the pid is only an identity ON it.
        (self.lock / "host").write_text(launcher_mod.this_host() + "\n", encoding="utf-8")

    def release(self) -> None:
        shutil.rmtree(self.lock, ignore_errors=True)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_start_time(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat: the kernel's own start time in jiffies.

    Doctrine: a pid alone is not an identity -- it is reused.  A kill or a
    liveness check is only valid for a pid whose start time still matches.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    tail = stat.rsplit(")", 1)[-1].split()
    return tail[19] if len(tail) > 19 else ""


def jobs_root() -> Path:
    return errors.state_root()


# --------------------------------------------------------------------------
# the thread's rollout file -- the one piece of a thread that is NODE-LOCAL
# --------------------------------------------------------------------------
#
# MEASURED (codex-cli 0.153.4, this host): a thread's transcript is written to
# `<CODEX_HOME>/sessions/<YYYY>/<MM>/<DD>/rollout-<stamp>-<threadId>.jsonl`.
# With CODEX_HOME on node-local tmp that file exists on ONE node, which is why
# `thread/resume` cannot simply be re-issued from another one.  The owner
# therefore copies the file into the job dir (shared) at the end of every turn,
# and an owner starting on a node that does not have it puts it back first.
#
# UNPROVEN: that a resume from the seeded copy alone reconstitutes the thread.
# It is built and guarded against the fake; only a real two-node run can say
# whether codex needs anything else out of its store, so cross-host `send`
# still REFUSES rather than relying on this.
ROLLOUT_DIR = "rollout"
_ROLLOUT_GLOB = "sessions/*/*/*/rollout-*{thread}*.jsonl"


def codex_home_of(launch) -> Path | None:
    home = launch.env.get("CODEX_HOME", "")
    return Path(home) if home else None


def _newest(base: Path, thread_id: str) -> Path | None:
    if not thread_id:
        return None
    try:
        matches = sorted(base.glob(_ROLLOUT_GLOB.format(thread=thread_id)))
    except OSError:
        return None
    return matches[-1] if matches else None


def archive_rollout(job: Job, home: Path | None, thread_id: str) -> Path | None:
    """Copy this thread's rollout into the job dir, where every node sees it."""
    if home is None:
        return None
    src = _newest(home, thread_id)
    if src is None:
        return None
    dst = job.root / ROLLOUT_DIR / src.relative_to(home)
    return dst if launcher_mod.atomic_copy(src, dst) else None


def seed_rollout(job: Job, home: Path | None, thread_id: str) -> bool:
    """Put an archived rollout back into THIS node's codex home before a resume."""
    if home is None or _newest(home, thread_id) is not None:
        return False
    base = job.root / ROLLOUT_DIR
    src = _newest(base, thread_id)
    if src is None:
        return False
    return launcher_mod.atomic_copy(src, home / src.relative_to(base))


def new_job_id(name: str | None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "job"))[:24]
    return f"{slug}-{stamp}-{suffix}"


def find_job(token: str) -> Job:
    root = jobs_root()
    exact = root / token
    if exact.is_dir():
        return Job(exact)
    matches = sorted(p for p in root.glob(f"*{token}*") if p.is_dir())
    if len(matches) == 1:
        return Job(matches[0])
    if not matches:
        raise SystemExit(f"codex-job: no such job: {token} (under {root})")
    raise SystemExit(f"codex-job: ambiguous job {token}: {[p.name for p in matches]}")


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

@dataclass
class Message:
    path: Path
    text: str
    kind: str = "text"

    @property
    def message_id(self) -> str:
        return self.path.name


def put_message(job: Job, text: str, kind: str = "text") -> Message:
    """Write one message file into inbox/ atomically (rename, never a partial read)."""
    job.inbox.mkdir(parents=True, exist_ok=True)
    tmp_dir = job.root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.{kind}"
    tmp = tmp_dir / name
    tmp.write_text(text, encoding="utf-8")
    final = job.inbox / name
    os.replace(tmp, final)
    return Message(final, text, kind)


def read_inbox(job: Job, seen: set[str]) -> list[Message]:
    """Every inbox file not yet seen, oldest first.  Files stay until delivered."""
    if not job.inbox.is_dir():
        return []
    out = []
    for path in sorted(job.inbox.iterdir()):
        if path.name in seen or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        seen.add(path.name)
        out.append(Message(path, text, path.suffix.lstrip(".") or "text"))
    return out


def retire(job: Job, message: Message) -> None:
    """A delivered message leaves inbox/; inbox emptiness IS the exit condition."""
    job.delivered.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(message.path, job.delivered / message.path.name)
    except OSError:
        pass


def inbox_pending(job: Job) -> int:
    try:
        return sum(1 for p in job.inbox.iterdir() if p.is_file())
    except OSError:
        return 0


# --------------------------------------------------------------------------
# the owner process -- the only thing that talks to codex
# --------------------------------------------------------------------------

def spawn_owner(job: Job, resume: bool) -> int:
    """Start a detached owner.  The CALLER must already hold job.lock."""
    log = (job.root / "owner.log").open("a", encoding="utf-8")
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    argv = [sys.executable, "-m", "token_kit.codex.job", "_owner", str(job.root)]
    if resume:
        argv.append("--resume")
    proc = subprocess.Popen(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, cwd=str(job.root),
    )
    job.write_owner(proc.pid)
    return proc.pid


def _finish(job: Job, rc: int, note: str = "") -> int:
    job.row(STATE_DONE, f"rc={rc} {note}".strip())
    (job.root / "rc").write_text(f"{rc}\n", encoding="utf-8")
    job.release()
    job.done_file.write_text(f"{rc}\n", encoding="utf-8")   # written LAST: `wait` waits on it
    return rc


def owner_main(job: Job, resume: bool) -> int:
    meta = job.meta()
    model, effort = meta.get("model", ""), meta.get("effort", "")
    cwd, sandbox = meta.get("cwd", ""), meta.get("sandbox", "read-only")
    linger_s = float(meta.get("linger_s", DEFAULT_LINGER_S))
    launcher = meta.get("launcher") or None
    turn_timeout_s = float(meta.get("turn_timeout_s", 1800.0))

    job.write_owner(os.getpid())
    job.row("owner", f"pid={os.getpid()} host={launcher_mod.this_host()} "
                     f"resume={int(resume)}")

    def log(direction: str, obj: Any) -> None:
        job.event(direction, obj)

    # The profile decides how codex is reached; on a machine whose codex home
    # must be node-local this prepares that home in THIS process before the
    # child exists.  An unpreparable home or a missing binary is rc 42 absent.
    try:
        launch = launcher_mod.prepare(override=launcher)
    except launcher_mod.LauncherUnavailable as exc:
        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason={exc.reason} {exc.detail}")
    job.row("launcher", f"{' '.join(launch.argv[:2])} "
                        f"CODEX_HOME={launch.env.get('CODEX_HOME', '<inherited>')}")
    # We are the process the wrapper's EXIT trap belongs to: sync on the way
    # out however we leave, SIGTERM from `stop --force` included.
    launcher_mod.install_exit_sync(launch)

    try:
        client = AppServerClient(launch.argv, child_env(launch.env), log)
    except CodexUnavailable as exc:
        launch.finish()
        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason={exc.reason} {exc.detail}")

    seen: set[str] = set()
    pending: list[Message] = []
    if not resume:
        prompt_file = Path(meta.get("task_file", ""))
        try:
            prompt = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            client.close()
            return _finish(job, EXIT_USAGE, f"task-file unreadable: {exc}")
        pending.append(Message(job.root / "task", prompt, "task"))

    try:
        # -- handshake ----------------------------------------------------
        init_id = client.request(
            "initialize",
            {"clientInfo": {"name": "token_kit.codex.job", "title": "token_kit job",
                            "version": "1"}},
        )
        deadline = time.monotonic() + INIT_DEADLINE_S
        while True:
            msg = client.read_message(deadline)
            if msg is None:
                tail = client.stderr_tail()
                reason = "auth" if any(m in tail.lower() for m in _AUTH_MARKERS) else "absent"
                return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason={reason} no initialize")
            if msg.get("id") == init_id:
                if "error" in msg:
                    detail = json.dumps(msg["error"])
                    if errors.is_quota(msg["error"]):
                        errors.board_quota(detail)
                        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {detail}")
                    reason = "auth" if any(m in detail.lower() for m in _AUTH_MARKERS) \
                        else "protocol"
                    return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason={reason} {detail}")
                break
        launch.release_startup()   # startup only: the next child may start now
        client.notify("initialized", {})

        # -- thread -------------------------------------------------------
        stored = job.thread_id()
        codex_home = codex_home_of(launch)
        if resume and stored:
            # The rollout file is node-local; if this node has never seen this
            # thread, put the archived copy back before asking codex for it.
            if seed_rollout(job, codex_home, stored):
                job.row("rollout", f"seeded {stored} into {codex_home}")
            req = client.request("thread/resume", {"threadId": stored, "model": model,
                                                   "cwd": cwd, "sandbox": sandbox})
        else:
            req = client.request("thread/start", {"model": model, "cwd": cwd,
                                                  "sandbox": sandbox,
                                                  "approvalPolicy": "never"})
        deadline = time.monotonic() + INIT_DEADLINE_S
        thread_id = ""
        while True:
            msg = client.read_message(deadline)
            if msg is None:
                return _finish(job, EXIT_BUSY_OR_ABSENT, "reason=protocol no thread response")
            if msg.get("id") == req:
                if "error" in msg:
                    detail = json.dumps(msg["error"])
                    if errors.is_quota(msg["error"]):
                        errors.board_quota(detail)
                        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {detail}")
                    return _finish(job, EXIT_CODEX_FAILED, f"thread refused: {detail}")
                result = msg.get("result") or {}
                thread = result.get("thread") or {}
                thread_id = thread.get("id") or result.get("threadId") or stored
                break
        if not thread_id:
            return _finish(job, EXIT_CODEX_FAILED, "codex returned no thread id")
        (job.root / "thread").write_text(thread_id + "\n", encoding="utf-8")
        job.row("thread", thread_id)

        # -- the serving loop ---------------------------------------------
        active_turn = ""            # the id `turn/steer` must match
        turn_req_id: int | None = None
        turn_msgs: list[Message] = []
        steer_wait: dict[int, Message] = {}
        texts: list[str] = []
        turns = 0
        last_rc = EXIT_OK
        idle_until = time.monotonic() + linger_s
        turn_deadline = 0.0
        stopping = False

        while True:
            # (a) anything new in the inbox?
            for message in read_inbox(job, seen):
                if message.kind == "stop":
                    stopping = True
                    job.delivery(message.message_id, "stop")
                    retire(job, message)
                    if active_turn:
                        client.request("turn/interrupt", {"threadId": thread_id,
                                                          "turnId": active_turn})
                    continue
                if active_turn:
                    req_id = client.request("turn/steer", {
                        "threadId": thread_id,
                        "expectedTurnId": active_turn,
                        "input": [{"type": "text", "text": message.text}],
                    })
                    steer_wait[req_id] = message
                else:
                    pending.append(message)

            # (b) idle with work -> the message becomes the next turn
            if not active_turn and pending and not stopping:
                inputs = [{"type": "text", "text": m.text} for m in pending]
                turn_req_id = client.request("turn/start", {
                    "threadId": thread_id, "input": inputs,
                    "model": model, "effort": effort, "cwd": cwd,
                })
                turn_msgs = list(pending)
                pending.clear()
                turns += 1
                texts = []
                turn_deadline = time.monotonic() + turn_timeout_s
                job.row(STATE_RUNNING, f"turn {turns} sent, {len(turn_msgs)} input(s)")
                for message in turn_msgs:
                    if message.kind == "task":
                        continue
                    disposition = "resumed" if (resume and turns == 1) else "queued-next-turn"
                    job.delivery(message.message_id, disposition)
                    retire(job, message)

            # (c) exit? only with an EMPTY inbox, so nothing can be lost
            if not active_turn and not pending:
                expired = time.monotonic() >= idle_until
                if stopping or expired:
                    if inbox_pending(job) == 0:
                        # TEST SEAM: widen the window between "the inbox looked
                        # empty" and "the lock is gone" so a `send` can land
                        # inside it on purpose.  Zero in production.
                        race = float(os.environ.get(EXIT_RACE_ENV, "0") or 0)
                        if race:
                            job.row("exit-window", f"{race:g}s")
                            time.sleep(race)
                        job.release()
                        if inbox_pending(job) == 0:
                            return _finish(job, last_rc,
                                           "stopped" if stopping else "linger expired")
                        if not job.claim():
                            # A sender took the lock in the window between our
                            # release and this rescan.  It is starting the next
                            # owner, so we write NOTHING terminal -- no `done`,
                            # no `rc`, and above all no release of a lock that
                            # is now somebody else's.
                            job.row("handover", "a sender owns the job now")
                            return last_rc
                        job.write_owner(os.getpid())
                        continue
                    idle_until = time.monotonic() + linger_s
                    continue

            # (d) the wire
            msg = client.read_message(time.monotonic() + TICK_S)
            if msg is None:
                if client.proc.poll() is not None:
                    return _finish(job, EXIT_BUSY_OR_ABSENT,
                                   f"reason=protocol codex exited rc={client.proc.poll()}")
                if active_turn and time.monotonic() > turn_deadline:
                    client.request("turn/interrupt", {"threadId": thread_id,
                                                      "turnId": active_turn})
                    active_turn = ""
                    last_rc = EXIT_CODEX_FAILED
                    job.row("failed", f"turn {turns} timed out after {turn_timeout_s:g}s")
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            msg_id = msg.get("id")

            # a steer answer
            if msg_id in steer_wait:
                message = steer_wait.pop(msg_id)
                if "error" in msg:
                    why = json.dumps(msg["error"])[:300]
                    if errors.is_quota(msg["error"]):
                        errors.board_quota(why)
                        job.delivery(message.message_id, "failed(quota)", why)
                        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {why}")
                    # Not steerable (review/compact), or the turn ended under
                    # us: the text becomes the next turn.  Never dropped.
                    pending.append(message)
                    job.event("steer_refused", {"message": message.message_id, "error": why})
                else:
                    job.delivery(message.message_id, "steered",
                                 (msg.get("result") or {}).get("turnId", ""))
                    retire(job, message)
                continue

            if method == "turn/started":
                active_turn = ((params.get("turn") or {}).get("id")) or active_turn
                job.row(STATE_RUNNING, f"turn {turns} id={active_turn}")
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and (item.get("text") or "").strip():
                    texts.append(item["text"])
                continue
            if method == "error":
                err = params.get("error") or {}
                detail = json.dumps(err)[:500]
                if errors.is_quota(err):
                    errors.board_quota(detail)
                    job.row("failed", f"reason=quota {detail}")
                    return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {detail}")
                job.row("failed", detail)
                continue
            if method is not None and msg_id is not None:
                # A server->client REQUEST we do not implement: answer it, or
                # the turn blocks forever waiting on us.
                client.send({"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32601,
                                       "message": f"{method} unsupported by token_kit job"}})
                continue

            turn: dict[str, Any] | None = None
            if method == "turn/completed":
                turn = params.get("turn") or {}
            elif msg_id == turn_req_id:
                if "error" in msg:
                    detail = json.dumps(msg["error"])[:500]
                    if errors.is_quota(msg["error"]):
                        errors.board_quota(detail)
                        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {detail}")
                    last_rc = EXIT_CODEX_FAILED
                    active_turn = ""
                    job.row("failed", f"turn/start error: {detail}")
                    for message in turn_msgs:
                        job.delivery(message.message_id, "failed(turn/start)", detail[:120])
                    turn_msgs = []
                    idle_until = time.monotonic() + linger_s
                    continue
                turn = (msg.get("result") or {}).get("turn") or {}
                if turn.get("status") in (None, "inProgress"):
                    turn = None
            if turn is None:
                continue

            status = turn.get("status")
            for item in turn.get("items") or []:
                if item.get("type") == "agentMessage" and item.get("text") not in texts:
                    if (item.get("text") or "").strip():
                        texts.append(item["text"])
            if status != "completed":
                err = turn.get("error") or {}
                detail = json.dumps(err)[:500] or str(status)
                if errors.is_quota(err):
                    errors.board_quota(detail)
                    return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason=quota {detail}")
                last_rc = EXIT_CODEX_FAILED
                job.row("failed", f"turn {turns} status={status} {detail}")
            else:
                answer = texts[-1].strip() if texts else ""
                if not answer:
                    last_rc = EXIT_CODEX_FAILED
                    job.row("failed", f"turn {turns} completed with no assistant message")
                else:
                    last_rc = EXIT_OK
                    with (job.root / "answer.md").open("a", encoding="utf-8") as handle:
                        handle.write(f"\n## turn {turns} ({turn.get('id', '')})\n\n"
                                     f"{answer}\n")
            active_turn = ""
            turn_req_id = None
            turn_msgs = []
            # The transcript this node just extended goes where every node can
            # read it.  Cheap (one copy of one jsonl), and it is the only part
            # of the thread that does not live in the shared job dir already.
            archived = archive_rollout(job, codex_home, thread_id)
            if archived is not None:
                job.row("rollout", f"archived {archived.name}")
            idle_until = time.monotonic() + linger_s
            job.row(STATE_IDLE, f"turn {turns} {status}; lingering {linger_s:g}s")
    except CodexUnavailable as exc:
        return _finish(job, EXIT_BUSY_OR_ABSENT, f"reason={exc.reason} {exc.detail}")
    finally:
        client.close()
        launch.finish()


# --------------------------------------------------------------------------
# the verbs
# --------------------------------------------------------------------------

def cmd_start(args) -> int:
    task_file = Path(args.task_file).resolve()
    if not task_file.is_file():
        print(f"codex-job: --task-file not found: {task_file}", file=sys.stderr)
        return EXIT_USAGE
    if not os.path.isabs(args.cwd) or not os.path.isdir(args.cwd):
        print(f"codex-job: --cwd must be an existing absolute directory: {args.cwd}",
              file=sys.stderr)
        return EXIT_USAGE
    left = errors.cooldown_active()
    if left and not args.ignore_cooldown:
        print(f"CODEX_UNAVAILABLE reason=quota cooldown {left:.0f}s left "
              f"({errors.cooldown_marker_path()}) -- do the step yourself with a Claude model",
              file=sys.stderr)
        return EXIT_BUSY_OR_ABSENT

    job = Job(jobs_root() / new_job_id(args.name))
    job.root.mkdir(parents=True, exist_ok=False)
    job.inbox.mkdir()
    (job.root / "meta.json").write_text(json.dumps({
        "job_id": job.job_id, "name": args.name or "", "model": args.model,
        "effort": args.effort, "cwd": args.cwd, "sandbox": args.sandbox,
        "task_file": str(task_file), "launcher": args.launcher or "",
        "linger_s": args.linger_s, "turn_timeout_s": args.turn_timeout_s,
        # The job dir is shared by every node; the owner, its pid and the
        # codex thread's rollout file are not. The host is part of the record.
        "host": launcher_mod.this_host(),
        "created": time.time(),
    }, indent=2) + "\n", encoding="utf-8")
    job.row(STATE_STARTING, f"model={args.model} effort={args.effort}")
    if not job.claim():  # a brand new dir: this cannot fail, but never assume
        print("codex-job: could not claim a new job", file=sys.stderr)
        return EXIT_CODEX_FAILED
    pid = spawn_owner(job, resume=False)
    job.row("owner-spawned", f"pid={pid}")
    print(job.job_id)
    return EXIT_OK


def cmd_send(args) -> int:
    job = find_job(args.job)
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    if not text.strip():
        print("codex-job: empty message", file=sys.stderr)
        return EXIT_USAGE
    # ORDER MATTERS: the file lands before ownership is even looked at.
    message = put_message(job, text)

    # HOST FIRST. The owner watches this inbox by LISTING it, so a file written
    # from another node reaches a live owner exactly as a local one does. What
    # must never happen here is the second half of the old code path: claiming
    # the lock and spawning a second owner that resumes a thread whose rollout
    # file lives in the OTHER node's CODEX_HOME.
    if job.is_remote():
        owner = job.owner_host()
        if not job.done_file.exists():
            # A ROUTING row, not a delivery row: the owner on the other node
            # writes the delivery row when it actually takes the message, and
            # the invariant "one delivery row per message" holds across nodes.
            job.event("routing", {"message": message.message_id,
                                  "disposition": f"queued-remote({owner})"})
            job.row("routing", f"{message.message_id} queued-remote({owner})")
            print(f"{message.message_id} queued-remote({owner}) "
                  f"-- the owner on {owner} picks it up from the shared inbox")
            return EXIT_OK
        # The owner's linger has ended on the other node. Nothing here can
        # resume that thread, and the message must not sit in the inbox
        # pretending it will be delivered: it is retired to undelivered/ with
        # a row, and the refusal is loud.
        undelivered = job.root / "undelivered"
        undelivered.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(message.path, undelivered / message.path.name)
        except OSError:
            pass
        job.delivery(message.message_id, "failed(remote-owner-gone)",
                     f"host={owner} kept at undelivered/{message.path.name}")
        print(f"codex-job: thread lives on {owner}; run `codex-job send` there "
              f"or start a new job. Your message is kept at "
              f"{undelivered / message.path.name}", file=sys.stderr)
        return EXIT_USAGE

    if job.owner_alive() and not job.done_file.exists():
        print(f"{message.message_id} queued (owner pid {job.owner_pid()} is live)")
        return EXIT_OK
    if not job.claim():
        print(f"{message.message_id} queued (another sender is starting an owner)")
        return EXIT_OK
    thread = job.thread_id()
    if not thread:
        job.release()
        print(f"{message.message_id} queued (job has no thread yet)")
        return EXIT_OK
    for stale in (job.done_file, job.root / "rc"):
        try:
            stale.unlink()
        except OSError:
            pass
    pid = spawn_owner(job, resume=True)
    job.row("owner-spawned", f"pid={pid} resume thread={thread}")
    print(f"{message.message_id} resumed (new owner pid {pid}, thread {thread})")
    return EXIT_OK


def _job_line(job: Job) -> str:
    meta = job.meta()
    if job.done_file.exists():
        state = "done"
    elif job.is_remote():
        # Never "orphan" from here: a pid on another node is not ours to read.
        state = "remote"
    else:
        state = "live" if job.owner_alive() else "orphan"
    # A HUMAN reads `list` and `status`, so the start time is written the way a
    # person says it. The job's own machine records (events.tsv, meta.json)
    # keep their ISO stamps: those are parsed and sorted.
    from token_kit import timefmt

    started = meta.get("created")
    human = timefmt.human(float(started)) if started else "?"
    return (f"{job.job_id}\t{state}\tstarted={human}\t"
            f"host={job.owner_host() or '?'}\trc={job.rc()}\t"
            f"thread={job.thread_id() or '-'}\t"
            f"{meta.get('model', '?')}/{meta.get('effort', '?')}\t"
            f"inbox={inbox_pending(job)}\t{job.last_status()}")


def cmd_status(args) -> int:
    job = find_job(args.job)
    print(_job_line(job))
    tail = job.answer_tail(args.tail_bytes)
    if tail:
        print(tail if tail.endswith("\n") else tail + "\n", end="")
    return EXIT_OK


def cmd_list(args) -> int:
    root = jobs_root()
    if not root.is_dir():
        print(f"codex-job: no jobs yet ({root})")
        return EXIT_OK
    for path in sorted(root.iterdir()):
        if path.is_dir():
            print(_job_line(Job(path)))
    return EXIT_OK


def cmd_stop(args) -> int:
    job = find_job(args.job)
    put_message(job, "stop", kind="stop")

    # HOST FIRST, for two reasons: the owner's pid is meaningless here (absent,
    # or alive and someone else's), and `_finish` would write `rc` and `done`
    # for a job that is still running on the other node. A stop across nodes is
    # a REQUEST FILE the owner honours (turn/interrupt, then exit) -- never a
    # kill, never a terminal row written from here.
    if job.is_remote():
        owner = job.owner_host()
        print(f"stop requested on {owner} (stop file written to the shared inbox); "
              f"its owner interrupts the turn within {TICK_S:g}s")
        if args.force:
            print(f"codex-job: --force cannot reach a process on {owner} -- "
                  f"run `codex-job stop --force` there", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK

    pid = job.owner_pid()
    if not job.owner_alive():
        return _finish(job, job.rc() if job.rc() is not None else EXIT_OK, "stopped, no owner")
    print(f"stop requested (owner pid {pid}); turn/interrupt goes out within {TICK_S:g}s")
    if args.force and pid:
        recorded = job.owner_start_time()
        if recorded and recorded != pid_start_time(pid):
            print(f"codex-job: pid {pid} is NOT the owner any more "
                  f"(start time {pid_start_time(pid)} != {recorded}) -- not killing",
                  file=sys.stderr)
            return EXIT_CODEX_FAILED
        os.kill(pid, signal.SIGTERM)
        print(f"SIGTERM sent to {pid} (start time {recorded} verified)")
    return EXIT_OK


def cmd_wait(args) -> int:
    """Block until the job ends, then print the answer and the delivery rows.

    Run this as a BACKGROUND command: the harness re-invokes Claude when a
    background command exits, so the completion notice costs no polling call.
    """
    job = find_job(args.job)
    # Cross-host, the status file is the ONLY thing that means anything here:
    # `done` is written by the owner wherever it runs, and no pid is consulted.
    deadline = time.monotonic() + args.timeout_s
    while not job.done_file.exists():
        if time.monotonic() >= deadline:
            print(f"codex-job: {job.job_id} still running after {args.timeout_s:g}s",
                  file=sys.stderr)
            print(_job_line(job), file=sys.stderr)
            return EXIT_CODEX_FAILED
        time.sleep(min(1.0, max(0.05, args.poll_s)))
    rc = job.rc() if job.rc() is not None else EXIT_CODEX_FAILED
    print(f"codex-job {job.job_id} rc={rc} thread={job.thread_id()}")
    for row in job.deliveries():
        print(f"  message {row.get('message', '?')}: {row.get('disposition', '?')} "
              f"{row.get('detail', '')}".rstrip())
    answer = job.last_answer() if not args.all_turns else job.answer_tail(args.max_bytes * 4)
    print(cap_bytes(answer.strip(), args.max_bytes, job.root / "answer.md"))
    if rc == EXIT_BUSY_OR_ABSENT:
        print(f"CODEX_UNAVAILABLE {job.last_status()} "
              "-- do the step yourself with a Claude model", file=sys.stderr)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex-job",
                                description="start, message and wait on codex jobs")
    sub = p.add_subparsers(dest="verb", required=True)

    s = sub.add_parser("start", help="start a job in the background; prints the job id")
    s.add_argument("--model", required=True, help="REQUIRED; there is no default, ever")
    s.add_argument("--effort", required=True, help="REQUIRED; there is no default, ever")
    s.add_argument("--cwd", required=True)
    s.add_argument("--task-file", required=True)
    s.add_argument("--name", default=None)
    s.add_argument("--sandbox", default="read-only", choices=list(SANDBOX_MODES))
    s.add_argument("--launcher", default=None)
    s.add_argument("--linger-s", type=float, default=DEFAULT_LINGER_S)
    s.add_argument("--turn-timeout-s", type=float, default=1800.0)
    s.add_argument("--ignore-cooldown", action="store_true",
                   help="start even though a quota cooldown is boarded")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("send", help="message a job (steers a live turn when there is one)")
    s.add_argument("job")
    s.add_argument("text", help="the message, or - to read stdin")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("status", help="one line, plus the tail of the answer")
    s.add_argument("job")
    s.add_argument("--tail-bytes", type=int, default=DEFAULT_ANSWER_TAIL)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="every job on this host")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("stop", help="turn/interrupt, then optionally SIGTERM the owner")
    s.add_argument("job")
    s.add_argument("--force", action="store_true",
                   help="also SIGTERM the owner pid, verified by its start time")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("wait", help="block until the job ends; run me in the background")
    s.add_argument("job")
    s.add_argument("--timeout-s", type=float, default=DEFAULT_WAIT_TIMEOUT_S)
    s.add_argument("--max-bytes", type=int, default=4000)
    s.add_argument("--poll-s", type=float, default=1.0)
    s.add_argument("--all-turns", action="store_true",
                   help="print the tail of every turn, not just the last answer")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("_owner", help=argparse.SUPPRESS)
    s.add_argument("root")
    s.add_argument("--resume", action="store_true")
    s.set_defaults(func=lambda a: owner_main(Job(Path(a.root)), a.resume))
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
