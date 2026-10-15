import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from token_kit import project_install as old
from token_kit import simple_install as new


class SimpleInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()

    def test_every_known_generated_block_upgrades_without_touching_user_text(self):
        path = self.project / 'AGENTS.md'
        for text in (*old._LEGACY_INSTRUCTION_VERSIONS, old.INSTRUCTIONS,
                     new._PREVIOUS_DISPATCH_INSTRUCTIONS):
            with self.subTest(version=len(text)):
                source = ('User prefix\n' + new._block(text) + '\nUser suffix\n').encode()
                path.write_bytes(source)
                plan = new.preview(self.project)
                self.assertEqual(path.read_bytes(), source)
                self.assertEqual(plan.edits[0].after.decode(),
                    'User prefix\n' + new._block(new.INSTRUCTIONS) + '\nUser suffix\n')

    def test_modified_generated_block_preserved(self):
        path = self.project / 'AGENTS.md'
        original = new._block(old.INSTRUCTIONS + '\nMy requirement\n')
        path.write_text(original)
        with self.assertRaises(old.InstallConflict):
            new.preview(self.project)
        self.assertEqual(path.read_text(), original)

    def test_existing_manifest_updated_mcp_untouched_and_rollback_exact(self):
        old.configure(self.project, codegraph=True)
        selected = ['AGENTS.md', old.MANIFEST, 'CLAUDE.md', '.mcp.json', '.codex/config.toml']
        original = {s: (self.project / s).read_bytes() for s in selected}
        plan = new.preview(self.project)
        recipe = new.apply(plan, self.root / 'backup', old_sessions_stopped=True)
        self.assertIn(new.INSTRUCTIONS.strip(), (self.project / 'AGENTS.md').read_text())
        self.assertEqual(new.preview(self.project).edits, ())
        for s in selected[2:]:
            self.assertEqual((self.project / s).read_bytes(), original[s])
        subprocess.run(['sh', str(recipe)], check=True)
        self.assertEqual({s: (self.project / s).read_bytes() for s in selected}, original)

    def test_stale_preview_and_live_sessions_refuse_before_backup(self):
        plan = new.preview(self.project)
        with self.assertRaises(old.InstallConflict):
            new.apply(plan, self.root / 'backup')
        (self.project / 'AGENTS.md').write_text('new user edit')
        with self.assertRaises(old.InstallConflict):
            new.apply(plan, self.root / 'backup', old_sessions_stopped=True)
        self.assertFalse((self.root / 'backup').exists())

    def test_new_file_rollback_and_symlink_refusal(self):
        plan = new.preview(self.project)
        recipe = new.apply(plan, self.root / 'backup', old_sessions_stopped=True)
        subprocess.run(['sh', str(recipe)], check=True)
        self.assertFalse((self.project / 'AGENTS.md').exists())
        target = self.root / 'user.md'
        target.write_text('untouched')
        (self.project / 'AGENTS.md').symlink_to(target)
        with self.assertRaises(old.InstallConflict):
            new.preview(self.project)
        self.assertEqual(target.read_text(), 'untouched')

    def test_exact_hooks_removed_other_hooks_and_custom_fields_preserved(self):
        manifest = self.root / 'manifest.tsv'
        manifest.write_text('hook\tPreToolUse\t*\t/old/token_kit_hook.sh\n'
                            'hook\tStop\t\t/old/stop.sh\n')
        owned = {'type': 'command', 'command': '/old/token_kit_hook.sh'}
        other = {'type': 'command', 'command': '/site/approval.sh'}
        custom = dict(owned, timeout=10)
        settings = {'permissions': {'deny': ['x']}, 'hooks': {
            'PreToolUse': [{'matcher': '*', 'hooks': [owned, other, custom]},
                          {'matcher': 'Read', 'hooks': [owned]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': '/old/stop.sh'}]}]}}
        path = self.root / 'settings.json'
        path.write_text(json.dumps(settings))
        result, removed = new.strip_owned_hooks(settings, manifest)
        self.assertEqual(len(removed), 2)
        self.assertEqual(result['permissions'], settings['permissions'])
        self.assertEqual(result['hooks']['PreToolUse'][0]['hooks'], [other, custom])
        self.assertEqual(result['hooks']['PreToolUse'][1], settings['hooks']['PreToolUse'][1])
        self.assertNotIn('Stop', result['hooks'])
        self.assertEqual(len(settings['hooks']['PreToolUse'][0]['hooks']), 3)
        self.assertTrue(new.inspect_legacy_hooks(path, manifest))
        plan = new.preview(settings=path, manifest=manifest)
        self.assertEqual(len(plan.edits), 1)
        self.assertEqual(json.loads(plan.edits[0].after), result)

    def test_unknown_hooks_reported_never_removed(self):
        path = self.root / 'settings.json'
        original = b'{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"token-kit hook"}]}]}}'
        path.write_bytes(original)
        plan = new.preview(settings=path)
        self.assertEqual(plan.edits, ())
        self.assertTrue(any('unverified' in d for d in plan.diagnostics))
        self.assertEqual(path.read_bytes(), original)

    def test_guidance_is_short_and_free_of_protocol(self):
        self.assertLess(len(new.INSTRUCTIONS.split()), 320)
        for forbidden in ['worker prepare', 'checkpoint', 'closure', 'five sections', '200 words', 'pyramid']:
            self.assertNotIn(forbidden, new.INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
