import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from token_kit import simple_adapters as adapters, simple_guidance as guidance
from token_kit.simple_types import LaunchOptions, TaskView
from token_kit.task_files import recovery_input


class DispatchGuideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view = TaskView(self.root, self.root, 'task', self.root / 'task_in.md',
                             self.root / 'task_out.md', self.root / 'task_out.md')
        self.view.assignment.write_text('Find a symbol')
        self.view.output.write_text('# Current state\nReady\n# History\n')
        self.guide = self.root / 'dispatch_guide.md'
        self.guide.write_text('First operator dispatch preference\n')
        self.env = patch.dict(os.environ, {'HOME': str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_injected(self, plan, text):
        self.assertIn(text, plan.env['TOKEN_KIT_SIMPLE_GUIDANCE'])
        if plan.engine == 'claude':
            self.assertIn(text, plan.argv[plan.argv.index('--append-system-prompt') + 1])
        else:
            self.assertIn(text, plan.argv[-1])

    def test_both_engines_reload_guide_for_successor(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine), patch.object(guidance, 'DISPATCH_GUIDE', self.guide):
                self.guide.write_text('First operator dispatch preference\n')
                options = LaunchOptions(engine=engine, headless=True, rollover=None)
                initial = adapters.prepare(self.view, options, 'Start work', self.root / 'initial')
                self.assert_injected(initial, 'First operator dispatch preference')
                self.guide.write_text('Updated operator dispatch preference\n')
                successor = adapters.prepare(self.view, options,
                    recovery_input(self.view, paths_only=True).text, self.root / 'successor')
                self.assert_injected(successor, 'Updated operator dispatch preference')
                self.assertNotIn('First operator dispatch preference', successor.env['TOKEN_KIT_SIMPLE_GUIDANCE'])

    def test_missing_guide_falls_back_for_both_engines(self):
        self.guide.unlink()
        with patch.object(guidance, 'DISPATCH_GUIDE', self.guide):
            self.assertEqual(guidance.load_dispatch_guide(), guidance.EFFORT)
            for engine in ('claude', 'codex'):
                plan = adapters.prepare(self.view, LaunchOptions(engine=engine, rollover=None),
                                        'Work', self.root / engine)
                self.assert_injected(plan, guidance.EFFORT)

    def test_real_guide_and_fallback_avoid_workflow_keywords(self):
        self.assertTrue(guidance.DISPATCH_GUIDE.is_file())
        for text in (guidance.load_dispatch_guide(), guidance.EFFORT):
            self.assertIsNone(re.search(r'\b(stop|cancel|abort)\b', text, re.I))
        self.assertLessEqual(len(guidance.load_dispatch_guide().splitlines()), 12)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', guidance.load_dispatch_guide())
