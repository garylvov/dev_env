"""launcher.py -- the kit's OWN codex launcher: a node-local CODEX_HOME, in process.

WHY THIS FILE EXISTS
    codex keeps its state in SQLite (state_5.sqlite, logs_2.sqlite, memories,
    goals).  SQLite's POSIX locking is unreliable over NFS, so a CODEX_HOME on
    a shared filesystem fails on launch with "(code: 15) locking protocol".
    The operator's `~/run_codex.bash` solves that for an interactive session by
    putting CODEX_HOME on node-local tmp and re-seeding it each run.  This
    module is the SAME BEHAVIOUR, owned by the kit, so nothing the kit runs
    depends on a file that lives outside the repo -- and so the races that a
    shell script cannot fix (a torn `cp` onto the shared auth.json, two
    children seeding a fresh node at once) are fixed here instead.

    `~/run_codex.bash` stays exactly as it is: the operator's interactive
    launcher, and the behaviour spec this file mirrors item for item.

WHAT IT MIRRORS, ITEM FOR ITEM
    node-local home      <tmp_root>/<home_prefix>-<user>, the SAME directory the
                         wrapper uses, so one seeded home serves both.  Sharing
                         is safe: a CODEX_HOME is designed for concurrent codex
                         processes (that is the ordinary laptop case) and the
                         store is on node-local disk, where SQLite locking
                         works.  Sharing is also what makes it cheap -- the
                         32 MB state DB is seeded once per node, not per child.
    config refresh       config.toml, version.json, models_cache.json,
                         cloud-config-bundle-cache.json copied shared -> local
                         every run (small, keeps config current).
    auth.json            newer wins, BOTH directions, at start and at exit,
                         including the `logout` case (both copies removed).
    SQLite seeding       state_5 / memories_1 / goals_1 copied only when absent
                         on the node, under a node-local lock.
    thread caps          TOKIO_WORKER_THREADS, RAYON_NUM_THREADS,
                         UV_THREADPOOL_SIZE, NODE_OPTIONS -- defaults only, so
                         a caller may override (dispatch raises the tokio floor).
    `update`             runs against the SHARED home: `codex update` detects a
                         standalone install by looking under CODEX_HOME, and a
                         node-local home has no packages/standalone.
    bypass flag          profile key, default true, exactly as the wrapper.
    exit sync            we do not exec: the sync back runs when the process
                         ends, including on SIGTERM (install_exit_sync).

WHAT IT FIXES THAT THE WRAPPER CANNOT
    atomic_copy          every copy onto the SHARED home is a temp file in the
                         same directory plus os.replace, so a reader never sees
                         a half-written auth.json.  `cp -f` truncates in place.
    SeedLock             two children landing on a fresh node together seed the
                         32 MB store once, not twice into each other's bytes.
    StartupLock          codex's own first-run work on a node is serialised
                         until the app-server handshake answers -- startup
                         only, never the turn.
"""

from __future__ import annotations

import errno
import getpass
import os
import shutil
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: Copied from the shared home on EVERY run: small, and they carry the config.
REFRESH_FILES = ("config.toml", "version.json", "models_cache.json",
                 "cloud-config-bundle-cache.json")
#: Seeded once per node, only when absent: these are the big ones.
SEED_DBS = ("state_5.sqlite", "memories_1.sqlite", "goals_1.sqlite")
#: Synced back to the shared home at exit when the sync switch is on.
SYNC_BACK_DBS = ("memories_1.sqlite", "goals_1.sqlite")

AUTH_FILE = "auth.json"

#: Thread caps, measured by the operator against codex 0.144.3 (see the
#: wrapper's own comment).  DEFAULTS: a caller that already set one keeps it.
THREAD_CAPS = {
    "TOKIO_WORKER_THREADS": "4",
    "RAYON_NUM_THREADS": "4",
    "UV_THREADPOOL_SIZE": "2",
    "NODE_OPTIONS": "--v8-pool-size=2",
}

BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
#: What a caller may type for it.  The profile key `bypass_approvals` is an
#: OFF switch, default on: codex runs yolo here, as the wrapper always did.
#: It does not defeat a per-thread sandbox: MEASURED on codex-cli 0.153.4, a
#: `thread/start {sandbox: "read-only"}` still comes back
#: `sandbox {type: readOnly, networkAccess: false}` with this flag on argv.
BYPASS_SYNONYMS = (BYPASS_FLAG, "--yolo")

