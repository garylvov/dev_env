"""Offline lifecycle checks with real task records and a mocked child process."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from token_kit import workflow
from token_kit.core.store import Store, read_json


class WorkflowLaunchTests(unittest.TestCase):
    def test_model_effort_policy_reaches_both_clients(self):
        from token_kit.adapters import claude, codex
        from token_kit.adapters.base import LaunchRequest
        for model in (None, "gpt-6-astra", "opus", "fable", "sonnet", "luna", "gpt-5.6-luna"):
            expected = "high" if model and "luna" in model else "medium"
            request = LaunchRequest(self.root, "continue", False, model=model)
            claude_plan = claude.prepare_launch(request, environ={})
            self.assertEqual(claude_plan.argv[claude_plan.argv.index("--effort") + 1], expected)
            codex_plan = codex.prepare_launch(request, environ={})
            self.assertIn(f'model_reasoning_effort="{expected}"', codex_plan.argv)
            self.assertIn(f'plan_mode_reasoning_effort="{expected}"', codex_plan.argv)

    def test_yolo_adapter_mapping_is_opt_in(self):
        from token_kit.adapters import claude, codex
        from token_kit.adapters.base import LaunchRequest
        for adapter, flag in ((claude, "--dangerously-skip-permissions"), (codex, "--yolo")):
            for enabled in (False, True):
                request = LaunchRequest(self.root, "continue", False, yolo=enabled)
                plan = adapter.prepare_launch(request, environ={})
                self.assertEqual(flag in plan.argv, enabled)
                self.assertEqual(plan.argv[-2:], ("--", "continue"))

    def test_cli_yolo_dry_run_and_codex_compaction_refusal(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = workflow.main(["launch", str(self.store.path), "--engine", "claude", "--yolo", "--dry-run"])
        self.assertEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["yolo"])
        self.assertIn("--dangerously-skip-permissions", report["argv"])
        with contextlib.redirect_stderr(errors):
            rc = workflow.main(["launch", str(self.store.path), "--engine", "codex", "--yolo", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("Strict no-compaction", errors.getvalue())
        self.assertEqual(self.records(), [])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "Continue parser fix", self.root)

    def records(self):
        return [read_json(p) for p in self.store.agent_path("coordinator").glob("runs/*/run.json")]

    def fake_child(self, result=0):
        child = Mock(pid=99999999)
        child.wait.return_value = result
        return child

    def test_dry_run_hides_environment_and_does_not_claim_or_execute(self):
        output = io.StringIO()
        bundle = self.store.resume_bundle("coordinator")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "secret-do-not-print"}), \
             patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.subprocess, "Popen") as popen, \
             contextlib.redirect_stdout(output):
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude", dry_run=True), 0)
        report = json.loads(output.getvalue())
        self.assertNotIn("secret-do-not-print", output.getvalue())
        self.assertNotIn("env", report)
        self.assertEqual(report["engine"], "claude")
        self.assertEqual(self.records(), [])
        popen.assert_not_called()

    def test_missing_binary_does_not_claim_a_run(self):
        bundle = self.store.resume_bundle("coordinator")
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value=None), \
             patch.object(workflow.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "Executable not found"):
                workflow.launch(self.store, "coordinator", "claude")
        self.assertEqual(self.records(), [])
        popen.assert_not_called()

    def test_launch_passes_literal_argv_cwd_environment_and_persists_bundle(self):
        child = self.fake_child()
        # Popen is shared with subprocess.run used by workspace_head; compute
        # the bundle before replacing Popen so that Git remains a real read.
        bundle = self.store.resume_bundle("coordinator")
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", return_value=child) as popen:
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude", model="sonnet", yolo=True), 0)
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(argv[-2], "--")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn(str(bundle["checkpoint"]), argv[-1])
        from token_kit.project_install import INSTRUCTIONS
        self.assertIn(INSTRUCTIONS.rstrip(), argv[-1])
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["env"]["DISABLE_COMPACT"], "1")
        self.assertFalse(kwargs.get("shell", False))
        record, = self.records()
        self.assertEqual(record["status"], "exited")
        self.assertTrue(record["yolo"])
        run = self.store.agent_path("coordinator") / "runs" / record["run_id"]
        self.assertEqual(read_json(run / "resume.json"), bundle)
        self.assertEqual((run / "prompt.md").read_text().rstrip(), argv[-1])

    def test_nonzero_exit_blocks_second_launch_until_reconciled(self):
        bundle = self.store.resume_bundle("coordinator")
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", return_value=self.fake_child(7)) as popen:
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude"), 7)
            with self.assertRaisesRegex(ValueError, "not reconciled"):
                workflow.launch(self.store, "coordinator", "claude")
            self.assertEqual(popen.call_count, 1)
        record, = self.records()
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["exit_code"], 7)

    def test_signal_exit_becomes_shell_exit_status(self):
        bundle = self.store.resume_bundle("coordinator")
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", return_value=self.fake_child(-15)):
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude"), 143)
        self.assertEqual(self.records()[0]["status"], "interrupted")

    def test_interrupt_terminates_then_kills_unresponsive_child(self):
        bundle = self.store.resume_bundle("coordinator")
        child = self.fake_child()
        child.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("claude", 5), -9]
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", return_value=child):
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude"), 130)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(self.records()[0]["status"], "interrupted")

    def test_spawn_failure_retains_unreconciled_run(self):
        bundle = self.store.resume_bundle("coordinator")
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                workflow.launch(self.store, "coordinator", "claude")
        self.assertEqual(self.records()[0]["status"], "interrupted")

    def test_metadata_failure_after_spawn_stops_child(self):
        bundle = self.store.resume_bundle("coordinator")
        child = self.fake_child()
        update = self.store.update_run
        def fail_running(agent, run_id, **fields):
            if fields.get("status") == "running":
                raise OSError("metadata unavailable")
            return update(agent, run_id, **fields)
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow.subprocess, "Popen", return_value=child), \
             patch.object(self.store, "update_run", side_effect=fail_running):
            with self.assertRaisesRegex(OSError, "metadata unavailable"):
                workflow.launch(self.store, "coordinator", "claude")
        child.terminate.assert_called_once()
        self.assertEqual(self.records()[0]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
