"""Ledger presentation keeps cumulative usage and latest context distinct."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core import ledger
from token_kit.core.store import Store, read_json


def row(agent="coordinator", engine="claude", **changes):
    return {"agent": agent, "run": "run1", "engine": engine, "status": "reported",
            "models": {"model1": dict(input=125, cached=20, cache_write=5,
                                       output=10, reasoning=0, total=135)},
            "context_tokens": 42, **changes}


class LedgerClarityTests(unittest.TestCase):
    def test_scopes_and_uncached_input_preserve_original_column_order(self):
        text = ledger.render({"main": row(), "managed": row("reviewer"),
                              "child": row("coordinator/native:child"),
                              "codex-thread:t1": row("coordinator/codex-worker", "codex")})
        self.assertIn("| Coordinator / managed agents | 2 | 0 | 270 |", text)
        self.assertIn("| Observed workers (native / transport) | 2 | 0 | 270 |", text)
        self.assertIn("| coordinator | run1 | claude | model1 | 125 | 20 | 5 | 10 | unknown | 135 | reported | 100 | 42 |", text)
        self.assertIn("Known reported total: **540**", text)

    def test_unknown_rows_hide_stale_counters_and_context(self):
        text = ledger.render({"main": row(status="unavailable"),
                              "child": row("coordinator/native:missing", models={}, status="unavailable")})
        self.assertIn("| Coordinator / managed agents | 1 | 1 | 0 |", text)
        self.assertIn("| Observed workers (native / transport) | 1 | 1 | 0 |", text)
        self.assertIn("| model1 | unknown | unknown | unknown | unknown | unknown | unknown | unavailable | unknown | unknown |", text)
        self.assertNotIn("135", text)
        self.assertIn("Known reported total: **0**", text)

    def test_reasoning_subset_and_context_are_not_added_to_total(self):
        values = dict(input=100, cached=75, cache_write=0, output=20, reasoning=15, total=120)
        text = ledger.render({"codex-thread:t1": row("coordinator/codex-worker", "codex",
                             models={"model1": values}, context_tokens=None)})
        self.assertIn("| 100 | 75 | unknown | 20 | 15 | 120 | reported | 25 | unknown |", text)
        self.assertIn("Known reported total: **120**", text)

    def test_empty_ledger_explicitly_limits_coverage(self):
        text = ledger.render({})
        self.assertIn("| Observed workers (native / transport) | 0 | 0 | 0 |", text)
        self.assertIn("zero observed rows does not establish zero usage", text)

    def test_wire_context_and_duplicate_updates_do_not_change_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store.create(root / "tasks", "clarity", root)
            usage = {"total": {"inputTokens": 100, "outputTokens": 20,
                               "cachedInputTokens": 75, "reasoningOutputTokens": 15},
                     "last": {"inputTokens": 10, "outputTokens": 2}}
            for _ in range(2):
                ledger.record_wire("t1", "model1", usage, task=str(store.path))
            data = read_json(store.path / "token-ledger.json")["codex-thread:t1"]
            self.assertEqual(data["context_tokens"], 12)
            self.assertEqual(data["models"]["model1"]["total"], 120)
            self.assertIn("| reported | 25 | 12 |", ledger.refresh(store))
            for latest in ({}, {"totalTokens": -1}, {"inputTokens": "bad"}):
                ledger.record_wire("t1", "model1", {**usage, "last": latest}, task=str(store.path))
                data = read_json(store.path / "token-ledger.json")["codex-thread:t1"]
                self.assertEqual(data["status"], "reported")
                self.assertIsNone(data["context_tokens"])
                self.assertEqual(data["models"]["model1"]["total"], 120)


if __name__ == "__main__":
    unittest.main()
