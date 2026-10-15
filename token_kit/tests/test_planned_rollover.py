"""Planned checkpoint rollovers close client segments without killing their supervisor."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit.core.store import Store, read_json, write_json, now


class PlannedRolloverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store.create(root / 'tasks', 'rollover', root)
        self.agent = 'coordinator'
        self.run = self.store.claim_run(self.agent, 'codex', True)
        self.before = self.store.latest(self.agent)
        self.checkpoint = self.store.checkpoint(self.agent)
        self.store.update_run(self.agent, self.run.name, status='exited', child_pid=99999999,
                              child_identity='missing', ended_at=now(),
                              rollover_checkpoint=str(self.checkpoint), exit_code=-15)
        write_json(self.run / 'runtime.json', dict(phase='ready', engine='codex', active_children=[],
                   checkpoint=str(self.checkpoint), previous_checkpoint=str(self.before)))

    def record(self):
        return read_json(self.run / 'run.json')

    def successor(self):
        successor = self.store.claim_run(self.agent, 'codex', True)
        self.store.checkpoint(self.agent)
        return successor

    def test_launch_automatically_reconciles_checkpoint_rollover(self):
        from token_kit import workflow
        # The fixture's earlier exited segment does not own this launch.
        self.store.reconcile_planned_rollover(self.agent, self.run.name)
        child = Mock(pid=99999998)
        child.poll.return_value = -15
        child.wait.return_value = -15

        def finish(child, store, agent, run, stop):
            previous = str(store.latest(agent))
            checkpoint = str(store.checkpoint(agent))
            control = dict(phase="ready", engine="claude", active_children=[],
                           previous_checkpoint=previous, checkpoint=checkpoint)
            write_json(run / "runtime.json", control)
            return -15, control

        original_popen = workflow.subprocess.Popen

        def spawn(argv, **kwargs):
            return original_popen(argv, **kwargs) if argv[0] == "git" else child

        with patch.object(workflow.shutil, "which", return_value="/bin/true"), \
                patch.object(workflow.subprocess, "Popen", side_effect=spawn), \
                patch.object(workflow.runtime, "wait_segment", side_effect=finish):
            rc, control = workflow._launch_segment(self.store, self.agent, "claude", "sonnet",
                                                   False, False, 1000)
        self.assertEqual(control["phase"], "ready")
        records = [read_json(p) for p in (self.run.parent).glob("*/run.json")]
        self.assertTrue(all(r["status"] == "reconciled" for r in records))
        self.assertEqual(len(records), 2)

    def test_original_supervisor_closes_ready_segment_and_claims_next(self):
        self.store.reconcile_planned_rollover(self.agent, self.run.name)
        self.assertEqual(self.record()['status'], 'reconciled')
        self.assertFalse(self.record()['planned_rollover']['external_completion_inferred'])
        self.store.close_run(self.agent, self.run.name, 'repeat')
        self.successor()

    def test_legacy_same_supervisor_successor_is_safe_and_idempotent(self):
        successor = self.successor()
        self.store.close_run(self.agent, self.run.name, 'legacy planned rollover')
        self.assertEqual(self.record()['status'], 'reconciled')
        self.assertEqual(read_json(successor / 'run.json')['status'], 'starting')
        self.store.close_run(self.agent, self.run.name, 'repeat')

    def test_ready_does_not_override_live_child(self):
        self.store.update_run(self.agent, self.run.name, child_pid=os.getpid(), child_identity=None)
        with self.assertRaisesRegex(ValueError, 'child process'):
            self.store.reconcile_planned_rollover(self.agent, self.run.name)

    def test_legacy_requires_same_supervisor_identity(self):
        successor = self.successor()
        self.store.update_run(self.agent, successor.name, supervisor_identity='different')
        with self.assertRaisesRegex(ValueError, 'matching active'):
            self.store.close_run(self.agent, self.run.name, 'no')

    def test_changed_working_state_blocks(self):
        self.successor()
        state = self.store.agent_path(self.agent) / 'STATE.md'
        state.write_text(state.read_text() + '\nChanged\n')
        with self.assertRaisesRegex(ValueError, 'current STATE'):
            self.store.close_run(self.agent, self.run.name, 'no')

    def test_tampered_old_checkpoint_blocks(self):
        self.successor()
        (self.checkpoint / 'STATE.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            self.store.close_run(self.agent, self.run.name, 'no')

    def test_unresolved_children_block(self):
        self.store.add_agent('child', 'bounded')
        write_json(self.store.agent_path('child') / 'lifecycle.json',
                   dict(owner_agent=self.agent, owner_run=self.run.name, phase='running'))
        with self.assertRaisesRegex(ValueError, 'Native child'):
            self.store.reconcile_planned_rollover(self.agent, self.run.name)

    def test_unmarked_exit_does_not_bypass_live_supervisor(self):
        self.successor()
        control = read_json(self.run / 'runtime.json')
        for change in ({'phase': 'running'}, {'active_children': ['unknown']},
                       {'checkpoint': 'different'}, {'engine': 'claude'},
                       {'previous_checkpoint': str(self.checkpoint)}):
            with self.subTest(change=change):
                write_json(self.run / 'runtime.json', dict(control, **change))
                with self.assertRaisesRegex(ValueError, 'may still be alive'):
                    self.store.close_run(self.agent, self.run.name, 'no')

    def test_supervisor_auto_close_requires_current_rollover_checkpoint(self):
        self.store.checkpoint(self.agent)
        with self.assertRaisesRegex(ValueError, 'checkpoint changed'):
            self.store.reconcile_planned_rollover(self.agent, self.run.name)


if __name__ == '__main__':
    unittest.main()
