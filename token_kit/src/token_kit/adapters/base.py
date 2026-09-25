"""Small launch contract; checkpoint construction belongs to the shared core."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


def default_effort(model: str | None) -> str:
    """Launch policy for a selected model; unspecified models use medium."""
    return "high" if re.search(r"(?:^|-)luna(?:$|-)", (model or "").lower()) else "medium"


class AdapterError(ValueError):
    """The requested launch cannot be prepared safely by this adapter."""


@dataclass(frozen=True)
class LaunchRequest:
    workspace: Path
    prompt: str | None  # None opens the interactive composer without a model turn.
    strict_no_compaction: bool = True
    model: str | None = None
    yolo: bool = False
    worker_task: str | None = None
    managed_hooks: bool = False
    non_interactive: bool = False


@dataclass(frozen=True)
class LaunchPlan:
    engine: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    strict_no_compaction: bool
