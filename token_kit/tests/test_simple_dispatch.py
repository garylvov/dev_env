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
        self.assertLessEqual(len(guidance.load_dispatch_guide().splitlines()), 25)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', guidance.load_dispatch_guide())

    def test_guide_and_fallback_have_no_concrete_model_names(self):
        for text in (guidance.load_dispatch_guide(), guidance.EFFORT):
            with self.subTest(text=text):
                self.assertIsNone(re.search(r"gpt-|\b(?:opus|sonnet|haiku|fable|sol|luna|terra|astra)\b", text, re.I))
                self.assertIn('model_reasoning_effort=<effort>', text)
                self.assertIn('--model <model> --effort <effort>', text)
                self.assertIn('codex debug models', text)
                self.assertIn('timeout 30s', text)
                self.assertNotIn('timeout 3s ', text)
                self.assertIn('run probes in parallel', text)
                self.assertIn('30 s timeout per model', text)
                self.assertIn('PASS = exact OK reply', text)
                self.assertIn('UNAVAILABLE = explicit error', text)
                self.assertIn('INCONCLUSIVE = timeout without OK', text)
                self.assertIn('Re-probe an INCONCLUSIVE model once', text)
                self.assertIn('never choose an older model over a newer one', text)
                self.assertIn('--effort low', text)
                self.assertIn('OK plus cleanup timeout', text)
                self.assertIn('usage-limit', text)

    def test_guide_and_fallback_require_canonical_model_page_reads(self):
        for text in (guidance.load_dispatch_guide(), guidance.EFFORT):
            with self.subTest(text=text):
                self.assertIn('https://platform.claude.com/docs/en/models/overview', text)
                self.assertIn('https://learn.chatgpt.com/docs/models?surface=app', text)
                self.assertIn('main agent fetches and reads', text)
                self.assertIn('canonical model pages with its web/fetch tool', text)
                self.assertIn('Cite both URLs with the date read in models.md', text)
                self.assertIn('note any disagreement with the local catalog/Claude CLI aliases', text)
                self.assertIn('If no fetch tool is available, say so in models.md', text)
                self.assertIn('fall back to local sources', text)
                self.assertIn("codex debug models shows what this account's CLI can call", text)

    def test_guide_and_fallback_scope_model_errors_to_cli_login(self):
        for text in (guidance.load_dispatch_guide(), guidance.EFFORT):
            with self.subTest(text=text):
                self.assertIn('explicit model-specific client error', text)
                self.assertIn('"requires usage credits"', text)
                self.assertIn('UNAVAILABLE from THIS CLI login right now', text)
                self.assertIn('not that the model needs credits in general', text)
                self.assertIn('Record the message verbatim in models.md', text)
                self.assertIn('only authMethod from claude auth status', text)
                self.assertIn('never email or tokens', text)
                self.assertIn('Re-probe at next refresh', text)
                self.assertIn('Codex usage-limit error switches affected roles', text)

    def test_model_decisions_injected_for_both_engines_and_successors(self):
        for engine in ('claude', 'codex'):
            options = LaunchOptions(engine=engine, headless=True, rollover=None)
            for prompt in ('Start work', recovery_input(self.view, paths_only=True).text):
                with self.subTest(engine=engine, prompt=prompt):
                    plan = adapters.prepare(self.view, options, prompt, self.root / engine)
                    self.assert_injected(plan, str(self.root / 'models.md'))
                    self.assert_injected(plan, 'missing or older than ~24h')
                    self.assert_injected(plan, 'successors/workers reuse unless stale or a model fails')
                    self.assertNotIn('--model', plan.argv)
                    self.assertFalse((self.root / 'models.md').exists())

    def test_launcher_only_passes_explicit_top_level_model(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                plan = adapters.prepare(self.view, LaunchOptions(engine=engine, headless=True,
                    rollover=None, model='operator-choice'), 'Work', self.root / engine)
                self.assertEqual(plan.argv[plan.argv.index('--model') + 1], 'operator-choice')
