"""Small shared contracts for the folder-first launcher."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TaskView:
    root: Path
    workspace: Path | None
    title: str
    assignment: Path
    state: Path
    output: Path
    preferences: Path | None = None
    alternate_state: Path | None = None
    legacy: bool = False
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchOptions:
    engine: str = "claude"
    model: str | None = None
    effort: str | None = None
    yolo: bool = False
    rollover: int | str | None = "60%"
    max_rollovers: int | None = None
    context_window: int | None = None
    executable: str | None = None
    non_interactive: bool = False


@dataclass(frozen=True)
class RecoveryInput:
    text: str
    paths: tuple[Path, ...] = ()
    message_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
