"""Cooperative continuation control; no live clients or termination signals."""
from pathlib import Path
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit.continuation import handoff
from token_kit.core.store import Store, read_json, write_json


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = Store.create(root / 'tasks', 'continue', root)
        self.run = self.store.claim_run('coordinator', 'codex', True)
        self.path = self.run / 'run.json'
        record = read_json(self.path)
        record.update(status='running', supervisor_pid=12345, child_pid=12346,
                      supervisor_identity='supervisor', child_identity='child')
        write_json(self.path, record)
        write_json(self.run / 'runtime.json', {'phase': 'running', 'engine': 'codex',
                                              'session_id': 'keep-me', 'other': 42})
        for name, result in [('continuation_candidate', self.run.name),
                             ('_continuation_candidate_locked', self.run.name),
                             ('recovery_ancestors', [])]:
            mock = patch.object(self.store, name, return_value=result, create=True)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def modify(self, **fields):
        record = read_json(self.path)
        record.update(fields)
        write_json(self.path, record)

    def test_dead_target_preserves_failure_and_releases(self):
        self.modify(status='interrupted', halt_kind='compaction', rollover_error='original failure')
        with patch.object(self.store, '_process_dead', return_value=True):
            with handoff(self.store, 'coordinator', 'codex') as result:
                self.assertEqual(result.recovery_from, self.run.name)
                result.release()
                result.release()
                with handoff(self.store, 'coordinator', 'codex'):
                    pass
        record = read_json(self.path)
        self.assertEqual(record['rollover_error'], 'original failure')
        self.assertEqual(record['continuation_previous_halt_kind'], 'compaction')
        self.assertEqual(record['halt_kind'], 'continuation_requested')

    def test_cooperative_shutdown_changes_only_control_fields(self):
        calls = 0
        def dead(record, prefix):
            nonlocal calls
            calls += 1
            return calls > 2
        with patch.object(self.store, '_process_dead', side_effect=dead), \
                patch('token_kit.continuation.process_identity', side_effect=lambda pid: 'supervisor' if pid == 12345 else 'child'), \
                patch('os.kill') as kill:
            with handoff(self.store, 'coordinator', 'codex') as result:
                self.assertEqual(result.recovery_from, self.run.name)
            kill.assert_not_called()
        control = read_json(self.run / 'runtime.json')
        self.assertEqual(control['phase'], 'halted')
        self.assertEqual(control['halt_kind'], 'continuation_requested')
        self.assertEqual(control['other'], 42)
        self.assertEqual(control['session_id'], 'keep-me')

    def test_timeout_requests_shutdown_but_never_claims(self):
        with patch.object(self.store, '_process_dead', return_value=False), \
                patch('token_kit.continuation.process_identity', side_effect=lambda pid: 'supervisor' if pid == 12345 else 'child'), \
                patch.object(self.store, 'claim_run') as claim:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                with handoff(self.store, 'coordinator', 'codex', timeout=0):
                    self.fail('must not yield')
            claim.assert_not_called()
        self.assertEqual(read_json(self.path)['status'], 'running')
        self.assertTrue(read_json(self.path)['continuation_requested'])

    def test_dead_target_without_runtime_is_supported(self):
        (self.run / 'runtime.json').unlink()
        with patch.object(self.store, '_process_dead', return_value=True):
            with handoff(self.store, 'coordinator', 'codex') as result:
                self.assertEqual(result.recovery_from, self.run.name)

    def test_run_identity_changed_during_wait(self):
        with patch.object(self.store, '_process_dead', return_value=True):
            # Mutate at the second candidate check to model another writer.
            def candidate(agent):
                if read_json(self.path).get('continuation_requested'):
                    self.modify(child_identity='replacement')
                return self.run.name
            self._continuation_candidate_locked.side_effect = candidate
            with self.assertRaisesRegex(ValueError, 'identity or recovery links changed'):
                with handoff(self.store, 'coordinator', 'codex'):
                    pass

    def test_lock_contention(self):
        with patch.object(self.store, '_process_dead', return_value=True):
            with handoff(self.store, 'coordinator', 'codex'):
                with self.assertRaisesRegex(ValueError, 'already in progress'):
                    with handoff(self.store, 'coordinator', 'codex'):
                        pass

    def test_rejects_host_engine_missing_pid_before_request(self):
        for fields, message in [({'host': 'another-host'}, 'original host'),
                                ({'engine': 'claude'}, 'same engine'),
                                ({'child_pid': None}, 'Missing child PID')]:
            with self.subTest(fields=fields):
                before = self.path.read_bytes()
                self.modify(**fields)
                with self.assertRaisesRegex(ValueError, message):
                    with handoff(self.store, 'coordinator', 'codex'):
                        pass
                self.assertEqual(read_json(self.run / 'runtime.json')['phase'], 'running')
                self.path.write_bytes(before)

    def test_uncertain_live_identity_refused(self):
        with patch.object(self.store, '_process_dead', return_value=False), \
                patch('token_kit.continuation.process_identity', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Uncertain supervisor'):
                with handoff(self.store, 'coordinator', 'codex'):
                    pass
        self.assertEqual(read_json(self.run / 'runtime.json')['phase'], 'running')

    def test_orphan_live_child_is_not_signalled(self):
        with patch.object(self.store, '_process_dead', side_effect=lambda record, prefix: prefix == 'supervisor'), \
                patch('token_kit.continuation.process_identity', return_value='child'):
            with self.assertRaisesRegex(ValueError, 'child is alive'):
                with handoff(self.store, 'coordinator', 'codex'):
                    pass

    def test_target_changed_during_wait(self):
        self._continuation_candidate_locked.side_effect = [self.run.name, 'newrun']
        with patch.object(self.store, '_process_dead', return_value=True):
            with self.assertRaisesRegex(ValueError, 'target changed'):
                with handoff(self.store, 'coordinator', 'codex'):
                    pass

    def test_inside_target_or_ancestor_refused(self):
        self.recovery_ancestors.return_value = [{'run_id': 'ancestor'}]
        for run in (self.run.name, 'ancestor'):
            with patch.dict(os.environ, {'TOKEN_KIT_RUN': run}):
                with self.assertRaisesRegex(ValueError, 'another terminal'):
                    with handoff(self.store, 'coordinator', 'codex'):
                        pass

    def test_no_target_needs_no_process_record(self):
        self.continuation_candidate.return_value = None
        with handoff(self.store, 'coordinator', 'codex') as result:
            self.assertIsNone(result.recovery_from)


if __name__ == '__main__':
    unittest.main()
