"""Worker registration and reservation through the compact CLI."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.store import Store, read_json
from token_kit.workflow import main


class WorkerPrepareCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store.create(self.root / "tasks", "CLI workers", self.root)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def prepare(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["worker", "prepare", str(self.store.path), "--agent", "parser",
                         "--engine", "claude", *args])
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_brief_registers_checkpoints_and_prepares(self):
        code, result, error = self.prepare("--brief", "Fix parser; verify edge cases.")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(result["spawn_authorized"])
        self.assertEqual(result["worker"]["parent_agent"], "coordinator")
        bundle = self.store.resume_bundle("parser")
        self.assertIn("Fix parser; verify edge cases.", Path(bundle["assignment"]).read_text())
        self.assertIsNotNone(self.store.latest("parser"))

    def test_assignment_file_and_custom_parent(self):
        self.store.add_agent("lead", "Coordinate parser work")
        assignment = self.root / "assignment with spaces.md"
        assignment.write_text("Fix parser\nCheck Unicode: café\n", encoding="utf-8")
        code, result, error = self.prepare("--assignment-file", str(assignment), "--parent", "lead")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["worker"]["parent_agent"], "lead")
        self.assertEqual(result["worker"]["owner_agent"], "lead")
        self.assertIn(assignment.read_text(), Path(self.store.resume_bundle("parser")["assignment"]).read_text())

    def test_existing_worker_still_prepares_without_assignment(self):
        self.store.add_agent("parser", "Original assignment")
        code, result, error = self.prepare()
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(result["spawn_authorized"])
        code, again, error = self.prepare()
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(again["spawn_authorized"])
        self.assertEqual(again["worker"]["ticket"], result["worker"]["ticket"])

    def test_existing_assignment_cannot_be_replaced(self):
        agent = self.store.add_agent("parser", "Original assignment")
        before = read_json(agent / "agent.json")
        assignment = (agent / "assignments/0001.md").read_bytes()
        for flag, value in (("--brief", "Replacement"),
                            ("--assignment-file", str(agent / "assignments/0001.md"))):
            with self.subTest(flag=flag):
                code, result, error = self.prepare(flag, value)
                self.assertEqual(code, 2)
                self.assertIsNone(result)
                self.assertIn("already exists", error)
                self.assertEqual(read_json(agent / "agent.json"), before)
                self.assertEqual((agent / "assignments/0001.md").read_bytes(), assignment)
                self.assertFalse((agent / "lifecycle.json").exists())

    def test_missing_worker_requires_assignment(self):
        code, result, error = self.prepare()
        self.assertEqual(code, 2)
        self.assertIn("Unknown agent", error)
        self.assertFalse((self.store.path / "agents/parser").exists())

    def test_empty_brief_and_parent_without_assignment_are_rejected(self):
        for args, message in ((("--brief", "  "), "Assignment cannot be empty"),
                              (("--parent", "coordinator"), "--parent requires")):
            with self.subTest(args=args):
                code, result, error = self.prepare(*args)
                self.assertEqual(code, 2)
                self.assertIn(message, error)
                self.assertFalse((self.store.path / "agents/parser").exists())

    def test_assignment_options_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.prepare("--brief", "Fix parser", "--assignment-file", "missing.md")
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.store.path / "agents/parser").exists())


if __name__ == "__main__":
    unittest.main()
