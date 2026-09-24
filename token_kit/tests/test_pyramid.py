"""Task pyramid reads and dynamic prompt propagation, without client calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import pyramid, worker_policy, workflow
from token_kit.core import lifecycle
from token_kit.core.store import Store


class PyramidTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.task.mkdir()
        self.file = self.task / pyramid.NAME

    def test_task_content_preserved_exactly(self):
        content = "# Session tiers\n\nSmart: scoped model\n"
        self.file.write_text(content)
        self.assertEqual(pyramid.read_pyramid(self.task), {
            "path": str(self.file), "source": "task", "content": content})
        self.assertEqual(self.file.read_text(), content)

    def test_missing_map_falls_back_without_creating_task(self):
        missing = self.root / "missing"
        result = pyramid.read_pyramid(missing)
        self.assertEqual(result["source"], "repository")
        self.assertEqual(result["content"], pyramid.REPOSITORY_PATH.read_text())
        self.assertFalse(missing.exists())

    def test_invalid_existing_map_never_falls_back(self):
        for data in (b"", b" \n\t", b"\xff", b"x" * (pyramid.MAX_BYTES + 1)):
            with self.subTest(data_length=len(data)):
                self.file.write_bytes(data)
                with self.assertRaises(ValueError):
                    pyramid.read_pyramid(self.task)
                self.assertEqual(self.file.read_bytes(), data)

    def test_symlink_and_dangling_map_rejected(self):
        target = self.root / "target"
        target.write_text("Model map")
        for destination in (target, self.root / "absent"):
            with self.subTest(destination=destination):
                self.file.symlink_to(destination)
                with self.assertRaises(ValueError):
                    pyramid.read_pyramid(self.task)
                self.file.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.task, target_is_directory=True)
        with self.assertRaises(ValueError):
            pyramid.read_pyramid(alias)

    def test_refresh_replaces_map_preserving_identity_and_body(self):
        self.file.write_text("Smart: old-choice")
        body = "Scope: parser only.\nKeep exact trailing space.  \n"
        old = worker_policy.brief(body, str(self.task), "parser")
        self.file.write_text("Smart: new-choice")
        new = worker_policy.brief(old, str(self.task))
        self.assertNotIn("old-choice", new)
        self.assertIn("new-choice", new)
        self.assertIn('"agent": "parser"', new)
        self.assertTrue(new.endswith(body))
        self.assertEqual(new.count(worker_policy._OPEN), 1)
        self.assertEqual(worker_policy.brief(new, str(self.task)), new)

    def test_cross_task_refresh_isolated_and_drops_old_identity(self):
        other = self.root / "other"
        other.mkdir()
        self.file.write_text("Smart: first-task-model")
        (other / pyramid.NAME).write_text("Smart: second-task-model")
        old = worker_policy.brief("Work", str(self.task), "first-worker")
        new = worker_policy.brief(old, str(other))
        self.assertIn("second-task-model", new)
        self.assertNotIn("first-task-model", new)
        self.assertNotIn("first-worker", new)
        self.assertEqual(self.file.read_text(), "Smart: first-task-model")

    def test_standalone_hook_reads_current_task_without_pythonpath(self):
        self.file.write_text("Smart: standalone-current-choice")
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                   "tool_input": {"prompt": "Work", "model": "explicit-worker-model"}}
        result = subprocess.run(shlex.split(worker_policy.hook_command(str(self.task))),
                                input=json.dumps(payload), capture_output=True, text=True,
                                env={"PATH": os.defpath}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output)
        self.assertEqual(output["updatedInput"]["model"], "explicit-worker-model")
        self.assertIn("standalone-current-choice", output["updatedInput"]["prompt"])


class PyramidIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store.create(self.root / "tasks", "Pyramid integration", self.root)
        self.agent = self.store.add_agent("parser", "Fix parser only")
        self.file = self.store.path / pyramid.NAME

    def test_invalid_prepare_does_not_reserve_attempt(self):
        self.file.write_text(" ")
        with self.assertRaises(ValueError):
            lifecycle.prepare(self.store, "parser", engine="claude")
        self.assertFalse((self.agent / "lifecycle.json").exists())
        self.file.write_text("Smart: repaired-map")
        result = lifecycle.prepare(self.store, "parser", engine="claude")
        self.assertTrue(result["spawn_authorized"])
        self.assertEqual(result["worker"]["generation"], 1)

    def test_native_spawn_uses_current_map_not_old_assignment_map(self):
        self.file.write_text("Smart: current-custom-model")
        result = lifecycle.prepare(self.store, "parser", engine="claude")
        self.assertIn("current-custom-model", result["spawn_prompt"])
        self.assertEqual(result["spawn_prompt"].count(worker_policy._OPEN), 1)

    def test_old_task_dry_run_read_only_then_real_launch_seeds(self):
        self.file.unlink(missing_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, "parser", "claude", dry_run=True), 0)
        self.assertFalse(self.file.exists())
        child = Mock(pid=99999999)
        child.poll.return_value = 0
        child.wait.return_value = 0
        with patch("token_kit.core.store.workspace_head", return_value=None), \
                patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
                patch.object(workflow.subprocess, "Popen", return_value=child), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, "parser", "claude"), 0)
        self.assertEqual(self.file.read_text(), pyramid.REPOSITORY_PATH.read_text())


if __name__ == "__main__":
    unittest.main()