#: Where the shared (usually NFS) codex home is.  Overridable for tests.
SHARED_HOME_ENV = "TOKEN_KIT_CODEX_SHARED_HOME"
#: Point the config loader at a scratch directory of config files (tests).
CONFIG_DIR_ENV = "TOKEN_KIT_CONFIG_DIR"
#: Which file in it to read, without the .toml (tests).
CONFIG_NAME_ENV = "TOKEN_KIT_CONFIG_NAME"
#: Copy memories/goals back to the shared home at exit.
SYNC_BACK_ENVS = ("TOKEN_KIT_CODEX_SYNC", "RUN_CODEX_SYNC")

#: How long a second child waits for the first one's node-local first run.
SEED_WAIT_S = 120.0
STARTUP_WAIT_S = 120.0


# --------------------------------------------------------------------------
# the profile's [codex] table -- the AUTHORITY for how codex is reached
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CodexConfig:
    """How codex is reached on this machine, with its defaults filled in.

    `node_local_home` is the key that matters: "this machine's codex home must
    be on node-local disk".  True on a cluster whose home is NFS; false on a
    workstation.  When it is true the kit uses its OWN launcher, always, and a
    bare `codex` against the shared home is refused rather than run.
    """

    launcher: str = "codex"
    node_local_home: bool = False
    tmp_root: Path = field(default_factory=lambda: Path("/tmp"))
    home_prefix: str = "codex-home"
    binary: str = ""
    bypass_approvals: bool = True


def config_dir() -> Path | None:
    """An explicit directory of config files, or None (the normal case)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    return Path(override) if override else None


def load_config(directory: Path | None = None, name: str | None = None) -> CodexConfig:
    """The `[codex]` answer for this machine.  Never raises on absence.

    Everything is detected or defaulted (`token_kit.config`); a `[codex]` table
    in the one optional override file wins over both.  `directory`/`name` name
    an explicit config FILE instead -- the seam a guard uses to hand this
    function a scratch `[codex]` table without touching the real machine.
    """
    from token_kit import config as config_mod

    directory = Path(directory) if directory else config_dir()
    chosen = name or os.environ.get(CONFIG_NAME_ENV) or ""
    src = directory / f"{chosen or 'config'}.toml" if directory else None
    resolved = config_mod.resolve(src if src and src.is_file() else None)
    return CodexConfig(
        launcher=str(resolved.codex_launcher),
        node_local_home=bool(resolved.codex_node_local_home),
        tmp_root=Path(resolved.codex_tmp_root),
        home_prefix=str(resolved.codex_home_prefix),
        binary=str(resolved.codex_binary),
        bypass_approvals=bool(resolved.codex_bypass_approvals),
    )


def shared_home() -> Path:
    """The codex home that survives a node change -- usually on NFS."""
    override = os.environ.get(SHARED_HOME_ENV)
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.codex"))


def node_local_home(config: CodexConfig) -> Path:
    return config.tmp_root / f"{config.home_prefix}-{getpass.getuser()}"


def resolve_binary(config: CodexConfig) -> str:
    """configured binary, else the standalone install, else PATH.  "" if absent."""
    if config.binary:
        candidate = os.path.expanduser(config.binary)
        return candidate if os.access(candidate, os.X_OK) else ""
    standalone = os.path.expanduser("~/.local/bin/codex")
    if os.access(standalone, os.X_OK):
        return standalone
    return shutil.which("codex") or ""


# --------------------------------------------------------------------------
# atomic copy -- never a torn file at the destination
# --------------------------------------------------------------------------

def atomic_copy(src: Path, dst: Path) -> bool:
    """Copy src over dst with NO window in which dst is partial.

    `cp -f` opens the destination and truncates it, so a concurrent reader can
    see an empty or half-written auth.json.  We write a temp file in the SAME
    directory (same filesystem, so the rename is atomic) and os.replace it.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f".{dst.name}.tk-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except OSError:
        try:
            tmp.unlink()
        except (OSError, NameError, UnboundLocalError):
            pass
        return False


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def sync_auth(shared: Path, local: Path) -> str:
    """auth.json, newer wins, in BOTH directions.  Returns what moved.

    codex rewrites the token under CODEX_HOME, and `codex login` writes there
    too, so seeding one way clobbers a fresh login with a revoked token.
    """
    a, b = shared / AUTH_FILE, local / AUTH_FILE
    ta, tb = _mtime_ns(a), _mtime_ns(b)
    if ta >= 0 and tb < 0:
        return "shared->local" if atomic_copy(a, b) else "none"
    if tb >= 0 and ta < 0:
        return "local->shared" if atomic_copy(b, a) else "none"
    if ta < 0 and tb < 0:
        return "none"
    if ta > tb:
        return "shared->local" if atomic_copy(a, b) else "none"
    if tb > ta:
        return "local->shared" if atomic_copy(b, a) else "none"
    return "none"


