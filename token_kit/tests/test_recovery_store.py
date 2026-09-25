"""Bounded run recovery and optional historical-state snapshots."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.store import Store, read_json, write_json, LEGACY_COMPACTION_REASON


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store.create(root / "tasks", "recovery", root)
        self.store.add_agent("worker", "bounded recovery")

    def interrupt(self, **fields):
        run = self.store.claim_run("worker", "codex", True)
        record = read_json(run / "run.json")
        record.update(status="interrupted", halt_kind="compaction",
                      child_pid=999999, child_identity="missing", supervisor_pid=999999,
                      supervisor_identity="missing", **fields)
        write_json(run / "run.json", record)
        return run

    def legacy_interrupt(self):
        run = self.interrupt()
        record = read_json(run / "run.json")
        record.pop("halt_kind")
        record["rollover_error"] = LEGACY_COMPACTION_REASON
        write_json(run / "run.json", record)
        write_json(run / "runtime.json", {"phase": "halted", "engine": "codex",
                                          "reason": LEGACY_COMPACTION_REASON})
        return run

    def test_legacy_detection_is_read_only_and_claim_records_provenance(self):
        old = self.legacy_interrupt()
        before = (old / "run.json").read_bytes()
        self.assertEqual(self.store.recovery_candidate("worker"), old.name)
        self.assertEqual((old / "run.json").read_bytes(), before)
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        old_record = read_json(old / "run.json")
        self.assertEqual(old_record["status"], "interrupted")
        self.assertNotIn("halt_kind", old_record)
        self.assertEqual(old_record["recovery_source"], "legacy_compaction_corroborated")
        self.assertEqual(read_json(successor / "run.json")["recovery_source"],
                         old_record["recovery_source"])
        with self.assertRaisesRegex(ValueError, "unfinished recovery reservation"):
            self.store.recovery_candidate("worker")

    def test_legacy_requires_exact_corroborating_runtime(self):
        old = self.legacy_interrupt()
        runtime = old / "runtime.json"
        good = runtime.read_bytes()
        for invalid in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(invalid=invalid):
                runtime.write_bytes(invalid)
                self.assertIsNone(self.store.recovery_candidate("worker"))
        runtime.unlink()
        self.assertIsNone(self.store.recovery_candidate("worker"))
        control = json.loads(good)
        for key, value in (("phase", "running"), ("reason", "generic failure"),
                           ("reason", LEGACY_COMPACTION_REASON + " extra"),
                           ("halt_kind", "child_compaction"), ("halt_kind", "manual"),
                           ("engine", "claude")):
            with self.subTest(key=key, value=value):
                write_json(runtime, dict(control, **{key: value}))
                self.assertIsNone(self.store.recovery_candidate("worker"))
        runtime.write_bytes(good)
        record = read_json(old / "run.json")
        for fields in ({"halt_kind": "manual"}, {"halt_kind": "child_compaction"},
                       {"halt_kind": "generic"}, {"rollover_error": "generic failure"},
                       {"rollover_error": "prefix " + LEGACY_COMPACTION_REASON}):
            with self.subTest(fields=fields):
                write_json(old / "run.json", dict(record, **fields))
                self.assertIsNone(self.store.recovery_candidate("worker"))
        write_json(old / "run.json", record)
        # A record elsewhere cannot corroborate this run.
        runtime.rename(old.parent / "runtime.json")
        self.assertIsNone(self.store.recovery_candidate("worker"))

    def test_failed_legacy_claim_leaves_no_migration_or_reservation(self):
        old = self.legacy_interrupt()
        record = read_json(old / "run.json")
        record["host"] = "another-host"
        write_json(old / "run.json", record)
        before = (old / "run.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Cross-host"):
            self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        self.assertEqual((old / "run.json").read_bytes(), before)
        self.assertEqual(len(list(old.parent.iterdir())), 1)
        # Classification is checked again at claim, not cached from discovery.
        self.assertEqual(self.store.recovery_candidate("worker"), old.name)
        (old / "runtime.json").unlink()
        with self.assertRaisesRegex(ValueError, "sole eligible"):
            self.store.claim_run("worker", "codex", True, recovery_from=old.name)

    def test_legacy_claim_retains_engine_and_unmarked_error_gates(self):
        old = self.legacy_interrupt()
        with self.assertRaisesRegex(ValueError, "same engine"):
            self.store.claim_run("worker", "claude", True, recovery_from=old.name)
        self.store.update_run("worker", old.name, unresolved_errors=True)
        self.assertIsNone(self.store.recovery_candidate("worker"))

    def test_candidate_requires_compaction_and_has_no_error(self):
        run = self.interrupt()
        self.assertEqual(self.store.recovery_candidate("worker"), run.name)
        record = read_json(run / "run.json")
        record["error"] = "unresolved"
        write_json(run / "run.json", record)
        self.assertIsNone(self.store.recovery_candidate("worker"))

    def test_host_and_live_process_are_refused(self):
        run = self.interrupt(host="elsewhere")
        with self.assertRaisesRegex(ValueError, "Cross-host"):
            self.store.claim_run("worker", "codex", True, recovery_from=run.name)
        record = read_json(run / "run.json")
        record["status"] = "exited"
        write_json(run / "run.json", record)
        run = self.interrupt()
        record = read_json(run / "run.json")
        from token_kit.core.store import process_identity
        record.update(supervisor_pid=os.getppid(), supervisor_identity=process_identity(os.getppid()))
        write_json(run / "run.json", record)
        with self.assertRaises(ValueError):
            self.store.claim_run("worker", "codex", True, recovery_from=run.name)

    def test_pid_start_identity_reuse_is_dead(self):
        run = self.interrupt()
        record = read_json(run / "run.json")
        record.update(child_pid=os.getpid(), child_identity="different-start")
        write_json(run / "run.json", record)
        successor = self.store.claim_run("worker", "codex", True, recovery_from=run.name)
        self.assertEqual(read_json(successor / "run.json")["recovery_from"], run.name)

    def test_successor_first_crash_is_fail_closed(self):
        old = self.interrupt()
        successor = old.parent / "crashed-successor"
        successor.mkdir()
        write_json(successor / "run.json", {"schema_version": 1, "run_id": successor.name,
                   "agent_id": "worker", "status": "starting", "recovery_from": old.name,
                   "recovery_pending": True})
        with self.assertRaisesRegex(ValueError, "Incomplete recovery link"):
            self.store.recovery_candidate("worker")

    def test_claim_reserves_old_after_successor(self):
        old = self.interrupt()
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        old_record = read_json(old / "run.json")
        new_record = read_json(successor / "run.json")
        self.assertTrue(old_record["recovery_pending"])
        self.assertEqual(old_record["recovery_to"], successor.name)
        self.assertEqual(new_record["recovery_from"], old.name)
        self.assertEqual(new_record["recovery_checkpoint"]["checkpoint_id"], self.store.latest("worker").name)

    def test_recovery_close_requires_fresh_checkpoint_and_children(self):
        old = self.interrupt()
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        record = read_json(successor / "run.json")
        record.update(status="running", child_pid=999999, child_identity="missing")
        write_json(successor / "run.json", record)
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.store.close_run("worker", old.name, "agent verified external jobs")
        self.store.checkpoint("worker")
        lifecycle = self.store.agent_path("worker") / "lifecycle.json"
        write_json(lifecycle, {"owner_agent": "worker", "owner_run": old.name, "phase": "running"})
        with self.assertRaisesRegex(ValueError, "Native children"):
            self.store.close_run("worker", old.name, "agent verified external jobs")

    def test_recovery_close_is_idempotent(self):
        old = self.interrupt()
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        record = read_json(successor / "run.json")
        record.update(status="running", child_pid=999999, child_identity="missing")
        write_json(successor / "run.json", record)
        self.store.checkpoint("worker")
        self.store.close_run("worker", old.name, "agent verified external jobs")
        self.store.close_run("worker", old.name, "retry")
        self.assertTrue(read_json(successor / "run.json")["recovery_completed"])

    def test_recovery_close_rejects_history_created_after_historyless_checkpoint(self):
        old = self.interrupt()
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        record = read_json(successor / "run.json")
        record.update(status="running", child_pid=999999, child_identity="missing")
        write_json(successor / "run.json", record)
        self.store.checkpoint("worker")
        (self.store.agent_path("worker") / "historical_state.md").write_text("late history")
        with self.assertRaisesRegex(ValueError, "historyless"):
            self.store.close_run("worker", old.name, "agent verified external jobs")

    def test_recovery_close_revalidates_evidence_and_head(self):
        old = self.interrupt()
        successor = self.store.claim_run("worker", "codex", True, recovery_from=old.name)
        record = read_json(successor / "run.json")
        record.update(status="running", child_pid=999999, child_identity="missing")
        write_json(successor / "run.json", record)
        evidence = Path(self.tmp.name) / "evidence.txt"
        evidence.write_text("before")
        self.store.checkpoint("worker", evidence=[str(evidence)])
        evidence.write_text("after")
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.store.close_run("worker", old.name, "agent verified external jobs")

        # Recreate a clean successor state and exercise the HEAD gate without
        # requiring a real repository in this unit test.
        evidence.write_text("before")
        checkpoint = self.store.checkpoint("worker", evidence=[str(evidence)])
        manifest_path = checkpoint / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["head"] = "committed-head"
        write_json(manifest_path, manifest)
        with patch("token_kit.core.store.workspace_head", return_value="different-head"):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                self.store.close_run("worker", old.name, "agent verified external jobs")

    def test_history_is_optional_snapshotted_and_tamper_checked(self):
        agent = self.store.agent_path("worker")
        self.assertFalse((agent / "historical_state.md").exists())
        first = self.store.latest("worker")
        self.assertIsNone(read_json(first / "manifest.json").get("historical_state_sha256"))
        (agent / "historical_state.md").write_bytes(b"old\r\n")
        checkpoint = self.store.checkpoint("worker")
        manifest = read_json(checkpoint / "manifest.json")
        self.assertEqual(manifest["historical_state_sha256"], manifest.get("historical_state_sha256"))
        self.assertEqual((checkpoint / "historical_state.md").read_bytes(), b"old\r\n")
        bundle = self.store.resume_bundle("worker")
        self.assertFalse(bundle["historical_state_changed"])
        (agent / "historical_state.md").write_bytes(b"tampered")
        self.assertTrue(self.store.resume_bundle("worker")["historical_state_changed"])
        # Appending bounded history is a valid next checkpoint and is not
        # mistaken for tampering with the immutable snapshot.
        self.store.checkpoint("worker")
        self.assertFalse(self.store.resume_bundle("worker")["historical_state_changed"])

    def test_old_manifest_without_history_remains_compatible(self):
        checkpoint = self.store.latest("worker")
        manifest_path = checkpoint / "manifest.json"
        manifest = read_json(manifest_path)
        manifest.pop("historical_state_sha256", None)
        manifest.pop("historical_state_path", None)
        write_json(manifest_path, manifest)
        # The state hash is unchanged; only optional history verification is skipped.
        self.assertEqual(self.store.latest("worker"), checkpoint)


if __name__ == "__main__":
    unittest.main()
