"""The markdown reader: what it accepts, what it refuses, and what it ignores.

The guide is a document a person reads.  That only works if the parser is
indifferent to everything around its two tables -- prose, headings, code
fences, even another pipe table in the examples -- and strict about the two
tables themselves.  Both halves are pinned here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import matrix as matrix_mod
from .runner import FIXTURE_MATRIX

GOOD = """# Guide

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| lookup | find a fact | codex does it all | `codex:x:high` > `claude:sonnet:low` | it is quoted |

| threshold | calls | what happens |
| --- | --- | --- |
| warn | 150 | one call is denied |
| floor | 230 | only out.md and the handback |
| hard | 250 | the refusal escalates |
"""


def write(tmp: Path, text: str) -> matrix_mod.Matrix:
    p = tmp / "m.md"
    p.write_text(text, encoding="utf-8")
    return matrix_mod.load(p)


class TestParser(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_good_table_parses_into_rows(self):
        m = write(self.tmp, GOOD)
        self.assertEqual(m.names(), ["lookup"])
        row = m.row("lookup")
        self.assertEqual(row["prefer"], ["codex:x:high", "claude:sonnet:low"])
        self.assertEqual(row["shape"], "codex-direct")
        self.assertEqual(m.budget(), (150, 230, 250))

    def test_a_missing_column_is_a_LOUD_failure(self):
        text = GOOD.replace("| kind | use when | who does it | prefer | done when |",
                            "| kind | use when | prefer | done when |")
        with self.assertRaises(matrix_mod.MatrixError) as cm:
            write(self.tmp, text)
        self.assertIn("no table whose header row is", str(cm.exception))

    def test_a_row_with_the_wrong_cell_count_is_a_LOUD_failure(self):
        text = GOOD.replace("| lookup | find a fact | codex does it all "
                            "| `codex:x:high` > `claude:sonnet:low` | it is quoted |",
                            "| lookup | find a fact | codex does it all |")
        with self.assertRaises(matrix_mod.MatrixError) as cm:
            write(self.tmp, text)
        self.assertIn("cells, not 5", str(cm.exception))

    def test_a_header_with_no_separator_under_it_is_a_LOUD_failure(self):
        text = GOOD.replace("| --- | --- | --- | --- | --- |\n", "", 1)
        with self.assertRaises(matrix_mod.MatrixError) as cm:
            write(self.tmp, text)
        self.assertIn("no separator row", str(cm.exception))

    def test_a_missing_budget_table_is_a_LOUD_failure(self):
        text = GOOD.split("| threshold |")[0]
        with self.assertRaises(matrix_mod.MatrixError):
            write(self.tmp, text)

    def test_the_parse_is_cached_on_mtime_and_reparses_after_an_edit(self):
        p = self.tmp / "m.md"
        p.write_text(GOOD, encoding="utf-8")
        first = matrix_mod.load(p)
        self.assertIs(matrix_mod.load(p), first, "an unedited guide is parsed once")
        p.write_text(GOOD.replace("| warn | 150 |", "| warn | 7 |"), encoding="utf-8")
        again = matrix_mod.load(p)
        self.assertIsNot(again, first)
        self.assertEqual(again.budget()[0], 7)


class TestExamplesAreInert(unittest.TestCase):
    """Prose, fences and even a second chart below Examples change nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def rows_of(self, text: str) -> list[dict]:
        return write(self.tmp, text).rows

    def test_the_fixtures_example_chart_is_not_the_chart(self):
        m = matrix_mod.load(FIXTURE_MATRIX)
        self.assertNotIn("not-a-real-kind", m.names())

    def test_adding_text_and_a_code_block_does_not_change_a_row(self):
        base = FIXTURE_MATRIX.read_text(encoding="utf-8")
        before = self.rows_of(base)
        after = self.rows_of(base + "\n\nOne more worked example, with a pipe | in it:\n\n"
                                    "```\ncodex-job send <job> 'a | b'\n```\n\n"
                                    "| kind | use when | who does it | prefer | done when |\n"
                                    "| --- | --- | --- | --- | --- |\n"
                                    "| another-example | nope | Claude does it all "
                                    "| `claude:opus:low` | never |\n")
        self.assertEqual(before, after)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, prefer: str) -> list[str]:
        text = GOOD.replace("`codex:x:high` > `claude:sonnet:low`", prefer)
        return matrix_mod.validate(write(self.tmp, text))

    def test_a_ladder_that_is_not_the_grammar_is_named(self):
        self.assertTrue(any("is not <engine>:<model>:<effort>" in p
                            for p in self.problems("`opus`")), self.problems("`opus`"))

    def test_a_ladder_that_does_not_end_in_claude_is_named(self):
        self.assertTrue(any("does not END in a claude candidate" in p
                            for p in self.problems("`codex:x:high`")))

    def test_the_no_pointless_climb_rule(self):
        """A dearer model at the same effort is a FLOOR, never an upgrade.

        Tier order is MEASURED list price, cheapest first: sonnet $2, opus $5,
        fable $10 per million input tokens.
        """
        self.assertEqual(matrix_mod.climb_problems("k", ["claude:sonnet:high",
                                                         "claude:opus:high"]), [])
        self.assertEqual(matrix_mod.climb_problems("k", ["claude:opus:high",
                                                         "claude:fable:high"]), [])
        self.assertEqual(matrix_mod.climb_problems("k", ["claude:fable:high",
                                                         "claude:opus:high"]), [],
                         "a descent is always fine")
        self.assertTrue(matrix_mod.climb_problems("k", ["claude:sonnet:medium",
                                                        "claude:fable:medium"]),
                        "two tiers in one step buys nothing and costs five times as much")
        self.assertTrue(matrix_mod.climb_problems("k", ["claude:sonnet:low",
                                                        "claude:opus:low",
                                                        "claude:fable:low"]),
                        "one step at a time still must not end two tiers above the first")

    def test_an_unknown_shape_is_named(self):
        text = GOOD.replace("codex does it all", "somebody does it somehow")
        problems = matrix_mod.validate(write(self.tmp, text))
        self.assertTrue(any("'who does it' is" in p for p in problems), problems)

    def test_an_unordered_budget_is_named(self):
        text = GOOD.replace("| floor | 230 |", "| floor | 900 |")
        problems = matrix_mod.validate(write(self.tmp, text))
        self.assertTrue(any("not ordered" in p for p in problems), problems)


