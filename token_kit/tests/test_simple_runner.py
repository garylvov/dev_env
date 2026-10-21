"""Offline process and rollover checks; subprocesses are small Python fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

from token_kit.simple_process import Slot, read, write, identity
from token_kit.simple_runtime import handle, initialize
from token_kit.simple_runner import run_session
from token_kit.simple_types import TaskView, LaunchOptions, RecoveryInput


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'STATE.md'
        self.state.write_text('ordinary notes')
        self.view = TaskView(self.root, self.root, 'Example', self.root / 'task.md',
                             self.state, self.root / 'out.md')

    def tearDown(self):
        self.tmp.cleanup()

    def test_alias_and_surviving_child_refuse_duplicate(self):
        slot = Slot(self.root, 'coordinator').acquire()
        slot.claim()
        child = slot.spawn([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, dict(os.environ))
        slot.close()  # supervisor disappears, child remains alive
        try:
            with self.assertRaisesRegex(ValueError, 'alive|uncertain'):
                Slot(self.root / '.', 'coordinator').acquire()
        finally:
            child.terminate()
            child.wait()

    def test_pipe_handshake_prevents_exec_after_parent_crash(self):
        marker = self.root / 'executed'
        script = '''
import os, sys
from pathlib import Path
from token_kit.simple_process import Slot
slot = Slot(Path(sys.argv[1]), 'coordinator').acquire()
slot.claim()
def crash(**fields):
    os._exit(17)
slot.publish = crash
slot.spawn([sys.executable, '-c', 'from pathlib import Path; Path(' + repr(sys.argv[2]) + ').touch()'], Path(sys.argv[1]), dict(os.environ))
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root), str(marker)], timeout=5)
        self.assertEqual(result.returncode, 17)
        self.assertFalse(marker.exists())
        # The handshake proves no client ran, so a dead supervisor without a published child is retryable.
        Slot(self.root, 'coordinator').acquire().close()

    def test_interrupted_setup_is_retryable(self):
        script = '''
import sys
from pathlib import Path
from token_kit.simple_process import Slot
slot = Slot(Path(sys.argv[1]), 'coordinator').acquire()
slot.claim()
import os; os._exit(3)  # killed during setup, before any spawn (e.g. Ctrl-C in a CodeGraph sync)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], timeout=5,
                                env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)})
        self.assertEqual(result.returncode, 3)
        self.assertEqual(read(self.root / '.token-kit/slots/coordinator/current.json')['status'], 'starting')
        Slot(self.root, 'coordinator').acquire().close()

    def test_new_tasks_keep_locks_inside_records_old_tasks_keep_fence(self):
        slot = Slot(self.root, 'coordinator').acquire()
        slot.claim()
        slot.publish(status='exited', exit_code=0)
        slot.close()
        self.assertEqual({p.name for p in self.root.iterdir()}, {'STATE.md', '.token-kit'})
        (self.root / 'task.json').write_text('{}')
        slot = Slot(self.root, 'coordinator').acquire()
        slot.claim()
        import fcntl
        with (self.root / '.continue.lock').open('a') as old:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        slot.close()
        self.assertFalse((self.root / 'agents').exists())

    def test_unknown_child_and_stale_events_cannot_control(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover=1), 'parent', self.state)
        for payload in ({'session_id': 'child', 'hook_event_name': 'SessionStart'},
                        {'session_id': 'parent', 'agent_id': 'worker', 'hook_event_name': 'SessionStart'},
                        {'session_id': 'parent', 'hook_event_name': 'PreCompact'}):
            self.assertEqual(handle(run, payload, run.name), {})
        self.assertEqual(read(run / 'control.json')['phase'], 'running')
        handle(run, {'session_id': 'parent', 'hook_event_name': 'SessionStart'}, 'old')
        self.assertFalse(read(run / 'control.json')['armed'])

    def test_one_note_then_safe_stop_without_checkpoint(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover=10, context_window=100_000), 'parent', self.state)
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 30_000}}}) + '\n')
        current = read(run / 'control.json')
        current['startup_tokens'] = 10_000
        write(run / 'control.json', current)
        payload = dict(base, hook_event_name='PostToolUse', transcript_path=str(transcript))
        self.assertIn('additionalContext', handle(run, payload, run.name)['hookSpecificOutput'])
        self.assertEqual(handle(run, payload, run.name), {})
        self.assertFalse(handle(run, dict(base, hook_event_name='Stop'), run.name)['continue'])
        self.assertEqual(read(run / 'control.json')['phase'], 'ready')

    def test_summary_message_is_model_context(self):
        for event in ('PostToolUse', 'Stop'):
            with self.subTest(event=event):
                run = self.root / event
                run.mkdir()
                assignment = self.root / 'task_in.md'
                output = self.root / 'task_out.md'
                initialize(run, LaunchOptions(rollover='60%', context_window=100_000), 'parent', self.state, assignment, output)
                transcript = self.root / 'transcript.jsonl'
                transcript.write_text(json.dumps({'type': 'assistant', 'message':
                    {'usage': {'input_tokens': 60_000}}}) + '\n')
                base = {'session_id': 'parent', 'transcript_path': str(transcript)}
                handle(run, dict(base, hook_event_name='SessionStart'), run.name)
                current = read(run / 'control.json')
                current['startup_tokens'] = 10_000
                write(run / 'control.json', current)
                response = handle(run, dict(base, hook_event_name=event), run.name)
                note = response['reason'] if event == 'Stop' else response['hookSpecificOutput']['additionalContext']
                self.assertIn('Context is at 60%', note)
                self.assertIn('Append a History entry', note)
                self.assertIn(str(output), note)
                self.assertIn('End your turn', note)
                self.assertEqual(read(run / 'control.json')['phase'], 'requested')
                stopped = handle(run, dict(base, hook_event_name='Stop'), run.name)
                self.assertFalse(stopped['continue'])
                self.assertEqual(read(run / 'control.json')['phase'], 'ready')

    def test_managed_prompts_record_verbatim_and_skip_kickoff(self):
        run = self.root / 'prompts'
        run.mkdir()
        assignment = self.root / 'claude_in.md'
        assignment.write_text('# Objective\nFix it\n\n# Asks\n')
        initialize(run, LaunchOptions(), 'parent', self.state,
                   assignment, self.state, kickoff_prompt='Token Kit successor kickoff')
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='UserPromptSubmit',
                         prompt='Token Kit successor kickoff'), run.name)
        ask = 'Please keep this exact.\n  Including whitespace and `$stuff`.'
        for _ in range(2):
            handle(run, dict(base, hook_event_name='UserPromptSubmit', prompt=ask), run.name)
        text = assignment.read_text()
        self.assertNotIn('Token Kit successor kickoff', text)
        self.assertEqual(text.count(ask), 2)
        self.assertRegex(text, r'(?m)^1\. [0-9]{4}-[0-9]{2}-[0-9]{2}T')
        self.assertRegex(text, r'(?m)^2\. [0-9]{4}-[0-9]{2}-[0-9]{2}T')
        self.assertNotIn('[ ]', text)
        self.assertEqual(read(run / 'control.json')['last_ask'], 2)
        for _ in range(2):
            handle(run, dict(base, hook_event_name='UserPromptSubmit',
                             prompt='One ask with a replayed hook', prompt_id='unique'), run.name)
        self.assertEqual(assignment.read_text().count('One ask with a replayed hook'), 1)
        self.assertEqual(read(run / 'control.json')['last_ask'], 3)
        # Subagent reports and peer messages arrive as prompts but are not the user's words.
        for machine in ('<task-notification>\n<task-id>a1</task-id>\nPROBE</task-notification>',
                        '<system-reminder>x</system-reminder>\n<teammate-message teammate_id="w">hi</teammate-message>',
                        '<cross-session-message from="s">hi</cross-session-message>'):
            handle(run, dict(base, hook_event_name='UserPromptSubmit', prompt=machine), run.name)
        text = assignment.read_text()
        self.assertNotIn('PROBE', text)
        self.assertNotIn('teammate-message', text)
        self.assertNotIn('cross-session-message', text)
        self.assertEqual(read(run / 'control.json')['last_ask'], 3)

    def test_main_stop_mirrors_session_crons_into_loops_md(self):
        run = self.root / 'loops'
        run.mkdir()
        assignment = self.root / 'claude_in.md'
        assignment.write_text('# Objective\nx\n\n# Asks\n')
        initialize(run, LaunchOptions(), 'parent', self.state, assignment, self.state)
        base = {'session_id': 'parent'}
        loops = self.root / 'loops.md'
        cron = {'id': 'c1', 'schedule': '*/30 * * * *', 'recurring': True, 'prompt': 'check the build'}
        shell = {'id': 't1', 'type': 'shell', 'status': 'running', 'command': 'tail -f log'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='Stop', session_crons=[], background_tasks=[]), run.name)
        self.assertFalse(loops.exists())  # nothing scheduled, nothing written
        # Child stops carry the parent's arrays too; only the main session writes.
        handle(run, dict(base, hook_event_name='SubagentStop', agent_id='w', agent_type='x',
                         session_crons=[cron], background_tasks=[]), run.name)
        self.assertFalse(loops.exists())
        handle(run, dict(base, hook_event_name='Stop', session_crons=[cron],
                         background_tasks=[shell, dict(shell, id='t2', status='completed', command='old')]), run.name)
        text = loops.read_text()
        self.assertIn('`*/30 * * * *` recurring: check the build (since ', text)
        self.assertIn('- shell: tail -f log', text)
        self.assertNotIn('old', text)
        # A firing loop is not a user message.
        handle(run, dict(base, hook_event_name='UserPromptSubmit', prompt='check the build'), run.name)
        self.assertNotIn('check the build', assignment.read_text())
        handle(run, dict(base, hook_event_name='Stop', session_crons=[], background_tasks=[]), run.name)
        self.assertFalse(loops.exists())  # a deleted loop is not resurrected

    def test_native_workers_defer_compaction_and_off_stays_native(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover='60%', context_window=200_000), 'parent', self.state)
        transcript = self.root / 'usage.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 150_000}}}) + '\n')
        current = read(run / 'control.json')
        current['startup_tokens'] = 10_000
        write(run / 'control.json', current)
        base = {'session_id': 'parent', 'transcript_path': str(transcript)}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='SubagentStart', agent_id='worker'), run.name)
        self.assertEqual(handle(run, dict(base, hook_event_name='PreCompact'), run.name), {})
        self.assertEqual(read(run / 'control.json')['phase'], 'running')
        handle(run, dict(base, hook_event_name='SubagentStop', agent_id='worker'), run.name)
        handle(run, dict(base, hook_event_name='PreCompact'), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'ready')
        initialize(run, LaunchOptions(rollover=None), 'parent', self.state)
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='PreCompact'), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'running')

    def test_symlink_metadata_refused(self):
        other = self.root / 'elsewhere'
        other.mkdir()
        (self.root / '.token-kit').symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            Slot(self.root, 'coordinator')

    def test_prompt_and_handoff_tools_do_not_count_as_progress(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover=100_000, context_window=100_000), 'parent', self.state)
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='UserPromptSubmit'), run.name)
        self.assertEqual(read(run / 'control.json')['activity'], 0)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 81_000}}}) + '\n')
        current = read(run / 'control.json')
        current['startup_tokens'] = 10_000
        write(run / 'control.json', current)
        handle(run, dict(base, hook_event_name='Stop', transcript_path=str(transcript)), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'requested')  # absolute capped at 80%
        handle(run, dict(base, hook_event_name='PostToolUse'), run.name)
        self.assertEqual(read(run / 'control.json')['activity'], 0)

    def _run_fake(self, failures=False, cap=None, resume=False, no_progress=False, headless=False, substantive=False):
        launched = []
        self.greets = []
        def prepare(view, options, prompt, run, agent='coordinator', greet=False):
            launched.append((options, prompt))
            self.greets.append(greet)
            action = len(launched) < 4 and options.rollover is not None
            script = '''
import json, os
from pathlib import Path
p = Path(os.environ['TOKEN_KIT_SIMPLE_RUN']) / 'control.json'
s = json.loads(p.read_text())
s.update(phase='ready', activity=1, valid_usage=True, rollover_entry='\\n### rollover FIXTURE\\n')
p.write_text(json.dumps(s))
'''
            if substantive:
                script += '\n(Path(os.environ["TOKEN_KIT_TASK"]) / "out.md").open("a").write("completed file ' + str(len(launched)) + '\\n")'
            if no_progress:
                script = script.replace('activity=1', 'activity=0')
            if not failures:
                script = script.replace("phase='ready'", "armed=True, phase='ready'")
            if not action:
                script = 'pass'
            if headless:
                script += '\nfrom pathlib import Path\nimport os\n(Path(os.environ["TOKEN_KIT_TASK"]) / "out.md").write_text("Summary replaced history")'
            script += '\nprint("HEADLESS_LOG_MARKER", flush=True)'
            if failures:
                script += '\nraise SystemExit(7)'
            return types.SimpleNamespace(argv=(sys.executable, '-c', script), cwd=self.root,
                                         env={'TOKEN_KIT_SIMPLE_SESSION_ID': 'parent'})
        from token_kit.task_files import safe_task_path, archive_history
        files = types.ModuleType('token_kit.task_files')
        files.safe_task_path = safe_task_path
        files.archive_history = archive_history
        files.recovery_input = lambda *a, **k: RecoveryInput('Latest plain state')
        files.save_settings = lambda *a, **k: None
        files.has_work_context = lambda *a, **k: True
        adapters = types.ModuleType('token_kit.simple_adapters')
        adapters.prepare = prepare
        with patch.dict(sys.modules, {'token_kit.task_files': files, 'token_kit.simple_adapters': adapters}):
            code = run_session(self.view, LaunchOptions(max_rollovers=cap, headless=headless), resume=resume)
        return code, launched

    def test_headless_successors_log_and_final_exit(self):
        code, launched = self._run_fake(headless=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 4)
        self.assertTrue(all(options.headless for options, prompt in launched))
        logs = list(self.root.glob('.token-kit/runs/*/stdout.log'))
        self.assertEqual(len(logs), 4)
        self.assertTrue(all('HEADLESS_LOG_MARKER' in log.read_text() for log in logs))
        self.assertIn('### rollover FIXTURE', self.view.output.read_text())
        code, launched = self._run_fake(headless=True, failures=True)
        self.assertEqual(code, 7)
        self.assertEqual(len(launched), 1)

    def test_startup_failure_returns_client_exit(self):
        code, launched = self._run_fake(failures=True, resume=True)
        self.assertEqual(code, 7)
        self.assertIn('Latest plain state', launched[0][1])

    def test_repeated_initial_context_rollover_keeps_successor_running(self):
        code, launched = self._run_fake(no_progress=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 3)
        self.assertIsNone(launched[-1][0].rollover)

    def test_only_a_new_tasks_first_launch_greets(self):
        self._run_fake()
        self.assertEqual(self.greets, [True] + [False] * (len(self.greets) - 1))
        self.assertGreater(len(self.greets), 1)
        self._run_fake(resume=True)
        self.assertFalse(any(self.greets))

    def test_recorded_loops_reach_continue_and_successors(self):
        from token_kit.simple_records import record_loops
        record_loops(self.root, [{'id': 'c1', 'schedule': '*/30 * * * *', 'recurring': True,
                                  'prompt': 'check the build'}], [], {})
        line = 'Recreate these loops from the previous session now'
        code, launched = self._run_fake(resume=True)
        self.assertIn(line, launched[0][1])
        self.assertIn('*/30 * * * * check the build', launched[0][1])
        self.assertTrue(all(line in prompt for _, prompt in launched[1:]))
        (self.root / 'loops.md').unlink()
        code, launched = self._run_fake(resume=True)
        self.assertFalse(any(line in prompt for _, prompt in launched))

    def test_prepare_failure_allows_corrected_configuration_retry(self):
        from token_kit.task_files import safe_task_path, archive_history
        files = types.ModuleType('token_kit.task_files')
        files.safe_task_path = safe_task_path
        files.archive_history = archive_history
        files.recovery_input = lambda *a, **k: RecoveryInput('notes')
        files.has_work_context = lambda *a, **k: True
        files.save_settings = lambda *a, **k: None
        adapters = types.ModuleType('token_kit.simple_adapters')
        def broken(*args, **kwargs):
            raise ValueError('Unsupported launch flag')
        adapters.prepare = broken
        with patch.dict(sys.modules, {'token_kit.task_files': files, 'token_kit.simple_adapters': adapters}):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                run_session(self.view, LaunchOptions())
        code, launched = self._run_fake(cap=0)
        self.assertEqual(code, 0)

    def test_definite_spawn_failure_allows_retry(self):
        slot = Slot(self.root, 'coordinator').acquire()
        slot.claim()
        with self.assertRaises(FileNotFoundError):
            slot.spawn([sys.executable, '-c', 'pass'], self.root / 'missing', dict(os.environ))
        slot.close()
        successor = Slot(self.root, 'coordinator').acquire()
        successor.close()

    def test_owned_stop_allows_provider_hooks_to_finish_naturally(self):
        from token_kit.simple_runner import _stop
        child = Mock(pid=123, returncode=0)
        child.poll.return_value = None
        with patch('token_kit.simple_runner.identity', return_value='owned'):
            self.assertTrue(_stop(child, {'child_identity': 'owned'}))
        child.wait.assert_called_once_with(timeout=1)
        child.send_signal.assert_not_called()

    def test_nonzero_exit_racing_owned_stop_is_not_success(self):
        from token_kit.simple_runner import _stop
        child = Mock(returncode=7)
        child.poll.return_value = 7
        self.assertFalse(_stop(child, {}))
        child.send_signal.assert_not_called()

    def test_child_start_revokes_ready_boundary(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover='60%', context_window=200_000), 'parent', self.state)
        transcript = self.root / 'usage.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 150_000}}}) + '\n')
        current = read(run / 'control.json')
        current['startup_tokens'] = 10_000
        write(run / 'control.json', current)
        base = {'session_id': 'parent', 'transcript_path': str(transcript)}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='PreCompact'), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'ready')
        handle(run, dict(base, hook_event_name='SubagentStart', agent_id='worker'), run.name)
        self.assertNotEqual(read(run / 'control.json')['phase'], 'ready')

    def test_continue_timeout_cancels_exact_request(self):
        owner = Slot(self.root, 'coordinator').acquire()
        run = owner.claim()
        initialize(run, LaunchOptions(), 'parent', self.state)
        current = read(run / 'control.json')
        current['armed'] = True
        write(run / 'control.json', current)
        child = owner.spawn([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, dict(os.environ))
        try:
            with self.assertRaisesRegex(ValueError, r'safe stop boundary.*exit the old session first \(/exit\)'):
                Slot(self.root, 'coordinator').acquire(resume=True, timeout=.01)
            self.assertFalse((owner.directory / 'continue.json').exists())
            current['threshold'] = None
            write(run / 'control.json', current)
            with self.assertRaisesRegex(ValueError, 'original terminal'):
                Slot(self.root, 'coordinator').acquire(resume=True)
        finally:
            child.terminate()
            child.wait()
            owner.close()

    def test_corrupt_optional_observation_degrades_once(self):
        from token_kit.simple_runner import _observe
        target = self.root / 'control.json'
        target.write_text('invalid JSON')
        notices = set()
        self.assertEqual(_observe(target, notices), {})
        self.assertEqual(_observe(target, notices), {})
        self.assertEqual(len(notices), 1)

    def test_stale_startup_usage_does_not_request_handoff(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover=10, context_window=100_000), 'parent', self.state)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 30_000}}}) + '\n')
        for event in ('SessionStart', 'UserPromptSubmit'):
            handle(run, {'session_id': 'parent', 'hook_event_name': event, 'transcript_path': str(transcript)}, run.name)
            self.assertEqual(read(run / 'control.json')['phase'], 'running')

    def test_two_mechanical_only_rollovers_disable_further_rollovers(self):
        code, launched = self._run_fake()
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 3)
        self.assertIsNone(launched[-1][0].rollover)
        from token_kit.simple_guidance import SUCCESSOR
        self.assertEqual(launched[-1][1], 'Latest plain state\n\n' + SUCCESSOR)

    def test_substantive_output_resets_stagnation(self):
        code, launched = self._run_fake(substantive=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 4)
        self.assertIsNotNone(launched[-1][0].rollover)

    def test_startup_growth_guard(self):
        run = self.root / 'growth'
        run.mkdir()
        initialize(run, LaunchOptions(rollover='10%', context_window=200_000), 'parent', self.state)
        base = {'session_id': 'parent', 'transcript_path': str(self.root / 'telemetry')}
        def sample(tokens):
            (self.root / 'telemetry').write_text(json.dumps({'type': 'assistant',
                'sessionId': 'parent', 'message': {'usage': {'input_tokens': tokens}}}) + '\n')
        sample(43_000)
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        self.assertEqual(handle(run, dict(base, hook_event_name='PostToolUse'), run.name), {})
        warning = read(run / 'control.json')['loop_warning']
        self.assertEqual(handle(run, dict(base, hook_event_name='PreCompact'), run.name), {})
        sample(62_999)
        self.assertEqual(handle(run, dict(base, hook_event_name='PostToolUse'), run.name), {})
        self.assertEqual(read(run / 'control.json')['loop_warning'], warning)
        sample(63_000)
        result = handle(run, dict(base, hook_event_name='PostToolUse'), run.name)
        self.assertIn('Append a History entry', result['hookSpecificOutput']['additionalContext'])
        control = read(run / 'control.json')
        self.assertEqual(control['startup_tokens'], 43_000)
        self.assertEqual(control['token_growth'], 20_000)
        self.assertEqual(control['phase'], 'requested')

    def test_claude_baseline_is_retained_while_window_is_unknown(self):
        run = self.root / 'unknown-window'
        run.mkdir()
        initialize(run, LaunchOptions(rollover='20%'), 'parent', self.state)
        transcript = self.root / 'usage.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'sessionId': 'parent',
            'message': {'model': 'custom', 'usage': {'input_tokens': 43_000}}}) + '\n')
        base = {'session_id': 'parent', 'transcript_path': str(transcript)}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        self.assertEqual(read(run / 'control.json')['startup_tokens'], 43_000)
        transcript.write_text(json.dumps({'type': 'assistant', 'sessionId': 'parent',
            'message': {'model': 'claude-sonnet-5-5', 'usage': {'input_tokens': 200_000}}}) + '\n')
        with patch.dict(os.environ, {}, clear=True):
            result = handle(run, dict(base, hook_event_name='PostToolUse'), run.name)
        self.assertIn('Append a History entry', result['hookSpecificOutput']['additionalContext'])
        control = read(run / 'control.json')
        self.assertEqual(control['startup_tokens'], 43_000)
        self.assertEqual(control['telemetry_window'], 1_000_000)

    def test_growth_minimum_scales_with_context_and_requires_threshold(self):
        from token_kit.simple_runtime import rollover_growth
        control = {}
        self.assertFalse(rollover_growth(control, 40_000, 1_000_000, 200_000))
        self.assertEqual(control['minimum_growth'], 100_000)
        self.assertFalse(rollover_growth(control, 160_000, 1_000_000, 200_000))
        self.assertTrue(rollover_growth(control, 200_000, 1_000_000, 200_000))
        self.assertFalse(rollover_growth(control, 20_000, 1_000_000, 200_000))
        self.assertEqual(control['startup_tokens'], 40_000)

    def test_error_not_replayed_and_zero_cap_respected(self):
        code, launched = self._run_fake(failures=True)
        self.assertEqual(code, 7)
        self.assertEqual(len(launched), 1)
        code, launched = self._run_fake(cap=0)
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 1)
        self.assertIsNone(launched[0][0].rollover)


if __name__ == '__main__':
    unittest.main()
