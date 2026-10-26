"""Public folder-first behavior, using only disposable task roots."""
import contextlib
import io
import json
import os
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
        from token_kit.task_files import create_task
        return create_task(self.tasks, title, self.work).root

    def test_headless_settings_persist_and_override(self):
        from dataclasses import asdict
        from token_kit.task_files import load_task, save_settings, load_settings
        task = self.create()
        view = load_task(task)
        args = cli.parser_for('run').parse_args(['Title', '--headless'])
        options = cli.resolve_options(args)
        save_settings(view, asdict(options))
        continued = cli.resolve_options(cli.parser_for('continue').parse_args([str(task)]), load_settings(view))
        self.assertTrue(continued.headless)
        overridden = cli.resolve_options(cli.parser_for('continue').parse_args([str(task), '--no-headless']), load_settings(view))
        self.assertFalse(overridden.headless)
        legacy = cli.resolve_options(cli.parser_for('continue').parse_args([str(task), '--no-headless']), {'non_interactive': True})
        self.assertFalse(legacy.non_interactive)
        with self.assertRaisesRegex(ValueError, 'headless'):
            cli.resolve_options(cli.parser_for('continue').parse_args([str(task)]), {'headless': 'bad'})

    def test_idle_rollover_options(self):
        args = cli.parser_for('continue').parse_args(['task', '--idle-rollover', '0', '--idle-min-growth', '5000'])
        options = cli.resolve_options(args)
        self.assertEqual((options.idle_rollover, options.idle_min_growth), (0, 5000))
        default = cli.resolve_options(cli.parser_for('continue').parse_args(['task']))
        self.assertEqual((default.idle_rollover, default.idle_min_growth), (50, 100_000))

    def test_saved_codex_task_continues_on_claude(self):
        from dataclasses import asdict
        from token_kit.task_files import load_task, save_settings, load_settings
        task = self.create()
        # Shape of a real Codex-era session.json settings object.
        session = task / '.token-kit/session.json'
        data = json.loads(session.read_text())
        data['settings'] = {'engine': 'codex', 'model': 'gpt-x', 'effort': 'medium', 'yolo': True,
                            'rollover': '80%', 'max_rollovers': None, 'context_window': None,
                            'executable': None, 'non_interactive': False, 'headless': True, 'sandbox': None}
        session.write_text(json.dumps(data))
        code, out, err = self.call('continue', str(task), '--root', self.tasks, '--dry-run')
        self.assertEqual(code, 0, err)
        self.assertIn('saved for Codex and now continues on Claude', err)
        settings = json.loads(out)['settings']
        self.assertEqual((settings['model'], settings['effort'], settings['yolo'], settings['headless']),
                         ('opus', None, False, True))
        self.assertNotIn('engine', settings)
        self.assertEqual(json.loads(session.read_text())['settings']['engine'], 'codex')
        view = load_task(task)
        save_settings(view, asdict(cli.resolve_options(cli.parser_for('continue').parse_args([str(task)]), load_settings(view))))
        self.assertNotIn('engine', load_settings(view))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli.resolve_options(cli.parser_for('continue').parse_args([str(task)]), load_settings(view))
        self.assertEqual(err.getvalue(), '')
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parser_for('continue').parse_args([str(task), '--engine', 'codex'])

    def test_preview_create_does_not_write(self):
        code, out, err = self.call('run', 'Draft', '--root', self.tasks, '--workspace', self.work, '--dry-run')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['settings']['rollover'], '80%')
        self.assertFalse(self.tasks.exists())

    def test_new_task_no_lifecycle_and_readonly_status(self):
        task = self.create()
        (task / f'{task.name}_out.md').write_text('Arbitrary notes. No headings.')
        before = {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()}
        code, out, err = self.call('status', task, '--full')
        self.assertEqual(code, 0, err)
        self.assertIn('Arbitrary notes', out)
        self.assertEqual(before, {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()})
        self.assertFalse((task / 'agents').exists())

    def test_obsolete_commands_fail_without_writes(self):
        task = self.create()
        before = {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()}
        for command in ('worker', 'close-run', 'agent', 'install'):
            code, out, err = self.call(command, task)
            self.assertEqual(code, 2)
            self.assertIn('No records changed', err)
        for command in ('send', 'checkpoint', 'done', 'reopen', 'retitle', 'resume', 'launch', 'new'):
            code, out, err = self.call(command, task, 'x')
            self.assertEqual(code, 2)
            self.assertIn('Unknown command', err)
        self.assertEqual(before, {str(p): p.read_bytes() for p in task.rglob('*') if p.is_file()})

    def test_ambiguous_continue_does_not_launch(self):
        self.create('Fix parser one')
        self.create('Fix parser two')
        code, out, err = self.call('continue', 'parser', '--root', self.tasks)
        self.assertEqual(code, 2)
        self.assertIn('--select', err)

    def test_bare_query_beats_same_named_project_directory(self):
        task = self.create('Finish retread')
        (self.work / 'retread').mkdir()
        previous = Path.cwd()
        try:
            os.chdir(self.work)
            args = cli.parser_for('continue').parse_args(['retread', '--root', str(self.tasks)])
            self.assertEqual(cli.select_task(args), task)
            args.words = ['./retread']
            self.assertEqual(cli.select_task(args).resolve(), self.work / 'retread')
            args.words = ['retr ead']
            self.assertEqual(cli.select_task(args), task)
            args.words = ['./missing']
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                cli.select_task(args)
        finally:
            os.chdir(previous)

    def test_run_task_is_resume_alias(self):
        task = self.create()
        with patch('token_kit.simple_runner.run_session', return_value=0) as run:
            code, out, err = self.call('run', '--task', task)
        self.assertEqual(code, 0, err)
        self.assertTrue(run.call_args.kwargs['resume'])
        self.assertIsNone(run.call_args.kwargs['prompt'])
        self.assertEqual(len(list(self.tasks.iterdir())), 1)

    def test_default_and_explicit_settings(self):
        parser = cli.parser_for('run')
        args = parser.parse_args(['X'])
        self.assertEqual(cli.resolve_options(args).rollover, '80%')
        args = parser.parse_args(['X', '--no-rollover'])
        self.assertIsNone(cli.resolve_options(args).rollover)
        args = parser.parse_args(['X', '--max-rollovers', '0'])
        self.assertEqual(cli.resolve_options(args).max_rollovers, 0)
        args = parser.parse_args(['X', '--no-rollover', '--max-rollovers', '5'])
        with self.assertRaises(ValueError):
            cli.resolve_options(args)

    def test_saved_zero_and_explicit_unlimited(self):
        args = cli.parser_for('continue').parse_args(['X'])
        self.assertEqual(cli.resolve_options(args, {'max_rollovers': 0}).max_rollovers, 0)
        args = cli.parser_for('continue').parse_args(['X', '--max-rollovers', 'unlimited'])
        self.assertIsNone(cli.resolve_options(args, {'max_rollovers': 5}).max_rollovers)

    def test_help_small(self):
        code, out, err = self.call('--help')
        self.assertEqual(code, 0)
        for old in ('prepare', 'close-run', 'checkpoint', 'retire'):
            self.assertNotIn(old, out)


if __name__ == '__main__':
    unittest.main()
