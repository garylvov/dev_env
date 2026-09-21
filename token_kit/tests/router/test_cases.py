"""The behaviour contract: every case of router_cases.jsonl, zero skips.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

The case file is the spec of a SOFT router.  Two properties are asserted here
on the FILE, not on a run, because they are the ones a future edit would most
easily quietly drop: the contract still contains a case proving a spawn that
names no kind goes through untouched, and it contains no `deny` outside the
call cap.  A router that starts refusing spawns again would pass every other
test in this package.
"""

from __future__ import annotations

import json
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

    def test_the_soft_contract_is_still_the_contract(self):
        cases = load_cases()
        names = {c["name"] for c in cases}
        self.assertIn("a-spawn-that-names-no-kind-goes-through-UNTOUCHED", names)
        self.assertIn("an-unknown-kind-is-ALLOWED-untouched", names)
        self.assertIn("topic-words-in-the-description-route-NOTHING", names)
        denies = [c for c in cases if c.get("expected", {}).get("decision") == "deny"]
        self.assertTrue(denies, "the call cap's denials are the ones that must stay")
        for c in denies:
            self.assertIn("cap-", c["name"],
                          f"{c['name']}: the only denials in this kit are the call cap's")

    def test_the_case_file_is_one_json_object_per_line(self):
        for i, line in enumerate(CASES.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                json.loads(line)  # a torn line must fail here, loudly


if __name__ == "__main__":
    unittest.main()
