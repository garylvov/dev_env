"""Regression checks for the Token Kit audit findings (each fails without its fix)."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from token_kit import simple_runtime
from token_kit.simple_adapters import folder_guidance
from token_kit.simple_guidance import CONCURRENCY
from token_kit.simple_process import read, write
from token_kit.simple_records import EXPERIMENTS_SKELETON, is_launch, record_launch
from token_kit.simple_runtime import handle, hooks, initialize, model_window
from token_kit.simple_types import LaunchOptions
from token_kit.task_files import append_ask, create_task, load_task, recovery_input


class HookTests(unittest.TestCase):
    """Hook state machine over a real task folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view = create_task(self.root / 'tasks', 'Audit', self.root, 'First ask')
        self.transcript = self.root / 'transcript.jsonl'
        self.usage(10_000)

    def start(self, rollover='60%', **control):
        self.run = self.view.root / '.token-kit' / 'run'
        self.run.mkdir(exist_ok=True)
        initialize(self.run, LaunchOptions(rollover=rollover, context_window=100_000), 'parent',
                   self.view.state, assignment=self.view.assignment, output=self.view.output)
        current = read(self.run / 'control.json')
        current.update(task_root=str(self.view.root), **control)
        write(self.run / 'control.json', current)
        self.hook('SessionStart')

    def usage(self, tokens, session='parent'):
        self.transcript.write_text(json.dumps({'type': 'assistant', 'sessionId': session,
                                               'message': {'usage': {'input_tokens': tokens}}}) + '\n')

    def hook(self, event, session='parent', **fields):
        payload = dict({'session_id': session, 'hook_event_name': event,
                        'transcript_path': str(self.transcript)}, **fields)
        return handle(self.run, payload, self.run.name)

    def control(self):
        return read(self.run / 'control.json')

    def request_summary(self):
        self.start(startup_tokens=10_000)
        self.usage(70_000)
        self.hook('PostToolUse')
        self.assertEqual(self.control()['phase'], 'requested')

    # Finding 1: rollover off keeps recording.
    def test_rollover_off_threshold_none_but_records(self):
        for off in ('off', 0, None):
            self.start(rollover=off)
            self.assertIsNone(self.control()['threshold'])
        self.hook('UserPromptSubmit', prompt='record me while rollover is off')
        self.assertIn('record me while rollover is off', self.view.assignment.read_text())

    # Finding 2: native compaction mid-summary never ends the session early.
    def test_precompact_during_requested_summary_waits_for_stop(self):
        self.request_summary()
        self.usage(95_000)
        self.assertEqual(self.hook('PreCompact', trigger='auto'), {})
        self.assertEqual(self.control()['phase'], 'requested')
        restated = self.hook('SessionStart', source='compact')
        self.assertIn('Append a History entry', restated['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.control()['phase'], 'requested')
        self.assertFalse(self.hook('Stop')['continue'])
        self.assertEqual(self.control()['phase'], 'ready')

    # Finding 3: /clear rebinds to the new session id.
    def test_clear_rebinds_session_and_logs_history(self):
        self.start()
        self.hook('SessionStart', session='fresh', source='clear')
        self.assertEqual(self.control()['expected_session'], 'fresh')
        self.assertIn('/clear started session fresh', self.view.output.read_text())
        self.hook('UserPromptSubmit', session='fresh', prompt='ask after clear')
        self.assertIn('ask after clear', self.view.assignment.read_text())
        # Other sources (a nested startup) never steal the binding.
        self.hook('SessionStart', session='intruder', source='startup')
        self.assertEqual(self.control()['expected_session'], 'fresh')

    # Finding 4: vanished native workers stop pinning native compaction.
    def test_stop_background_tasks_clear_stale_children(self):
        self.start()
        self.hook('SubagentStart', agent_id='w1', agent_type='general-purpose')
        running = [{'id': 't1', 'type': 'subagent', 'status': 'running', 'agent_type': 'general-purpose'}]
        self.hook('Stop', background_tasks=running)
        self.assertEqual(self.control()['children'], ['w1'])
        self.hook('Stop', background_tasks=[{'id': 't1', 'type': 'shell', 'status': 'running'}])
        self.assertEqual(self.control()['children'], [])

    # Named Agent workers report as 'teammate'; a rollover would kill them.
    def test_teammates_defer_rollover_with_one_note(self):
        self.start(startup_tokens=10_000)
        self.usage(70_000)
        teammate = [{'id': 't1', 'type': 'teammate', 'status': 'running', 'description': 'fleet submitter'}]
        note = self.hook('Stop', background_tasks=teammate)
        self.assertEqual(self.control()['phase'], 'running')
        self.assertTrue(self.control()['unknown_children'])
        self.assertIn('rollover waits', note['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('rollover waits', json.dumps(self.hook('Stop', background_tasks=teammate)))
        self.hook('Stop', background_tasks=[dict(teammate[0], status='completed')])
        self.assertEqual(self.control()['phase'], 'requested')

    def test_unknown_children_clear_at_main_stop_and_children_expire(self):
        self.start()
        self.hook('SubagentStart')  # no agent_id: fate unknown
        self.assertTrue(self.control()['unknown_children'])
        self.hook('Stop')
        self.assertFalse(self.control().get('unknown_children'))
        self.hook('SubagentStart', agent_id='w2', agent_type='general-purpose')
        current = self.control()
        current['child_seen']['w2'] = time.time() - simple_runtime.CHILD_TTL - 1
        write(self.run / 'control.json', current)
        self.hook('PostToolUse')
        self.assertEqual(self.control()['children'], [])

    def test_child_activity_keeps_it_alive(self):
        self.start()
        self.hook('SubagentStart', agent_id='w3', agent_type='general-purpose')
        current = self.control()
        current['child_seen']['w3'] = time.time() - simple_runtime.CHILD_TTL - 1
        write(self.run / 'control.json', current)
        self.hook('PostToolUse', agent_id='w3', agent_type='general-purpose', tool_name='Read')
        self.hook('PostToolUse')
        self.assertEqual(self.control()['children'], ['w3'])

    # Finding 9: a failed record never discards the user's message.
    def test_record_failure_lets_prompt_through(self):
        self.start()
        with patch('token_kit.task_files.append_ask', side_effect=OSError('disk full')):
            result = self.hook('UserPromptSubmit', prompt='important')
        self.assertNotIn('decision', result)
        self.assertIn('append it verbatim', result['hookSpecificOutput']['additionalContext'])
        self.assertIn('disk full', self.control()['record_warning'])

    # Token waste: the system prompt already carries the guidance.
    def test_first_prompt_does_not_repeat_guidance(self):
        self.start()
        with patch.dict(os.environ, {'TOKEN_KIT_SIMPLE_GUIDANCE': 'GUIDANCE SENTINEL'}):
            result = self.hook('UserPromptSubmit', prompt='hello')
        self.assertNotIn('GUIDANCE SENTINEL', json.dumps(result))

    # Feature A: one nudge per message whose turn left asks.md untouched.
    def test_stop_nudges_once_when_asks_not_updated(self):
        self.start(rollover=None)
        asks = self.view.root / 'asks.md'
        os.utime(asks, (1, 1))
        self.hook('UserPromptSubmit', prompt='a new ask')
        number = self.control()['last_ask']
        nudge = self.hook('Stop')
        self.assertEqual(nudge['hookSpecificOutput']['additionalContext'],
                         f'Token Kit: asks.md not updated for #{number}.')
        self.assertEqual(self.hook('Stop'), {})
        self.hook('UserPromptSubmit', prompt='another ask')
        asks.touch()
        self.assertEqual(self.hook('Stop'), {})

    # Feature B: peak tokens feed the usage log.
    def test_peak_tokens_tracked(self):
        self.start(rollover=None)
        self.usage(30_000)
        self.hook('PostToolUse')
        self.usage(20_000)
        self.hook('PostToolUse')
        self.assertEqual(self.control()['peak_tokens'], 30_000)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    # Feature B: one usage row per client run.
    def test_usage_log_row(self):
        from token_kit.simple_runner import _log_usage
        view = create_task(self.root / 'tasks', 'Usage', self.root, 'Work')
        run = self.root / 'r1'
        _log_usage(view, run, {'startup_tokens': 9, 'peak_tokens': 90, 'reason': 'x'}, True, 0)
        row = json.loads((view.root / '.token-kit/usage.jsonl').read_text())
        self.assertEqual((row['run_id'], row['startup_tokens'], row['peak_tokens'], row['rollover']), ('r1', 9, 90, True))

    # Finding 6: launch detection.
    def test_launch_detection(self):
        for command in ('python -m pkg.train --seed 1', 'torchrun --nproc_per_node 8 run.py',
                        'srun -N1 python run.py', 'accelerate launch run.py', 'ray job submit -- python run.py',
                        'python scripts/train.py', 'uv run train_ppo.py'):
            with self.subTest(command=command):
                self.assertTrue(is_launch(command))
        for command in ('pytest tests/test_train_loop.py', 'uv run pytest -k train', 'cd fleet && ls',
                        'python constraints.py', 'python -m pkg.constraints'):
            with self.subTest(command=command):
                self.assertFalse(is_launch(command))

    # Finding 8: pasted numbered lists do not advance message numbers.
    def test_pasted_numbered_list_does_not_skew_numbering(self):
        view = create_task(self.root / 'tasks', 'Numbers', self.root, 'Plan:\n12. [ ] step\n13. 2026 goals')
        self.assertEqual(append_ask(view.root, view.assignment, 'next'), 2)

    # Token waste: the prompt is written once, as message #1.
    def test_create_task_writes_prompt_once(self):
        view = create_task(self.root / 'tasks', 'Once', self.root, 'Single sentinel')
        text = view.assignment.read_text()
        self.assertEqual(text.count('Single sentinel'), 1)
        self.assertRegex(text, r'^\n1\. [0-9]{4}-')
        self.assertNotIn('# Asks', text)

    # Finding 10: hooks only append; the model's edits survive.
    def test_experiments_append_only(self):
        root = self.root / 'task'
        root.mkdir()
        record_launch(root, 'sbatch one.sh', 'main')
        path = root / 'experiments.md'
        curated = path.read_text().replace('- [?] E1', '- [running] E1') + 'model note\n'
        path.write_text(curated)
        record_launch(root, 'sbatch two.sh', 'main')
        text = path.read_text()
        self.assertTrue(text.startswith(curated))
        self.assertEqual(text.count('\n## Unlabeled\n'), 1)
        self.assertIn('[?] E2', text.splitlines()[-1])
        text = text.replace('\n## Unlabeled\n', '\n## Open (moved)\n')
        path.write_text(text)
        record_launch(root, 'sbatch three.sh', 'main')
        self.assertTrue(path.read_text().startswith(text))
        self.assertTrue(path.read_text().rstrip().endswith('sbatch three.sh'))
        self.assertTrue(path.read_text().startswith(text) and EXPERIMENTS_SKELETON in path.read_text())

    # Token waste: legacy prose and repeated rules appear only when they apply.
    def test_guidance_has_no_legacy_prose_or_repeats(self):
        view = create_task(self.root / 'tasks', 'Lean', self.root, 'Work')
        guidance = folder_guidance(view)
        self.assertEqual(guidance.count(CONCURRENCY.strip()), 1)
        self.assertNotIn('supersedes older generated', guidance)
        self.assertNotIn('Legacy task.md', guidance)
        recovered = recovery_input(view, paths_only=True).text
        self.assertNotIn('superseded', recovered)
        self.assertNotIn(CONCURRENCY.strip(), recovered)
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / 'task.md').write_text('old task')
        (legacy / 'STATE.md').write_text('old state')
        self.assertIn('Legacy task.md', folder_guidance(load_task(legacy, self.root)))

    # Finding 11: the hook survives losing its interpreter.
    def test_hook_command_falls_back_when_interpreter_vanishes(self):
        with patch.object(simple_runtime.sys, 'executable', str(self.root / 'gone/python3')):
            command = hooks()['Stop'][0]['hooks'][0]['command']
        result = subprocess.run(shlex.split(command), input='{}', text=True, capture_output=True, timeout=60,
                                env=dict(os.environ, TOKEN_KIT_SIMPLE_RUN=str(self.root / 'none')))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '{}'), result.stderr)

    # Finding 12: dated, aliased and unknown claude ids still give a window.
    def test_model_window_normalizes_ids(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(model_window('claude-opus-5-5-20261001')[0], 1_000_000)
            self.assertEqual(model_window('claude-sonnet-4-6[1m]')[0], 200_000)
            self.assertEqual(model_window('opus')[0], 1_000_000)
            self.assertEqual(model_window('claude-new-thing-9'), (200_000, 'assumed_default'))
            self.assertEqual(model_window('custom-gateway-model'), (None, None))

    def test_unknown_claude_model_reports_assumption(self):
        transcript = self.root / 't.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'sessionId': 's', 'message': {
            'model': 'claude-new-thing-9', 'usage': {'input_tokens': 5}}}) + '\n')
        with patch.dict(os.environ, {}, clear=True):
            sample = simple_runtime.usage_sample(transcript, 's')
        self.assertEqual(sample['context_window'], 200_000)
        self.assertIn('claude-new-thing-9', sample['window_note'])


if __name__ == '__main__':
    unittest.main()
