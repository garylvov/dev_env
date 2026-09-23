"""Readable immutable folder names and terminal-only color."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.store import Store, read_json
from token_kit.workflow import startup_line


class SessionDisplayTests(unittest.TestCase):
    def test_title_slug_is_safe_bounded_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for title, slug in (("Fix parser", "fix-parser"), ("../../Fix: Parser!", "fix-parser"),
                                ("Café", "cafe"), ("!?!", "session"), ("a" * 300, "a" * 64)):
                first = Store.create(root / "tasks", title, root)
                second = Store.create(root / "tasks", title, root)
                self.assertRegex(first.path.name, r"^\d{8}T\d{6}-" + slug + r"-[a-f0-9]{8}$")
                self.assertNotEqual(first.path, second.path)
                self.assertEqual(first.path.parent, root / "tasks")
                self.assertEqual(read_json(first.path / "task.json")["title"], title)
                original = first.path
                first.update_task(title="New display title")
                self.assertEqual(Store(original).task_id, first.task_id)
                self.assertTrue(original.exists())

    def render(self, tty, env, heading=False):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(output, "isatty", return_value=tty), \
             patch.dict("os.environ", env, clear=True):
            startup_line("Token Kit: test", heading=heading)
        return output.getvalue()

    def test_interactive_banner_is_purple(self):
        self.assertEqual(self.render(True, {"TERM": "xterm"}, True), "\033[1;35mToken Kit: test\033[0m\n")
        self.assertEqual(self.render(True, {"TERM": "xterm"}), "\033[35mToken Kit\033[0m: test\n")

    def test_logs_dumb_terminal_and_no_color_stay_plain(self):
        for tty, env in ((False, {}), (True, {"TERM": "dumb"}), (True, {"NO_COLOR": ""})):
            self.assertEqual(self.render(tty, env, True), "Token Kit: test\n")
