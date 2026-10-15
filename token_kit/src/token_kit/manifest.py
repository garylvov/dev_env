"""The manifest: the only record of what the installer added, so --uninstall can
remove exactly that and nothing else.

Plain TSV, append-only in spirit, one row per thing installed. Kinds:

    symlink  <dest>    <src>
    file     <path>
    setting  <key>     ADDED
    hook     <event>   <matcher>  <command>
    backup   <backup>  <original>
"""

from __future__ import annotations

from pathlib import Path


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.rows: list[list[str]] = []
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.strip():
                    self.rows.append(line.split("\t"))

    def add(self, *fields: str) -> None:
        row = [str(f) for f in fields]
        if row not in self.rows:
            self.rows.append(row)

    def of_kind(self, kind: str) -> list[list[str]]:
        return [r for r in self.rows if r and r[0] == kind]

    def added_setting_keys(self) -> list[str]:
        return [r[1] for r in self.of_kind("setting") if len(r) > 2 and r[2] == "ADDED"]

    def hook_command(self, event: str = "PreToolUse") -> str | None:
        """The command registered for ONE event.

        There are several now (PreToolUse, SessionStart, PostToolUse, Stop),
        so "the last hook row" is not an answer: an uninstall that asked for
        it removed one event and left the others dangling.
        """
        rows = [r for r in self.of_kind("hook") if len(r) > 3 and r[1] == event]
        return rows[-1][3] if rows else None

    def hook_rows(self) -> list[tuple[str, str, str]]:
        """(event, matcher, command) for every hook this install registered."""
        return [(r[1], r[2], r[3]) for r in self.of_kind("hook") if len(r) > 3]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join("\t".join(r) + "\n" for r in self.rows))

    def delete(self) -> None:
        if self.path.is_file():
            self.path.unlink()
