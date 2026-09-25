"""Explicit continuation preserves and reconciles interrupted recovery chains."""
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit.core.store import Store, read_json, write_json, process_identity
from token_kit.core import lifecycle


class ContinueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store.create(root / 'tasks', 'continue', root)

    def interrupt(self, run):
        self.store.update_run('coordinator', run.name, status='interrupted', halt_kind='manual',
                              child_pid=999999, child_identity='missing', supervisor_pid=999999,
                              supervisor_identity='missing')

    def chain(self):
        a = self.store.claim_run('coordinator', 'codex', True)
        self.interrupt(a)
        b = self.store.claim_run('coordinator', 'codex', True, recovery_from=a.name, continuation=True)
        self.interrupt(b)
        c = self.store.claim_run('coordinator', 'codex', True, recovery_from=b.name, continuation=True)
        return a, b, c

    def test_chain_closes_oldest_first_using_fresh_leaf_checkpoint(self):
        a, b, c = self.chain()
        self.assertEqual(self.store.continuation_candidate('coordinator'), c.name)
        self.assertEqual([r['run_id'] for r in self.store.recovery_ancestors('coordinator', c.name)], [a.name, b.name])
        with self.assertRaisesRegex(ValueError, 'oldest-first'):
            self.store.close_run('coordinator', b.name, 'reviewed')
        with self.assertRaisesRegex(ValueError, 'fresh committed'):
            self.store.close_run('coordinator', a.name, 'reviewed')
        self.store.checkpoint('coordinator')
        self.store.close_run('coordinator', a.name, 'reviewed A')
        middle = read_json(b / 'run.json')
        self.assertTrue(middle['recovery_completed'])
        self.assertTrue(middle['recovery_pending'])
        self.assertEqual(middle['status'], 'interrupted')
        self.store.close_run('coordinator', b.name, 'reviewed B')
        self.assertFalse(read_json(c / 'run.json')['recovery_pending'])
        self.assertTrue(read_json(c / 'run.json')['recovery_completed'])

    def test_explicit_only_and_live_ancestor_proof(self):
        a, b, c = self.chain()
        self.interrupt(c)
        self.assertIsNone(self.store.recovery_candidate('coordinator'))
        self.store.update_run('coordinator', a.name, child_pid=os.getpid(), child_identity=process_identity(os.getpid()))
        with self.assertRaisesRegex(ValueError, 'may still be alive'):
            self.store.claim_run('coordinator', 'codex', True, recovery_from=c.name, continuation=True)
        self.assertNotIn('recovery_to', read_json(c / 'run.json'))

    def test_cycle_and_missing_link_rejected(self):
        a, b, c = self.chain()
        self.store.update_run('coordinator', a.name, recovery_from=c.name)
        self.store.update_run('coordinator', c.name, recovery_to=a.name)
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            self.store.continuation_candidate('coordinator')
        self.store.update_run('coordinator', c.name, recovery_to='missing')
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            self.store.continuation_candidate('coordinator')

    def test_disconnected_run_rejected(self):
        a, b, c = self.chain()
        extra = c.parent / 'other'
        extra.mkdir()
        write_json(extra / 'run.json', dict(read_json(a / 'run.json'), run_id='other', recovery_to=None, recovery_pending=False))
        with self.assertRaisesRegex(ValueError, 'Disconnected|ambiguous'):
            self.store.continuation_candidate('coordinator')

    def test_handoff_lock_excludes_normal_claim(self):
        with (self.store.path / '.continue.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, 'handoff'):
                self.store.claim_run('coordinator', 'codex', True)
            self.store.claim_run('coordinator', 'codex', True, continuation=True)

    def test_retire_worker_from_earlier_ancestor(self):
        self.store.add_agent('worker', 'inspect partial operation')
        a = self.store.claim_run('coordinator', 'codex', True)
        worker = lifecycle.prepare(self.store, 'worker', engine='codex', owner_agent='coordinator', owner_run=a.name)['worker']
        lifecycle.bind(self.store, 'worker', worker['ticket'], '/root/old')
        self.interrupt(a)
        b = self.store.claim_run('coordinator', 'codex', True, recovery_from=a.name, continuation=True)
        self.interrupt(b)
        c = self.store.claim_run('coordinator', 'codex', True, recovery_from=b.name, continuation=True)
        self.store.checkpoint('worker')
        result = lifecycle.retire(self.store, 'worker', worker['ticket'], 'Partial operation inspected; no retry.',
                                  operations_reconciled=True, recovery_agent='coordinator', recovery_run=c.name)
        self.assertEqual(result['retirement']['predecessor_run'], a.name)
        self.assertFalse(result['retirement']['native_closure_confirmed'])

    def test_pending_request_exited_tip_remains_selectable(self):
        old = self.store.claim_run('coordinator', 'codex', True)
        self.interrupt(old)
        self.store.update_run('coordinator', old.name, halt_kind='compaction', continuation_requested=True)
        self.assertIsNone(self.store.recovery_candidate('coordinator'))
        self.store.update_run('coordinator', old.name, status='exited')
        self.assertEqual(self.store.continuation_candidate('coordinator'), old.name)

    def test_exited_native_owner_requires_reconciliation(self):
        self.store.add_agent('worker', 'inspect partial operation')
        old = self.store.claim_run('coordinator', 'codex', True)
        lifecycle.prepare(self.store, 'worker', engine='codex', owner_agent='coordinator', owner_run=old.name)
        self.store.update_run('coordinator', old.name, status='exited')
        self.assertEqual(self.store.continuation_candidate('coordinator'), old.name)

    def test_reconciled_history_does_not_pin_host_or_engine(self):
        a, b, c = self.chain()
        self.store.checkpoint('coordinator')
        self.store.close_run('coordinator', a.name, 'reviewed')
        self.store.close_run('coordinator', b.name, 'reviewed')
        self.store.update_run('coordinator', a.name, host='historical-host', engine='claude')
        self.interrupt(c)
        self.store.claim_run('coordinator', 'codex', True, recovery_from=c.name, continuation=True)
