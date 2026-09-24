"""Offline managed-session compaction recovery regressions."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_kit import runtime, workflow
from token_kit.core.store import Store, read_json


class RecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store.create(self.root / "tasks", "recovery", self.root)

    def compaction_run(self):
        run = self.store.claim_run("coordinator", "claude", True, recovery_from=None)
        self.store.update_run("coordinator", run.name, status="interrupted", halt_kind="compaction")
        return run

    def test_compaction_halt_restarts_in_same_managed_loop(self):
        halted = {"phase": "halted", "halt_kind": "compaction",
                  "reason": "compaction", "sample": {}}
        with patch.object(workflow, "_launch_segment", side_effect=[(75, halted), (0, {})]) as launch, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude",
                                              rollover_tokens=None, max_rollovers=1), 0)
        self.assertEqual(launch.call_count, 2)
        self.assertTrue(launch.call_args_list[1].kwargs["allow_recovery"])

    def test_thresholdless_managed_launch_still_waits_for_lifecycle(self):
        bundle = self.store.resume_bundle("coordinator")
        child = Mock(pid=123456789)
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
                patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
                patch.object(workflow.subprocess, "Popen", return_value=child), \
                patch.object(runtime, "wait_segment", return_value=(0, {})) as wait, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, "coordinator", "claude",
                                              rollover_tokens=None, max_rollovers=0), 0)
        wait.assert_called_once()
        argv = wait.call_args.args
        self.assertIs(argv[0], child)

    def test_zero_restart_budget_does_not_select_compaction_recovery(self):
        self.compaction_run()
        candidate = Mock(wraps=self.store.recovery_candidate)
        with patch.object(self.store, "recovery_candidate", candidate), \
                patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "not reconciled"):
                workflow.launch(self.store, "coordinator", "claude", max_rollovers=0)
        candidate.assert_not_called()

    def test_recovery_preserves_exact_working_state_and_rc0_guard(self):
        predecessor = self.compaction_run()
        state = self.store.agent_path("coordinator") / "STATE.md"
        original = b"# Agent state\n\n## Objective\nchanged before recovery\xff\n"
        state.write_bytes(original)
        bundle = self.store.resume_bundle("coordinator")
        child = Mock(pid=123456789)
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
                patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
                patch.object(workflow.subprocess, "Popen", return_value=child), \
                patch.object(runtime, "wait_segment", return_value=(0, {})), \
                contextlib.redirect_stderr(io.StringIO()):
            rc, control = workflow._launch_segment(self.store, "coordinator", "claude", None,
                                                   False, False, None)
        self.assertEqual(rc, 75)
        self.assertEqual(control["halt_kind"], "recovery_unreconciled")
        records = [read_json(path) for path in self.store.agent_path("coordinator").glob("runs/*/run.json")]
        successor = next(record for record in records if record.get("recovery_from") == predecessor.name)
        input_path = self.store.agent_path("coordinator") / "runs" / successor["run_id"] / "recovery-input.md"
        self.assertEqual(input_path.read_bytes(), original)
        self.assertEqual(successor["status"], "interrupted")

    def test_recovery_ready_signal_with_negative_rc_also_fails_closed(self):
        self.compaction_run()
        bundle = self.store.resume_bundle("coordinator")
        child = Mock(pid=123456789)
        with patch.object(self.store, "resume_bundle", return_value=bundle), \
                patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
                patch.object(workflow.subprocess, "Popen", return_value=child), \
                patch.object(runtime, "wait_segment", return_value=(-15, {"phase": "ready"})), \
                contextlib.redirect_stderr(io.StringIO()):
            rc, control = workflow._launch_segment(self.store, "coordinator", "claude", None,
                                                   False, False, None)
        self.assertEqual(rc, 75)
        self.assertEqual(control["halt_kind"], "recovery_unreconciled")


if __name__ == "__main__":
    unittest.main()
