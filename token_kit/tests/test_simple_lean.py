"""Rollover guidance stays bounded while the original evidence survives."""
from pathlib import Path
import re
import tempfile
import unittest
from token_kit.task_files import create_task, recovery_input, archive_history, HISTORY_LINK
from token_kit.simple_guidance import summary_request, TASK_RULES, CONCURRENCY, EFFORT
from token_kit.simple_adapters import folder_guidance


class LeanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view = create_task(self.root / 'tasks', 'Lean', self.root, 'Assignment sentinel ' * 2000)

    def test_injected_recovery_has_paths_without_file_contents(self):
        self.view.output.write_text('# Current state\nOutput sentinel\n# History\nHistory sentinel')
        recovered = recovery_input(self.view, paths_only=True)
        self.assertIn(str(self.view.assignment), recovered.text)
        self.assertIn(str(self.view.output), recovered.text)
        self.assertNotIn('Assignment sentinel', recovered.text)
        self.assertNotIn('Output sentinel', recovered.text)
        self.assertNotIn('History sentinel', recovered.text)
        self.assertLess(len(recovered.text), 2000)

    def test_injected_guidance_avoids_workflow_keywords(self):
        for text in (TASK_RULES, CONCURRENCY, EFFORT, folder_guidance(self.view),
                     recovery_input(self.view, paths_only=True).text,
                     summary_request('20%', self.view.output, self.view.assignment),
                     summary_request('20%', self.view.output, self.view.assignment, self.root / 'STATE.md')):
            self.assertIsNone(re.search(r'\b(stop|cancel|abort)\b', text, re.I), text)
        note = summary_request('20%', self.view.output, self.view.assignment)
        self.assertIn('every ask', note)
        self.assertIn('Do not re-read files you already wrote', note)

    def test_archive_preserves_all_bytes_in_order_across_repeated_moves(self):
        prefix = b'# Current state\r\nSparse\r\n# History\r\n'
        entries = [f'\r\n### #{i} date\r\n'.encode() + ('\u03b1 finding ' * 500).encode() + b'\r\n' for i in range(5)]
        suffix = b'# Other section\r\nKeep me\r\n'
        original = b''.join(entries)
        self.view.output.write_bytes(prefix + original + suffix)
        self.assertGreater(archive_history(self.view), 0)
        archive = self.view.root / 'docs/history.md'
        def preserved():
            current = self.view.output.read_bytes()
            self.assertTrue(current.startswith(prefix + HISTORY_LINK))
            self.assertTrue(current.endswith(suffix))
            recent = current[len(prefix + HISTORY_LINK):-len(suffix)]
            self.assertLessEqual(len(recent.decode()), 8000)
            return archive.read_bytes() + recent
        self.assertEqual(preserved(), original)
        before = (self.view.output.read_bytes(), archive.read_bytes())
        self.assertEqual(archive_history(self.view), 0)
        self.assertEqual(before, (self.view.output.read_bytes(), archive.read_bytes()))
        extra = b'\n### #5 date\n' + b'new finding ' * 500 + b'\n'
        self.view.output.write_bytes(self.view.output.read_bytes().replace(suffix, extra + suffix))
        self.assertGreater(archive_history(self.view), 0)
        self.assertEqual(preserved(), original + extra)

    def test_small_or_unstructured_history_is_untouched(self):
        for raw in (b'# Current state\n# History\n### one\nshort\n', b'# History\n' + b'x' * 9000):
            self.view.output.write_bytes(raw)
            self.assertEqual(archive_history(self.view), 0)
            self.assertEqual(self.view.output.read_bytes(), raw)

    def test_nested_output_links_to_task_root_archive(self):
        from dataclasses import replace
        output = self.view.root / 'agents/editor_out.md'
        output.parent.mkdir()
        original = b'# History\n### old\n' + b'x' * 9000 + b'\n### recent\nnew\n'
        output.write_bytes(original)
        nested = replace(self.view, output=output, state=output)
        self.assertGreater(archive_history(nested), 0)
        self.assertIn(b'[docs/history.md](../docs/history.md)', output.read_bytes())
        self.assertEqual(archive_history(nested), 0)

    def test_archive_rejects_escaping_symlink_without_changes(self):
        original = b'# History\n### old\n' + b'x' * 9000 + b'\n### recent\nnew\n'
        self.view.output.write_bytes(original)
        target = self.root / 'outside.md'
        target.write_bytes(b'outside')
        (self.view.root / 'docs/history.md').symlink_to(target)
        with self.assertRaises(ValueError):
            archive_history(self.view)
        self.assertEqual(self.view.output.read_bytes(), original)
        self.assertEqual(target.read_bytes(), b'outside')
