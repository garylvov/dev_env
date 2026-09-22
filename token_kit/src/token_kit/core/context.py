"""Recognize shared workflows so old global hooks do not inject a second policy."""
from __future__ import annotations

import json
import os
from pathlib import Path


def shared_workflow(cwd: str | Path | None = None) -> bool:
    if os.environ.get("TOKEN_KIT_TASK"):
        return True
    path = Path(cwd or Path.cwd()).resolve()
    # Only explicit ancestor paths, never a repository/tree scan.
    for directory in (path, *path.parents):
        marker = directory / ".token-kit/project-install.json"
        try:
            manifest = json.loads(marker.read_text())
            if manifest.get("version") == 1 and manifest.get("engines"):
                return True
        except (OSError, ValueError, AttributeError):
            pass
        if (directory / ".git").exists():
            break
    return False
