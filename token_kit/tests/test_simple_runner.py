"""Offline process and rollover checks; subprocesses are small Python fixtures."""
import json
import os
from pathlib import Path
import signal
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
        slot.claim('claude')
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
slot.claim('claude')
def crash(**fields):
    os._exit(17)
slot.publish = crash
slot.spawn([sys.executable, '-c', 'from pathlib import Path; Path(' + repr(sys.argv[2]) + ').touch()'], Path(sys.argv[1]), dict(os.environ))
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root), str(marker)], timeout=5)
        self.assertEqual(result.returncode, 17)
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            Slot(self.root, 'coordinator').acquire()

    def test_legacy_durable_claim_and_fence(self):
        (self.root / 'task.json').write_text('{}')
        slot = Slot(self.root, 'coordinator').acquire()
        run = slot.claim('claude')
        import fcntl
        with (self.root / '.continue.lock').open('a') as old:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = slot.spawn([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, dict(os.environ))
        slot.close()
        try:
            bridge = read(self.root / 'agents/coordinator/runs' / run.name / 'run.json')
            self.assertEqual(bridge['status'], 'running')
            self.assertEqual(bridge['child_identity'], identity(child.pid))
        finally:
            child.terminate()
            child.wait()

    def test_old_launcher_refuses_after_supervisor_crash(self):
        # Execute unchanged legacy Store admission with our durable bridge, after
        # removing every live flock. No test invokes a real provider.
        write(self.root / 'task.json', {'schema_version': 1, 'workspace': str(self.root), 'task_id': 'fixture'})
        agent = self.root / 'agents/coordinator'
        agent.mkdir(parents=True)
        write(agent / 'agent.json', {'schema_version': 1, 'agent_id': 'coordinator'})
        slot = Slot(self.root, 'coordinator').acquire()
        slot.claim('claude')
        child = slot.spawn([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, dict(os.environ))
        slot.close()
        script = "from token_kit.core.store import Store; import sys; Store(sys.argv[1]).claim_run('coordinator','claude',False)"
        try:
            result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not reconciled', result.stderr)
        finally:
            child.terminate()
            child.wait()

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
        initialize(run, LaunchOptions(rollover=10), 'parent', self.state)
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 11}}}) + '\n')
        payload = dict(base, hook_event_name='PostToolUse', transcript_path=str(transcript))
        self.assertIn('additionalContext', handle(run, payload, run.name)['hookSpecificOutput'])
        self.assertEqual(handle(run, payload, run.name), {})
        self.assertFalse(handle(run, dict(base, hook_event_name='Stop'), run.name)['continue'])
        self.assertEqual(read(run / 'control.json')['phase'], 'ready')

    def test_summary_message_is_model_context_for_both_engines(self):
        for engine in ('claude', 'codex'):
            for event in ('PostToolUse', 'Stop'):
                with self.subTest(engine=engine, event=event):
                    run = self.root / (engine + event)
                    run.mkdir()
                    assignment = self.root / 'task_in.md'
                    output = self.root / 'task_out.md'
                    initialize(run, LaunchOptions(engine=engine, context_window=100),
                               'parent', self.state, assignment, output)
                    transcript = self.root / 'transcript.jsonl'
                    transcript.write_text(json.dumps({'type': 'assistant', 'message':
                        {'usage': {'input_tokens': 60}}}) + '\n')
                    base = {'session_id': 'parent', 'transcript_path': str(transcript)}
                    with patch('token_kit.simple_runtime.owned_hook_ancestry', return_value=True), \
                         patch('token_kit.simple_adapters.codex_root_session', return_value='parent'), \
                         patch('token_kit.simple_adapters.codex_usage_sample', return_value={
                             'context_tokens': 60, 'context_window': 100}):
                        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
                        response = handle(run, dict(base, hook_event_name=event), run.name)
                        note = response['reason'] if event == 'Stop' else response['hookSpecificOutput']['additionalContext']
                        self.assertIn('Context is at 60%', note)
                        self.assertIn('Finish summarizing everything', note)
                        self.assertIn(str(assignment), note)
                        self.assertIn(str(output), note)
                        self.assertIn('Then stop', note)
                        self.assertEqual(read(run / 'control.json')['phase'], 'requested')
                        stopped = handle(run, dict(base, hook_event_name='Stop'), run.name)
                        self.assertFalse(stopped['continue'])
                        self.assertEqual(read(run / 'control.json')['phase'], 'ready')

    def test_managed_prompts_record_verbatim_and_skip_kickoff_for_both_engines(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                run = self.root / (engine + '-prompts')
                run.mkdir()
                assignment = self.root / (engine + '_in.md')
                assignment.write_text('# Objective\nFix it\n\n# Asks\n')
                initialize(run, LaunchOptions(engine=engine), 'parent', self.state,
                           assignment, self.state, kickoff_prompt='Token Kit successor kickoff')
                base = {'session_id': 'parent'}
                with patch('token_kit.simple_runtime.owned_hook_ancestry', return_value=True), \
                     patch('token_kit.simple_adapters.codex_root_session', return_value='parent'):
                    handle(run, dict(base, hook_event_name='SessionStart'), run.name)
                    handle(run, dict(base, hook_event_name='UserPromptSubmit',
                                     prompt='Token Kit successor kickoff'), run.name)
                    ask = 'Please keep this exact.\n  Including whitespace and `$stuff`.'
                    for _ in range(2):
                        handle(run, dict(base, hook_event_name='UserPromptSubmit', prompt=ask), run.name)
                    text = assignment.read_text()
                    self.assertNotIn('Token Kit successor kickoff', text)
                    self.assertEqual(text.count(ask), 2)
                    self.assertIn('1. [ ]', text)
                    self.assertIn('2. [ ]', text)
                    self.assertRegex(text, r'1\. \[ \] [0-9]{4}-[0-9]{2}-[0-9]{2}')
                    self.assertEqual(read(run / 'control.json')['last_ask'], 2)
                    for _ in range(2):
                        handle(run, dict(base, hook_event_name='UserPromptSubmit',
                                         prompt='One ask with a replayed hook', prompt_id='unique'), run.name)
                    self.assertEqual(assignment.read_text().count('One ask with a replayed hook'), 1)
                    self.assertEqual(read(run / 'control.json')['last_ask'], 3)


    def test_native_workers_defer_compaction_and_off_stays_native(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(), 'parent', self.state)
        base = {'session_id': 'parent'}
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

    def test_old_worker_anywhere_blocks_task_adoption(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            import socket
            write(self.root / 'agents/worker/runs/old/run.json', {
                'status': 'running', 'host': socket.gethostname(),
                'child_pid': child.pid, 'child_identity': identity(child.pid),
                'supervisor_pid': os.getpid(), 'supervisor_identity': identity(os.getpid())})
            with self.assertRaisesRegex(ValueError, 'Legacy client'):
                Slot(self.root, 'coordinator').acquire()
        finally:
            child.terminate()
            child.wait()

    def test_prompt_and_handoff_tools_do_not_count_as_progress(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(rollover=100, context_window=100), 'parent', self.state)
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='UserPromptSubmit'), run.name)
        self.assertEqual(read(run / 'control.json')['activity'], 0)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 81}}}) + '\n')
        handle(run, dict(base, hook_event_name='Stop', transcript_path=str(transcript)), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'requested')  # absolute capped at 80%
        handle(run, dict(base, hook_event_name='PostToolUse'), run.name)
        self.assertEqual(read(run / 'control.json')['activity'], 0)

    def test_codex_child_notices_observed_after_parent_proof(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(engine='codex'), 'parent', self.state)
        current = read(run / 'control.json')
        current['armed'] = True
        write(run / 'control.json', current)
        adapters = types.ModuleType('token_kit.simple_adapters')
        adapters.codex_root_session = lambda payload: None if payload.get('agent_id') else 'parent'
        with patch.dict(sys.modules, {'token_kit.simple_adapters': adapters}), patch('token_kit.simple_runtime.owned_hook_ancestry', return_value=True):
            handle(run, {'session_id': 'parent', 'agent_id': 'worker', 'hook_event_name': 'SubagentStart'}, run.name)
            self.assertEqual(read(run / 'control.json')['children'], ['worker'])
            handle(run, {'session_id': 'parent', 'hook_event_name': 'PreCompact'}, run.name)
            self.assertEqual(read(run / 'control.json')['phase'], 'running')
            handle(run, {'session_id': 'child', 'agent_id': 'worker', 'hook_event_name': 'SubagentStop'}, run.name)
            self.assertEqual(read(run / 'control.json')['children'], ['worker'])

    def _run_fake(self, failures=False, cap=None, resume=False, marked=None, no_progress=False):
        launched = []
        def prepare(view, options, prompt, run, agent='coordinator'):
            launched.append((options, prompt))
            action = len(launched) < 4 and options.rollover is not None
            script = '''
import json, os
from pathlib import Path
p = Path(os.environ['TOKEN_KIT_SIMPLE_RUN']) / 'control.json'
s = json.loads(p.read_text())
s.update(phase='ready', activity=1, valid_usage=True)
p.write_text(json.dumps(s))
'''
            if no_progress:
                script = script.replace('activity=1', 'activity=0')
            if not failures:
                script = script.replace("phase='ready'", "armed=True, phase='ready'")
            if not action:
                script = 'pass'
            if failures:
                script += '\nraise SystemExit(7)'
            return types.SimpleNamespace(argv=(sys.executable, '-c', script), cwd=self.root,
                                         env={'TOKEN_KIT_SIMPLE_SESSION_ID': 'parent'})
        files = types.ModuleType('token_kit.task_files')
        files.recovery_input = lambda *a, **k: RecoveryInput('Latest plain state', message_ids=('message-1',))
        files.save_settings = lambda *a, **k: None
        files.has_work_context = lambda *a, **k: True
        files.mark_messages_presented = lambda *a, **k: marked.append(a[1]) if marked is not None else None
        adapters = types.ModuleType('token_kit.simple_adapters')
        adapters.prepare = prepare
        with patch.dict(sys.modules, {'token_kit.task_files': files, 'token_kit.simple_adapters': adapters}):
            code = run_session(self.view, LaunchOptions(max_rollovers=cap), resume=resume)
        return code, launched

    def test_startup_failure_leaves_steering_pending(self):
        marked = []
        code, launched = self._run_fake(failures=True, resume=True, marked=marked)
        self.assertEqual(code, 7)
        self.assertIn('Latest plain state', launched[0][1])
        self.assertEqual(marked, [])

    def test_repeated_initial_context_rollover_stops_without_progress(self):
        code, launched = self._run_fake(no_progress=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(launched), 2)

    def test_only_injected_message_ids_marked_after_activity(self):
        marked = []
        code, launched = self._run_fake(resume=True, marked=marked)
        self.assertEqual(code, 0)
        self.assertTrue(marked)
        self.assertTrue(all(ids == ('message-1',) for ids in marked))

    def test_prepare_failure_allows_corrected_configuration_retry(self):
        files = types.ModuleType('token_kit.task_files')
        files.recovery_input = lambda *a, **k: RecoveryInput('notes')
        files.has_work_context = lambda *a, **k: True
        files.save_settings = lambda *a, **k: None
        files.mark_messages_presented = lambda *a, **k: None
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
        slot.claim('claude')
        with self.assertRaises(FileNotFoundError):
            slot.spawn([sys.executable, '-c', 'pass'], self.root / 'missing', dict(os.environ))
        slot.close()
        successor = Slot(self.root, 'coordinator').acquire()
        successor.close()

    def test_nonzero_exit_racing_owned_stop_is_not_success(self):
        from token_kit.simple_runner import _stop
        child = Mock(returncode=7)
        child.poll.return_value = 7
        self.assertFalse(_stop(child, {}))
        child.send_signal.assert_not_called()

    def test_child_start_revokes_ready_boundary(self):
        run = self.root / 'run'
        run.mkdir()
        initialize(run, LaunchOptions(), 'parent', self.state)
        base = {'session_id': 'parent'}
        handle(run, dict(base, hook_event_name='SessionStart'), run.name)
        handle(run, dict(base, hook_event_name='PreCompact'), run.name)
        self.assertEqual(read(run / 'control.json')['phase'], 'ready')
        handle(run, dict(base, hook_event_name='SubagentStart', agent_id='worker'), run.name)
        self.assertNotEqual(read(run / 'control.json')['phase'], 'ready')

    def test_continue_timeout_cancels_exact_request(self):
        owner = Slot(self.root, 'coordinator').acquire()
        run = owner.claim('claude')
        initialize(run, LaunchOptions(), 'parent', self.state)
        current = read(run / 'control.json')
        current['armed'] = True
        write(run / 'control.json', current)
        child = owner.spawn([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, dict(os.environ))
        try:
            with self.assertRaisesRegex(ValueError, 'safe stop boundary'):
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
        initialize(run, LaunchOptions(rollover=10), 'parent', self.state)
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 11}}}) + '\n')
        for event in ('SessionStart', 'UserPromptSubmit'):
            handle(run, {'session_id': 'parent', 'hook_event_name': event, 'transcript_path': str(transcript)}, run.name)
            self.assertEqual(read(run / 'control.json')['phase'], 'running')

    def test_three_plain_rollovers_then_normal_exit(self):
        code, launched = self._run_fake()
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 4)
        self.assertEqual(launched[-1][1], 'Latest plain state')

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
