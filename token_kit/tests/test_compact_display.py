"""CLI summaries retain actionable records without injecting historical payloads."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from token_kit import display, workflow
from token_kit.core.store import Store, read_json, write_json


class CompactDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store.create(self.root / "tasks", "Compact", self.root)

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(list(args)), 0)
        return output.getvalue(), json.loads(output.getvalue())

    def test_resume_compact_messages_remain_pending_and_full_text_available(self):
        message_id = self.store.send("coordinator", "critical instructions " * 100)
        before = self.store.resume_bundle("coordinator")
        text, compact = self.invoke("resume", str(self.store.path))
        message = compact["pending_messages"][0]
        self.assertEqual(message["message_id"], message_id)
        self.assertTrue(message["text_truncated"])
        self.assertLessEqual(len(message["text"]), 160)
        self.assertEqual(read_json(Path(message["path"]))["text"], before["pending_messages"][0]["text"])
        self.assertEqual(self.store.resume_bundle("coordinator"), before)
        _, full = self.invoke("resume", str(self.store.path), "--full")
        self.assertEqual(full, before)
        self.assertNotIn("content", compact["trigger_pyramid"])
        for key in ("assignment", "checkpoint", "working_state", "historical_state", "changed_evidence",
                    "historical_state_changed", "working_state_changed", "head_changed"):
            self.assertEqual(compact[key], before[key])
        self.assertLess(len(text.splitlines()), 30)

    def test_actionable_records_survive_projection_without_mutation(self):
        bundle = self.store.resume_bundle("coordinator")
        bundle["children"] = [
            {"agent_id": "done", "phase": "completed"},
            {"agent_id": "retired_ok", "phase": "retired", "operations_reconciled": True},
            {"agent_id": "retired_unknown", "phase": "retired"},
            {"agent_id": "active", "phase": "running", "ticket": "t", "parent_agent": "coordinator"},
            {"agent_id": "blocked", "phase": "needs_reconciliation"},
        ]
        bundle["runs"] = [
            {"run_id": "old", "status": "reconciled", "created_at": "1"},
            {"run_id": "active", "status": "running", "created_at": "2"},
            {"run_id": "interrupted", "status": "interrupted"},
            {"run_id": "pending", "status": "exited", "recovery_pending": True},
            {"run_id": "error", "status": "exited", "error": "bad"},
            {"run_id": "unknown", "status": "new_unknown_status"},
            {"run_id": "incoming", "status": "exited", "recovery_from": "old", "recovery_completed": False},
        ]
        bundle["changed_evidence"] = ["/evidence/missing", "/evidence/changed"]
        original = copy.deepcopy(bundle)
        view = display.resume_view(bundle, self.store.path)
        self.assertEqual(bundle, original)
        self.assertEqual({row["agent_id"] for row in view["children"]}, {"retired_unknown", "active", "blocked"})
        self.assertEqual(view["omitted_children"], {"completed": 1, "retired": 1})
        self.assertEqual({row["run_id"] for row in view["runs"]}, {"active", "interrupted", "pending", "error", "unknown", "incoming"})
        self.assertEqual(view["omitted_runs"], 1)
        self.assertEqual(view["changed_evidence"], bundle["changed_evidence"])
        self.assertEqual(json.loads(display.dumps(view)), view)

    def test_latest_run_uses_time_not_uuid(self):
        rows = [{"run_id": "z", "created_at": "2025", "status": "reconciled"},
                {"run_id": "a", "created_at": "2026", "status": "exited"}]
        self.assertEqual(display.relevant_runs(rows), [rows[1]])

    def test_status_all_agents_keep_identity_and_full_history(self):
        self.store.add_agent("child", "Inspect", "coordinator")
        record = {"agent_id": "child", "parent_agent": "coordinator", "phase": "completed",
                  "ticket": "ticket", "native_id": "native", "events": [], "history": [{"detail": "old"}]}
        write_json(self.store.agent_path("child") / "lifecycle.json", record)
        _, compact = self.invoke("status", str(self.store.path))
        rows = {row["agent"]["agent_id"]: row for row in compact["agents"]}
        self.assertEqual(rows["child"]["agent"]["parent_agent"], "coordinator")
        self.assertEqual(rows["child"]["worker"]["native_id"], "native")
        self.assertEqual(rows["child"]["worker"]["ticket"], "ticket")
        self.assertNotIn("history", rows["child"]["worker"])
        self.assertIsNone(rows["coordinator"]["worker"])
        _, full = self.invoke("status", str(self.store.path), "--full")
        child = next(row for row in full["agents"] if row["agent"]["agent_id"] == "child")
        self.assertEqual(child["worker"]["history"], record["history"])


if __name__ == "__main__":
    unittest.main()
