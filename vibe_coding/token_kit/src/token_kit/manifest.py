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

    def hook_command(self) -> str | None:
        rows = self.of_kind("hook")
        return rows[-1][3] if rows and len(rows[-1]) > 3 else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join("\t".join(r) + "\n" for r in self.rows))

    def delete(self) -> None:
        if self.path.is_file():
            self.path.unlink()
