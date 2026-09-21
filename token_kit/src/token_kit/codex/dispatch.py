"""dispatch.py -- run ONE codex turn on demand, as a child process, then exit.

    dispatch --model M --effort E --cwd <abs> --task-file <in.md> --out <out.md>
             [--resume <thread-id>] [--max-bytes 4000] [--timeout-s 1800]
             [--launcher <path>] [--sandbox read-only] [--max-children 2]
             [--wait-s 20]

WHAT IT TALKS TO
    `codex app-server --stdio`: the app-server protocol over the child's own
    stdin/stdout.  No port, no daemon, no endpoint file, no ssh, no auth
    question -- the child inherits the caller's CODEX_HOME and auth.
    Methods used, all present in this binary's own generated schema
    (`codex app-server generate-json-schema --experimental`):
        initialize / initialized
        thread/start   {model, cwd, sandbox, approvalPolicy}
        thread/resume  {threadId, model, cwd, sandbox}   <- resume by EXPLICIT id
        turn/start     {threadId, input[], model, effort, cwd}
        turn/interrupt {threadId, turnId}                <- clean cancellation
    and the notifications `turn/started`, `item/completed`, `turn/completed`,
    `thread/tokenUsage/updated`.

OUTPUT CONTRACT
    stdout        ONLY the final assistant message, capped at --max-bytes with
                  one `[truncated at N of M bytes; full answer: <path>]` line.
    <out>         the full final answer.
    <out>.thread  the thread id, so a later call can `--resume` it.
    <out>.log.jsonl  every JSON-RPC line in both directions.  Never printed.

EXIT CODES
    0   answered
    2   usage
    3   codex ran and failed (never an empty rc 0)
    42  codex not available -- launcher/binary absent, auth refused, or the
        per-host concurrency bound is full.  The caller does the step itself
        with a Claude model.  Reason word on stderr: absent|auth|busy|protocol.

TWO MEASURED CLUSTER FACTS THIS FILE ENCODES
    1. `TOKIO_WORKER_THREADS` <= 4 makes `codex app-server --stdio` exit rc 0
       having written NOTHING -- no error, no banner, no response.  The
       operator's `~/run_codex.bash` exports 4 by default, so a dispatcher that
       used it unchanged would see a silent, exit-0, empty server every time.
       We therefore raise the floor to MIN_TOKIO_WORKER_THREADS in the child
       env; the launcher sets the caps as DEFAULTS, so our value survives.
    2. CODEX_HOME must be on node-local disk wherever the home is a shared
       filesystem: codex's SQLite store fails there with locking errors.  That
       is `token_kit.codex.launcher`'s job -- it prepares the node-local home
       in THIS process and we spawn the binary directly, no shell in between.
       The profile decides; a machine that needs a node-local home never falls
       back to a bare `codex` against the shared one.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CODEX_FAILED = 3
EXIT_BUSY_OR_ABSENT = 42

#: Measured on the cluster login node with codex-cli 0.153.4: 1/2/4 -> silent empty exit,
#: 6/8/12 -> a normal `initialize` response.
MIN_TOKIO_WORKER_THREADS = 6

#: An answer smaller than this cannot say anything useful, so a cap below it is
#: a usage error rather than a silent near-empty stdout.
MIN_MAX_BYTES = 200

DEFAULT_MAX_BYTES = 4000
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_MAX_CHILDREN = 2
DEFAULT_WAIT_S = 20.0

#: How long `initialize` may take before we call codex unavailable.  The
#: handshake measured 0.26 s here; the launcher's first run on a fresh node
#: also seeds a node-local CODEX_HOME, hence the margin.
INIT_DEADLINE_S = 25.0

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

_AUTH_MARKERS = ("not logged in", "unauthorized", "auth", "login", "401", "credential")


class CodexUnavailable(Exception):
    """codex could not be reached at all.  Carries the reason word for rc 42."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"reason={reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


class CodexTurnFailed(Exception):
    """codex ran and the turn did not complete.  rc 3, never rc 0."""


@dataclass
class DispatchResult:
    answer: str
    thread_id: str
    turn_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    resumed: bool = False


# --------------------------------------------------------------------------
# launcher resolution
# --------------------------------------------------------------------------

def default_launcher() -> str | None:
    """The EXPLICIT launcher path the profile names, or None.

    None is not "codex is missing": it means "this machine uses the kit's own
    launcher" (`token_kit.codex.launcher`), which is the answer whenever the
    profile says the codex home must be node-local.  Nothing here detects
    anything -- the PROFILE is the authority, and the old detection (is
    ~/run_codex.bash there? no? then bare `codex`) was the silent fall-through
    that put SQLite back on the shared filesystem.
    """
    from token_kit.codex import launcher as launcher_mod

    cfg = launcher_mod.load_config()
    if cfg.node_local_home:
        return None
    named = cfg.launcher or "codex"
    if os.path.sep in named:
        path = os.path.expanduser(named)
        return path if os.path.exists(path) else None
    return shutil.which(named)


def launcher_command(launcher: str) -> list[str]:
    """Argv prefix that starts the app server through `launcher`."""
    if launcher.endswith((".bash", ".sh")):
        return ["bash", launcher, "app-server", "--stdio"]
    return [launcher, "app-server", "--stdio"]


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The env the codex child runs with.

    Raises the tokio worker floor (see module docstring, fact 1) and never
    touches CODEX_HOME -- the launcher owns that.
    """
    env = dict(os.environ if base is None else base)
    try:
        current = int(env.get("TOKIO_WORKER_THREADS", "0"))
    except ValueError:
        current = 0
    if current < MIN_TOKIO_WORKER_THREADS:
        env["TOKIO_WORKER_THREADS"] = str(MIN_TOKIO_WORKER_THREADS)
    return env


# --------------------------------------------------------------------------
# per-host concurrency bound -- a lockfile directory, never on NFS
# --------------------------------------------------------------------------

def slot_root() -> Path:
    override = os.environ.get("TOKEN_KIT_CODEX_SLOT_DIR")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "token_kit-codex"
    return Path("/tmp") / f"token_kit-codex-{getpass.getuser()}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # pragma: no cover -- defensive
        return exc.errno != errno.ESRCH
    return True


class Slot:
    """One of at most `max_children` codex children on this host.

    `mkdir` is the atomic primitive; the pid inside lets a crashed holder's
    slot be reclaimed instead of wedging the host forever.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def acquire(cls, max_children: int, wait_s: float, root: Path | None = None) -> "Slot":
        base = root or slot_root()
        base.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            for index in range(max_children):
                path = base / f"slot-{index}"
                try:
                    path.mkdir()
                except FileExistsError:
                    if cls._reclaim_if_dead(path):
                        try:
                            path.mkdir()
                        except FileExistsError:
                            continue
                    else:
                        continue
                (path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
                return cls(path)
            if time.monotonic() >= deadline:
                raise CodexUnavailable(
                    "busy", f"all {max_children} codex slots held on this host"
                )
            time.sleep(0.1)

    @staticmethod
    def _reclaim_if_dead(path: Path) -> bool:
        try:
            pid = int((path / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            shutil.rmtree(path, ignore_errors=True)
            return True
        if _pid_alive(pid):
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True

    def release(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# the JSON-RPC client
# --------------------------------------------------------------------------

class AppServerClient:
    """Line-delimited JSON-RPC over the codex child's own pipes."""

    def __init__(self, argv: list[str], env: dict[str, str], log: Callable[[str, Any], None]):
        self._log = log
        try:
            self.proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            raise CodexUnavailable("absent", f"{argv[0]}: {exc}") from exc
        self._buf = ""
        self._next_id = 0

    # -- wire ------------------------------------------------------------
    def send(self, obj: dict[str, Any]) -> None:
        self._log("send", obj)
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise CodexUnavailable("protocol", f"codex closed stdin: {exc}") from exc

    def request(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def read_message(self, deadline: float) -> dict[str, Any] | None:
        """Next JSON object, or None when the deadline passes or codex exits."""
        assert self.proc.stdout is not None
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    self._log("recv_unparsed", {"line": line[:4000]})
                    continue
                self._log("recv", obj)
                return obj
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.proc.stdout], [], [], min(remaining, 1.0))
            if not ready:
                if self.proc.poll() is not None and not self._buf:
                    return None
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                return None
            self._buf += chunk.decode("utf-8", "replace")

    def stderr_tail(self, limit: int = 2000) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            os.set_blocking(self.proc.stderr.fileno(), False)
            data = self.proc.stderr.read() or ""
        except (OSError, ValueError):
            data = ""
        return data[-limit:]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:  # pragma: no cover -- defensive
                    pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------------------
# one turn
# --------------------------------------------------------------------------

def _agent_texts(items: Iterable[dict[str, Any]]) -> list[str]:
    out = []
    for item in items or ():
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out


def dispatch(
    *,
    model: str,
    effort: str,
    cwd: str,
    prompt: str,
    launcher: str | None = None,
    resume_thread_id: str | None = None,
    sandbox: str = "read-only",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    log_path: Path | None = None,
) -> DispatchResult:
    """Run exactly one codex turn and return its final answer.

    Raises CodexUnavailable (rc 42) or CodexTurnFailed (rc 3).  Never returns
    an empty answer with a success status.
    """
    if not model or not effort:
        raise ValueError("model and effort are required; there is no default")

    from token_kit.codex import launcher as launcher_mod

    # The profile decides HOW codex is reached; a missing launcher, a missing
    # binary or a node-local home that cannot be made are all rc 42 `absent`.
    # There is no fall-through to a bare `codex` against a shared-filesystem
    # CODEX_HOME -- that is the corruption the launcher exists to prevent.
    try:
        launch = launcher_mod.prepare(override=launcher)
    except launcher_mod.LauncherUnavailable as exc:
        raise CodexUnavailable(exc.reason, exc.detail) from exc

    log_file = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8")

    def log(direction: str, obj: Any) -> None:
        if log_file is None:
            return
        log_file.write(json.dumps({"t": time.time(), "dir": direction, "msg": obj}) + "\n")
        log_file.flush()

    started = time.monotonic()
    client = AppServerClient(launch.argv, child_env(launch.env), log)
    try:
        # -- handshake ---------------------------------------------------
        init_id = client.request(
            "initialize",
            {"clientInfo": {"name": "token_kit.codex.dispatch", "title": "token_kit dispatch",
                            "version": "1"}},
        )
        init_deadline = time.monotonic() + INIT_DEADLINE_S
        while True:
            msg = client.read_message(init_deadline)
            if msg is None:
                tail = client.stderr_tail()
                reason = "auth" if any(m in tail.lower() for m in _AUTH_MARKERS) else "absent"
                raise CodexUnavailable(
                    reason,
                    f"no initialize response (rc={client.proc.poll()}) {tail.strip()[-300:]}",
                )
            if msg.get("id") == init_id:
                if "error" in msg:
                    detail = json.dumps(msg["error"])
                    reason = "auth" if any(m in detail.lower() for m in _AUTH_MARKERS) else "protocol"
                    raise CodexUnavailable(reason, detail)
                break
        # The handshake answered: codex's own first-run work on this node is
        # done, so the next child may start.  The lock never covers the turn.
        launch.release_startup()
        client.notify("initialized", {})

        # -- thread ------------------------------------------------------
        if resume_thread_id:
            thread_id_req = client.request(
                "thread/resume",
                {"threadId": resume_thread_id, "model": model, "cwd": cwd, "sandbox": sandbox},
            )
        else:
            thread_id_req = client.request(
                "thread/start",
                {"model": model, "cwd": cwd, "sandbox": sandbox, "approvalPolicy": "never"},
            )
        thread_deadline = time.monotonic() + INIT_DEADLINE_S
        thread_id = ""
        while True:
            msg = client.read_message(thread_deadline)
            if msg is None:
                raise CodexUnavailable(
                    "protocol", f"no thread response {client.stderr_tail()[-300:]}"
                )
            if msg.get("id") == thread_id_req:
                if "error" in msg:
                    detail = json.dumps(msg["error"])
                    if any(m in detail.lower() for m in _AUTH_MARKERS):
                        raise CodexUnavailable("auth", detail)
                    raise CodexTurnFailed(f"thread refused: {detail}")
                result = msg.get("result") or {}
                thread = result.get("thread") or {}
                thread_id = thread.get("id") or result.get("threadId") or resume_thread_id or ""
                break
        if not thread_id:
            raise CodexTurnFailed("codex returned no thread id")

        # -- the turn ----------------------------------------------------
        turn_req = client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": model,
                "effort": effort,
                "cwd": cwd,
            },
        )
        deadline = time.monotonic() + timeout_s
        turn_id = ""
        texts: list[str] = []
        usage: dict[str, Any] = {}
        while True:
            msg = client.read_message(deadline)
            if msg is None:
                if turn_id:
                    client.notify("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                raise CodexTurnFailed(
                    f"turn did not complete within {timeout_s:g}s "
                    f"(thread {thread_id}) {client.stderr_tail()[-300:]}"
                )
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "turn/started":
                turn_id = ((params.get("turn") or {}).get("id")) or params.get("turnId") or turn_id
            elif method == "item/completed":
                texts.extend(_agent_texts([params.get("item") or {}]))
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage") or usage
            elif method is not None and "id" in msg:
                # A server->client REQUEST we do not implement: answer it, or
                # the turn stalls forever waiting on us.
                client.send({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601,
                                       "message": f"{method} unsupported by token_kit dispatch"}})
                continue

            turn: dict[str, Any] | None = None
            if method == "turn/completed":
                turn = params.get("turn") or {}
            elif msg.get("id") == turn_req:
                if "error" in msg:
                    raise CodexTurnFailed(f"turn/start error: {json.dumps(msg['error'])}")
                turn = (msg.get("result") or {}).get("turn") or {}
                if turn.get("status") in (None, "inProgress"):
                    turn = None
            if turn is None:
                continue

            status = turn.get("status")
            turn_id = turn.get("id") or turn_id
            texts.extend(t for t in _agent_texts(turn.get("items") or []) if t not in texts)
            if status != "completed":
                from token_kit.codex import errors as _errors

                error = turn.get("error") or {}
                if _errors.is_quota(error):
                    # Out of quota is not "codex ran and failed" (rc 3, retry):
                    # it is "codex is unavailable" (rc 42, do it yourself), and
                    # it boards a cooldown so the next caller does not re-hit it.
                    marker = _errors.board_quota(json.dumps(error))
                    raise CodexUnavailable("quota", f"{error.get('message', '')} "
                                                    f"cooldown boarded at {marker}")
                err = error.get("message") or status or "unknown"
                raise CodexTurnFailed(f"turn status={status}: {err}")
            answer = texts[-1].strip() if texts else ""
            if not answer:
                raise CodexTurnFailed("turn completed with no assistant message")
            return DispatchResult(
                answer=answer,
                thread_id=thread_id,
                turn_id=turn_id,
                usage=usage,
                duration_s=time.monotonic() - started,
                resumed=bool(resume_thread_id),
            )
    finally:
        client.close()
        # Not an exec, so this always runs: auth.json back to the shared home
        # (newer wins) and the startup lock dropped even on a failed turn.
        launch.finish()
        if log_file is not None:
            log_file.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cap_bytes(answer: str, max_bytes: int, full_path: Path) -> str:
    raw = answer.encode("utf-8")
    if len(raw) <= max_bytes:
        return answer
    head = raw[:max_bytes].decode("utf-8", "ignore")
    return f"{head}\n[truncated at {max_bytes} of {len(raw)} bytes; full answer: {full_path}]"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dispatch", add_help=True,
                                description="run one codex turn on demand")
    p.add_argument("--model", required=True, help="REQUIRED; there is no default, ever")
    p.add_argument("--effort", required=True, help="REQUIRED; there is no default, ever")
    p.add_argument("--cwd", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--resume", default=None, metavar="THREAD_ID")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--launcher", default=None)
    p.add_argument("--sandbox", default="read-only", choices=list(SANDBOX_MODES))
    p.add_argument("--max-children", type=int, default=DEFAULT_MAX_CHILDREN)
    p.add_argument("--wait-s", type=float, default=DEFAULT_WAIT_S)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK

    if args.max_bytes < MIN_MAX_BYTES:
        print(f"dispatch: --max-bytes below the {MIN_MAX_BYTES} floor", file=sys.stderr)
        return EXIT_USAGE
    if args.max_children < 1:
        print("dispatch: --max-children must be >= 1", file=sys.stderr)
        return EXIT_USAGE
    task_file = Path(args.task_file)
    if not task_file.is_file():
        print(f"dispatch: --task-file not found: {task_file}", file=sys.stderr)
        return EXIT_USAGE
    if not os.path.isabs(args.cwd) or not os.path.isdir(args.cwd):
        print(f"dispatch: --cwd must be an existing absolute directory: {args.cwd}",
              file=sys.stderr)
        return EXIT_USAGE

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = task_file.read_text(encoding="utf-8")

    try:
        slot = Slot.acquire(args.max_children, args.wait_s)
    except CodexUnavailable as exc:
        print(f"CODEX_UNAVAILABLE reason={exc.reason} {exc.detail} "
              "-- do the step yourself with a Claude model", file=sys.stderr)
        return EXIT_BUSY_OR_ABSENT

    with slot:
        try:
            result = dispatch(
                model=args.model,
                effort=args.effort,
                cwd=args.cwd,
                prompt=prompt,
                launcher=args.launcher,
                resume_thread_id=args.resume,
                sandbox=args.sandbox,
                timeout_s=args.timeout_s,
                log_path=Path(f"{out_path}.log.jsonl"),
            )
        except CodexUnavailable as exc:
            print(f"CODEX_UNAVAILABLE reason={exc.reason} {exc.detail} "
                  "-- do the step yourself with a Claude model", file=sys.stderr)
            return EXIT_BUSY_OR_ABSENT
        except CodexTurnFailed as exc:
            print(f"CODEX_FAILED {exc}", file=sys.stderr)
            return EXIT_CODEX_FAILED

    out_path.write_text(result.answer + "\n", encoding="utf-8")
    Path(f"{out_path}.thread").write_text(result.thread_id + "\n", encoding="utf-8")
    print(f"dispatch: thread={result.thread_id} turn={result.turn_id} "
          f"{result.duration_s:.1f}s usage={json.dumps(result.usage) if result.usage else 'none'}",
          file=sys.stderr)
    sys.stdout.write(cap_bytes(result.answer, args.max_bytes, out_path) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
