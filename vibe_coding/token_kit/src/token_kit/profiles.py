"""Machine profiles: every machine-specific fact, in one small TOML per machine kind.

Autodetect is crude and explicit, and -- this is the part that matters for a
PUBLIC repo -- the evidence it looks at is DECLARED BY THE PROFILE, not written
into this module. Each profile carries a `[detect]` table naming the commands
and directories that identify that machine, plus a `priority`; the most
specific match wins and the fallback is the profile with the lowest priority
(`workstation`). So a site path appears in exactly one place, the site's own
profile TOML, and `cli.py census` can hold every module at zero.

`--profile` always wins.
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
    reference_source: Path
    codex_launcher: str
    python_hooks: bool
    slurm: bool
    hook_matcher: str
    session_start: bool = True
    settings: dict = field(default_factory=dict)

    def as_env(self) -> dict:
        """The values the kit's own scripts read, as a flat env mapping."""
        return {
            "TOKEN_KIT_PROFILE": self.name,
            "TK_DATA_ROOT": str(self.data_root),
            "TK_CAMPAIGN_DIR": str(self.campaign_dir),
            "TK_EVIDENCE_DIR": str(self.evidence_dir),
            "TK_REFERENCE_SOURCE": str(self.reference_source),
            "TK_CODEX_LAUNCHER": self.codex_launcher,
            "TK_PYTHON_HOOKS": "yes" if self.python_hooks else "no",
            "TK_SLURM": "yes" if self.slurm else "no",
        }


def _matches(detect_table: dict) -> bool:
    """Does this machine satisfy a profile's declared `[detect]` evidence?

    An empty table never matches on its own -- that is the fallback profile,
    chosen by priority when nothing else matched.
    """
    cmds = detect_table.get("requires_cmd") or []
    dirs = detect_table.get("requires_dir") or []
    if isinstance(cmds, str):
        cmds = [cmds]
    if isinstance(dirs, str):
        dirs = [dirs]
    if not cmds and not dirs:
        return False
    return (all(shutil.which(c) for c in cmds)
            and all(Path(os.path.expanduser(d)).is_dir() for d in dirs))


def detect(profiles_dir: Path | None = None) -> str:
    """Name of the profile this machine looks like, read from the profiles."""
    profiles_dir = profiles_dir or DEFAULT_PROFILES_DIR
    best: tuple[int, str] | None = None
    fallback: tuple[int, str] | None = None
    for src in sorted(profiles_dir.glob("*.toml")):
        try:
            with src.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        table = raw.get("detect", {}) or {}
        prio = int(table.get("priority", 0))
        if fallback is None or prio < fallback[0]:
            fallback = (prio, src.stem)
        if _matches(table) and (best is None or prio > best[0]):
            best = (prio, src.stem)
    if best:
        return best[1]
    return fallback[1] if fallback else "workstation"


#: The profiles shipped with the kit: <kit>/profiles.
DEFAULT_PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "profiles"


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def load(profiles_dir: Path, name: str | None = None) -> Profile:
    chosen = name or detect(profiles_dir)
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
        reference_source=_path(paths.get("reference_source",
                                         paths.get("data_root", "~"))),
        codex_launcher=raw.get("codex", {}).get("launcher", "codex"),
        python_hooks=bool(host.get("python_hooks", True)),
        slurm=bool(host.get("slurm", False)),
        hook_matcher=raw.get("hook", {}).get("matcher", "*"),
        session_start=bool(raw.get("hook", {}).get("session_start", True)),
        settings=dict(raw.get("settings", {})),
    )
