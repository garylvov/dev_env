"""The agent generator, and the fact that made it worth building.

MEASURED live (CLI 2.1.278, a PreToolUse hook dumping its own stdin): an agent
file carrying `effort: low` in its frontmatter gives the SUBAGENT
effort={"level":"low"}; the same file at `effort: high` gives
effort={"level":"high"}; the main thread reads medium in both runs.  So effort
is a real frontmatter key and it is per FILE -- which is why one file per
(kind, claude candidate) is the unit, not one per kind.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import gen_agents, matrix as matrix_mod
from .runner import FIXTURE_MATRIX


def fixture(tmp: Path, text: str | None = None) -> matrix_mod.Matrix:
    src = tmp / "m.md"
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
            self.assertIn("kit-design-opus-high.md", names)
            self.assertIn("kit-design-fable-high.md", names)
            self.assertIn("kit-codex-runner.md", names,
                          "one generic codex runner, not one wrapper per topic")
            self.assertNotIn("kit-lookup-gpt-5.6-luna-high.md", names,
                             "a codex candidate is dispatched, never given an agent file")
            body = (tmp / "out" / "kit-design-fable-high.md").read_text()
            self.assertIn("\nmodel: fable\n", body)
            self.assertIn("\neffort: high\n", body)
            self.assertIn("[KIND: design", body)

    def test_editing_a_row_changes_the_output(self):
        """The guard: the generator is a READ of the table, not a copy of it."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            before = gen_agents.body(fixture(tmp).row("design"),
                                     gen_agents.ladder.parse("claude:fable:high"),
                                     (150, 230, 250))
            text = FIXTURE_MATRIX.read_text(encoding="utf-8").replace(
                "one approach is recommended with its cost", "CHANGED BY THE GUARD")
            after = gen_agents.body(fixture(tmp, text).row("design"),
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
