"""Ticket consumption and managed-worker exit evidence without model calls."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core import lifecycle
from token_kit.core.store import Store, read_json


class ManagedWorkerCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store.create(self.root / "tasks", "Managed", self.root)
        self.store.add_agent("review", "Review only")
        self.parent = self.store.claim_run("coordinator", "codex", True)
        self.ticket = lifecycle.prepare(self.store, "review", engine="claude", model="fable",
            owner_agent="coordinator", owner_run=self.parent.name)["worker"]["ticket"]

    def claim(self, **overrides):
        args = dict(worker_ticket=self.ticket, worker_model="fable")
        args.update(overrides)
        return self.store.claim_run("review", "claude", True, **args)

    def exit(self, run, rc=0, **kwargs):
        self.store.update_run("review", run.name, status="exited" if rc == 0 else "interrupted", exit_code=rc)
        return lifecycle.finish_managed(self.store, "review", self.ticket, run.name,
                                       returncode=rc, **kwargs)

    def checkpoint(self):
        return self.store.checkpoint("review")

    def test_prepare_exposes_managed_route_with_percentage(self):
        import shlex
        self.store.add_agent("other", "Other review")
        prepared = lifecycle.prepare(self.store, "other", engine="claude", model="fable", threshold="80%")
        command = shlex.split(prepared["managed_launch_command"])
        self.assertEqual(command[:3], ["token-kit", "launch", str(self.store.path)])
        self.assertEqual(command[command.index("--ticket") + 1], prepared["worker"]["ticket"])
        self.assertNotIn("managed_launch_note", prepared)
        self.assertEqual(prepared["worker"]["rollover_tokens"], "80%")

    def test_cross_engine_claim_links_and_fences_ticket(self):
        run = self.claim()
        self.assertEqual(read_json(run / "run.json")["worker_ticket"], self.ticket)
        self.assertEqual(lifecycle.inspect(self.store, "review")["managed_run_id"], run.name)
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.claim()
        with self.assertRaisesRegex(ValueError, "cannot bind"):
            lifecycle.bind(self.store, "review", self.ticket, "native-id")

    def test_wrong_ticket_model_owner_and_native_binding_rejected(self):
        with self.assertRaisesRegex(ValueError, "ticket"):
            self.claim(worker_ticket="wrong")
        with self.assertRaisesRegex(ValueError, "engine/model"):
            self.claim(worker_model="opus")
        self.store.update_run("coordinator", self.parent.name, status="exited")
        with self.assertRaisesRegex(ValueError, "owner"):
            self.claim()
        self.store.update_run("coordinator", self.parent.name, status="running")
        lifecycle.bind(self.store, "review", self.ticket, "native")
        with self.assertRaisesRegex(ValueError, "bound"):
            self.claim()

    def test_exit_without_completion_is_not_success_or_retry_authority(self):
        run = self.claim()
        self.assertEqual(self.exit(run)["phase"], "stopped")
        with self.assertRaisesRegex(ValueError, "reconciled"):
            self.claim()

    def test_completion_verified_and_no_replay(self):
        run = self.claim()
        (self.store.agent_path("review") / "out.md").write_text("Review findings")
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Done", complete=True)
        self.assertEqual(self.exit(run, rollover_ready=True)["phase"], "completed")
        with self.assertRaisesRegex(ValueError, "reconciled"):
            self.claim()

    def test_failure_cannot_complete(self):
        run = self.claim()
        (self.store.agent_path("review") / "out.md").write_text("Findings")
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Done", complete=True)
        self.assertEqual(self.exit(run, rc=2)["phase"], "needs_reconciliation")

    def test_changed_output_recorded_and_live_process_refused(self):
        import os
        run = self.claim()
        output = self.store.agent_path("review") / "out.md"
        output.write_text("Original")
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Done", complete=True)
        output.write_text("Changed")
        self.checkpoint()
        result = self.exit(run)
        self.assertEqual(result["phase"], "completed")
        self.assertIn("revised", result["completion_advisory"])
        self.assertNotEqual(result["completion_requested_checkpoint"], result["completion_final_checkpoint"])
        self.store.update_run("review", run.name, child_pid=os.getpid())
        with self.assertRaisesRegex(ValueError, "alive"):
            self.exit(run)

    def test_missing_saved_output_reconciles_managed_stop_without_completion(self):
        run = self.claim()
        output = self.store.agent_path("review") / "out.md"
        output.write_text("Findings")
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Done", complete=True)
        output.unlink()
        result = self.exit(run)
        self.assertEqual(result["phase"], "stopped")
        self.assertIsNone(result["completion_final_output_sha256"])
        self.assertIn("missing or empty", result["completion_advisory"])

    def test_rollover_segment_requires_checkpoint_and_can_claim_once(self):
        run = self.claim()
        with self.assertRaisesRegex(ValueError, "fresh checkpoint"):
            self.exit(run, rollover_ready=True)
        self.checkpoint()
        self.assertEqual(self.exit(run, rollover_ready=True)["phase"], "rollover_requested")
        second = self.claim()
        self.assertNotEqual(run.name, second.name)
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.claim()

    def test_completion_wins_when_supervisor_stops_ready_client(self):
        run = self.claim()
        (self.store.agent_path("review") / "out.md").write_text("Findings")
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Done", complete=True)
        self.store.update_run("review", run.name, status="exited", exit_code=-15)
        result = lifecycle.finish_managed(self.store, "review", self.ticket, run.name,
                                         returncode=-15, rollover_ready=True)
        self.assertEqual(result["phase"], "completed")
        self.assertFalse(result.get("segment_ready"))

    def test_explicit_rollover_request_without_threshold(self):
        run = self.claim()
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Handoff")
        self.assertTrue(self.exit(run)["segment_ready"])
        self.claim()

    def test_changed_explicit_rollover_checkpoint_rejected(self):
        run = self.claim()
        self.checkpoint()
        lifecycle.request(self.store, "review", self.ticket, "Handoff")
        self.checkpoint()
        with self.assertRaisesRegex(ValueError, "changed after rollover"):
            self.exit(run)

    def test_finalizer_does_not_revive_explicitly_stopped_attempt(self):
        run = self.claim()
        self.checkpoint()
        self.store.update_run("review", run.name, status="exited")
        lifecycle.stopped(self.store, "review", self.ticket, "Confirmed stopped")
        result = lifecycle.finish_managed(self.store, "review", self.ticket, run.name,
                                         returncode=0, rollover_ready=True)
        self.assertEqual(result["phase"], "stopped")
        with self.assertRaisesRegex(ValueError, "reconciled"):
            self.claim()

    def test_compaction_recovery_preserves_dirty_working_state(self):
        run = self.claim()
        self.store.update_run("review", run.name, status="interrupted", halt_kind="compaction")
        state = self.store.agent_path("review") / "STATE.md"
        state.write_text(state.read_text() + "\nUncommitted recovery evidence.\n")
        successor = self.claim(recovery_from=run.name)
        self.assertEqual(read_json(successor / "run.json")["recovery_from"], run.name)
        self.assertIn("Uncommitted recovery evidence", state.read_text())

    def test_partial_claim_publication_cannot_authorize_second_spawn(self):
        with patch.object(lifecycle, "publish_locked", side_effect=OSError("interrupted write")):
            with self.assertRaises(OSError):
                self.claim()
        with self.assertRaisesRegex(ValueError, "not reconciled"):
            self.claim()
        records = list((self.store.agent_path("review") / "runs").glob("*/run.json"))
        self.assertEqual(len(records), 1)

    def test_arbitrary_interruption_cannot_claim_recovery(self):
        run = self.claim()
        self.exit(run, rc=2)
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.claim(recovery_from=run.name)


if __name__ == "__main__":
    unittest.main()