# --------------------------------------------------------------------------
# node-local locks: seeding, and startup
# --------------------------------------------------------------------------

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


def _pid_start_time(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat.  A pid alone is not an identity."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    tail = stat.rsplit(")", 1)[-1].split()
    return tail[19] if len(tail) > 19 else ""


class NodeLock:
    """A node-local mkdir lock whose holder is identified by pid + start time.

    mkdir is the atomic primitive; a holder that died leaves a directory whose
    pid is gone or whose start time moved, and the next caller reclaims it
    instead of waiting forever.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self, wait_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.mkdir()
            except FileExistsError:
                if self._reclaim_if_dead():
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
                continue
            except OSError:
                return False
            (self.path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            (self.path / "starttime").write_text(
                _pid_start_time(os.getpid()) + "\n", encoding="utf-8")
            self.held = True
            return True

    def _reclaim_if_dead(self) -> bool:
        try:
            pid = int((self.path / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            # A holder that has not written its pid yet is a microsecond old,
            # not dead: only an OLD unnamed lock is reclaimed.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < 30:
                return False
            shutil.rmtree(self.path, ignore_errors=True)
            return True
        recorded = ""
        try:
            recorded = (self.path / "starttime").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if _pid_alive(pid) and (not recorded or recorded == _pid_start_time(pid)):
            return False
        shutil.rmtree(self.path, ignore_errors=True)
        return True

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.path, ignore_errors=True)
            self.held = False

    def __enter__(self) -> "NodeLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# preparing the node-local home
# --------------------------------------------------------------------------

def prepare_home(config: CodexConfig, *, reseed: bool = False) -> Path:
    """Make the node-local CODEX_HOME usable, and return it.

    Raises OSError when the node-local home cannot be made -- the caller turns
    that into rc 42 `absent`.  It never falls back to the shared home: that
    fallback IS the corruption this module exists to prevent.
    """
    home = node_local_home(config)
    shared = shared_home()
    if reseed:
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass

    for name in REFRESH_FILES:
        src = shared / name
        if src.is_file():
            atomic_copy(src, home / name)

    sync_auth(shared, home)

    # Seeding is the expensive, once-per-node half: serialise it so two
    # children starting together do not copy 32 MB into each other.
    missing = [db for db in SEED_DBS
               if not (home / db).exists() and (shared / db).is_file()]
    if missing:
        lock = NodeLock(config.tmp_root / f"{config.home_prefix}-{getpass.getuser()}.seed.lock")
        if lock.acquire(SEED_WAIT_S):
            try:
                for db in SEED_DBS:
                    if not (home / db).exists() and (shared / db).is_file():
                        atomic_copy(shared / db, home / db)
            finally:
                lock.release()
    return home


def sync_back(config: CodexConfig, *, logout: bool = False) -> None:
    """The wrapper's EXIT trap: auth both ways, memories/goals on request.

    `logout` removes BOTH auth copies, so the next start cannot resurrect a
    revoked token from the shared home.
    """
    home = node_local_home(config)
    shared = shared_home()
    if logout:
        for path in (shared / AUTH_FILE, home / AUTH_FILE):
            try:
                path.unlink()
            except OSError:
                pass
        return
    sync_auth(shared, home)
    if any(os.environ.get(name) == "1" for name in SYNC_BACK_ENVS):
        for db in SYNC_BACK_DBS:
            src = home / db
            if src.is_file():
                atomic_copy(src, shared / db)


def thread_cap_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The four caps as DEFAULTS: a caller's own value always wins."""
    env = dict(os.environ if base is None else base)
    for key, value in THREAD_CAPS.items():
        env.setdefault(key, value)
    return env


