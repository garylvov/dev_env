"""The behaviour contract: all 25 cases of router_cases.jsonl, zero skips.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

The bash reader passes 19 of these and skips 6 -- the `prefer` ladder, which
it never built.  A skip is not a pass, so this runner has none.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401  -- puts src/ on sys.path
from .runner import CASES, load_cases, run_all


class TestRouterCases(unittest.TestCase):
    def test_every_case_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            passed, failed, messages = run_all(Path(tmp))
        detail = "\n".join(m for m in messages if m.startswith("FAIL"))
        self.assertEqual(failed, 0, f"\n{detail}")
        self.assertEqual(passed, len(load_cases()),
                         "every non-note case must have run")
        self.assertEqual(passed, 25, "the contract is 25 cases; it grew or shrank")

    def test_the_contract_file_is_the_one_shipped(self):
        """A case file with no `prefer` cases would make the suite a lie."""
        text = CASES.read_text(encoding="utf-8")
        self.assertIn("implemented_in_bash", text)
        self.assertEqual(text.count('"implemented_in_bash":false'), 6)


if __name__ == "__main__":
    unittest.main()
