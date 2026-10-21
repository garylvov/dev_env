import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_kit import simple_cli as cli


class ShortCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_launch(self, args, path, *, create=False):
        self.calls.append((path, create, getattr(args, "title", None)))
        return 0

    def run_cli(self, *argv):
        with patch.object(cli, "_launch", side_effect=self.fake_launch):
            return cli.main([*argv, "--root", str(self.root)])

    def make_task(self, name):
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}_in.md").write_text("1. 2026-10-21T00:00:00\nhello\n")
        (folder / f"{name}_out.md").write_text("# Current state\n")
        return folder

    def test_bare_name_starts_new_task(self):
        self.assertEqual(self.run_cli("agrescap"), 0)
        self.assertEqual(self.calls, [(None, True, "agrescap")])

    def test_bare_name_continues_existing_task(self):
        folder = self.make_task("agrescap_20261021_1421_ec21ec6b")
        self.make_task("agrescap_other_20261021_1421_aaaaaaaa")  # prefix-similar, different task
        self.assertEqual(self.run_cli("agrescap"), 0)
        self.assertEqual(self.calls[0][0].resolve(), folder.resolve())
        self.assertFalse(self.calls[0][1])

    def test_unquoted_words_form_one_name(self):
        self.assertEqual(self.run_cli("fix", "parser", "bug", "--no-codegraph"), 0)
        self.assertEqual(self.calls, [(None, True, "fix parser bug")])

    def test_aliases_route_to_commands(self):
        self.assertEqual(self.run_cli("r", "fresh"), 0)
        self.assertEqual(self.calls, [(None, True, "fresh")])
        self.assertEqual(cli.ALIASES["c"], "continue")

    def test_options_are_not_names(self):
        with patch.object(cli, "_launch", side_effect=self.fake_launch):
            self.assertEqual(cli.main(["--bogus"]), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
