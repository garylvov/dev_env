"""Durable native-worker lifecycle; no native model calls."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core import lifecycle
from token_kit.core.store import Store
from token_kit import runtime, workflow


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store.create(self.root / "tasks", "Lifecycle", self.root)
        self.store.add_agent("parser", "Fix the parser")

    def prepare(self, **kwargs):
        return lifecycle.prepare(self.store, "parser", engine="claude", **kwargs)

    def test_reservation_is_not_reissued_after_restart(self):
        first = self.prepare()
        other = Store(self.store.path)
        again = lifecycle.prepare(other, "parser", engine="claude")
        self.assertTrue(first["spawn_authorized"])
        self.assertFalse(again["spawn_authorized"])
        self.assertEqual(first["worker"]["ticket"], again["worker"]["ticket"])

    def test_rollover_requires_checkpoint_and_explicit_reconciliation(self):
        ticket = self.prepare()["worker"]["ticket"]
        lifecycle.bind(self.store, "parser", ticket, "native1")
        with self.assertRaisesRegex(ValueError, "fresh checkpoint"):
            lifecycle.request(self.store, "parser", ticket, "budget")
        self.store.checkpoint("parser")
        lifecycle.request(self.store, "parser", ticket, "budget")
        lifecycle.observe_native(self.store, "coordinator", None, "native1", "stop")
        self.assertFalse(self.prepare()["spawn_authorized"])
        lifecycle.stopped(self.store, "parser", ticket, "Closed native thread; inspected external jobs")
        replacement = self.prepare()["worker"]
        self.assertEqual(replacement["generation"], 2)
        with self.assertRaisesRegex(ValueError, "Stale"):
            lifecycle.request(self.store, "parser", ticket, "late")
        with self.assertRaisesRegex(ValueError, "another attempt"):
            lifecycle.bind(self.store, "parser", replacement["ticket"], "native1")
        lifecycle.bind(self.store, "parser", replacement["ticket"], "native2")
        lifecycle.observe_native(self.store, "coordinator", None, "native1", "stop")
        self.assertEqual(lifecycle.inspect(self.store, "parser")["phase"], "running")

    def test_outbox_repairs_crash_and_ack_does_not_hide_child(self):
        with patch.object(lifecycle, "deliver_locked", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.prepare()
        bundle = Store(self.store.path).resume_bundle("coordinator")
        self.assertEqual(len(bundle["pending_messages"]), 1)
        ids = [row["message_id"] for row in bundle["pending_messages"]]
        self.store.checkpoint("coordinator", incorporated=ids)
        bundle = self.store.resume_bundle("coordinator")
        self.assertEqual(bundle["pending_messages"], [])
        self.assertEqual(bundle["children"][0]["phase"], "launching")
        self.assertFalse(self.prepare()["spawn_authorized"])

    def test_stop_before_bind_is_durable(self):
        ticket = self.prepare()["worker"]["ticket"]
        lifecycle.observe_native(self.store, "coordinator", None, "early", "stop")
        state = lifecycle.bind(Store(self.store.path), "parser", ticket, "early")
        self.assertEqual(state["phase"], "needs_reconciliation")

    def test_complete_is_terminal_and_output_is_pinned(self):
        ticket = self.prepare()["worker"]["ticket"]
        self.store.checkpoint("parser")
        output = self.store.agent_path("parser") / "out.md"
        output.write_text("Done")
        lifecycle.request(self.store, "parser", ticket, "done", complete=True)
        output.write_text("Changed")
        with self.assertRaisesRegex(ValueError, "result changed"):
            lifecycle.stopped(self.store, "parser", ticket, "closed")
        output.write_text("Done")
        lifecycle.stopped(self.store, "parser", ticket, "closed")
        self.assertFalse(self.prepare()["spawn_authorized"])
        self.store.update_task(status="done")

    def test_completion_checkpoint_is_pinned(self):
        ticket = self.prepare()["worker"]["ticket"]
        self.store.checkpoint("parser")
        (self.store.agent_path("parser") / "out.md").write_text("Done")
        lifecycle.request(self.store, "parser", ticket, "done", complete=True)
        self.store.checkpoint("parser")
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            lifecycle.stopped(self.store, "parser", ticket, "closed")

    def test_native_and_managed_attempts_are_mutually_exclusive(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "native worker"):
            self.store.claim_run("parser", "claude", True)
        self.store.add_agent("other", "Other work")
        self.store.claim_run("other", "claude", True)
        with self.assertRaisesRegex(ValueError, "managed worker"):
            lifecycle.prepare(self.store, "other", engine="claude")

    def test_parent_hierarchy_and_active_task_guard(self):
        self.store.add_agent("nested", "Nested work", "parser")
        lifecycle.prepare(self.store, "nested", engine="claude", owner_agent="coordinator")
        self.assertEqual(len(self.store.resume_bundle("parser")["children"]), 1)
        self.assertEqual(len(self.store.resume_bundle("coordinator")["children"]), 0)
        with self.assertRaises(ValueError):
            self.store.update_task(status="done")
        with self.assertRaises(ValueError):
            self.store.add_agent("bad", "Bad", "missing")

    def test_status_exposes_nested_parent_and_attempt_without_event_history(self):
        self.store.add_agent("nested", "Nested work", "parser")
        state = lifecycle.prepare(self.store, "nested", engine="codex")["worker"]
        lifecycle.bind(self.store, "nested", state["ticket"], "native-nested")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(["status", str(self.store.path)]), 0)
        agents = {row["agent"]["agent_id"]: row for row in json.loads(output.getvalue())["agents"]}
        self.assertEqual(agents["nested"]["agent"]["parent_agent"], "parser")
        self.assertEqual(agents["nested"]["worker"]["native_id"], "native-nested")
        self.assertEqual(agents["nested"]["worker"]["ticket"], state["ticket"])
        self.assertNotIn("events", agents["nested"]["worker"])
        self.assertNotIn("history", agents["nested"]["worker"])
        self.assertIsNone(agents["coordinator"]["worker"])

    def setup_hooks(self):
        self.run = self.store.claim_run("coordinator", "claude", True)
        runtime.initialize(self.store, "coordinator", self.run, "claude", None)
        self.hook("SessionStart")
        state = self.prepare(owner_run=self.run.name, threshold=100)["worker"]
        lifecycle.bind(self.store, "parser", state["ticket"], "native")
        self.transcript = self.root / "child.jsonl"
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "opus", "usage": {"input_tokens": 150, "output_tokens": 10}}}) + "\n")
        return state["ticket"]

    def hook(self, event, **fields):
        return runtime.handle(self.store, "coordinator", self.run,
                              {"hook_event_name": event, "session_id": "parent", **fields})

    def test_child_budget_nudge_and_parent_notification(self):
        ticket = self.setup_hooks()
        response = self.hook("SubagentStop", agent_id="native", agent_transcript_path=str(self.transcript))
        self.assertEqual(response["decision"], "block")
        self.assertIn(ticket, response["reason"])
        self.store.checkpoint("parser")
        lifecycle.request(self.store, "parser", ticket, "budget")
        self.hook("SubagentStop", agent_id="native", agent_transcript_path=str(self.transcript))
        response = self.hook("PostToolUse")
        self.assertIn("rollover_requested", response["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("PostToolUse"), {})
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.assertEqual(self.hook("Stop"), {})

    def test_child_precompact_does_not_halt_parent(self):
        self.setup_hooks()
        response = self.hook("PreCompact", agent_id="native")
        self.assertFalse(response["continue"])
        self.assertEqual(json.loads((self.run / "runtime.json").read_text())["phase"], "running")
        self.assertEqual(lifecycle.inspect(self.store, "parser")["phase"], "needs_reconciliation")

    def test_child_stopping_without_requested_checkpoint_needs_reconciliation(self):
        self.setup_hooks()
        self.hook("SubagentStop", agent_id="native", agent_transcript_path=str(self.transcript))
        self.hook("SubagentStop", agent_id="native", agent_transcript_path=str(self.transcript))
        self.assertEqual(lifecycle.inspect(self.store, "parser")["phase"], "needs_reconciliation")
        self.assertFalse(self.prepare()["spawn_authorized"])

    def test_parent_transcript_not_billed_to_child(self):
        self.setup_hooks()
        self.hook("PostToolUse", agent_id="native", transcript_path=str(self.transcript))
        self.assertEqual(lifecycle.inspect(self.store, "parser")["phase"], "running")

    def test_cli_reservation_and_ticket_validation(self):
        with patch.dict("os.environ", {"TOKEN_KIT_AGENT": "coordinator", "TOKEN_KIT_RUN": ""}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(workflow.main(["worker", "prepare", str(self.store.path),
                                 "--agent", "parser", "--engine", "codex", "--rollover-tokens", "100k"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["spawn_authorized"])
        self.assertEqual(result["worker"]["rollover_tokens"], 100000)
        self.assertNotIn("history", result["worker"])

    def test_codex_child_context_requests_checkpoint(self):
        ticket = self.setup_hooks()
        control_path = self.run / "runtime.json"
        control = json.loads(control_path.read_text())
        control["engine"] = "codex"
        control_path.write_text(json.dumps(control))
        self.transcript.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 150, "output_tokens": 10, "total_tokens": 160},
                "last_token_usage": {"total_tokens": 160}, "model_context_window": 1000}}}) + "\n")
        result = self.hook("PostToolUse", session_id="native", transcript_path=str(self.transcript))
        self.assertIn(ticket, result["hookSpecificOutput"]["additionalContext"])

    def test_new_parent_run_sees_unresolved_child(self):
        self.setup_hooks()
        self.store.update_run("coordinator", self.run.name, status="completed")
        self.run = self.store.claim_run("coordinator", "claude", True)
        runtime.initialize(self.store, "coordinator", self.run, "claude", None)
        result = self.hook("SessionStart")
        self.assertIn("needs_reconciliation", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.prepare()["spawn_authorized"])


if __name__ == "__main__":
    unittest.main()
