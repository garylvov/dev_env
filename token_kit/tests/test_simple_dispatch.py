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
        self.assertIn(text, plan.argv[plan.argv.index('--append-system-prompt') + 1])

    def test_guide_reloaded_for_successor(self):
        with patch.object(guidance, 'DISPATCH_GUIDE', self.guide):
            options = LaunchOptions(headless=True, rollover=None)
            initial = adapters.prepare(self.view, options, 'Start work', self.root / 'initial')
            self.assert_injected(initial, 'First operator dispatch preference')
            self.guide.write_text('Updated operator dispatch preference\n')
            successor = adapters.prepare(self.view, options,
                recovery_input(self.view, paths_only=True).text, self.root / 'successor')
            self.assert_injected(successor, 'Updated operator dispatch preference')
            self.assertNotIn('First operator dispatch preference', successor.env['TOKEN_KIT_SIMPLE_GUIDANCE'])

    def test_missing_guide_falls_back(self):
        self.guide.unlink()
        with patch.object(guidance, 'DISPATCH_GUIDE', self.guide):
            self.assertEqual(guidance.load_dispatch_guide(), guidance.EFFORT)
            plan = adapters.prepare(self.view, LaunchOptions(rollover=None), 'Work', self.root / 'run')
            self.assert_injected(plan, guidance.EFFORT)

    def test_real_guide_matches_fallback_and_stays_short(self):
        self.assertEqual(guidance.DISPATCH_GUIDE.read_bytes(), guidance.EFFORT.encode())
        self.assertIsNone(re.search(r'\b(stop|cancel|abort)\b', guidance.EFFORT, re.I))
        self.assertLessEqual(len(guidance.EFFORT.splitlines()), 20)
        self.assertNotIn('codex', guidance.EFFORT.lower())

    def test_guide_contract(self):
        text = guidance.EFFORT
        # Versions are resolved from sources, never hard-coded.
        self.assertIsNone(re.search(r"gpt-\d|claude-(?:opus|sonnet|haiku|fable)-|\b(?:opus|sonnet|haiku|fable)\s+\d", text, re.I))
        for phrase in (
                'The main session is always Claude',
                'Shunt-backed Claude Code subagents', 'systemctl --user is-active shunt',
                'otherwise every role uses Claude models',
                'builds <task>/models.md only if it does not exist', 'Rebuild or edit only when the user asks',
                'Existing matrices require no discovery or probes',
                'https://platform.claude.com/docs/en/models/overview',
                'https://platform.claude.com/docs/en/about-claude/pricing',
                'pinned in each Shunt agent file', 'record Shunt status with date',
                'If no fetch tool is available, say so in models.md',
                '`cost rank` (1 = cheapest)', '`intelligence rank` (1 = most capable)',
                'never infer intelligence from price', 'RELATIVE proxy for usage-limit burn',
                'ORDERED options spanning both families', 'cost 3, intel 2',
                'typically Claude Opus by operator preference, labeled as an override',
                'timeout 30s claude -p --model <model> --effort low "reply OK"',
                'PASS = exact OK reply', 'UNAVAILABLE = explicit error', 'INCONCLUSIVE = timeout without OK',
                'never choose an older model over a newer one',
                "use the next ordered option for that run", "task's _out.md History",
                'do not edit models.md or automatically re-probe',
                'OTHER model family', 'Claude-authored work is checked by a Shunt GPT agent',
                'GPT-authored work by Claude', 'verifier intelligence rank <= author intelligence rank',
                'verify with a Claude model at least as capable as the author',
                '--model <model> --effort <effort>'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_model_decisions_injected_for_starts_and_successors(self):
        options = LaunchOptions(headless=True, rollover=None)
        for prompt in ('Start work', recovery_input(self.view, paths_only=True).text):
            with self.subTest(prompt=prompt):
                plan = adapters.prepare(self.view, options, prompt, self.root / 'run')
                self.assert_injected(plan, str(self.root / 'models.md'))
                self.assert_injected(plan, 'main agent builds only if missing')
                self.assert_injected(plan, 'rebuild or edit only when the user asks')
                self.assertNotIn('--model', plan.argv)
                self.assertFalse((self.root / 'models.md').exists())

    def test_existing_matrix_untouched_by_initial_and_successor_launches(self):
        matrix = self.root / 'models.md'
        matrix.write_bytes(b'Fixed operator matrix\n')
        os.utime(matrix, (1, 1))
        before = (matrix.read_bytes(), matrix.stat().st_mtime_ns)
        for prompt in ('Start work', recovery_input(self.view, paths_only=True).text):
            adapters.prepare(self.view, LaunchOptions(headless=True, rollover=None), prompt, self.root / 'run')
            self.assertEqual((matrix.read_bytes(), matrix.stat().st_mtime_ns), before)

    def test_launcher_only_passes_explicit_top_level_model(self):
        plan = adapters.prepare(self.view, LaunchOptions(headless=True, rollover=None, model='operator-choice'),
                                'Work', self.root / 'run')
        self.assertEqual(plan.argv[plan.argv.index('--model') + 1], 'operator-choice')
