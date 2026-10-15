"""Context window overrides survive CLI handoffs and beat provider defaults."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit import workflow, runtime
from token_kit.core.store import Store, read_json


class ContextWindowTests(unittest.TestCase):
    def test_all_entry_points_accept_positive_token_count_only(self):
        parser = workflow.build_parser()
        for command in (['run'], ['pick'], ['continue'], ['launch', '/task', '--engine', 'claude'],
                        ['worker', 'prepare', '/task', '--agent', 'review']):
            self.assertEqual(parser.parse_args(command + ['--context-window', '200k']).context_window, 200000)
            for value in ('0', '-1', '80%', '1.5', 'true'):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args(command + ['--context-window', value])

    def test_continue_inherits_or_overrides_and_engine_change_drops_override(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store.create(Path(root) / 'tasks', 'Review', Path(root))
            previous = {'engine': 'claude', 'context_window': 200000, 'model': 'fable'}
            for extra, expected in (([], '--context-window 200000'),
                                    (['--context-window', '1m'], '--context-window 1000000'),
                                    (['--engine', 'codex'], None), (['--model', 'sonnet'], None)):
                output = io.StringIO()
                with patch.object(workflow, 'latest_picker_run', return_value=previous), contextlib.redirect_stdout(output):
                    self.assertEqual(workflow.main(['continue', '--task', str(store.path), '--print', *extra]), 0)
                if expected:
                    self.assertIn(expected, output.getvalue())
                else:
                    self.assertNotIn('--context-window', output.getvalue())

    def test_runtime_override_beats_reported_window(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store.create(Path(root) / 'tasks', 'Review', Path(root))
            run = store.claim_run('coordinator', 'codex', True)
            transcript = Path(root) / 'usage.jsonl'
            transcript.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110},
                'last_token_usage': {'total_tokens': 110}, 'model_context_window': 1000}}}) + '\n')
            runtime.initialize(store, 'coordinator', run, 'codex', '80%', context_window=2000)
            for event in ('SessionStart', 'PostToolUse'):
                self.assertEqual(runtime.handle(store, 'coordinator', run, {'hook_event_name': event,
                    'session_id': 's', 'transcript_path': str(transcript)}), {})
            control = read_json(run / 'runtime.json')
            self.assertEqual(control['effective_threshold'], 1600)
            self.assertEqual(control['observed_window'], 2000)
            self.assertEqual(control['context_window_source'], 'explicit_override')

    def test_exit_resume_command_preserves_override(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            store = Store.create(Path(root) / 'tasks', 'Review', Path(root))
            with contextlib.redirect_stderr(output):
                workflow.exit_summary(store, 'coordinator', 'claude', 'fable', False, '80%', None,
                                      0, {}, context_window=200000)
        self.assertIn('--context-window 200000', output.getvalue())
