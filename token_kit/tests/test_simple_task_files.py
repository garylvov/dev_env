import json
from pathlib import Path
import tempfile
import unittest
from token_kit.task_files import create_task, load_task, recovery_input, save_settings, load_settings


class TaskFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()

    def plain(self):
        return create_task(self.root / 'tasks', 'Free form', self.workspace, 'Do useful work')

    def legacy(self):
        root = self.root / 'old'
        agent = root / 'agents/coordinator'
        agent.mkdir(parents=True)
        (root / 'task.json').write_text(json.dumps({'title': 'Old', 'workspace': str(self.workspace)}))
        (agent / 'in.md').write_text('Old assignment')
        (agent / 'STATE.md').write_text('Blocked on obsolete closure')
        return root, agent

    def test_create_minimal_and_read_only_load(self):
        view = self.plain()
        self.assertEqual({p.name for p in view.root.iterdir()}, {'task.md', 'STATE.md', '.token-kit'})
        (view.state).write_text('anything I want')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in view.root.rglob('*') if p.is_file()}
        loaded = load_task(view.root)
        recovery = recovery_input(loaded)
        self.assertIn('anything I want', recovery.text)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in view.root.rglob('*') if p.is_file()})

    def test_divergent_legacy_state_and_later_worker_result(self):
        root, agent = self.legacy()
        (root / 'STATE.md').write_text('Different root notes')
        child = root / 'agents/ordinary-worker'
        child.mkdir()
        (child / 'out.md').write_text('Finished the missing implementation')
        view = load_task(root)
        self.assertEqual(view.state, agent / 'STATE.md')
        recovery = recovery_input(view)
        self.assertIn('Different root notes', recovery.text)
        self.assertIn('Finished the missing implementation', recovery.text)
        self.assertIn('superseded', recovery.text)
        (agent / 'STATE.md').unlink()
        self.assertEqual(load_task(root).state, root / 'STATE.md')

    def test_wrapper_removed_only_from_view(self):
        view = self.plain()
        from token_kit.task_files import _LEGACY_POLICY_BODY
        raw = '<!-- token-kit worker policy v2 -->\n' + _LEGACY_POLICY_BODY + '\n<!-- /token-kit worker policy -->\nToken Kit record: {"task": "/old"}\n\nKeep the actual assignment and checkpoint terminology.'
        view.assignment.write_text(raw)
        recovery = recovery_input(view)
        self.assertNotIn(_LEGACY_POLICY_BODY, recovery.text)
        self.assertIn('Keep the actual assignment and checkpoint terminology.', recovery.text)
        self.assertEqual(raw, view.assignment.read_text())

    def test_steering_across_three_segments_and_legacy_acknowledgment(self):
        root, agent = self.legacy()
        (agent / 'messages').mkdir()
        (agent / 'checkpoints/c1').mkdir(parents=True)
        (agent / 'agent.json').write_text(json.dumps({'latest_checkpoint': 'c1'}))
        (agent / 'checkpoints/c1/manifest.json').write_text(json.dumps({'incorporated_messages': ['old']}))
        for identifier in ('old', 'new'):
            (agent / f'messages/{identifier}.json').write_text(json.dumps({'message_id': identifier, 'text': identifier + ' steering'}))
        view = load_task(root)
        preview = recovery_input(view)
        self.assertEqual(preview.message_ids, ('new',))
        self.assertFalse((root / '.token-kit').exists())
        self.assertEqual(recovery_input(view, mark_presented=True).message_ids, ('new',))
        for _ in range(3):
            recovered = recovery_input(view, mark_presented=True)
            self.assertEqual(recovered.message_ids, ())
            self.assertNotIn('new steering', recovered.text)
            self.assertNotIn('old steering', recovered.text)

    def test_customized_wrappers_and_preferences_are_preserved(self):
        from token_kit.task_files import _LEGACY_POLICY_BODY
        view = self.plain()
        for content in ('User change: never deploy', _LEGACY_POLICY_BODY + '\nUser preference: use chosen model'):
            raw = '<!-- token-kit worker policy v2 -->\n' + content + '\n<!-- /token-kit worker policy -->\nTask assignment'
            view.assignment.write_text(raw)
            self.assertIn(raw, recovery_input(view).text)

    def test_worker_settings_do_not_replace_coordinator_settings(self):
        view = self.plain()
        save_settings(view, {'engine': 'claude', 'model': 'coordinator', 'yolo': False})
        worker = load_task(view.root, agent='editor')
        save_settings(worker, {'engine': 'codex', 'model': 'worker', 'yolo': True})
        self.assertEqual(load_settings(view), {'engine': 'claude', 'model': 'coordinator', 'yolo': False})
        self.assertEqual(load_settings(worker), {'engine': 'codex', 'model': 'worker', 'yolo': True})
        self.assertEqual(load_settings(view, agent='editor'), load_settings(worker))

    def test_legacy_worker_settings_come_from_worker_runs(self):
        root, coordinator = self.legacy()
        for name, engine in (('coordinator', 'claude'), ('editor', 'codex')):
            run = root / 'agents' / name / 'runs/one'
            run.mkdir(parents=True)
            (run / 'run.json').write_text(json.dumps({'engine': engine, 'model': name}))
        self.assertEqual(load_settings(load_task(root))['model'], 'coordinator')
        self.assertEqual(load_settings(load_task(root, agent='editor'))['model'], 'editor')

    def test_root_messages_and_label_title(self):
        view = self.plain()
        (view.root / '.token-kit/labels.json').write_text(json.dumps({'title': 'Renamed', 'status': 'done'}))
        messages = view.root / 'messages'
        messages.mkdir()
        (messages / 'hello.json').write_text(json.dumps({'message_id': 'hello', 'text': 'User steering'}))
        view = load_task(view.root)
        self.assertEqual(view.title, 'Renamed')
        self.assertEqual(recovery_input(view, mark_presented=True).message_ids, ('hello',))
        self.assertEqual(recovery_input(view).message_ids, ())

    def test_concurrent_settings_updates_do_not_lose_unrelated_keys(self):
        from concurrent.futures import ThreadPoolExecutor
        view = self.plain()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n: save_settings(view, {f'key{n}': n}), range(8)))
        self.assertEqual(load_settings(view), {f'key{n}': n for n in range(8)})

    def test_missing_legacy_state_uses_snapshot_without_writing(self):
        root, agent = self.legacy()
        (agent / 'STATE.md').unlink()
        snapshot = agent / 'checkpoints/c1'
        snapshot.mkdir(parents=True)
        (agent / 'agent.json').write_text(json.dumps({'latest_checkpoint': 'c1'}))
        (snapshot / 'manifest.json').write_text(json.dumps({'incorporated_messages': []}))
        (snapshot / 'STATE.md').write_text('Historical progress survives')
        recovery = recovery_input(load_task(root))
        self.assertIn('Historical progress survives', recovery.text)
        self.assertFalse((agent / 'STATE.md').exists())

    def test_settings_preserve_other_metadata(self):
        view = self.plain()
        path = view.root / '.token-kit/session.json'
        data = json.loads(path.read_text())
        data['process'] = {'pid': 123}
        path.write_text(json.dumps(data))
        save_settings(view, {'model': 'exact', 'yolo': False, 'rollover': None})
        save_settings(view, {'effort': 'high'})
        self.assertEqual(json.loads(path.read_text())['process'], {'pid': 123})
        self.assertEqual(load_settings(view), {'model': 'exact', 'yolo': False, 'rollover': None, 'effort': 'high'})
        path.write_text('invalid')
        with self.assertRaises(ValueError):
            save_settings(view, {})
        self.assertEqual(path.read_text(), 'invalid')

    def test_settings_over_256_old_runs_and_skip_malformed(self):
        root, agent = self.legacy()
        for index in range(258):
            run = agent / f'runs/{index:04d}'
            run.mkdir(parents=True)
            (run / 'run.json').write_text(json.dumps({'engine': 'codex', 'model': 'exact', 'created_at': '2026-09-26T00:00:00', 'max_rollovers': 0, 'yolo': True, 'context_window': 10000}))
        (agent / 'runs/0258').mkdir()
        (agent / 'runs/0258/run.json').write_text('bad')
        result = load_settings(load_task(root))
        self.assertEqual(result['max_rollovers'], 0)
        self.assertEqual(result['model'], 'exact')
        self.assertTrue(result['yolo'])

    def test_lanes_and_arbitrary_agents(self):
        root = self.root / 'lanes-task'
        root.mkdir()
        (root / 'STATE.md').write_text(f'# Legacy title\nCwd: {self.workspace}\nUseful work')
        lane = root / 'lanes/editor'
        lane.mkdir(parents=True)
        (lane / 'in.md').write_text('Edit')
        (lane / 'STATE.md').write_text('Working')
        view = load_task(root)
        self.assertEqual(view.workspace, self.workspace)
        self.assertEqual(view.title, 'Legacy title')
        self.assertIn('Edit', recovery_input(view, agent='editor').text)
        with self.assertRaises(ValueError):
            load_task(root, agent='../escape')

    def test_symlink_escape_and_bounded_text(self):
        view = self.plain()
        view.state.unlink()
        view.state.symlink_to(self.workspace / 'outside')
        with self.assertRaises(ValueError):
            load_task(view.root)
        view.state.unlink()
        view.state.write_text('s' * 100000)
        view.assignment.write_text('a' * 100000)
        result = recovery_input(load_task(view.root))
        self.assertLess(len(result.text.encode()), 66000)
        self.assertTrue(any('truncated' in d for d in result.diagnostics))


if __name__ == '__main__':
    unittest.main()
