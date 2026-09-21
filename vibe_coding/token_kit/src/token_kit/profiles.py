"""Machine profiles: every machine-specific fact, in one small TOML per machine kind.

Autodetect is deliberately crude and explicit: Slurm plus /oscar means the
cluster, everything else is a workstation. `--profile` always wins.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Profile:
    name: str
    source: Path
    data_root: Path
    campaign_dir: Path
    evidence_dir: Path
    codex_launcher: str
    python_hooks: bool
    slurm: bool
    hook_matcher: str
    settings: dict = field(default_factory=dict)

    def as_env(self) -> dict:
        """The values the kit's own scripts read, as a flat env mapping."""
        return {
            "TOKEN_KIT_PROFILE": self.name,
            "TK_DATA_ROOT": str(self.data_root),
            "TK_CAMPAIGN_DIR": str(self.campaign_dir),
            "TK_EVIDENCE_DIR": str(self.evidence_dir),
            "TK_CODEX_LAUNCHER": self.codex_launcher,
            "TK_PYTHON_HOOKS": "yes" if self.python_hooks else "no",
            "TK_SLURM": "yes" if self.slurm else "no",
        }


def detect() -> str:
    """Name of the profile this machine looks like."""
    if shutil.which("sbatch") and Path("/oscar").is_dir():
        return "oscar"
    return "workstation"


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def load(profiles_dir: Path, name: str | None = None) -> Profile:
    chosen = name or detect()
    src = profiles_dir / f"{chosen}.toml"
    if not src.is_file():
        available = ", ".join(sorted(p.stem for p in profiles_dir.glob("*.toml"))) or "none"
        raise SystemExit(f"install: no such profile: {src}\n  available: {available}")
    with src.open("rb") as fh:
        raw = tomllib.load(fh)

    paths = raw.get("paths", {})
    host = raw.get("host", {})
    return Profile(
        name=raw.get("name", chosen),
        source=src,
        data_root=_path(paths.get("data_root", "~")),
        campaign_dir=_path(paths.get("campaign_dir", "~/token_kit/campaign")),
        evidence_dir=_path(paths.get("evidence_dir", "~/token_kit/evidence")),
        codex_launcher=raw.get("codex", {}).get("launcher", "codex"),
        python_hooks=bool(host.get("python_hooks", True)),
        slurm=bool(host.get("slurm", False)),
        hook_matcher=raw.get("hook", {}).get("matcher", "*"),
        settings=dict(raw.get("settings", {})),
    )
