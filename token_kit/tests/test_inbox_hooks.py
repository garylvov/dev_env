"""Durable cross-engine messages reach active hooks without claiming idle wake."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import inbox, runtime
from token_kit.core.store import Store, read_json, write_json


class InboxHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "messages", self.root)
        self.run = self.store.claim_run("coordinator", "claude", True)
        runtime.initialize(self.store, "coordinator", self.run, "claude", None)
        self.hook("SessionStart")

    def hook(self, event, **fields):
        return runtime.handle(self.store, "coordinator", self.run, {
            "hook_event_name": event, "session_id": "parent", **fields})

    def context(self, response):
        return response["hookSpecificOutput"]["additionalContext"]

    def test_active_delivery_deduplicates_but_does_not_acknowledge(self):
        identifier = self.store.send("coordinator", "Recheck the second finding")
        self.assertIn(identifier, self.context(self.hook("PostToolUse")))
        self.assertEqual(self.hook("PostToolUse"), {})
        self.assertEqual(self.hook("Stop"), {})
        pending = self.store.resume_bundle("coordinator")["pending_messages"]
        self.assertEqual([row["message_id"] for row in pending], [identifier])

    def test_each_active_hook_supports_both_engines(self):
        for engine in ("claude", "codex"):
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
                with self.subTest(engine=engine, event=event):
                    control = read_json(self.run / "runtime.json")
                    control["engine"] = engine
                    write_json(self.run / "runtime.json", control)
                    identifier = self.store.send("coordinator", f"new {engine} {event}")
                    self.assertIn(identifier, self.context(self.hook(event)))

    def test_stop_once_and_recursive_stop_leaves_new_message_queued(self):
        self.store.send("coordinator", "first")
        self.assertEqual(self.hook("Stop")["decision"], "block")
        identifier = self.store.send("coordinator", "second")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        self.assertIn(identifier, self.context(self.hook("PostToolUse")))
        self.assertEqual(self.hook("Stop"), {})

    def test_new_run_redelivers_only_unacknowledged_messages(self):
        first = self.store.send("coordinator", "first")
        second = self.store.send("coordinator", "second")
        self.hook("PostToolUse")
        self.store.checkpoint("coordinator", incorporated=[first])
        self.store.update_run("coordinator", self.run.name, status="exited")
        successor = self.store.claim_run("coordinator", "claude", True)
        runtime.initialize(self.store, "coordinator", successor, "claude", None)
        result = runtime.handle(self.store, "coordinator", successor, {
            "hook_event_name": "SessionStart", "session_id": "next"})
        text = self.context(result)
        self.assertIn(second, text)
        self.assertNotIn(first, text)

    def test_safety_and_lifecycle_results_take_precedence(self):
        identifier = self.store.send("coordinator", "pending")
        with patch.object(runtime.lifecycle, "notice", return_value=("notice", "Reconcile first")):
            self.assertEqual(self.context(self.hook("PostToolUse")), "Reconcile first")
        self.assertIn(identifier, self.context(self.hook("PostToolUse")))
        other = self.store.send("coordinator", "later")
        self.hook("PreCompact")
        self.assertFalse(self.hook("PostToolUse")["continue"])
        self.assertNotIn(other, read_json(self.run / "runtime.json")["inbox_delivered:coordinator"])

    def test_unregistered_native_child_never_receives_parent_messages(self):
        identifier = self.store.send("coordinator", "parent secret")
        self.assertEqual(self.hook("PostToolUse", agent_id="unknown-child"), {})
        self.assertIn(identifier, self.context(self.hook("PostToolUse")))

    def test_registered_native_child_receives_only_own_messages(self):
        self.store.add_agent("child", "bounded work", parent="coordinator")
        self.store.checkpoint("child")
        parent_id = self.store.send("coordinator", "parent only")
        child_id = self.store.send("child", "child only")
        with patch.object(runtime.lifecycle, "native_worker", return_value={
                "agent_id": "child", "phase": "running", "ticket": "ticket"}), \
             patch.object(runtime.lifecycle, "budget_nudge", return_value=None):
            text = self.context(self.hook("PostToolUse", agent_id="bound-child"))
        self.assertIn(child_id, text)
        self.assertNotIn(parent_id, text)
        self.assertIn(parent_id, self.context(self.hook("PostToolUse")))

    def test_bounded_excerpts_and_batch_leave_all_messages_unacknowledged(self):
        identifiers = {self.store.send("coordinator", "α" * 16000) for _ in range(10)}
        response = self.context(self.hook("PostToolUse"))
        self.assertLess(len(response.encode()), 6500)
        self.assertIn("excerpt; read full message before acting", response)
        self.assertIn("--agent coordinator --full", response)
        control = read_json(self.run / "runtime.json")
        self.assertEqual(len(control["inbox_delivered:coordinator"]), inbox.MAX_MESSAGES)
        self.hook("PostToolUse")
        self.assertEqual(set(read_json(self.run / "runtime.json")["inbox_delivered:coordinator"]), identifiers)
        self.assertEqual(len(self.store.resume_bundle("coordinator")["pending_messages"]), 10)

    def test_message_hook_does_not_hash_checkpoint_history_or_evidence(self):
        self.store.send("coordinator", "hello")
        with patch.object(self.store, "latest", side_effect=AssertionError("full checkpoint read")), \
             patch.object(self.store, "resume_bundle", side_effect=AssertionError("full resume")):
            self.assertIn("hello", self.context(self.hook("PostToolUse")))

    def test_publication_order_is_preserved_across_batches(self):
        identifiers = [self.store.send("coordinator", f"instruction {n}") for n in range(10)]
        first = self.context(self.hook("PostToolUse"))
        positions = [first.index(identifier) for identifier in identifiers[:8]]
        self.assertEqual(positions, sorted(positions))
        for identifier in identifiers[8:]:
            self.assertNotIn(identifier, first)
        second = self.context(self.hook("PostToolUse"))
        self.assertLess(second.index(identifiers[8]), second.index(identifiers[9]))

    def test_completion_notifies_without_replaying_historical_lifecycle_noise(self):
        identifiers = []
        for kind in ("worker_running", "worker_stopped", "worker_completed"):
            identifier = self.store.send("coordinator", kind)
            path = self.store.agent_path("coordinator") / "messages" / (identifier + ".json")
            row = read_json(path)
            row.update(source="worker_lifecycle", kind=kind)
            write_json(path, row)
            identifiers.append(identifier)
        response = self.hook("Stop")
        self.assertEqual(response["decision"], "block")
        self.assertIn(identifiers[2], response["reason"])
        for identifier in identifiers[:2]:
            self.assertNotIn(identifier, response["reason"])
        self.assertEqual(self.hook("Stop"), {})
        self.assertEqual(len(self.store.resume_bundle("coordinator")["pending_messages"]), 3)

    def test_filtered_lifecycle_messages_do_not_block_stop(self):
        identifier = self.store.send("coordinator", "historical reservation")
        path = self.store.agent_path("coordinator") / "messages" / (identifier + ".json")
        row = read_json(path)
        row.update(source="worker_lifecycle", kind="worker_launching")
        write_json(path, row)
        self.assertEqual(self.hook("Stop"), {})
        self.assertEqual(self.hook("Stop"), {})


if __name__ == "__main__":
    unittest.main()