# --------------------------------------------------------------------------
# the launch
# --------------------------------------------------------------------------

class LauncherUnavailable(Exception):
    """codex cannot be launched at all.  `reason` is the rc 42 word."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"reason={reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


@dataclass
class PreparedLaunch:
    """An argv + env that is ready to become a codex child, and its cleanup."""

    argv: list[str]
    env: dict[str, str]
    config: CodexConfig
    home: Path | None = None
    internal: bool = False
    startup_lock: NodeLock | None = None

    def release_startup(self) -> None:
        """Call this the moment the app-server handshake answers.

        The lock covers STARTUP only -- codex's own first-run work on a node --
        never the turn, which may run for half an hour.
        """
        if self.startup_lock is not None:
            self.startup_lock.release()
            self.startup_lock = None

    def finish(self, *, logout: bool = False) -> None:
        self.release_startup()
        if self.internal:
            sync_back(self.config, logout=logout)


def _launcher_argv(path: str, subcommand: tuple[str, ...]) -> list[str]:
    if path.endswith((".bash", ".sh")):
        return ["bash", path, *subcommand]
    return [path, *subcommand]


def prepare(
    *,
    override: str | None = None,
    config: CodexConfig | None = None,
    base_env: dict[str, str] | None = None,
    subcommand: tuple[str, ...] = ("app-server", "--stdio"),
    serialise_startup: bool = True,
    reseed: bool = False,
) -> PreparedLaunch:
    """Resolve HOW codex is started on this machine, and get it ready.

    THE PROFILE IS THE AUTHORITY.  There are exactly three outcomes:

      * an explicit `override` path -- the operator's own wrapper, or a test's
        fake.  It is trusted with CODEX_HOME and nothing is prepared for it;
        a path that is not there is rc 42 `absent`, never a bare-codex retry.
      * `node_local_home = true` -- the kit's own launcher, ALWAYS: the
        node-local home is prepared here and the binary is spawned directly,
        no shell in the path.  A home that cannot be prepared, or a binary that
        is not there, is rc 42 `absent`.
      * otherwise -- the resolved `launcher` value as given (a plain `codex` on
        a workstation), with no home preparation at all.
    """
    cfg = config or load_config()
    env = thread_cap_env(base_env)

    if override:
        probe = override.split()[0]
        if os.path.sep in probe and not os.path.exists(probe):
            raise LauncherUnavailable("absent", f"launcher not found: {probe}")
        if os.path.sep not in probe and shutil.which(probe) is None:
            raise LauncherUnavailable("absent", f"launcher not on PATH: {probe}")
        # A node-local profile with an inherited CODEX_HOME that is NOT
        # node-local is the exact corruption case: refuse rather than let
        # SQLite meet the shared filesystem.
        inherited = env.get("CODEX_HOME", "")
        if cfg.node_local_home and inherited and not _under(inherited, cfg.tmp_root):
            raise LauncherUnavailable(
                "absent",
                f"this machine requires a node-local codex home and "
                f"CODEX_HOME={inherited} is not under {cfg.tmp_root}")
        return PreparedLaunch(argv=_launcher_argv(override, subcommand), env=env, config=cfg)

    if cfg.node_local_home:
        binary = resolve_binary(cfg)
        if not binary:
            raise LauncherUnavailable(
                "absent",
                f"this machine requires the kit launcher and no codex binary "
                f"was found (configured binary={cfg.binary or 'unset'})")
        lock: NodeLock | None = None
        if serialise_startup:
            lock = NodeLock(cfg.tmp_root
                            / f"{cfg.home_prefix}-{getpass.getuser()}.startup.lock")
            if not lock.acquire(STARTUP_WAIT_S):
                raise LauncherUnavailable(
                    "busy", f"another codex start held {lock.path} for {STARTUP_WAIT_S:g}s")
        try:
            if subcommand and subcommand[0] == "update":
                # `codex update` detects a standalone install under CODEX_HOME;
                # the node-local home has no packages/standalone, so this one
                # verb runs against the SHARED home.  No SQLite is involved.
                env["CODEX_HOME"] = str(shared_home())
                home = None
            else:
                home = prepare_home(cfg, reseed=reseed)
                env["CODEX_HOME"] = str(home)
        except OSError as exc:
            if lock is not None:
                lock.release()
            raise LauncherUnavailable(
                "absent", f"cannot prepare a node-local codex home: {exc}") from exc
        argv = [binary]
        rest = list(subcommand)
        if cfg.bypass_approvals:
            # Always yolo, exactly like the wrapper, and `--yolo` stays a
            # synonym the caller may type without it appearing twice.
            rest = [a for a in rest if a not in BYPASS_SYNONYMS]
            argv.append(BYPASS_FLAG)
        else:
            # The key is off, but codex only knows the long form.
            rest = [BYPASS_FLAG if a == "--yolo" else a for a in rest]
        argv.extend(rest)
        return PreparedLaunch(argv=argv, env=env, config=cfg, home=home,
                              internal=True, startup_lock=lock)

    named = cfg.launcher or "codex"
    path = os.path.expanduser(named)
    if os.path.sep in path:
        if not os.path.exists(path):
            raise LauncherUnavailable(
                "absent", f"the configured launcher {named} is not there")
        return PreparedLaunch(argv=_launcher_argv(path, subcommand), env=env, config=cfg)
    found = shutil.which(path)
    if not found:
        raise LauncherUnavailable("absent", f"no {named} on PATH")
    return PreparedLaunch(argv=[found, *subcommand], env=env, config=cfg)


def _under(path: str, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False


def install_exit_sync(launch: PreparedLaunch) -> None:
    """Run the exit sync when THIS process ends -- including on SIGTERM.

    The wrapper deliberately does not `exec` so its EXIT trap runs.  We are the
    process, so the equivalent is an atexit hook plus a SIGTERM handler that
    chains to whatever was there before.
    """
    import atexit

    done = {"ran": False}

    def once() -> None:
        if done["ran"]:
            return
        done["ran"] = True
        try:
            launch.finish()
        except Exception:  # noqa: BLE001 -- an exit sync must never mask the exit
            pass

    atexit.register(once)
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):  # pragma: no cover -- exercised by a subprocess guard
        once()
        if callable(previous):
            previous(signum, frame)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    try:
        signal.signal(signal.SIGTERM, on_term)
    except ValueError:  # not the main thread
        pass


def this_host() -> str:
    """The one name every host comparison in the kit uses."""
    override = os.environ.get("TOKEN_KIT_HOSTNAME")
    return override or socket.gethostname()


# --------------------------------------------------------------------------
# CLI -- `codex-run [args...]`, the human's interactive entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Start interactive codex the way the kit does: node-local home, synced.

    This is the one-command replacement for the operator's bash wrapper on a
    NEW machine.  `--reseed` nukes the node-local home first, exactly as the
    wrapper's own flag does.
    """
    import subprocess

    args = list(sys.argv[1:] if argv is None else argv)
    reseed = False
    if args and args[0] == "--reseed":
        reseed = True
        args.pop(0)
    logout = bool(args) and args[0] == "logout"

    cfg = load_config()
    try:
        launch = prepare(config=cfg, subcommand=tuple(args), reseed=reseed,
                         serialise_startup=bool(args[:1] != ["update"]))
    except LauncherUnavailable as exc:
        print(f"codex-run: CODEX_UNAVAILABLE {exc}", file=sys.stderr)
        return 42
    print(f"codex-run: CODEX_HOME={launch.env.get('CODEX_HOME', '<inherited>')} "
          f"(node {this_host()})", file=sys.stderr)
    launch.release_startup()
    try:
        proc = subprocess.run(launch.argv, env=launch.env)
        return proc.returncode
    finally:
        # Not an exec: the sync back always runs, which is the whole reason the
        # wrapper does not exec either.
        launch.finish(logout=logout)


if __name__ == "__main__":
    raise SystemExit(main())
