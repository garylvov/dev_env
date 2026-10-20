"""The conversation (_in.md) and the upsampled ask list (asks.md) stay separate."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from token_kit import simple_guidance as guidance, simple_release
from token_kit.simple_adapters import folder_guidance
from token_kit.task_files import ASKS_SKELETON, append_ask, create_task, has_work_context, load_task, recovery_input

INSTALL = Path(__file__).resolve().parents[1] / 'install.sh'


class AsksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_new_task_has_asks_skeleton_that_successors_find(self):
        view = create_task(self.root / 'tasks', 'Asks', self.root, 'First ask')
        asks = view.root / 'asks.md'
        self.assertEqual(asks.read_text(), ASKS_SKELETON)
        recovered = recovery_input(view, paths_only=True)
        self.assertIn(str(asks), recovered.text)
        self.assertIn(str(asks), folder_guidance(view))
        worker = view.root / 'agents/editor_in.md'
        worker.parent.mkdir()
        worker.write_text('Worker task')
        self.assertNotIn(str(asks), recovery_input(load_task(view.root, agent='editor'), agent='editor', paths_only=True).text)

    def test_skeleton_alone_is_not_work(self):
        view = create_task(self.root / 'tasks', 'Idle', self.root)
        self.assertFalse(has_work_context(view))
        (view.root / 'asks.md').write_text(ASKS_SKELETON + '- [ ] Ship the installer (from #1)\n')
        self.assertTrue(has_work_context(view))

    def test_many_asks_append_only_and_status_marks_keep_numbering(self):
        view = create_task(self.root / 'tasks', 'Long', self.root, 'Ask 1')
        for n in range(2, 201):
            self.assertEqual(append_ask(view.root, view.assignment, f'Ask {n}'), n)
        before = view.assignment.read_bytes()
        # Agents mark progress in place, including the newest ask.
        view.assignment.write_text(before.decode().replace('\n200. [ ]', '\n200. [~]').replace('\n7. [ ]', '\n7. [x]'))
        marked = view.assignment.read_bytes()
        self.assertEqual(append_ask(view.root, view.assignment, 'Ask 201'), 201)
        self.assertTrue(view.assignment.read_bytes().startswith(marked))

    def test_guidance_names_both_records_briefly(self):
        rules = guidance.TASK_RULES
        self.assertIn('Never rewrite it', rules)
        self.assertIn('asks.md', rules)
        self.assertIn('(from #3, #7)', rules)
        self.assertIn('never delete', rules)
        self.assertIn('create it if missing', rules)
        # Scope creep: asides count, open asks stay on top, conflicts name a winner, finish lists open asks.
        self.assertIn('including asides buried in longer messages', rules)
        self.assertIn('open and in-progress asks at the top', rules)
        self.assertIn('say which wins', rules)
        self.assertIn('Re-read it before declaring work finished and before rollover', rules)
        self.assertIn('final reply lists every still-open ask', rules)
        self.assertLessEqual(len(rules.splitlines()), 11)
        self.assertLess(ASKS_SKELETON.index('## Open'), ASKS_SKELETON.index('## Done'))
        note = guidance.summary_request('80%', 'out.md', 'in.md', None, '/task/asks.md')
        self.assertIn('Re-read /task/asks.md; make sure every request from every message has a line, open ones first.', note)
        self.assertNotIn('asks.md', guidance.summary_request('80%', 'out.md', 'in.md'))

    def test_guide_routes_gpt_workers_through_shunt_when_active(self):
        for text in (guidance.load_dispatch_guide(), guidance.EFFORT):
            self.assertIn('systemctl --user is-active shunt', text)
            self.assertIn('~/.claude/agents/', text)
            self.assertIn('<task>-<model>-<effort>', text)
            self.assertIn('Use codex exec when Shunt is inactive', text)
            self.assertIn('Codex main sessions keep claude -p', text)
            self.assertIn('route (Shunt subagent or codex exec)', text)


class FreshInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='token kit fresh ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root / 'release'
        self.launcher = self.release / 'token_kit/src/token_kit/bin/token-kit'
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text('#!/bin/sh\necho new "$@"\n')
        self.launcher.chmod(0o755)
        self.home = self.root / 'home'
        self.home.mkdir()

    def test_fresh_install_without_legacy_root_is_idempotent(self):
        plan = simple_release.fresh_install_plan(self.release, self.home)
        link = self.home / '.local/bin/token-kit'
        self.assertEqual({row['path'] for row in plan},
                         {str(self.home / '.local/share/token-kit/simple-bin/token-kit'), str(link), str(self.home / '.bashrc')})
        rollback = simple_release.apply_activation(plan, self.root / 'backup')
        self.assertEqual(subprocess.check_output([str(link), 'list'], text=True).strip(), 'new list')
        self.assertEqual(simple_release.fresh_install_plan(self.release, self.home), [])
        subprocess.run(['python3', str(rollback)], check=True, capture_output=True)
        self.assertFalse(link.exists() or link.is_symlink())

    def test_fresh_plan_keeps_an_existing_legacy_dispatcher(self):
        old = self.root / 'old/token_kit/src/token_kit/bin/token-kit'
        old.parent.mkdir(parents=True)
        old.write_bytes(self.launcher.read_bytes())
        old.chmod(0o755)
        simple_release.apply_activation(simple_release.launcher_install_plan(self.release, self.root / 'old', self.home),
                                        self.root / 'backup')
        self.assertEqual(simple_release.fresh_install_plan(self.release, self.home), [])
        with self.assertRaises(ValueError):
            simple_release.fresh_install_plan(self.root / 'old', self.home)

    def test_real_installer_dry_run_and_install_in_temporary_home(self):
        home = self.root / 'installer home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith('TOKEN_KIT_')}
        dry = subprocess.run(['bash', str(INSTALL), '--home', str(home), '--dry-run'], env=env,
                             capture_output=True, text=True, check=True)
        self.assertIn(str(home / '.local/bin/token-kit'), dry.stdout)
        self.assertEqual(list(home.iterdir()), [])
        subprocess.run(['bash', str(INSTALL), '--home', str(home)], env=env, capture_output=True, text=True, check=True)
        self.assertTrue((home / '.local/bin/token-kit').is_symlink())
        again = subprocess.run(['bash', str(INSTALL), '--home', str(home)], env=env, capture_output=True, text=True, check=True)
        self.assertIn('already installed', again.stdout)


if __name__ == '__main__':
    unittest.main()
