"""Public folder-first behavior, using only disposable task roots."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from token_kit import simple_cli as cli
from token_kit.__main__ import main


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'source'
        self.work.mkdir()
        self.tasks = self.root / 'tasks'

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def create(self, title='A task'):
        code, out, err = self.call('new', title, '--root', self.tasks, '--workspace', self.work)
        self.assertEqual(code, 0, err)
        return Path(out.strip())

    def test_preview_create_does_not_write(self):
        code, out, err = self.call('run', 'Draft', '--root', self.tasks, '--workspace', self.work, '--dry-run')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['settings']['rollover'], '80%')
        self.assertFalse(self.tasks.exists())

    def test_new_task_no_lifecycle_and_readonly_resume(self):
        task = self.create()
        (task / 'STATE.md').write_text('Arbitrary notes. No headings.')
        before = {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()}
        code, out, err = self.call('resume', task)
        self.assertEqual(code, 0, err)
        self.assertIn('Arbitrary notes', out)
        self.assertEqual(before, {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()})
        self.assertFalse((task / 'agents').exists())

    def test_obsolete_commands_fail_without_writes(self):
        task = self.create()
        before = {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()}
        for command in ('worker', 'close-run', 'agent'):
            code, out, err = self.call(command, task)
            self.assertEqual(code, 2)
            self.assertIn('No records changed', err)
        self.assertEqual(before, {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()})

    def test_label_not_process_approval(self):
        task = self.create()
        legacy = task / 'agents' / 'busy'
        legacy.mkdir(parents=True)
        (legacy / 'lifecycle.json').write_text('{broken')
        code, out, err = self.call('done', task)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((task / '.token-kit/labels.json').read_text())['status'], 'done')
        self.assertEqual((legacy / 'lifecycle.json').read_text(), '{broken')

    def test_ambiguous_continue_does_not_launch(self):
        self.create('Fix parser one')
        self.create('Fix parser two')
        code, out, err = self.call('continue', 'parser', '--root', self.tasks)
        self.assertEqual(code, 2)
        self.assertIn('--select', err)

    def test_run_task_is_resume_alias(self):
        task = self.create()
        with patch('token_kit.simple_runner.run_session', return_value=0) as run:
            code, out, err = self.call('run', '--task', task)
        self.assertEqual(code, 0, err)
        self.assertTrue(run.call_args.kwargs['resume'])
        self.assertEqual(len(list(self.tasks.iterdir())), 1)

    def test_default_and_explicit_settings(self):
        parser = cli.parser_for('run')
        args = parser.parse_args(['X'])
        self.assertEqual(cli.resolve_options(args).rollover, '80%')
        args = parser.parse_args(['X', '--no-rollover'])
        self.assertIsNone(cli.resolve_options(args).rollover)
        args = parser.parse_args(['X', '--max-rollovers', '0'])
        self.assertIsNone(cli.resolve_options(args).rollover)
        args = parser.parse_args(['X', '--no-rollover', '--max-rollovers', '5'])
        with self.assertRaises(ValueError):
            cli.resolve_options(args)

    def test_engine_override_drops_provider_specific_defaults(self):
        args = cli.parser_for('run').parse_args(['X', '--engine', 'codex'])
        options = cli.resolve_options(args, {'engine': 'claude', 'model': 'opus', 'effort': 'high', 'yolo': False})
        self.assertIsNone(options.model)
        self.assertIsNone(options.effort)
        self.assertFalse(options.yolo)

    def test_saved_zero_and_explicit_unlimited(self):
        args = cli.parser_for('continue').parse_args(['X'])
        self.assertIsNone(cli.resolve_options(args, {'max_rollovers': 0}).rollover)
        args = cli.parser_for('continue').parse_args(['X', '--max-rollovers', 'unlimited'])
        self.assertIsNone(cli.resolve_options(args, {'max_rollovers': 5}).max_rollovers)

    def test_help_small(self):
        code, out, err = self.call('--help')
        self.assertEqual(code, 0)
        for old in ('prepare', 'close-run', 'checkpoint', 'retire'):
            self.assertNotIn(old, out)


if __name__ == '__main__':
    unittest.main()
