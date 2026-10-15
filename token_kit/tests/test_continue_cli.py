"""Saved-task selection and continuation preflight, without native clients."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from token_kit import workflow
from token_kit.core.store import Store, read_json


class ContinueCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store.create(self.root / "tasks", "Retread continuation", self.root)

    def invoke(self, argv):
        self.output, self.errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            return workflow.main(argv)

    def test_print_single_fuzzy_match_inherits_without_launch_or_mutation(self):
        before = sorted(str(p.relative_to(self.store.path)) for p in self.store.path.rglob('*'))
        previous = dict(engine="codex", model="astra", yolo=True, rollover_tokens="80%", max_rollovers=2)
        with patch.object(workflow, 'latest_picker_run', return_value=previous), patch.object(workflow, 'run') as launch:
            rc = self.invoke(['continue', 'retrread', '--root', str(self.root / 'tasks'), '--print'])
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertIn('token-kit continue --task', self.output.getvalue())
        self.assertIn('--engine codex --model astra --rollover-perc 80 --yolo --max-rollovers 2', self.output.getvalue())
        self.assertEqual(before, sorted(str(p.relative_to(self.store.path)) for p in self.store.path.rglob('*')))

    def test_exact_path_and_overrides(self):
        for selector in ([str(self.store.path)], ['--task', str(self.store.path)]):
            with patch.object(workflow, 'latest_picker_run', return_value=dict(engine='codex', yolo=True)), patch.object(workflow, 'run', return_value=0) as launch:
                self.assertEqual(self.invoke(['continue', *selector, '--engine', 'claude', '--model', 'sonnet', '--no-yolo', '--max-rollovers', '0']), 0)
            args = launch.call_args.args[0]
            self.assertTrue(args.continue_session)
            self.assertFalse(args.yolo)
            self.assertEqual(args.max_rollovers, 0)
            self.assertEqual(args.engine, 'claude')
            self.assertEqual(args.model, 'sonnet')

    def test_multiple_matches_require_selection_without_terminal(self):
        Store.create(self.root / 'tasks', 'Retread second', self.root)
        with patch.object(workflow.sys.stdin, 'isatty', return_value=False), patch.object(workflow, 'run') as launch:
            self.assertEqual(self.invoke(['continue', 'retread', '--root', str(self.root / 'tasks')]), 2)
            launch.assert_not_called()
            self.assertEqual(self.invoke(['continue', 'retread', '--root', str(self.root / 'tasks'), '--limit', '1']), 2)
            self.assertEqual(self.invoke(['continue', 'retread', '--root', str(self.root / 'tasks'), '--select', '2', '--print']), 0)

    def test_preflight_failure_never_hands_off(self):
        with patch('token_kit.continuation.handoff') as handoff, patch.object(workflow.shutil, 'which', return_value=None):
            self.assertEqual(self.invoke(['continue', '--task', str(self.store.path)]), 2)
            handoff.assert_not_called()

    def test_run_passes_handoff_after_preflight_even_with_zero_budget(self):
        token = Mock(recovery_from=None)
        @contextlib.contextmanager
        def handoff(*args):
            yield token
        with patch('token_kit.continuation.handoff', side_effect=handoff) as controller, patch.object(workflow.shutil, 'which', return_value='/bin/client'), patch.object(workflow, 'launch', return_value=0) as launch:
            self.assertEqual(self.invoke(['continue', '--task', str(self.store.path), '--max-rollovers', '0']), 0)
        controller.assert_called_once()
        self.assertIs(launch.call_args.kwargs['continuation'], token)
        self.assertEqual(launch.call_args.kwargs['max_rollovers'], 0)

    def test_claim_releases_handoff_and_persists_budget(self):
        token = Mock(recovery_from=None)
        child = Mock(pid=99999999)
        with patch.object(self.store, 'claim_run', wraps=self.store.claim_run) as claim, patch.object(workflow.shutil, 'which', return_value='/bin/client'), patch.object(workflow, 'subprocess', Mock(Popen=Mock(return_value=child), TimeoutExpired=__import__('subprocess').TimeoutExpired)), patch.object(workflow.runtime, 'wait_segment', return_value=(0, {})), patch.object(workflow.ledger, 'refresh'):
            rc, _ = workflow._launch_segment(self.store, 'coordinator', 'claude', None, False, False, None, max_rollovers=0, continuation=token)
        self.assertEqual(rc, 0)
        self.assertTrue(claim.call_args.kwargs['continuation'])
        token.release.assert_called_once()
        records = list((self.store.agent_path('coordinator') / 'runs').glob('*/run.json'))
        self.assertEqual(read_json(records[0])['max_rollovers'], 0)

    def test_footer_preserves_budget_without_threshold(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            workflow.exit_summary(self.store, 'coordinator', 'claude', None, False, None, 0, 0, {})
        self.assertIn('token-kit continue --task', output.getvalue())
        self.assertIn('--max-rollovers 0', output.getvalue())


if __name__ == '__main__':
    unittest.main()
