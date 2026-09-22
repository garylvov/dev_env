"""One public command surface, including historical executable aliases."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "src"))
from token_kit.__main__ import main
from token_kit import cli, task, workflow
from token_kit.core.context import shared_workflow
from token_kit.core.store import Store, read_json
from token_kit.project_install import configure


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.tasks = self.root / "work"

    def call(self, *argv, entry=main):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            rc = entry([str(a) for a in argv])
        return rc, output.getvalue(), error.getvalue()

    def create(self, entry=main):
        rc, out, err = self.call("new", "unified task", "--root", self.tasks,
                                 "--workspace", self.project, entry=entry)
        self.assertEqual(rc, 0, err)
        return Path(out.strip())

    def test_python_entrypoints_create_the_same_schema(self):
        for entry in (main, cli.main, task.main, workflow.main):
            folder = self.create(entry)
            self.assertEqual(read_json(folder / "task.json")["workspace"], str(self.project))
            self.assertTrue((folder / "agents/coordinator/checkpoints").is_dir())
            self.assertFalse((folder / "lanes").exists())

    def test_shell_installer_dry_run_and_install(self):
        command = ["bash", str(KIT / "install.sh"), "--project", str(self.project),
                   "--engine", "both"]
        preview = subprocess.run([*command, "--dry-run"], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)["dry_run"])
        self.assertFalse((self.project / "AGENTS.md").exists())
        installed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        instructions = (self.project / "AGENTS.md").read_text()
        self.assertIn("Opus while we have it", instructions)
        self.assertIn("generic request to red-team still defaults to Codex", instructions)
        self.assertTrue((self.project / "CLAUDE.md").is_file())

    def test_migrate_routes_to_shared_core_without_changing_source(self):
        source = self.root / "legacy"
        source.mkdir()
        state = f"# Old task\n\nCwd: {self.project}\n"
        (source / "STATE.md").write_text(state)
        rc, out, err = self.call("migrate", source, "--root", self.tasks)
        self.assertEqual(rc, 0, err)
        folder = Path(out.strip())
        self.assertTrue((folder / "task.json").is_file())
        self.assertTrue((folder / "migration.json").is_file())
        self.assertEqual((source / "STATE.md").read_text(), state)
        rc, repeated, err = self.call("migrate", source, "--root", self.tasks)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, repeated)

    def test_neutral_default_root_is_independent_of_claude_config(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                     "XDG_CONFIG_HOME": str(self.root / "config"),
                                     "CLAUDE_CONFIG_DIR": str(self.root / "claude")}):
            rc, out, err = self.call("new", "default", "--workspace", self.project)
        self.assertEqual(rc, 0, err)
        self.assertEqual(Path(out.strip()).parent, self.root / "config/token_kit")
        self.assertFalse((self.project / "tasks").exists())

    def test_project_install_is_default_and_no_global_hooks_are_created(self):
        fake_home = self.root / "home"
        fake_home.mkdir()
        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            rc, _, err = self.call("install", "--project", self.project, "--engine", "both")
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.project / "AGENTS.md").is_file())
        self.assertIn("token-kit", (self.project / "AGENTS.md").read_text())
        self.assertFalse((fake_home / ".claude").exists())
        self.assertTrue(shared_workflow(self.project))

    def test_alias_executables_call_the_same_core(self):
        for name in ("token-kit", "token-kit-task", "token-kit-workflow"):
            result = subprocess.run([str(KIT / "src/token_kit/bin" / name), "new", name,
                                     "--root", str(self.tasks), "--workspace", str(self.project)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(result.stdout.strip()) / "task.json").is_file())

    def test_old_task_requires_migration_before_resume(self):
        old = self.root / "old"
        old.mkdir()
        (old / "STATE.md").write_text("# old\nCwd: " + str(self.project))
        rc, _, err = self.call("resume", old)
        self.assertEqual(rc, 2)
        self.assertIn("token-kit migrate", err)
        self.assertFalse((old / "task.json").exists())

    def test_list_find_status_and_retitle_preserve_identity(self):
        folder = self.create()
        identity = read_json(folder / "task.json")["task_id"]
        self.assertEqual(self.call("retitle", folder, "new title")[0], 0)
        rc, out, err = self.call("find", "new", "title", "--root", self.tasks)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)[0]["task_id"], identity)
        self.assertEqual(self.call("done", folder)[0], 0)
        self.assertEqual(json.loads(self.call("list", "--root", self.tasks, "--open")[1]), [])
        self.assertEqual(self.call("reopen", folder)[0], 0)
        self.assertEqual(len(json.loads(self.call("list", "--root", self.tasks, "--open")[1])), 1)

    def test_done_refuses_unreconciled_run(self):
        folder = self.create()
        Store(folder).claim_run("coordinator", "claude", True)
        rc, _, error = self.call("done", folder)
        self.assertEqual(rc, 2)
        self.assertIn("Reconcile", error)

    def test_legacy_hooks_are_silent_in_shared_projects(self):
        from token_kit.router import hook
        from token_kit.respawn import reader
        configure(self.project)
        payload = {"cwd": str(self.project), "hook_event_name": "SessionStart"}
        with patch.object(hook, "session_start", side_effect=AssertionError("legacy injection")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hook.handle(payload), 0)
        self.assertEqual(reader.handle(None, {**payload, "hook_event_name": "Stop"}), "")
        self.assertEqual(reader.nudge(None, {**payload, "hook_event_name": "Stop"}), "")

    def test_launch_prompt_uses_canonical_commands(self):
        folder = self.create()
        rc, out, err = self.call("launch", folder, "--engine", "claude", "--dry-run")
        self.assertEqual(rc, 0, err)
        prompt = json.loads(out)["argv"][-1]
        self.assertIn("token-kit checkpoint", prompt)
        self.assertNotIn("token-kit-workflow", prompt)


if __name__ == "__main__":
    unittest.main()
