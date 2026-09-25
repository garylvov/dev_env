"""Readable immutable folder names and terminal-only color."""
import contextlib
import io
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core.store import Store, component, read_json, task_component
from token_kit.workflow import startup_line


class SessionDisplayTests(unittest.TestCase):
    def test_clock_colons_are_allowed_only_for_safe_task_components(self):
        name = "fix_parser_12:07pm_sept23_abcd"
        self.assertEqual(task_component(name), name)
        with self.assertRaises(ValueError):
            component(name)
        for unsafe in ("../task", "/task", "task/child", "task\\child", "a" * 129, "task\n"):
            with self.assertRaises(ValueError):
                task_component(unsafe)

    def test_title_slug_is_safe_bounded_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for title, slug in (("Fix parser", "fix_parser"), ("../../Fix: Parser!", "fix_parser"),
                                ("Café", "cafe"), ("!?!", "session"), ("a" * 300, "a" * 64)):
                first = Store.create(root / "tasks", title, root)
                second = Store.create(root / "tasks", title, root)
                self.assertRegex(first.path.name, "^" + slug + r"_\d{1,2}:\d{2}(am|pm)_[a-z]{3,4}\d{1,2}_[a-f0-9]{4}$")
                self.assertNotEqual(first.path, second.path)
                self.assertEqual(first.path.parent, root / "tasks")
                self.assertEqual(read_json(first.path / "task.json")["title"], title)
                original = first.path
                first.update_task(title="New display title")
                self.assertEqual(Store(original).task_id, first.task_id)
                self.assertTrue(original.exists())

    def test_exact_local_time_format_and_collision_retry(self):
        from token_kit.core import store as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "tasks" / "fix_parser_12:07pm_sept23_aaaa"
            existing.mkdir(parents=True)
            (existing / "keep.txt").write_text("untouched")
            with patch.object(module, "datetime") as clock, \
                 patch.object(module, "now", return_value="2026-09-23T16:07:00+00:00"), \
                 patch.object(module.uuid, "uuid4", side_effect=[uuid.UUID(ch * 32) for ch in "abc"]):
                clock.now.return_value.astimezone.return_value = datetime(2026, 9, 23, 12, 7)
                task = Store.create(root / "tasks", "Fix parser", root)
            self.assertEqual(task.path.name, "fix_parser_12:07pm_sept23_bbbb")
            self.assertEqual((existing / "keep.txt").read_text(), "untouched")
            self.assertEqual(read_json(task.path / "task.json")["created_at"], "2026-09-23T16:07:00+00:00")

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
