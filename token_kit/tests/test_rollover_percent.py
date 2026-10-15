"""Percentage rollover arithmetic and lifecycle boundary tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_kit import rollover, runtime
from token_kit.core import lifecycle
from token_kit.core.store import Store, read_json


class RolloverHelperTests(unittest.TestCase):
    def test_parse_normalizes_absolute_and_percentage_targets(self):
        self.assertEqual(rollover.parse_limit(500000), 500000)
        self.assertEqual(rollover.parse_limit("500k"), 500000)
        self.assertEqual(rollover.parse_limit("080%"), "80%")
        self.assertEqual(rollover.parse_limit("95%"), "95%")
        for value in (True, 1.5, 0, "0", "100%", "80.5%", "tokens"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    rollover.parse_limit(value)

    def test_effective_limit_uses_floor_for_percent_and_caps_legacy_absolute(self):
        self.assertEqual(rollover.effective_limit("95%", 272000), 258400)
        self.assertEqual(rollover.effective_limit("80%", 272000), 217600)
        self.assertEqual(rollover.effective_limit(500000, 272000), 217600)
        self.assertEqual(rollover.effective_limit(500000), 500000)
        self.assertEqual(rollover.effective_limit(500000, 0), 500000)
        self.assertEqual(rollover.format_limit("80%"), "80%")
        self.assertEqual(rollover.format_limit(500000), "500,000")

    def test_percentage_requires_observed_window_and_at_least_one_token(self):
        with self.assertRaisesRegex(ValueError, "context window"):
            rollover.effective_limit("80%")
        with self.assertRaisesRegex(ValueError, "one token"):
            rollover.effective_limit("1%", 1)


class RolloverLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "test", self.root)
        self.store.add_agent("parser", "Parse the input")

    def test_native_percentage_persists_effective_target_below_boundary(self):
        ticket = lifecycle.prepare(self.store, "parser", engine="codex", threshold="80%")["worker"]["ticket"]
        lifecycle.bind(self.store, "parser", ticket, "native")
        self.assertIsNone(lifecycle.budget_nudge(self.store, "parser", ticket, 100, 1000))
        state = lifecycle.inspect(self.store, "parser")
        self.assertEqual(state["rollover_tokens"], "80%")
        self.assertEqual(state["effective_threshold"], 800)
        self.assertEqual(state["observed_window"], 1000)
        self.assertEqual(state["phase"], "running")

    def test_native_percentage_missing_window_requests_reconciliation(self):
        ticket = lifecycle.prepare(self.store, "parser", engine="codex", threshold="80%")["worker"]["ticket"]
        lifecycle.bind(self.store, "parser", ticket, "native")
        self.assertIsNone(lifecycle.budget_nudge(self.store, "parser", ticket, 100, None))
        state = lifecycle.inspect(self.store, "parser")
        self.assertEqual(state["phase"], "needs_reconciliation")
        self.assertIn("context window", state["rollover_error"])
        self.assertIsNotNone(lifecycle.notice(self.store, "coordinator"))

    def test_invalid_percentage_is_rejected_before_reservation(self):
        with self.assertRaises(ValueError):
            lifecycle.prepare(self.store, "parser", engine="codex", threshold="100%")
        self.assertFalse((self.store.agent_path("parser") / "lifecycle.json").exists())


class RolloverRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "test", self.root)
        self.run = self.store.claim_run("coordinator", "codex", True)
        self.transcript = self.root / "transcript.jsonl"

    def hook(self, event):
        return runtime.handle(self.store, "coordinator", self.run, {
            "hook_event_name": event, "session_id": "session", "transcript_path": str(self.transcript)})

    def test_runtime_persists_effective_percentage_after_observation(self):
        self.transcript.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
                "last_token_usage": {"total_tokens": 100}, "model_context_window": 1000}}}) + "\n")
        runtime.initialize(self.store, "coordinator", self.run, "codex", "80%")
        self.hook("SessionStart")
        self.assertEqual(self.hook("PostToolUse"), {})
        control = read_json(self.run / "runtime.json")
        self.assertEqual(control["threshold"], "80%")
        self.assertEqual(control["effective_threshold"], 800)
        self.assertEqual(control["observed_window"], 1000)

    def test_runtime_halts_when_usage_has_no_window_for_percentage(self):
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "opus", "usage": {
                "input_tokens": 100, "output_tokens": 10}}}) + "\n")
        runtime.initialize(self.store, "coordinator", self.run, "claude", "80%")
        self.hook("SessionStart")
        result = self.hook("PostToolUse")
        self.assertFalse(result["continue"])
        control = read_json(self.run / "runtime.json")
        self.assertEqual(control["phase"], "halted")
        self.assertIn("context window", control["reason"])


if __name__ == "__main__":
    unittest.main()
