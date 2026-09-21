"""Sealed dials for the rollover supervisor: CLI arguments plus optional TOML.

No env-var dials. Everything the supervisor uses is a default here, a key in
the `[supervisor]` table of the kit's optional override file (or of a file
named by `--config`), or a CLI argument.

RUNS FROM ANYWHERE. The supervisor is told which handoff file to watch:
`--state-file <path>`, defaulting to `./STATE.md` in the current directory.
Nothing here names a directory of work. The registry, the log and the markers
go under the XDG state dir, keyed by a hash of the state file's ABSOLUTE path,
so two supervised sessions in two different directories never share a lock, a
log or a rollover marker.

THE CEILING IS A COST DECISION, NOT A WINDOW DECISION.
Every call re-pays the whole standing context (cache_read is ~95% of spend), so
a session living at 900k pays roughly four times per call what one living at
235k pays. Rolling over EARLY is the saving; the usable window is irrelevant to
it. Raising these to "use the window" inverts the purpose; raise them only on a
measured cost argument.

What one such measurement DOES establish, kept because it makes this supervisor
matter more: native auto-compaction is not a fallback at 250k. 28
`compactMetadata` records over the five largest transcripts of a 1M-window model
show `trigger="auto"` firing at preTokens 967042 .. 1006280, median ~999800 --
i.e. at ~1,000,000, not at the `autoCompactWindow: 250000` that settings.json
carries. Left alone, a 1M session runs to ~1e6 tokens before anything stops it.
For a 200k-window model the same method gives 150000 / 180000.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

BIN = Path(__file__).resolve().parent / "bin" / "token-kit-supervise"

#: The default handoff file, relative to wherever the operator is standing.
DEFAULT_STATE_FILE = "STATE.md"


@dataclass
class Config:
    # --- thresholds (context tokens of the last main-thread request) ---------
    soft_tokens: int = 180000        # ask the session to bring the state file current
    hard_tokens: int = 235000        # roll over regardless
    drain_wait_secs: int = 600       # how long in-flight work may drain
    poll_secs: int = 60              # transcript poll period

    # --- what is watched, and where its bookkeeping lives --------------------
    # The handoff file. `--state-file`; default ./STATE.md. Its absolute path
    # is the identity of this supervised session.
    state_file: str = DEFAULT_STATE_FILE
    # The directory a relaunched session starts in. Empty = the state file's
    # own directory.
    cwd: str = ""
    # Where the registry, log, markers and seeds go. Empty = the XDG state dir
    # keyed by a hash of the state file's absolute path.
    run_root: str = ""
    claude_home: str = ""            # filled from ~/.claude when empty

    # --- relaunch identity ---------------------------------------------------
    # The relaunched session must be the same animal: same binary, same flags.
    claude_bin: str = "claude"
    claude_flags: str = ""
    tmux_bin: str = "tmux"
    tmux_prefix: str = "ccsup"
    # The seed reaches the new session as ONE short CLI argument naming the seed
    # FILE. Never `tmux send-keys` into a prompt -- forbidden outright. 0
    # restores the defect where the seed was written and nobody read it.
    seed_as_arg: bool = True
    # Resolve the new session's pid and spawn its watcher. Without this the
    # supervisor survives exactly one rollover.
    autowatch: bool = True
    launch_pid_wait_secs: int = 90
    supervisor_cmd: str = str(BIN)   # how a watcher re-invokes this tool

    # --- kill discipline -----------------------------------------------------
    # SIGTERM, then SIGKILL after a bounded wait. A kill is verified by the
    # pid's kernel start time no longer matching, never by an exit code.
    term_wait_secs: int = 30
    kill_poll_secs: int = 3

    # --- heartbeat -----------------------------------------------------------
    heartbeat_stale_secs: int = 240

    # --- test seams ----------------------------------------------------------
    # Recorders stand in for the real actions so a guard can run with nothing
    # real launched or killed. Empty means "do the real thing".
    rollover_cmd: str = ""
    kill_cmd: str = ""

    # ---------------------------------------------------------------- derived
    @property
    def state_path(self) -> Path:
        return Path(os.path.expanduser(self.state_file)).absolute()

    @property
    def work_dir(self) -> Path:
        """Where a relaunched session starts: `--cwd`, else beside the state file."""
        return Path(os.path.expanduser(self.cwd)).absolute() if self.cwd \
            else self.state_path.parent

    @property
    def session_key(self) -> str:
        """The identity of this supervised session: a hash of the ABSOLUTE
        state-file path. Two projects, two keys, no collision -- and no part of
        anyone's directory names ends up in a shared state directory."""
        return hashlib.sha256(str(self.state_path).encode()).hexdigest()[:16]

    @property
    def run_dir(self) -> Path:
        if self.run_root:
            return Path(os.path.expanduser(self.run_root)) / self.session_key
        from token_kit import config as config_mod
        return config_mod.state_home() / "supervise" / self.session_key

    @property
    def log(self) -> Path:
        return self.run_dir / "ccsup.log"

    @property
    def heartbeat(self) -> Path:
        return self.run_dir / "watch.heartbeat"

    @property
    def lock_dir(self) -> Path:
        return self.run_dir / "watch.lock"

    @property
    def request(self) -> Path:
        """The SOFT channel: an appended request beside the state file, where
        the session it is addressed to is already looking."""
        return self.state_path.parent / "ROLLOVER_REQUEST.md"

    @property
    def flags_file(self) -> Path:
        return self.run_dir / "launch.flags"

    @property
    def pid_file(self) -> Path:
        return self.run_dir / "session.pid"


_NAMES = {f.name: f.type for f in fields(Config)}


def _coerce(name: str, value):
    want = _NAMES[name]
    if want == "int":
        return int(value)
    if want == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return str(value)


def _apply(cfg: Config, table: dict, where: str) -> None:
    for key, value in table.items():
        if key not in _NAMES:
            raise SystemExit(f"supervise: unknown config key in {where}: {key}")
        setattr(cfg, key, _coerce(key, value))


def load(config_file: str | Path | None = None, overrides: dict | None = None) -> Config:
    """Defaults <- the kit's override file <- --config file <- CLI. Unknown keys refuse."""
    cfg = Config()
    cfg.claude_home = str(Path.home() / ".claude")

    from token_kit import config as config_mod
    kit = config_mod.read_override()
    if isinstance(kit.get("supervisor"), dict):
        _apply(cfg, kit["supervisor"], str(config_mod.override_path()))

    if config_file:
        src = Path(config_file)
        if not src.is_file():
            raise SystemExit(f"supervise: no such config file: {src}")
        with src.open("rb") as fh:
            raw = tomllib.load(fh)
        _apply(cfg, raw.get("supervisor", raw), str(src))

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key not in _NAMES:
            raise SystemExit(f"supervise: unknown dial: {key}")
        setattr(cfg, key, _coerce(key, value))

    return cfg
