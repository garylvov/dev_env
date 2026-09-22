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

    def test_default_launch_is_session_only(self):
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch:
            rc, _, error = self.call("Fix parser", "--workspace", self.workspace,
                                     "--root", self.tasks, "--yolo", "--model", "opus")
        self.assertEqual(rc, 0, error)
        store = launch.call_args.args[0]
        self.assertEqual(store.workspace, self.workspace)
        self.assertEqual(launch.call_args.args[1:], ("coordinator", "claude", "opus"))
        self.assertTrue(launch.call_args.kwargs["yolo"])
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertIn("Guidance: session-only", error)
        self.assertIn(str(store.path), error)
        self.assertIn("automatic rollover: disabled", error)
        self.assertIn("--model opus --yolo", error)

    def test_default_root_uses_config_directory(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(workflow.default_root(), self.root / ".config/token_kit")
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": ""}), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(workflow.default_root(), self.root / ".config/token_kit")

    def test_run_without_root_uses_config_directory(self):
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.root / "config")}), \
             patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch:
            rc, _, error = self.call("Fix parser", "--workspace", self.workspace)
        self.assertEqual(rc, 0, error)
        self.assertEqual(launch.call_args.args[0].path.parent, self.root / "config/token_kit")

    def test_preview_writes_nothing(self):
        rc, output, error = self.call("Fix parser", "--workspace", self.workspace,
                                     "--root", self.tasks, "--dry-run")
        self.assertEqual(rc, 0, error)
        self.assertEqual(json.loads(output)["project_changes"], [])
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
            rc, _, error = self.call("--workspace", self.workspace, "--root", self.tasks, "--install-project")
        self.assertEqual(rc, 2)
        self.assertIn("symlink", error)
        self.assertFalse(self.tasks.exists())

    def test_project_install_is_explicit(self):
        rc, out, error = self.call("--workspace", self.workspace, "--install-project", "--dry-run")
        self.assertEqual(rc, 0, error)
        self.assertIn("AGENTS.md", json.loads(out)["project_changes"])
        self.assertEqual(list(self.workspace.iterdir()), [])
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0):
            rc, _, error = self.call("--workspace", self.workspace, "--root", self.tasks, "--install-project")
        self.assertEqual(rc, 0, error)
        self.assertTrue((self.workspace / "AGENTS.md").is_file())
        self.assertTrue((self.workspace / "CLAUDE.md").is_file())

    def test_session_only_preserves_existing_project_files(self):
        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.workspace / name).write_text("User instructions\n")
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0):
            rc, _, error = self.call("--workspace", self.workspace, "--root", self.tasks)
        self.assertEqual(rc, 0, error)
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertEqual((self.workspace / name).read_text(), "User instructions\n")
        self.assertFalse((self.workspace / ".token-kit").exists())

    def test_codegraph_requires_explicit_project_install(self):
        rc, _, error = self.call("--workspace", self.workspace, "--codegraph")
        self.assertEqual(rc, 2)
        self.assertIn("--install-project", error)
        self.assertEqual(list(self.workspace.iterdir()), [])
