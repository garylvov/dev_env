"""A release switch preserves pinned sessions and restores exact prior files."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from token_kit import simple_release
from token_kit.simple_release import activation_plan, apply_activation


class ReleaseTests(unittest.TestCase):
    def test_post_publication_failure_restores_current_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'command'
            target.write_text('old')
            plan = [{'path': str(target), 'before': simple_release._capture(target),
                     'after': {'kind':'file','hex':b'new'.hex(),'mode':0o755}}]
            restore = simple_release._restore
            def fail_after_write(path, row):
                restore(path, row)
                if row == plan[0]['after']:
                    raise OSError('Injected after publication')
            with patch.object(simple_release, '_restore', side_effect=fail_after_write):
                with self.assertRaises(OSError):
                    apply_activation(plan, root/'backup')
            self.assertEqual(target.read_text(), 'old')
            subprocess.run([sys.executable,str(root/'backup/rollback.py')],check=True,capture_output=True)

    def test_dispatch_and_exact_rollback(self):
        with tempfile.TemporaryDirectory(prefix='token kit ') as temporary:
            root = Path(temporary)
            for name in ('new', 'old'):
                path = root/name/'token_kit/src/token_kit/bin/token-kit'
                path.parent.mkdir(parents=True)
                path.write_text('#!/bin/sh\nprintf "%s\\n" ' + name + '\n')
                path.chmod(0o755)
            home = root/'home'
            (home/'.local/bin').mkdir(parents=True)
            old = root/'old/token_kit/src/token_kit/bin/token-kit'
            link = home/'.local/bin/token-kit'
            link.symlink_to(old)
            rc = home/'.bashrc'
            rc.write_text('# user contents without final newline')
            prior = rc.read_bytes()
            plan = activation_plan(root/'new', root/'old', home)
            self.assertEqual(rc.read_bytes(), prior)
            rollback = apply_activation(plan, root/'backup')
            clean = {key: value for key, value in os.environ.items() if not key.startswith('TOKEN_KIT_')}
            def launch(extra=None, args=()):
                return subprocess.check_output([str(link), *args], env={**clean, **(extra or {})}, text=True).strip()
            self.assertEqual(launch(), 'new')
            self.assertEqual(launch({'TOKEN_KIT_RUN': 'old-session'}), 'old')
            self.assertEqual(launch({'TOKEN_KIT_RUN': 'new-session', 'TOKEN_KIT_SIMPLE_RUN': '/new'}), 'new')
            self.assertEqual(launch(args=('hook',)), 'old')
            found = subprocess.check_output(['bash','--noprofile','--norc','-c','source "$1"; command -v token-kit','test',str(rc)],
                                            env={**clean,'PATH':'/usr/bin:/bin'},text=True).strip()
            self.assertEqual(found, str(home/'.local/share/token-kit/simple-bin/token-kit'))
            subprocess.run([sys.executable, str(rollback)], check=True, capture_output=True)
            self.assertEqual(rc.read_bytes(), prior)
            self.assertEqual(os.readlink(link), str(old))

    def test_changed_preview_and_changed_rollback_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('new','old'):
                path = root/name/'token_kit/src/token_kit/bin/token-kit'
                path.parent.mkdir(parents=True)
                path.touch()
            home = root/'home'
            home.mkdir()
            rc = home/'.bashrc'
            rc.write_text('original\n')
            plan = activation_plan(root/'new',root/'old',home)
            rc.write_text('changed\n')
            with self.assertRaises(ValueError):
                apply_activation(plan,root/'unused-backup')
            self.assertFalse((root/'unused-backup').exists())
            plan = activation_plan(root/'new',root/'old',home)
            rollback = apply_activation(plan,root/'backup')
            rc.write_text(rc.read_text()+'# subsequent user edit\n')
            result = subprocess.run([sys.executable,str(rollback)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('subsequent user edit',rc.read_text())


if __name__ == '__main__':
    unittest.main()
