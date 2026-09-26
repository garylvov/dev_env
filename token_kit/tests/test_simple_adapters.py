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

    def test_off_does_not_probe_or_inject_hooks_and_preserves_defaults(self):
        with patch.object(adapters.subprocess, 'run') as run:
            plan = self.prepare(rollover='off')
        run.assert_not_called()
        self.assertEqual(plan.argv[-2:], ('--', '-prompt\n$(not shell)'))
        self.assertIn('--append-system-prompt', plan.argv)
        self.assertIn('--add-dir', plan.argv)
        self.assertNotIn('TOKEN_KIT_RUN', plan.env)
        self.assertNotIn('TOKEN_KIT_SIMPLE_SESSION_ID', plan.env)
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

    def test_codex_degraded_preserves_effort_default(self):
        plan = self.prepare(engine='codex', rollover='off')
        self.assertNotIn('-c', plan.argv)
        self.assertNotIn('TOKEN_KIT_SIMPLE_SESSION_ID', plan.env)
        self.assertFalse(json.loads(plan.env['TOKEN_KIT_SIMPLE_CAPABILITIES'])['managed_hooks'])
        plan = self.prepare(engine='codex', effort='high', yolo=True, rollover='off')
        self.assertIn('model_reasoning_effort="high"', plan.argv)
        self.assertNotIn('--dangerously-bypass-hook-trust', plan.argv)
        with self.assertRaisesRegex(AdapterError, 'print mode'):
            self.prepare(engine='codex', non_interactive=True)

    def test_probe_reports_syntax_not_trust(self):
        help_result = subprocess.CompletedProcess([], 0, '--session-id --settings', '')
        with patch.object(adapters.subprocess, 'run', return_value=help_result) as run:
            cap = adapters.capabilities(LaunchOptions())
        self.assertTrue(cap.managed_hooks)
        self.assertEqual(cap.hook_trust, 'unverified')
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

    def test_codex_parent_metadata_and_usage(self):
        path = self.root/'transcript.jsonl'
        meta = {'id': 'parent', 'session_id': 'parent', 'source': 'cli'}
        payload = {'session_id': 'parent', 'transcript_path': str(path)}
        def write():
            path.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\n')
        write()
        self.assertEqual(adapters.codex_root_session(payload), 'parent')
        for update in ({'source': {'subagent': {}}}, {'id': 'child'},
                       {'parent_thread_id': 'other'}, {'session_id': 'other'}, {'source': 'vscode'}):
            old = meta.copy()
            meta.update(update)
            write()
            self.assertIsNone(adapters.codex_root_session(payload))
            meta = old
        write()
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count',
                'info': {'last_token_usage': {'total_tokens': 42},
                         'total_token_usage': {'total_tokens': 900}, 'model_context_window': 100}}}))
        self.assertEqual(adapters.codex_usage(path), (42, 100))

    def test_codex_candidates_use_local_process_and_preserve_hook_trust(self):
        fake = types.ModuleType('token_kit.simple_runtime')
        fake.codex_config = lambda: ['--enable', 'hooks']
        with patch.dict(sys.modules, {'token_kit.simple_runtime': fake}), patch.object(
                adapters, 'capabilities', return_value=adapters.Capabilities(True, ())):
            plan = self.prepare(engine='codex')
        self.assertIn('--no-daemon', plan.argv)
        self.assertNotIn('--dangerously-bypass-hook-trust', plan.argv)

    def test_codex_existing_hooks_are_not_overwritten(self):
        config = self.root/'.codex/config.toml'
        config.parent.mkdir()
        body = '[hooks]\nStop = [{ hooks = [{ type = "command", command = "site-policy" }] }]\n'
        config.write_text(body)
        with patch.object(adapters, 'capabilities', return_value=adapters.Capabilities(True, ())):
            plan = self.prepare(engine='codex')
        self.assertNotIn('--enable', plan.argv)
        self.assertIn('preserved', plan.env['TOKEN_KIT_SIMPLE_CAPABILITIES'])

    def test_nonoverlapping_policy_hook_does_not_disable_rollover(self):
        config = self.root/'.codex/config.toml'
        config.parent.mkdir()
        body = '[hooks]\nPreToolUse = [{ hooks = [{ type = "command", command = "site-policy" }] }]\n'
        config.write_text(body)
        with patch.object(adapters, 'capabilities', return_value=adapters.Capabilities(True, ())):
            plan = self.prepare(engine='codex')
        self.assertIn('--no-daemon', plan.argv)
        self.assertFalse(any('hooks.PreToolUse=' in arg for arg in plan.argv))
        self.assertEqual(config.read_text(), body)

    def test_idle_guidance_and_explicit_codex_prompt(self):
        for engine in ('claude', 'codex'):
            plan = adapters.prepare(self.view, LaunchOptions(engine=engine, rollover='off'),
                                    None, self.root/'run')
            self.assertNotIn('--', plan.argv)
            self.assertIn('--add-dir', plan.argv)
            if engine == 'claude':
                self.assertIn('--append-system-prompt', plan.argv)
            else:
                self.assertIn('no injected folder guidance', plan.env['TOKEN_KIT_SIMPLE_CAPABILITIES'])
        explicit = self.prepare(engine='codex', rollover='off')
        self.assertIn(str(self.view.assignment), explicit.argv[-1])
        self.assertTrue(explicit.argv[-1].endswith('-prompt\n$(not shell)'))

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
