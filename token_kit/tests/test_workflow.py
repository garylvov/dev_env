"""Offline fault and portability checks; no model or real configuration writes."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.store import Store, atomic_text, read_json
from token_kit.workflow import main, launch


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "source with spaces"
        self.workspace.mkdir()
        self.store = Store.create(self.root / "tasks", "Fix parser", self.workspace)
        self.store.add_agent("parser", "Fix parser and verify behavior")

    def test_checkpoint_is_snapshot_and_original_assignment_is_preserved(self):
        agent = self.store.agent_path("parser")
        old = self.store.latest("parser")
        state = (agent / "STATE.md").read_text().replace("None yet.", "Parser changed.", 1)
        atomic_text(agent / "STATE.md", state)
        new = self.store.checkpoint("parser")
        self.assertNotEqual(old, new)
        self.assertNotEqual((old / "STATE.md").read_text(), state)
        self.assertEqual((new / "STATE.md").read_text(), state)
        self.assertEqual(self.store.resume_bundle("parser")["assignment"], str(agent / "assignments/0001.md"))

    def test_failed_checkpoint_does_not_replace_latest(self):
        before = self.store.latest("parser")
        from token_kit.core import store as module
        write = module.write_json
        def fail_manifest(path, value):
            if path.name == "manifest.json":
                raise OSError("simulated disk failure")
            return write(path, value)
        with patch.object(module, "write_json", fail_manifest):
            with self.assertRaises(OSError):
                self.store.checkpoint("parser")
        self.assertEqual(self.store.latest("parser"), before)

    def test_checkpoint_tampering_is_detected(self):
        atomic_text(self.store.latest("parser") / "STATE.md", "tampered")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.store.resume_bundle("parser")

    def test_message_survives_restart_until_explicitly_incorporated(self):
        message = self.store.send("parser", "Also update tests\nkeep exact text")
        restarted = Store(self.store.path)
        self.assertEqual(restarted.resume_bundle("parser")["pending_messages"][0]["message_id"], message)
        restarted.checkpoint("parser", incorporated=[message])
        self.assertEqual(restarted.resume_bundle("parser")["pending_messages"], [])
        restarted.checkpoint("parser")
        self.assertEqual(restarted.resume_bundle("parser")["pending_messages"], [])
        self.assertTrue((restarted.agent_path("parser") / "messages" / (message + ".json")).is_file())

    def test_unknown_message_and_invalid_state_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown message"):
            self.store.checkpoint("parser", incorporated=["missing"])
        atomic_text(self.store.agent_path("parser") / "STATE.md", "## Next\ncontinue")
        with self.assertRaisesRegex(ValueError, "Objective"):
            self.store.checkpoint("parser")

    def test_changed_evidence_is_reported(self):
        evidence = self.workspace / "parser.py"
        evidence.write_text("old")
        self.store.checkpoint("parser", evidence=["parser.py"])
        self.store.checkpoint("parser")
        evidence.write_text("new")
        self.assertEqual(self.store.resume_bundle("parser")["changed_evidence"], [str(evidence)])

    def test_assignment_and_manifest_identity_are_validated(self):
        assignment = self.store.agent_path("parser") / "assignments/0001.md"
        assignment.write_text("different assignment")
        with self.assertRaisesRegex(ValueError, "assignment.*modified"):
            self.store.resume_bundle("parser")

    def test_workspace_change_is_not_silently_accepted(self):
        from token_kit.core.store import write_json
        metadata = read_json(self.store.path / "task.json")
        metadata["workspace"] = str(self.root)
        write_json(self.store.path / "task.json", metadata)
        with self.assertRaisesRegex(ValueError, "workspace"):
            Store(self.store.path).resume_bundle("parser")

    def test_intermediate_symlink_is_rejected(self):
        messages = self.store.agent_path("parser") / "messages"
        messages.rmdir()
        messages.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store.send("parser", "test")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_remote_run_cannot_be_reconciled_from_local_pid_checks(self):
        run = self.store.claim_run("parser", "claude", True)
        self.store.update_run("parser", run.name, host="other-host")
        with self.assertRaisesRegex(ValueError, "Cross-host"):
            self.store.close_run("parser", run.name, "local process not found")

    def test_bad_agent_ids_cannot_escape(self):
        for value in ("../escape", "/tmp/escape", "", ".", "a/b"):
            with self.assertRaises(ValueError):
                self.store.add_agent(value, "assignment")

    def test_overlapping_runs_are_refused_and_engine_switch_preserves_identity(self):
        first = self.store.claim_run("parser", "claude", True)
        with self.assertRaisesRegex(ValueError, "not reconciled"):
            self.store.claim_run("parser", "codex", True)
        self.store.update_run("parser", first.name, status="exited", exit_code=0)
        second = self.store.claim_run("parser", "codex", True)
        self.assertEqual(read_json(second / "run.json")["agent_id"], "parser")
        self.assertEqual(len(self.store.resume_bundle("parser")["runs"]), 2)

    def test_cannot_reconcile_a_live_run(self):
        run = self.store.claim_run("parser", "claude", True)
        with self.assertRaisesRegex(ValueError, "alive"):
            self.store.close_run("parser", run.name, "checked workspace")

    def test_claude_dry_run_does_not_claim_run_or_print_environment(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.dict(os.environ, {"API_SECRET": "never-print-this"}):
            self.assertEqual(launch(self.store, "parser", "claude", dry_run=True), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["engine"], "claude")
        self.assertNotIn("never-print-this", output.getvalue())
        self.assertEqual(self.store.resume_bundle("parser")["runs"], [])

    def test_strict_codex_refusal_does_not_create_run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launch(self.store, "parser", "codex", dry_run=True), 0)
        self.assertEqual(self.store.resume_bundle("parser")["runs"], [])

    def test_cli_creates_task_and_exports_checkpoint(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["new", "cli task", "--root", str(self.root / "cli tasks"),
                           "--workspace", str(self.workspace)])
        self.assertEqual(result, 0)
        task = Path(output.getvalue().strip())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["resume", str(task)]), 0)
        self.assertEqual(json.loads(output.getvalue())["workspace"], str(self.workspace))


if __name__ == "__main__":
    unittest.main()
