"""The row-agent generator, and the fact that made it worth building.

MEASURED live (CLI 2.1.278, a PreToolUse hook dumping its own stdin): an agent
file carrying `effort: low` in its frontmatter gives the SUBAGENT
effort={"level":"low"}; the same file at `effort: high` gives
effort={"level":"high"}; the main thread reads medium in both runs.  So effort
is a real frontmatter key and it is per FILE -- which is why one file per
(row, claude candidate) is the unit, not one per row.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import gen_agents, matrix as matrix_mod
from .runner import FIXTURE_MATRIX


def fixture(tmp: Path, text: str | None = None) -> matrix_mod.Matrix:
    src = tmp / "m.toml"
    body = text if text is not None else FIXTURE_MATRIX.read_text(encoding="utf-8")
    src.write_text(body.replace("$AGENTS_DIR", str(tmp / "agents")), encoding="utf-8")
    return matrix_mod.load(src)


class TestGenerate(unittest.TestCase):
    def test_one_file_per_row_and_claude_candidate_carrying_its_effort(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            m = fixture(tmp)
            written = gen_agents.generate(m, tmp / "out")
            names = {p.name for p in written}
            self.assertIn("row-design-review-fable-high.md", names)
            self.assertIn("row-design-review-opus-high.md", names)
            self.assertNotIn("row-watch-poll-wait-NONE-medium.md", names,
                             "a refuse row has no candidate, so it gets no file")
            body = (tmp / "out" / "row-design-review-fable-high.md").read_text()
            self.assertIn("\nmodel: fable\n", body)
            self.assertIn("\neffort: high\n", body)
            self.assertIn("[MATRIX ROW: design-review", body)

    def test_editing_a_row_changes_the_output(self):
        """The guard: the generator is a READ of the table, not a copy of it."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            before = gen_agents.body(fixture(tmp).row("design-review"),
                                     gen_agents.ladder.parse("claude:fable:high"),
                                     (150, 230, 250))
            text = FIXTURE_MATRIX.read_text(encoding="utf-8").replace(
                'stop = "each objection names the section it attacks and what would change '
                'the verdict"', 'stop = "CHANGED BY THE GUARD"')
            after = gen_agents.body(fixture(tmp, text).row("design-review"),
                                    gen_agents.ladder.parse("claude:fable:high"),
                                    (150, 230, 250))
            self.assertNotEqual(before, after)
            self.assertIn("CHANGED BY THE GUARD", after)

    def test_every_generated_file_has_the_frontmatter_the_cli_requires(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            for p in gen_agents.generate(fixture(tmp), tmp / "out"):
                head = p.read_text().splitlines()
                self.assertEqual(head[0], "---", p.name)
                keys = [ln.split(":")[0] for ln in head[1:head[1:].index("---") + 1]]
                for required in ("name", "description", "model", "effort"):
                    self.assertIn(required, keys, p.name)


if __name__ == "__main__":
    unittest.main()
