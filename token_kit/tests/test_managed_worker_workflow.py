"""Managed cross-engine workers use real stores and offline client boundaries."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import os
import textwrap
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit.core import lifecycle
from token_kit.core.store import Store, read_json
from token_kit.workflow import launch, main


class ManagedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store.create(self.root / 'tasks', 'Review', self.root)
        self.store.add_agent('review', 'Review the plan; do not edit source.')
        self.ticket = lifecycle.prepare(self.store, 'review', engine='claude', model='fable')['worker']['ticket']

    def run_worker(self, wait, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(patch('token_kit.workflow.shutil.which', return_value='/fake/claude'))
            stack.enter_context(patch('token_kit.core.store.workspace_head', return_value=None))
            stack.enter_context(patch('token_kit.core.lifecycle.workspace_head', return_value=None))
            client = stack.enter_context(patch('token_kit.workflow.subprocess.Popen'))
            client.return_value.pid = 999999999
            client.return_value.poll.return_value = 0
            client.return_value.wait.return_value = 0
            stack.enter_context(patch('token_kit.workflow.runtime.wait_segment', side_effect=wait))
            rc = launch(self.store, 'review', 'claude', worker_ticket=self.ticket, max_rollovers=1, **kwargs)
            return rc, client

    def complete(self, child, store, agent, run, stop):
        (store.agent_path(agent) / 'out.md').write_text('Plan reviewed; findings recorded.')
        store.checkpoint(agent, evidence=[str(store.agent_path(agent) / 'out.md')])
        lifecycle.request(store, agent, self.ticket, 'Review done', complete=True)
        return 0, {'phase': 'active', 'session_id': 'claude-session'}

    def test_ticket_launch_tracks_run_environment_and_completion(self):
        rc, client = self.run_worker(self.complete)
        self.assertEqual(rc, 0)
        state = lifecycle.inspect(self.store, 'review')
        self.assertEqual(state['phase'], 'completed')
        argv = client.call_args.args[0]
        self.assertIn('--print', argv)
        self.assertIn('fable', argv)
        self.assertEqual(client.call_args.kwargs['stdin'], subprocess.DEVNULL)
        env = client.call_args.kwargs['env']
        self.assertEqual(env['TOKEN_KIT_RUN'], state['managed_run_id'])
        self.assertEqual(env['TOKEN_KIT_AGENT'], 'review')
        self.assertIn('worker complete', argv[-1])
        self.assertIn(self.ticket, argv[-1])
        record = read_json(self.store.agent_path('review') / 'runs' / state['managed_run_id'] / 'run.json')
        self.assertEqual(record['worker_ticket'], self.ticket)
        self.assertEqual(record['status'], 'exited')

    def test_zero_exit_without_completion_is_not_success(self):
        rc, _ = self.run_worker(lambda *a: (0, {'phase': 'active', 'session_id': 'session'}))
        self.assertEqual(rc, 75)
        self.assertEqual(lifecycle.inspect(self.store, 'review')['phase'], 'needs_reconciliation')

    def test_zero_exit_without_hook_handshake_is_not_success(self):
        def no_handshake(*args):
            self.complete(*args)
            return 0, {'phase': 'active', 'session_id': None}
        rc, _ = self.run_worker(no_handshake)
        self.assertEqual(rc, 75)
        self.assertEqual(lifecycle.inspect(self.store, 'review')['phase'], 'needs_reconciliation')

    def test_completion_wins_over_ready(self):
        def complete_ready(*args):
            self.complete(*args)
            return 0, {'phase': 'ready', 'session_id': 'session'}
        rc, client = self.run_worker(complete_ready)
        self.assertEqual(rc, 0)
        self.assertEqual(client.call_count, 1)

    def test_inherited_percentage_runs_with_context_override(self):
        with self.store.locked():
            state = lifecycle.read_locked(self.store, 'review')
            state['rollover_tokens'] = '80%'
            lifecycle.publish_locked(self.store, state)
        rc, _ = self.run_worker(self.complete, context_window=200000)
        self.assertEqual(rc, 0)
        worker = lifecycle.inspect(self.store, 'review')
        run = self.store.agent_path('review') / 'runs' / worker['managed_run_id']
        self.assertEqual(read_json(run / 'run.json')['context_window'], 200000)
        self.assertEqual(read_json(run / 'runtime.json')['context_window'], 200000)
        self.assertEqual(read_json(run / 'runtime.json')['threshold'], '80%')

    def test_model_mismatch_does_not_consume_ticket(self):
        with self.assertRaisesRegex(ValueError, 'model must match'):
            launch(self.store, 'review', 'claude', model='opus', worker_ticket=self.ticket)
        self.assertFalse(list((self.store.agent_path('review') / 'runs').glob('*/run.json')))

    def test_cli_ticket_passes_to_launcher(self):
        with patch('token_kit.workflow.launch', return_value=0) as mocked:
            self.assertEqual(main(['launch', str(self.store.path), '--agent', 'review', '--engine', 'claude', '--ticket', self.ticket]), 0)
        self.assertEqual(mocked.call_args.kwargs['worker_ticket'], self.ticket)

    def test_explicit_rollover_below_threshold_is_managed(self):
        calls = []
        def next_segment(*args):
            calls.append(args[3].name)
            if len(calls) == 1:
                self.store.checkpoint('review')
                lifecycle.request(self.store, 'review', self.ticket, 'Context budget reached')
                return 0, {'phase': 'active', 'session_id': 'first'}
            return self.complete(*args)
        rc, client = self.run_worker(next_segment, rollover_tokens=150000)
        self.assertEqual(rc, 0)
        self.assertEqual(client.call_count, 2)

    def test_compaction_recovery_reconciles_prior_segment_before_completion(self):
        calls = []
        def recover(*args):
            run = args[3]
            calls.append(run)
            if len(calls) == 1:
                state = self.store.agent_path('review') / 'STATE.md'
                state.write_text(state.read_text().replace('None yet.', 'Partial review recorded.', 1))
                return 75, {'phase': 'halted', 'halt_kind': 'compaction',
                            'reason': 'Compaction requested', 'session_id': 'first'}
            predecessor = calls[0]
            self.assertIn('Partial review recorded.', (run / 'recovery-input.md').read_text())
            self.assertIn('Reconciliation is the only', (run / 'prompt.md').read_text())
            self.store.checkpoint('review')
            self.store.close_run('review', predecessor.name, 'Verified no children or external operations')
            return self.complete(*args)
        rc, client = self.run_worker(recover)
        self.assertEqual(rc, 0)
        self.assertEqual(client.call_count, 2)
        self.assertEqual(read_json(calls[0] / 'run.json')['status'], 'reconciled')
        self.assertEqual(lifecycle.inspect(self.store, 'review')['phase'], 'completed')

    def test_real_subprocess_hooks_checkpoint_and_complete(self):
        executable = self.root / 'bin' / 'claude'
        executable.parent.mkdir()
        executable.write_text('#!' + sys.executable + '\n' + textwrap.dedent("""
            import os
            from pathlib import Path
            from token_kit.core.store import Store, read_json
            from token_kit.core import lifecycle
            from token_kit import runtime
            store = Store(Path(os.environ['TOKEN_KIT_TASK']))
            agent = os.environ['TOKEN_KIT_AGENT']
            assert agent == 'review'
            run_id = os.environ['TOKEN_KIT_RUN']
            assert run_id != 'parent-run'
            run = store.agent_path(agent) / 'runs' / run_id
            assert read_json(run / 'run.json')['worker_ticket'] == os.environ['TEST_WORKER_TICKET']
            runtime.handle(store, agent, run, {'hook_event_name': 'SessionStart', 'session_id': 'offline-claude'})
            (store.agent_path(agent) / 'out.md').write_text('Offline review complete.')
            store.checkpoint(agent, evidence=[str(store.agent_path(agent) / 'out.md')])
            lifecycle.request(store, agent, os.environ['TEST_WORKER_TICKET'], 'Review complete', complete=True)
        """))
        executable.chmod(0o755)
        environment = {'PATH': str(executable.parent) + os.pathsep + os.environ.get('PATH', ''),
                       'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                       'TOKEN_KIT_RUN': 'parent-run', 'TEST_WORKER_TICKET': self.ticket}
        with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = launch(self.store, 'review', 'claude', worker_ticket=self.ticket)
        self.assertEqual(rc, 0)
        state = lifecycle.inspect(self.store, 'review')
        self.assertEqual(state['phase'], 'completed')
        self.assertIsNone(state['native_id'])
        parent = self.store.resume_bundle('coordinator')
        self.assertTrue(any(m['kind'] == 'worker_completed' for m in parent['pending_messages']))

    def test_rollover_keeps_reserved_alias_and_ticket(self):
        calls = []
        def rollover_then_complete(*args):
            calls.append(args[3].name)
            if len(calls) == 1:
                self.store.checkpoint('review')
                return 0, {'phase': 'ready', 'session_id': 'first', 'sample': {'current_model': 'claude-fable-5-1'}}
            return self.complete(*args)
        rc, client = self.run_worker(rollover_then_complete)
        self.assertEqual(rc, 0)
        self.assertEqual(client.call_count, 2)
        self.assertNotEqual(calls[0], calls[1])
        for call in client.call_args_list:
            self.assertIn('fable', call.args[0])
            self.assertIn(self.ticket, call.args[0][-1])


if __name__ == '__main__':
    unittest.main()
