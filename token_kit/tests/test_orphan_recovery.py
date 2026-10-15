"""Orphan authority retirement preserves operation outcomes without inventing closure."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_kit.core import lifecycle
from token_kit.core.store import Store, read_json, write_json, process_identity


class OrphanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store.create(root / 'tasks', 'orphan recovery', root)
        self.store.add_agent('worker', 'inspect partial publication')
        self.old = self.store.claim_run('coordinator', 'codex', True)
        worker = lifecycle.prepare(self.store, 'worker', engine='codex',
                                   owner_agent='coordinator', owner_run=self.old.name)['worker']
        self.ticket = worker['ticket']
        lifecycle.bind(self.store, 'worker', self.ticket, '/root/old-worker')
        self.store.update_run('coordinator', self.old.name, status='interrupted', halt_kind='compaction',
                              child_pid=999999, child_identity='missing', supervisor_pid=999999,
                              supervisor_identity='missing')
        self.new = self.store.claim_run('coordinator', 'codex', True, recovery_from=self.old.name)
        path = self.store.agent_path('worker') / 'STATE.md'
        path.write_text(path.read_text() + '\nPartial publication: linux-64 exists; linux-aarch64 absent. No retry authorized.\n')
        self.store.checkpoint('worker')

    def retire(self, **kwargs):
        options = dict(operations_reconciled=True, recovery_agent='coordinator', recovery_run=self.new.name)
        options.update(kwargs)
        return lifecycle.retire(self.store, 'worker', self.ticket,
                                'Inspected publication: no live or uncertain operation. Partial outcome preserved; no retry.',
                                **options)

    def test_retire_checkpoint_close_preserves_partial_outcome(self):
        before = (self.store.agent_path('worker') / 'STATE.md').read_bytes()
        record = self.retire()
        self.assertEqual(record['phase'], 'retired')
        self.assertFalse(record['retirement']['native_closure_confirmed'])
        self.assertEqual((self.store.agent_path('worker') / 'STATE.md').read_bytes(), before)
        self.assertIsNone(lifecycle.notice(self.store, 'coordinator'))
        self.store.checkpoint('coordinator')
        self.store.close_run('coordinator', self.old.name, 'Operations reconciled; partial release remains unresolved work')
        self.assertEqual(read_json(self.old / 'run.json')['status'], 'reconciled')
        with self.assertRaises(ValueError):
            self.store.update_task(status='done')
        replacement = lifecycle.prepare(self.store, 'worker', owner_agent='coordinator', owner_run=self.new.name)
        self.assertTrue(replacement['spawn_authorized'])
        self.assertNotEqual(replacement['worker']['ticket'], self.ticket)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            lifecycle.request(self.store, 'worker', self.ticket, 'old request')

    def test_missing_assertion_leaves_state_unchanged(self):
        path = lifecycle.path_of(self.store, 'worker')
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'operations-reconciled'):
            self.retire(operations_reconciled=False)
        self.assertEqual(path.read_bytes(), before)

    def test_process_and_ownership_failures_do_not_mutate(self):
        old_good = (self.old / 'run.json').read_bytes()
        new_good = (self.new / 'run.json').read_bytes()
        state_path = lifecycle.path_of(self.store, 'worker')
        worker_good = state_path.read_bytes()
        cases = [(self.old, {'child_pid': None}),
                 (self.old, {'child_pid': os.getpid(), 'child_identity': process_identity(os.getpid())}),
                 (self.old, {'host': 'another-host'}),
                 (self.new, {'host': 'another-host'}),
                 (self.new, {'status': 'interrupted'}),
                 (self.new, {'supervisor_pid': 999999, 'supervisor_identity': 'missing'}),
                 (self.new, {'engine': 'claude'}),
                 (self.old, {'recovery_to': 'missing'})]
        for directory, fields in cases:
            with self.subTest(fields=fields):
                self.old.joinpath('run.json').write_bytes(old_good)
                self.new.joinpath('run.json').write_bytes(new_good)
                record = read_json(directory / 'run.json')
                write_json(directory / 'run.json', dict(record, **fields))
                with self.assertRaises(ValueError):
                    self.retire()
                self.assertEqual(state_path.read_bytes(), worker_good)
        self.old.joinpath('run.json').write_bytes(old_good)
        self.new.joinpath('run.json').write_bytes(new_good)
        worker = read_json(state_path)
        worker['owner_run'] = 'wrong-run'
        write_json(state_path, worker)
        before = state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'belong'):
            self.retire()
        self.assertEqual(state_path.read_bytes(), before)

    def test_permission_failure_leaves_state_unchanged(self):
        before = lifecycle.path_of(self.store, 'worker').read_bytes()
        with patch('token_kit.core.store.os.kill', side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, 'Cannot establish'):
                self.retire()
        self.assertEqual(lifecycle.path_of(self.store, 'worker').read_bytes(), before)

    def test_fresh_checkpoint_and_evidence_required(self):
        path = lifecycle.path_of(self.store, 'worker')
        worker = read_json(path)
        worker['started_checkpoint'] = str(self.store.latest('worker'))
        write_json(path, worker)
        with self.assertRaisesRegex(ValueError, 'fresh worker checkpoint'):
            self.retire()
        artifact = self.store.agent_path('worker') / 'artifacts' / 'operation.txt'
        artifact.write_text('partial publication')
        self.store.checkpoint('worker', evidence=[str(artifact)])
        artifact.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            self.retire()

    def test_retired_ticket_cannot_revive_or_claim_completion(self):
        self.retire()
        before = lifecycle.path_of(self.store, 'worker').read_bytes()
        for call in (lambda: lifecycle.bind(self.store, 'worker', self.ticket, '/root/old-worker'),
                     lambda: lifecycle.request(self.store, 'worker', self.ticket, 'done', complete=True),
                     lambda: lifecycle.stopped(self.store, 'worker', self.ticket, 'closed')):
            with self.assertRaises(ValueError):
                call()
        lifecycle.observe_native(self.store, 'coordinator', self.old.name, '/root/old-worker', 'stop')
        lifecycle.record_native_identity(self.store, 'coordinator', self.old.name, '/root/old-worker', 'uuid')
        self.assertEqual(lifecycle.path_of(self.store, 'worker').read_bytes(), before)

    def test_notice_signature_ignores_metadata_and_stopped_is_not_pending(self):
        first = lifecycle.notice(self.store, 'coordinator')
        path = lifecycle.path_of(self.store, 'worker')
        record = read_json(path)
        record.update(observed_window=123456, effective_threshold=9999, arbitrary_metadata='updated')
        write_json(path, record)
        self.assertEqual(lifecycle.notice(self.store, 'coordinator')[0], first[0])
        record['phase'] = 'stopped'
        write_json(path, record)
        self.assertIsNone(lifecycle.notice(self.store, 'coordinator'))

    def test_changed_or_new_history_requires_checkpoint(self):
        history = self.store.agent_path('worker') / 'historical_state.md'
        history.write_text('Partial publication operation evidence')
        with self.assertRaisesRegex(ValueError, 'historical state changed'):
            self.retire()
        self.store.checkpoint('worker')
        history.write_text('Changed operation evidence')
        with self.assertRaisesRegex(ValueError, 'historical state changed'):
            self.retire()
        self.store.checkpoint('worker')
        self.retire()

    def test_old_progress_checkpoint_and_invalid_timestamp_are_rejected(self):
        checkpoint = self.store.latest('worker')
        path = checkpoint / 'manifest.json'
        manifest = read_json(path)
        for timestamp in ('2000-01-01T00:00:00+00:00', 'invalid', '2026-09-23T00:00:00'):
            with self.subTest(timestamp=timestamp):
                write_json(path, dict(manifest, created_at=timestamp))
                with self.assertRaisesRegex(ValueError, 'during this recovery'):
                    self.retire()
        write_json(path, manifest)
        self.retire()

    def test_managed_worker_run_blocks_retirement(self):
        directory = self.store.agent_path('worker') / 'runs' / 'uncertain'
        directory.mkdir()
        write_json(directory / 'run.json', {'status': 'interrupted'})
        with self.assertRaisesRegex(ValueError, 'managed worker'):
            self.retire()


if __name__ == '__main__':
    unittest.main()
