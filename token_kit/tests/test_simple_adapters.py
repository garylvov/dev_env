import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from token_kit import simple_adapters as adapters
from token_kit.adapters.base import AdapterError
from token_kit.simple_types import LaunchOptions, TaskView


class SimpleAdaptersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view = TaskView(self.root, self.root, 'task', self.root/'task.md', self.root/'STATE.md', self.root/'out.md')
        self.env = patch.dict(os.environ, {'HOME': str(self.root), 'TOKEN_KIT_RUN': 'parent',
                                         'TOKEN_KIT_SIMPLE_SESSION_ID': 'parent-id'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def prepare(self, **kwargs):
        return adapters.prepare(self.view, LaunchOptions(**kwargs), '-prompt\n$(not shell)', self.root/'run')

    def test_rollover_off_still_installs_recording_hooks(self):
        help_result = subprocess.CompletedProcess([], 0, '--session-id --settings', '')
        for rollover in ('off', None, 0):
            with self.subTest(rollover=rollover), patch.object(adapters.subprocess, 'run', return_value=help_result):
                plan = self.prepare(rollover=rollover)
                self.assertIn('--settings', plan.argv)
                self.assertEqual(plan.argv[plan.argv.index('--session-id') + 1], plan.env['TOKEN_KIT_SIMPLE_SESSION_ID'])
        self.assertEqual(plan.argv[-2:], ('--', '-prompt\n$(not shell)'))
        self.assertIn('--append-system-prompt', plan.argv)
        self.assertIn('--add-dir', plan.argv)
        self.assertNotIn('TOKEN_KIT_RUN', plan.env)
        self.assertNotIn('DISABLE_COMPACT', plan.env)
        self.assertNotIn('TOKEN_KIT_SIMPLE_RUN', os.environ)
        self.assertFalse(plan.strict_no_compaction)

    def test_uuid_preselected_and_only_new_hooks(self):
        fake = types.ModuleType('token_kit.simple_runtime')
        fake.hooks = lambda: {'SessionStart': [{'hooks': [{'command': 'simple-only'}]}]}
        with patch.dict(sys.modules, {'token_kit.simple_runtime': fake}), patch.object(
                adapters, 'capabilities', return_value=adapters.Capabilities(True, ())):
            plan = self.prepare(model='some-model', effort='high', non_interactive=True)
            next_plan = self.prepare()
        ident = plan.argv[plan.argv.index('--session-id') + 1]
        self.assertEqual(ident, plan.env['TOKEN_KIT_SIMPLE_SESSION_ID'])
        self.assertNotEqual(ident, next_plan.env['TOKEN_KIT_SIMPLE_SESSION_ID'])
        self.assertIn('--print', plan.argv)
        self.assertIn('high', plan.argv)
        self.assertIn('simple-only', plan.argv[plan.argv.index('--settings') + 1])

    def test_headless_argv_and_refusals(self):
        self.assertIn('--print', self.prepare(headless=True, rollover='off').argv)
        with self.assertRaisesRegex(AdapterError, 'requires a prompt'):
            adapters.prepare(self.view, LaunchOptions(headless=True), None, self.root/'run')

    def test_claude_yolo_headless_hooks_need_no_home_hook_install(self):
        settings = self.root / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text('{"permissions": {"allow": []}}')
        before = settings.read_bytes()
        with patch.object(adapters, 'capabilities', return_value=adapters.Capabilities(True, ())):
            initial = self.prepare(yolo=True, headless=True)
            successor = self.prepare(yolo=True, headless=True)
        self.assertEqual(settings.read_bytes(), before)
        for plan in (initial, successor):
            self.assertIn('--print', plan.argv)
            self.assertIn('--dangerously-skip-permissions', plan.argv)
            managed = json.loads(plan.argv[plan.argv.index('--settings') + 1])['hooks']
            self.assertTrue({'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop'} <= managed.keys())
        self.assertNotEqual(initial.env['TOKEN_KIT_SIMPLE_SESSION_ID'], successor.env['TOKEN_KIT_SIMPLE_SESSION_ID'])

    def test_probe_reports_syntax_not_trust(self):
        help_result = subprocess.CompletedProcess([], 0, '--session-id --settings', '')
        with patch.object(adapters.subprocess, 'run', return_value=help_result) as run:
            cap = adapters.capabilities(LaunchOptions())
        self.assertTrue(cap.managed_hooks)
        self.assertEqual(run.call_args.args[0], ['claude', '--help'])
        with patch.object(adapters.subprocess, 'run', side_effect=OSError('missing')):
            self.assertFalse(adapters.capabilities(LaunchOptions()).managed_hooks)

    def test_legacy_hook_refused_without_mutating_config(self):
        config = self.root/'.claude/settings.json'
        config.parent.mkdir()
        body = json.dumps({'hooks': {'Stop': [{'hooks': [{'command': 'python /old/token_kit/runtime.py'}]}]}})
        config.write_text(body)
        with self.assertRaisesRegex(AdapterError, 'cleanup'):
            self.prepare(rollover='off')
        self.assertEqual(config.read_text(), body)

    def test_unrelated_hooks_and_nonhook_mentions_preserved(self):
        config = self.root/'.claude/settings.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'command': 'other-tool'}]}]},
                                      'description': 'token_kit'}))
        self.prepare(rollover='off')

    def test_idle_guidance(self):
        plan = adapters.prepare(self.view, LaunchOptions(rollover='off'), None, self.root/'run')
        self.assertNotIn('--', plan.argv)
        self.assertIn('--add-dir', plan.argv)
        self.assertIn('--append-system-prompt', plan.argv)

    def test_new_task_greeting_with_or_without_prompt(self):
        plan = adapters.prepare(self.view, LaunchOptions(rollover='60%'), None, self.root/'run', greet=True)
        self.assertEqual(plan.argv[-2], '--')
        greeting = plan.argv[-1]
        self.assertIn(str(self.root.resolve()), greeting)
        self.assertIn('rollover at 60%', greeting)
        self.assertIn('CodeGraph off', greeting)
        self.assertIn('at most 8 lines', greeting)
        self.assertLess(len(greeting.split()), 90)
        self.assertNotIn('--', adapters.prepare(self.view, LaunchOptions(), None, self.root/'run').argv)
        with_prompt = adapters.prepare(self.view, LaunchOptions(), 'do work', self.root/'run', greet=True)
        # A new task with a prompt is greeted too, then starts on the prompt.
        self.assertIn('at most 8 lines', with_prompt.argv[-1])
        self.assertTrue(with_prompt.argv[-1].endswith('\n\ndo work'))
        plain = adapters.prepare(self.view, LaunchOptions(), 'do work', self.root/'run')
        self.assertEqual(plain.argv[-2:], ('--', 'do work'))
        with self.assertRaises(AdapterError):  # headless still needs a real prompt
            adapters.prepare(self.view, LaunchOptions(headless=True), None, self.root/'run', greet=True)

    def test_unrelated_token_kit_named_hook_not_legacy(self):
        config = self.root/'.claude/settings.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'command': 'python /custom/token_kit_metrics.py'}]}]}}))
        self.prepare(rollover='off')

    def test_bad_argv_values_rejected(self):
        for kwargs in ({'model': '--bad'}, {'effort': 'x\0y'}, {'executable': '-bad'}):
            with self.assertRaises(AdapterError):
                self.prepare(rollover='off', **kwargs)


if __name__ == '__main__':
    unittest.main()
