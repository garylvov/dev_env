"""Lifecycle notices are nonblocking; context rollover and inbox steering remain intact."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import runtime
from token_kit.core.store import Store, read_json


class OrphanHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "test", self.root)
        self.run = self.store.claim_run("coordinator", "codex", True)
        runtime.initialize(self.store, "coordinator", self.run, "codex", None)
        self.hook("SessionStart")
        self.notice = patch.object(runtime.lifecycle, "notice", return_value=("pending", "Worker needs reconciliation"))
        self.mock_notice = self.notice.start()
        self.addCleanup(self.notice.stop)

    def hook(self, event, **fields):
        return runtime.handle(self.store, "coordinator", self.run, {
            "hook_event_name": event, "session_id": "parent", **fields})

    def test_unchanged_advisory_displays_only_first_stop(self):
        self.assertEqual(self.hook("Stop"), {"systemMessage": self.mock_notice.return_value[1]})
        self.assertEqual(self.hook("Stop"), {})

    def test_intervening_tool_notice_does_not_repeat_stop_advisory(self):
        self.assertEqual(self.hook("Stop"), {"systemMessage": self.mock_notice.return_value[1]})
        self.assertIn("hookSpecificOutput", self.hook("PostToolUse"))
        self.assertEqual(self.hook("Stop"), {})

    def test_changed_advisory_displays_at_new_stop(self):
        self.hook("Stop")
        self.mock_notice.return_value = ("different-ticket", "Another worker needs reconciliation")
        self.assertEqual(self.hook("Stop"), {"systemMessage": self.mock_notice.return_value[1]})

    def test_recursive_stop_allows_handoff_and_records_advisory(self):
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {"systemMessage": self.mock_notice.return_value[1]})
        self.assertEqual(self.hook("Stop"), {})
        self.assertEqual(read_json(self.run / "runtime.json")["worker_stop_notice:coordinator"], "pending")

    def test_recursive_stop_allows_handoff_even_if_advisory_changed(self):
        self.hook("Stop")
        self.mock_notice.return_value = ("different-ticket", "Another worker needs reconciliation")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {"systemMessage": self.mock_notice.return_value[1]})

    def test_recursive_subagent_stop_displays_nonblocking_advisory(self):
        with patch.object(runtime.lifecycle, "native_worker", return_value={
                "agent_id": "worker", "phase": "running", "ticket": "ticket"}), \
             patch.object(runtime.lifecycle, "budget_nudge", return_value=None), \
             patch.object(runtime.lifecycle, "observe_native"):
            self.assertEqual(self.hook("SubagentStop", agent_id="native", stop_hook_active=True),
                             {"systemMessage": self.mock_notice.return_value[1]})

    def test_marker_never_turns_advisory_into_block(self):
        for value in (False, None, 1, "true", "false", [], {}, [True]):
            with self.subTest(value=value):
                self.mock_notice.return_value = (repr(value), "Worker needs reconciliation")
                self.assertEqual(self.hook("Stop", stop_hook_active=value),
                                 {"systemMessage": self.mock_notice.return_value[1]})

    def test_advisory_does_not_delay_pending_inbox_steering(self):
        with patch.object(runtime.inbox, "pending_batch", return_value=(["message1"], "User asks to revise")):
            result = self.hook("Stop")
        self.assertEqual(result, {"systemMessage": "Worker needs reconciliation",
                                  "decision": "block", "reason": "User asks to revise"})
        self.assertEqual(read_json(self.run / "runtime.json")["inbox_delivered:coordinator"], ["message1"])

    def test_subagent_stop_is_nonblocking(self):
        self.store.add_agent("worker", "Test assignment", parent="coordinator")
        with patch.object(runtime.lifecycle, "native_worker", return_value={
                "agent_id": "worker", "phase": "running", "ticket": "ticket"}), \
             patch.object(runtime.lifecycle, "budget_nudge", return_value=None), \
             patch.object(runtime.lifecycle, "observe_native"):
            self.assertEqual(self.hook("SubagentStop", agent_id="native"),
                             {"systemMessage": "Worker needs reconciliation"})
            self.assertEqual(self.hook("SubagentStop", agent_id="native"), {})

    def test_recursive_stop_preserves_core_safety_halt(self):
        self.hook("PreCompact")
        result = self.hook("Stop", stop_hook_active=True)
        self.assertIs(result["continue"], False)
        self.assertEqual(read_json(self.run / "runtime.json")["phase"], "halted")

    def test_recursive_stop_preserves_rollover_checkpoint_request(self):
        control = read_json(self.run / "runtime.json")
        control["threshold"] = 100
        (self.run / "runtime.json").write_text(json.dumps(control))
        transcript = self.root / "usage.jsonl"
        transcript.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 180, "output_tokens": 20, "total_tokens": 200},
                "last_token_usage": {"total_tokens": 150},
                "model_context_window": 1000}}}) + "\n")
        result = self.hook("Stop", stop_hook_active=True, transcript_path=str(transcript))
        self.assertEqual(result["decision"], "block")
        self.assertEqual(read_json(self.run / "runtime.json")["phase"], "checkpoint_requested")

    def test_recursive_native_stop_preserves_worker_budget_block(self):
        with patch.object(runtime.lifecycle, "native_worker", return_value={
                "agent_id": "worker", "phase": "running", "ticket": "ticket"}), \
             patch.object(runtime.lifecycle, "budget_nudge", return_value="Checkpoint worker first"):
            result = self.hook("SubagentStop", agent_id="native", stop_hook_active=True)
        self.assertEqual(result, {"decision": "block", "reason": "Checkpoint worker first"})


if __name__ == "__main__":
    unittest.main()
