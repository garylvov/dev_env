"""Real launcher/adapter/hook integration with disposable local fake clients."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from token_kit.simple_runner import run_session
from token_kit.simple_types import LaunchOptions
from token_kit.task_files import create_task

FAKE = r'''
import json, os, signal, subprocess, sys
from pathlib import Path
if '--help' in sys.argv:
    print('--session-id --settings --append-system-prompt --add-dir')
    raise SystemExit(0)
run = Path(os.environ['TOKEN_KIT_SIMPLE_RUN'])
task = Path(os.environ['TOKEN_KIT_TASK'])
count_path = task / 'fake-launches.json'
rows = json.loads(count_path.read_text()) if count_path.exists() else []
session = sys.argv[sys.argv.index('--session-id')+1] if '--session-id' in sys.argv else None
rows.append({'session': session, 'argv': sys.argv[1:]})
count_path.write_text(json.dumps(rows))
if '--settings' not in sys.argv:
    raise SystemExit(0)
settings = json.loads(sys.argv[sys.argv.index('--settings')+1])
transcript = run / 'transcript.jsonl'
transcript.write_text(json.dumps({'type':'assistant','sessionId':session,'message':{'model':'claude-sonnet-4-6','usage':{'input_tokens':10000}}})+'\n')
def hook(event):
    import shlex
    command = settings['hooks'][event][0]['hooks'][0]['command']
    result = subprocess.run(shlex.split(command), input=json.dumps({'hook_event_name':event,'session_id':session,'transcript_path':str(transcript),'prompt':sys.argv[-1] if event == 'UserPromptSubmit' else None}), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)
hook('SessionStart')
if len(rows) < 3:
    hook('UserPromptSubmit')
    transcript.write_text(json.dumps({'type':'assistant','sessionId':session,'message':{'model':'claude-sonnet-4-6','usage':{'input_tokens':90000}}})+'\n')
    note = hook('PostToolUse')['hookSpecificOutput']['additionalContext']
    assert 'Context is at 90%' in note and 'Finish summarizing everything' in note
    assert task.name + '_in.md' in note and task.name + '_out.md' in note
    with (task / (task.name + '_out.md')).open('a') as notes:
        notes.write('ordinary note from segment '+str(len(rows))+'\n')
    hook('Stop')
    signal.alarm(10)
    signal.pause()
else:
    transcript.write_text(json.dumps({'type':'assistant','sessionId':session,'message':{'model':'claude-sonnet-4-6','usage':{'input_tokens':1}}})+'\n')
    hook('Stop')
'''


class IntegrationTests(unittest.TestCase):
    def test_two_real_handshake_rollovers_with_ordinary_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'workspace'
            work.mkdir()
            view = create_task(root / 'tasks', 'Fake integration', work, 'Do useful work')
            client = root / 'fake-claude'
            client.write_text('#!' + sys.executable + '\n' + FAKE)
            client.chmod(0o755)
            with patch.dict(os.environ, {'HOME': str(root), 'CLAUDE_CONFIG_DIR': str(root / 'config')}):
                code = run_session(view, LaunchOptions(executable=str(client), rollover='60%', context_window=100000), prompt='Do useful work')
            self.assertEqual(code, 0)
            rows = json.loads((view.root / 'fake-launches.json').read_text())
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row['session'] for row in rows}), 3)
            self.assertIn('ordinary note from segment 2', view.state.read_text())
            self.assertEqual(view.state.read_text().count('### rollover '), 2)
            self.assertEqual(view.assignment.read_text().count('1. [ ]'), 1)
            self.assertNotIn('2. [ ]', view.assignment.read_text())
            self.assertFalse((view.root / 'agents/coordinator/checkpoints').exists())
            self.assertFalse((view.root / 'agents/coordinator/lifecycle.json').exists())
            self.assertEqual(len(list((view.root / '.token-kit/runs').glob('*/control.json'))), 3)
            recipe = json.loads((view.root / '.token-kit/slots/coordinator/native-recovery.json').read_text())
            self.assertNotIn('--settings', recipe['argv'])
            self.assertIn('ordinary note from segment 2', recipe['argv'][-1])
            view.state.unlink()
            from token_kit.task_files import recovery_input
            fallback = recovery_input(view)
            self.assertIn('ordinary note from segment 2', fallback.text)
            self.assertIn('Previous saved STATE fallback', fallback.text)
            for row in rows[1:]:
                prompt = row['argv'][row['argv'].index('--')+1]
                self.assertIn('ordinary note from segment', prompt)
                self.assertIn(str(view.assignment), prompt)
                self.assertIn(str(view.output), prompt)


if __name__ == '__main__':
    unittest.main()
