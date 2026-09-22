"""Offline launch contract tests; no Claude subprocess or model calls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from token_kit.adapters.base import AdapterError, LaunchRequest
from token_kit.adapters.claude import prepare_launch


class ClaudeLaunchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)

    def request(self, **kwargs):
        return LaunchRequest(workspace=self.workspace, prompt="Continue assignment", **kwargs)

    def test_strict_sets_environment_and_session_settings_without_mutating_input(self):
        env = {"DISABLE_COMPACT": "0", "PATH": "/custom/bin", "ANTHROPIC_API_KEY": "test"}
        original = env.copy()
        plan = prepare_launch(self.request(), environ=env)
        self.assertEqual(env, original)
        self.assertEqual(plan.env, {**original, "DISABLE_COMPACT": "1"})
        settings = json.loads(plan.argv[plan.argv.index("--settings") + 1])
        self.assertEqual(settings, {"env": {"DISABLE_COMPACT": "1"}})
        self.assertTrue(plan.strict_no_compaction)
        self.assertEqual(plan.engine, "claude")

    def test_opt_out_preserves_existing_environment_policy(self):
        env = {"DISABLE_COMPACT": "1"}
        plan = prepare_launch(self.request(strict_no_compaction=False), environ=env)
        self.assertEqual(plan.env, env)
        self.assertNotIn("--settings", plan.argv)
        self.assertFalse(plan.strict_no_compaction)

    def test_default_environment_is_copied_and_explicit_empty_is_respected(self):
        with patch.dict(os.environ, {"TK_TEST_VALUE": "present"}, clear=True):
            plan = prepare_launch(self.request())
            self.assertEqual(plan.env["TK_TEST_VALUE"], "present")
            self.assertNotIn("DISABLE_COMPACT", os.environ)
            empty_plan = prepare_launch(self.request(), environ={})
            self.assertEqual(empty_plan.env, {"DISABLE_COMPACT": "1"})

    def test_prompt_is_one_literal_argument_and_cannot_inject_options(self):
        prompt = '--resume session; $(touch secret) `echo nope`\n"quoted"'
        plan = prepare_launch(LaunchRequest(self.workspace, prompt), environ={})
        self.assertEqual(plan.argv[-2:], ("--", prompt))
        self.assertEqual(plan.argv.count(prompt), 1)
        self.assertNotIn("--resume", plan.argv)

    def test_fresh_interactive_session_preserves_permission_defaults(self):
        plan = prepare_launch(self.request(model="sonnet"), environ={})
        self.assertEqual(plan.argv[plan.argv.index("--model") + 1], "sonnet")
        for forbidden in ("--resume", "-r", "--continue", "-c", "-p", "--print",
                          "--dangerously-skip-permissions", "--permission-mode"):
            self.assertNotIn(forbidden, plan.argv)

    def test_offline_plan_does_not_require_installed_binary(self):
        plan = prepare_launch(self.request(), executable="/uninstalled/path with spaces/claude", environ={})
        self.assertEqual(plan.argv[0], "/uninstalled/path with spaces/claude")
        self.assertEqual(plan.cwd, self.workspace.resolve())

    def test_missing_or_file_workspace_rejected(self):
        for workspace in (self.workspace / "missing", Path(__file__)):
            with self.subTest(workspace=workspace), self.assertRaises(AdapterError):
                prepare_launch(LaunchRequest(workspace, "Continue"), environ={})

    def test_invalid_arguments_rejected_before_launch(self):
        for executable in ("", "\0", "--resume"):
            with self.subTest(executable=executable), self.assertRaises(AdapterError):
                prepare_launch(self.request(), executable=executable, environ={})
        for prompt in ("", " \n", "NUL\0"):
            with self.subTest(prompt=prompt), self.assertRaises(AdapterError):
                prepare_launch(LaunchRequest(self.workspace, prompt), environ={})
        for model in ("", " ", "--resume", "NUL\0"):
            with self.subTest(model=model), self.assertRaises(AdapterError):
                prepare_launch(self.request(model=model), environ={})


if __name__ == "__main__":
    unittest.main()