class TestNothingShippedNamesAProject(unittest.TestCase):
    """The banned-word guard, run over everything this kit ships.

    A kit that names one user's project, cluster or doctrine is not reusable
    by anybody else, and a comment counts: this reads whole files.
    """

    def test_the_shipped_guide_names_no_project(self):
        text = (KIT_DIR / "agent_trigger_matrix.md").read_text(encoding="utf-8")
        self.assertEqual(matrix_mod.banned_words_in(text), [])

    def test_the_router_source_names_no_project(self):
        import re
        for p in sorted((KIT_DIR / "src" / "token_kit" / "router").glob("*.py")):
            text = p.read_text(encoding="utf-8")
            # the list itself is the one place the words are allowed to appear
            text = re.sub(r"BANNED_WORDS = \([^)]*\)", "BANNED_WORDS = ()", text)
            self.assertEqual(matrix_mod.banned_words_in(text), [], p.name)

    def test_every_generated_agent_names_no_project(self):
        from token_kit.router import gen_agents
        with tempfile.TemporaryDirectory() as t:
            m = matrix_mod.load(KIT_DIR / "agent_trigger_matrix.md")
            for p in gen_agents.generate(m, Path(t) / "out"):
                self.assertEqual(matrix_mod.banned_words_in(p.read_text(encoding="utf-8")), [],
                                 p.name)

    def test_the_checked_in_agents_dir_holds_no_agent_files_at_all(self):
        """The four personas and the six topic wrappers are gone for good."""
        self.assertEqual(sorted(p.name for p in (KIT_DIR / "agents").glob("*.md")), [])

    def test_the_guard_can_fail(self):
        """The negative control, built FROM the list so this file names nothing."""
        word = matrix_mod.BANNED_WORDS[0]
        self.assertEqual(matrix_mod.banned_words_in(f"a line naming {word} in passing"), [word])
        self.assertEqual(matrix_mod.banned_words_in(f"{word}oid is a different word"), [])


if __name__ == "__main__":
    unittest.main()
