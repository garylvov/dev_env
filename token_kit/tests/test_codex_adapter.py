"""Offline checks for provider launch boundaries and honest capabilities."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_kit.adapters.base import AdapterError, LaunchRequest
from token_kit.adapters.codex import prepare_launch


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)

    def request(self, **kwargs):
        return LaunchRequest(
            workspace=self.workspace, prompt=kwargs.pop("prompt", "continue"),
            strict_no_compaction=kwargs.pop("strict_no_compaction", False), **kwargs,
        )

    def test_strict_default_refuses_unverified_guarantee(self):
        request = LaunchRequest(workspace=self.workspace, prompt="continue")
        with self.assertRaisesRegex(AdapterError, "Strict no-compaction is not verified"):
            prepare_launch(request)

    def test_fresh_launch_preserves_arbitrary_prompt_as_one_argument(self):
        prompt = '--resume "thread"\n$(touch /nope); `echo nope`'
        plan = prepare_launch(self.request(prompt=prompt), environ={})
        self.assertEqual(
            ("codex", "--cd", str(self.workspace.resolve()), "-c", 'model_reasoning_effort="medium"',
             "-c", 'plan_mode_reasoning_effort="medium"', "--", prompt), plan.argv,
        )
        self.assertEqual("codex", plan.engine)
        self.assertFalse(plan.strict_no_compaction)
        self.assertEqual(self.workspace.resolve(), plan.cwd)
        self.assertEqual({}, plan.env)

    def test_model_and_executable_are_single_arguments(self):
        plan = prepare_launch(
            self.request(model='custom model "quoted"'),
            executable="/path with spaces/codex", environ={},
        )
        self.assertEqual("/path with spaces/codex", plan.argv[0])
        self.assertEqual(("--model", 'custom model "quoted"'), plan.argv[3:5])
        self.assertNotIn("resume", plan.argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", plan.argv)
        self.assertNotIn("--ask-for-approval", plan.argv)
        self.assertNotIn("--config", plan.argv)

    def test_environment_is_a_copy_without_auth_or_config_mutation(self):
        original = {"CODEX_HOME": "/node-local/codex", "PATH": "/bin"}
        plan = prepare_launch(self.request(), environ=original)
        self.assertEqual(original, plan.env)
        plan.env["PATH"] = "/other"
        self.assertEqual("/bin", original["PATH"])
        with patch.dict(os.environ, original, clear=True):
            self.assertEqual(original, prepare_launch(self.request()).env)

    def test_missing_workspace_refused(self):
        with self.assertRaisesRegex(AdapterError, "not a directory"):
            prepare_launch(LaunchRequest(
                workspace=self.workspace / "missing", prompt="x", strict_no_compaction=False,
            ))

    def test_invalid_argument_values_refused(self):
        for executable in ("", "codex\x00", "--help"):
            with self.subTest(executable=executable), self.assertRaises(AdapterError):
                prepare_launch(self.request(), executable=executable)
        for model in ("", " ", "model\x00", "--help"):
            with self.subTest(model=model), self.assertRaises(AdapterError):
                prepare_launch(self.request(model=model))
        for prompt in ("", " ", "hello\x00"):
            with self.subTest(prompt=prompt), self.assertRaises(AdapterError):
                prepare_launch(self.request(prompt=prompt))


if __name__ == "__main__":
    unittest.main()
