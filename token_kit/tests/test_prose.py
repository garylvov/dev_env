"""House style for anything a person or a model reads: no em dashes.

Covers the documents and every string the kit prints or injects, so the rule
holds for generated agent files and hook messages too, not only the README.
"""

from __future__ import annotations

import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)
SUFFIXES = {".md", ".py", ".sh", ".toml", ""}


def offenders(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if EM_DASH in line:
                hits.append(f"{path.relative_to(root)}:{number}")
    return hits


class TestProse(unittest.TestCase):
    def test_no_em_dash_anywhere_in_the_kit(self):
        self.assertEqual(offenders(KIT_DIR), [])

    def test_the_scan_can_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "doc.md").write_text(f"one {EM_DASH} two\n", encoding="utf-8")
            self.assertEqual(offenders(Path(tmp)), ["doc.md:1"])


if __name__ == "__main__":
    unittest.main()
