"""One-command setup and launch; never invoke a live model in tests."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import workflow
from token_kit.core.store import Store


class QuickstartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "source with spaces"
        self.workspace.mkdir()
        self.tasks = self.root / "tasks"

    def call(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            rc = workflow.main(["run", *map(str, args)])
        return rc, output.getvalue(), error.getvalue()

    def test_setup_and_launch_are_one_command(self):
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch:
            rc, _, error = self.call("Fix parser", "--workspace", self.workspace,
                                     "--root", self.tasks, "--yolo", "--model", "opus")
        self.assertEqual(rc, 0, error)
        store = launch.call_args.args[0]
        self.assertEqual(store.workspace, self.workspace)
        self.assertEqual(launch.call_args.args[1:], ("coordinator", "claude", "opus"))
        self.assertTrue(launch.call_args.kwargs["yolo"])
        self.assertTrue((self.workspace / "AGENTS.md").is_file())
        self.assertTrue((self.workspace / "CLAUDE.md").is_file())
        self.assertIn(str(store.path), error)
        self.assertIn("automatic rollover: not implemented", error)
        self.assertIn("--model opus --yolo", error)

    def test_preview_writes_nothing(self):
        rc, output, error = self.call("Fix parser", "--workspace", self.workspace,
                                     "--root", self.tasks, "--dry-run")
        self.assertEqual(rc, 0, error)
        self.assertIn("AGENTS.md", json.loads(output)["project_changes"])
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertFalse(self.tasks.exists())

    def test_codex_rejection_creates_no_orphan_task_or_project_files(self):
        rc, _, error = self.call("Fix parser", "--workspace", self.workspace,
                                 "--root", self.tasks, "--engine", "codex")
        self.assertEqual(rc, 2)
        self.assertIn("Strict no-compaction", error)
        self.assertFalse(self.tasks.exists())
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_missing_binary_creates_nothing(self):
        with patch.object(workflow.shutil, "which", return_value=None):
            rc, _, error = self.call("--workspace", self.workspace, "--root", self.tasks)
        self.assertEqual(rc, 2)
        self.assertIn("Executable not found", error)
        self.assertFalse(self.tasks.exists())
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_existing_task_keeps_its_identity(self):
        store = Store.create(self.tasks, "Existing", self.workspace)
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch:
            rc, _, error = self.call("--task", store.path)
        self.assertEqual(rc, 0, error)
        self.assertEqual(launch.call_args.args[0].task_id, store.task_id)
        self.assertEqual(len(list(self.tasks.iterdir())), 1)

    def test_resume_cannot_silently_ignore_new_task_arguments(self):
        rc, _, error = self.call("Different", "--task", self.root / "task")
        self.assertEqual(rc, 2)
        self.assertIn("cannot be combined", error)

    def test_setup_conflict_does_not_create_task(self):
        (self.workspace / "AGENTS.md").symlink_to(self.root / "elsewhere")
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"):
            rc, _, error = self.call("--workspace", self.workspace, "--root", self.tasks)
        self.assertEqual(rc, 2)
        self.assertIn("symlink", error)
        self.assertFalse(self.tasks.exists())
