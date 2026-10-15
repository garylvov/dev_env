"""Automatic review opens the native UI, never grants hook trust itself."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import runtime


class HookReviewTests(unittest.TestCase):
    def setUp(self):
        self.stdin = patch.object(sys.stdin, "isatty", return_value=True)
        self.stdout = patch.object(sys.stdout, "isatty", return_value=True)
        self.stdin.start()
        self.stdout.start()
        self.addCleanup(self.stdin.stop)
        self.addCleanup(self.stdout.stop)

    def test_trusted_hooks_do_not_open_review(self):
        with patch.object(runtime, "verify_codex") as verify, patch.object(runtime, "review_hooks") as review:
            runtime.ensure_codex_hooks()
        verify.assert_called_once()
        review.assert_not_called()

    def test_review_then_recheck_same_executable_and_workspace(self):
        with patch.object(runtime, "verify_codex", side_effect=[runtime.HookReviewRequired("trust"), None]) as verify, \
             patch.object(runtime, "review_hooks", return_value=0) as review:
            runtime.ensure_codex_hooks("/bin/custom-codex", Path("/workspace"))
        self.assertEqual(verify.call_count, 2)
        review.assert_called_once_with("/bin/custom-codex", Path("/workspace"))
        for call in verify.call_args_list:
            self.assertEqual(call.args, ("/bin/custom-codex", Path("/workspace")))

    def test_noninteractive_or_nontrust_errors_do_not_open_review(self):
        for interactive, error in ((False, runtime.HookReviewRequired("trust")),
                                   (True, ValueError("missing hooks"))):
            with patch.object(sys.stdin, "isatty", return_value=interactive), \
                 patch.object(runtime, "verify_codex", side_effect=error), \
                 patch.object(runtime, "review_hooks") as review:
                with self.assertRaises(ValueError):
                    runtime.ensure_codex_hooks()
                review.assert_not_called()

    def test_declining_approval_does_not_loop(self):
        with patch.object(runtime, "verify_codex", side_effect=runtime.HookReviewRequired("trust")) as verify, \
             patch.object(runtime, "review_hooks", return_value=0) as review:
            with self.assertRaises(runtime.HookReviewRequired):
                runtime.ensure_codex_hooks()
        self.assertEqual(verify.call_count, 2)
        review.assert_called_once()

    def test_cancelled_review_does_not_launch(self):
        with patch.object(runtime, "verify_codex", side_effect=runtime.HookReviewRequired("trust")) as verify, \
             patch.object(runtime, "review_hooks", return_value=130):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                runtime.ensure_codex_hooks()
        verify.assert_called_once()

    def test_review_removes_managed_identity_and_never_bypasses_trust(self):
        with patch.dict(os.environ, {"TOKEN_KIT_TASK": "/task", "TOKEN_KIT_AGENT": "a", "TOKEN_KIT_RUN": "r"}), \
             patch.object(runtime.subprocess, "call", return_value=0) as call:
            self.assertEqual(runtime.review_hooks(workspace=Path("/workspace")), 0)
        argv = call.call_args.args[0]
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        self.assertNotIn("--yolo", argv)
        self.assertEqual(call.call_args.kwargs["cwd"], Path("/workspace"))
        for key in ("TOKEN_KIT_TASK", "TOKEN_KIT_AGENT", "TOKEN_KIT_RUN"):
            self.assertNotIn(key, call.call_args.kwargs["env"])
