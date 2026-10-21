"""Hook-written worker records and experiment capture, from mock hook payloads."""
from pathlib import Path
import re
import tempfile
import unittest

from token_kit.simple_process import read
from token_kit.simple_runtime import handle, hooks, initialize
from token_kit.simple_records import EXPERIMENTS_SKELETON, experiments_note, is_launch, record_launch
from token_kit.simple_types import LaunchOptions
from token_kit.simple_guidance import CONCURRENCY, SUCCESSOR, TASK_RULES, summary_request
from token_kit.task_files import create_task
from token_kit.simple_adapters import prepare


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view = create_task(self.root / 'tasks', 'Records', self.root, 'Do it')
        self.run = self.view.root / '.token-kit' / 'run'
        self.run.mkdir()
        initialize(self.run, LaunchOptions(rollover=None), 'parent', self.view.state,
                   assignment=self.view.assignment, output=self.view.output)
        control = read(self.run / 'control.json')
        control['task_root'] = str(self.view.root)
        from token_kit.simple_process import write
        write(self.run / 'control.json', control)
        self.hook('SessionStart')

    def hook(self, event, **fields):
        return handle(self.run, dict({'session_id': 'parent', 'hook_event_name': event}, **fields), self.run.name)

    def spawn(self, status, agent_id, **tool_input):
        response = {'status': status, 'agentId': agent_id, 'resolvedModel': 'claude-haiku-5-5'}
        if status == 'completed':
            response['content'] = [{'type': 'text', 'text': 'done'}]
        return self.hook('PostToolUse', tool_name='Agent', tool_response=response,
                         tool_input=dict({'prompt': 'Count the lines\nverbatim', 'description': 'Count lines',
                                          'subagent_type': 'general-purpose'}, **tool_input))

    def finish(self, agent_id, message, agent_type='general-purpose'):
        return self.hook('SubagentStop', agent_id=agent_id, agent_type=agent_type,
                         agent_transcript_path='/t/agent.jsonl', last_assistant_message=message)

    def experiments(self):
        return (self.view.root / 'experiments.md').read_text()

    def test_background_worker_in_and_final_records(self):
        self.hook('SubagentStart', agent_id='a1b2c3d4e5', agent_type='general-purpose')
        self.assertEqual(self.spawn('async_launched', 'a1b2c3d4e5', name='count-haiku-low'), {})
        record = (self.view.root / 'agents/count-haiku-low_in.md').read_text()
        self.assertIn('agentId: a1b2c3d4e5', record)
        self.assertIn('model: claude-haiku-5-5', record)
        self.assertIn('background', record)
        self.assertIn('Count the lines\nverbatim', record)
        self.finish('a1b2c3d4e5', 'Found 42 lines.\nDetails follow.')
        out = (self.view.root / 'agents/count-haiku-low_out.md').read_text()
        self.assertRegex(out, r'### final \d{4}-\d{2}-\d{2}T')
        self.assertIn('transcript: /t/agent.jsonl', out)
        self.assertIn('Found 42 lines.\nDetails follow.', out)
        self.assertIn('- worker count-haiku-low (claude-haiku-5-5) finished: Found 42 lines. '
                      '-> agents/count-haiku-low_out.md', self.view.output.read_text())
        self.assertEqual(read(self.run / 'control.json')['children'], [])

    def test_foreground_worker_finishing_before_its_call_returns(self):
        self.finish('ffee001122', 'Foreground answer')
        self.assertFalse((self.view.root / 'agents').exists())
        self.spawn('completed', 'ffee001122')
        name = 'Count-lines-ffee00'
        self.assertIn('foreground', (self.view.root / f'agents/{name}_in.md').read_text())
        self.assertIn('Foreground answer', (self.view.root / f'agents/{name}_out.md').read_text())
        self.assertIn(f'finished: Foreground answer -> agents/{name}_out.md', self.view.output.read_text())

    def test_handback_message_preferred_over_last_message(self):
        self.spawn('async_launched', 'h1h2h3h4', name='hb')
        self.hook('PostToolUse', agent_id='h1h2h3h4', agent_type='general-purpose',
                  tool_name='SubagentHandback', tool_input={'message': 'Handback report'})
        self.finish('h1h2h3h4', 'trailing chatter')
        out = (self.view.root / 'agents/hb_out.md').read_text()
        self.assertIn('Handback report', out)
        self.assertNotIn('trailing chatter', out)

    def test_internal_agents_with_empty_type_are_ignored(self):
        self.finish('internal1', 'compaction summary', agent_type='')
        self.spawn('async_launched', 'internal1', name='x')
        self.assertFalse((self.view.root / 'agents/x_out.md').exists())
        self.assertNotIn('finished', self.view.output.read_text())

    def test_launches_captured_from_parent_and_worker(self):
        self.spawn('async_launched', 'w9w9w9w9', name='trainer-sonnet-low')
        self.assertEqual(self.hook('PreToolUse', tool_name='Bash',
                                   tool_input={'command': 'nohup sleep 1 >/dev/null 2>&1 &'}), {})
        self.hook('PreToolUse', tool_name='Bash', agent_id='w9w9w9w9', agent_type='general-purpose',
                  tool_input={'command': 'cd /x && systemd-run --user --collect uv run python scripts/train.py --seed 3'})
        self.hook('PreToolUse', tool_name='Bash', agent_id='zz11', agent_type='Explore',
                  tool_input={'command': 'sbatch ' + 'x' * 400})
        text = self.experiments()
        # Append-only: the hook never rewrites what the model curates above.
        self.assertTrue(text.startswith(EXPERIMENTS_SKELETON))
        unlabeled = text[text.index('## Unlabeled'):]
        lines = [l for l in unlabeled.splitlines() if l.startswith('- [')]
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r'^- \[\?\] E1 \d{4}-\d{2}-\d{2}T\d{2}:\d{2} unlabeled launch by main: nohup sleep 1')
        self.assertIn('E2', lines[1])
        self.assertIn('by trainer-sonnet-low: cd /x && systemd-run', lines[1])
        self.assertIn('by Explore-zz11:', lines[2])
        self.assertTrue(lines[2].endswith('x...'))
        self.assertLess(len(lines[2]), 400)

    def test_queries_and_other_tools_not_captured(self):
        for command in ('fleet status', 'squeue -u me', 'systemctl --user status shunt', 'cat scripts/train.py',
                        'grep -n lr scripts/train.py', 'git diff train.py', 'journalctl --user -u run-x',
                        'sacct -j 5', 'fleet logs job1 | tail', 'ls', 'python train.py --help',
                        'systemctl --user is-active shunt'):
            self.assertFalse(is_launch(command), command)
        for command in ('python scripts/train.py', 'uv run train_ppo.py', 'fleet submit job.toml',
                        'squeue && sbatch run.sh', 'setsid python x.py &'):
            self.assertTrue(is_launch(command), command)
        self.hook('PreToolUse', tool_name='Bash', tool_input={'command': 'fleet status'})
        self.hook('PreToolUse', tool_name='Read', tool_input={'command': 'nohup x'})
        self.assertEqual(self.experiments(), EXPERIMENTS_SKELETON)

    def test_record_failure_never_blocks_and_is_logged(self):
        (self.view.root / 'experiments.md').unlink()
        (self.view.root / 'experiments.md').mkdir()
        self.assertEqual(self.hook('PreToolUse', tool_name='Bash', tool_input={'command': 'nohup x &'}), {})
        self.assertIn('experiments.md', read(self.run / 'control.json')['record_errors'][-1])

    def test_closed_lines_keep_numbering_and_kickoff_counts_open(self):
        path = self.view.root / 'experiments.md'
        path.write_text(EXPERIMENTS_SKELETON.replace('## Open\n', '## Open\n\n- [running] E3 2026-10-01 q cmd: c where: w\n')
                        + '- [done] E9 2026-10-02 q verdict: v -> docs/v.md\n')
        self.assertEqual(record_launch(self.view.root, 'sbatch a', 'main'), 10)
        record_launch(self.view.root, 'sbatch b', 'main')
        note = experiments_note(self.view.root)
        self.assertEqual(note, f'Open experiments: E3 (running), E10 (unlabeled), E11 (unlabeled) -- see {path}\n')
        self.assertNotIn('sbatch', note)
        self.assertEqual(experiments_note(self.root), '')
        plan = prepare(self.view, LaunchOptions(rollover=None, codegraph=False), 'go', self.run)
        self.assertIn(note, plan.env['TOKEN_KIT_SIMPLE_GUIDANCE'])

    def test_bash_only_pretool_hook_and_short_guidance(self):
        settings = hooks()
        self.assertEqual(settings['PreToolUse'][0]['matcher'], 'Bash')
        self.assertNotIn('matcher', settings['PostToolUse'][0])
        self.assertLess(len((TASK_RULES + CONCURRENCY).split()), 320)
        self.assertIn('experiments.md', TASK_RULES)
        self.assertIn('workers append dated progress lines', TASK_RULES)
        self.assertIn('label any [?] lines', SUCCESSOR)
        self.assertNotIn('[?]', summary_request('80%', 'out.md', 'asks.md'))
        self.assertIsNone(re.search(r'\b(stop|cancel|abort)\b', SUCCESSOR + TASK_RULES, re.I))


if __name__ == '__main__':
    unittest.main()
