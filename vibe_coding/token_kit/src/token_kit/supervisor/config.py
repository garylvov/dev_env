"""Sealed dials for the rollover supervisor: a TOML file plus CLI arguments.

No env-var dials. Everything the supervisor uses is a key here, a key in the
TOML file named by `--config`, or a CLI argument. Machine paths come from
`token_kit.profiles`, never from a literal in this file.

THE CEILING IS A COST DECISION, NOT A WINDOW DECISION.
Every call re-pays the whole standing context (cache_read is ~95% of spend), so
a session living at 900k pays roughly four times per call what one living at
235k pays. Rolling over EARLY is the saving; the usable window is irrelevant to
it. A lane raised these to 900000/950000 on 2026-09-20 reasoning "235k is 23%
of the window -- waste"; that inverts the campaign's purpose and the main thread
reverted it the same night. Do not raise them to "use the window"; raise them
only on a measured cost argument.

What that lane's measurement DOES establish, kept because it makes this
supervisor matter more: native auto-compaction is NOT a fallback at 250k. 28
`compactMetadata` records over the five largest transcripts of a 1M-window model
show `trigger="auto"` firing at preTokens 967042 .. 1006280, median ~999800 --
i.e. at ~1,000,000, not at the `autoCompactWindow: 250000` that settings.json
carries. Left alone, a 1M session runs to ~1e6 tokens before anything stops it.
For a 200k-window model the same method gives 150000 / 180000.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[3]
PROFILES_DIR = KIT_DIR / "profiles"
BIN = Path(__file__).resolve().parent / "bin" / "token-kit-supervise"


@dataclass
class Config:
    # --- thresholds (context tokens of the last main-thread request) ---------
    soft_tokens: int = 180000        # ask the session to bring STATE.md current
    hard_tokens: int = 235000        # roll over regardless
    drain_wait_secs: int = 600       # how long in-flight work may drain
    poll_secs: int = 60              # transcript poll period

    # --- where state lives ---------------------------------------------------
    campaign_dir: str = ""           # filled from the profile when empty
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

    @property
    def run_dir(self) -> Path:
        return Path(self.campaign_dir) / ".ccsup"

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
        return Path(self.campaign_dir) / "ROLLOVER_REQUEST.md"

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


def load(config_file: str | Path | None = None, overrides: dict | None = None,
         profile: str | None = None) -> Config:
    """Defaults <- profile <- TOML file <- CLI overrides. Unknown keys refuse."""
    cfg = Config()

    from token_kit import profiles
    prof = profiles.load(PROFILES_DIR, profile)
    cfg.campaign_dir = str(prof.campaign_dir)
    cfg.claude_home = str(Path.home() / ".claude")

    if config_file:
        src = Path(config_file)
        if not src.is_file():
            raise SystemExit(f"supervise: no such config file: {src}")
        with src.open("rb") as fh:
            raw = tomllib.load(fh)
        table = raw.get("supervisor", raw)
        for key, value in table.items():
            if key not in _NAMES:
                raise SystemExit(f"supervise: unknown config key in {src}: {key}")
            setattr(cfg, key, _coerce(key, value))

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key not in _NAMES:
            raise SystemExit(f"supervise: unknown dial: {key}")
        setattr(cfg, key, _coerce(key, value))

    return cfg
