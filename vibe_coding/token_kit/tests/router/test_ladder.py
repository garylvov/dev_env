"""The `prefer` ladder: grammar, availability, the cooldown, the dispatch line.

Each case here is one branch of the closed availability set.  The point of the
ladder is that the operator can see WHY a candidate was skipped, so every test
asserts the reason word, not just the choice.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import ladder, matrix as matrix_mod
from .runner import FIXTURE_MATRIX


def load_fixture(tmp: Path, *replaces: tuple[str, str]) -> matrix_mod.Matrix:
    import re
    text = FIXTURE_MATRIX.read_text(encoding="utf-8").replace("$AGENTS_DIR", str(tmp / "agents"))
    (tmp / "agents").mkdir(parents=True, exist_ok=True)
    for rx, rep in replaces:
        text = "".join(re.sub(rx, lambda _m, r=rep: r, ln, count=1)
                       for ln in text.splitlines(keepends=True))
    path = tmp / "m.toml"
    path.write_text(text, encoding="utf-8")
    return matrix_mod.load(path)


class TestGrammar(unittest.TestCase):
    def test_parses_engine_model_effort(self):
        c = ladder.parse("codex:gpt-6-astra:high")
        self.assertEqual((c.engine, c.model, c.effort), ("codex", "gpt-6-astra", "high"))
        self.assertTrue(c.is_codex)

    def test_rejects_anything_else(self):
        for bad in ("opus", "claude:opus", "claude:opus:enormous", "gemini:x:high", ""):
            self.assertIsNone(ladder.parse(bad), bad)

    def test_every_live_row_is_valid_and_ends_in_claude(self):
        with tempfile.TemporaryDirectory() as t:
            m = load_fixture(Path(t))
            self.assertEqual(matrix_mod.validate(m), [])

    def test_validate_catches_a_codex_only_list(self):
        with tempfile.TemporaryDirectory() as t:
            m = load_fixture(Path(t),
                             (r'^prefer = \["codex:gpt-6-astra:high".*',
                              'prefer = ["codex:gpt-6-astra:high"]'))
            problems = matrix_mod.validate(m)
            self.assertTrue(any("does not END in a claude candidate" in p for p in problems),
                            problems)


class TestAvailability(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["TOKEN_KIT_CODEX_SLOT_DIR"] = str(self.tmp / "slots")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_codex_available_when_the_binary_is_here(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "sh"'))
        self.assertIsNone(ladder.codex_unavailable_reason(m.codex, self.tmp / "s"))

    def test_binary_absent(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "codex-no-such-binary"'))
        self.assertEqual(ladder.codex_unavailable_reason(m.codex, self.tmp / "s"),
                         "binary_absent")

    def test_wrapper_dir_absent(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "sh"'),
                         (r"^agents_dir = .*", 'agents_dir = "/nonexistent-agents-dir"'))
        self.assertEqual(ladder.codex_unavailable_reason(m.codex, self.tmp / "s"),
                         "wrapper_unavailable")

    def test_per_host_bound_full_reads_the_dispatcher_s_own_lock_dir(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "sh"'))
        from token_kit.codex import dispatch
        root = dispatch.slot_root()
        self.assertEqual(root, self.tmp / "slots", "the override env must be honoured")
        for i in range(dispatch.DEFAULT_MAX_CHILDREN):
            d = root / f"slot-{i}"
            d.mkdir(parents=True)
            (d / "pid").write_text(f"{os.getpid()}\n")   # a pid that IS alive
        self.assertEqual(ladder.codex_unavailable_reason(m.codex, self.tmp / "s"),
                         "codex_bound_full")

    def test_a_dead_holder_s_slot_does_not_count(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "sh"'))
        from token_kit.codex import dispatch
        for i in range(dispatch.DEFAULT_MAX_CHILDREN):
            d = dispatch.slot_root() / f"slot-{i}"
            d.mkdir(parents=True)
            (d / "pid").write_text("2147480000\n")       # a pid that is not
        self.assertIsNone(ladder.codex_unavailable_reason(m.codex, self.tmp / "s"))

    def test_quota_cooldown_is_live_then_expires(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "sh"'))
        state = self.tmp / "sess"
        marker = ladder.arm_cooldown(m.codex, state)
        self.assertEqual(ladder.codex_unavailable_reason(m.codex, state), "quota_cooldown")
        old = time.time() - (float(m.codex["quota_cooldown_s"]) + 60)
        os.utime(marker, (old, old))
        self.assertIsNone(ladder.codex_unavailable_reason(m.codex, state))

    def test_zero_concurrency_takes_a_tier_out_of_service(self):
        m = load_fixture(self.tmp, (r"^fable = .*", "fable = 0"))
        self.assertEqual(ladder.claude_unavailable_reason("fable", m.concurrency),
                         "concurrency_full")
        self.assertIsNone(ladder.claude_unavailable_reason("opus", m.concurrency))


class TestChoice(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["TOKEN_KIT_CODEX_SLOT_DIR"] = str(self.tmp / "slots")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_first_available_wins_and_the_skips_are_named(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "codex-no-such-binary"'),
                         (r"^fable = .*", "fable = 0"))
        row = m.row("design-review")
        chosen, skipped = ladder.choose(row, m, self.tmp / "s")
        self.assertEqual(chosen.text, "claude:opus:high")
        self.assertEqual([(c.text, r) for c, r in skipped],
                         [("codex:gpt-6-astra:high", "binary_absent"),
                          ("claude:fable:high", "concurrency_full")])

    def test_nothing_available_returns_none_rather_than_blocking(self):
        m = load_fixture(self.tmp, (r"^binary = .*", 'binary = "codex-no-such-binary"'),
                         (r"^fable = .*", "fable = 0"), (r"^opus = .*", "opus = 0"))
        chosen, skipped = ladder.choose(m.row("design-review"), m, self.tmp / "s")
        self.assertIsNone(chosen)
        self.assertEqual(len(skipped), 3)

    def test_the_dispatch_line_carries_THIS_candidate_s_model_and_effort(self):
        m = load_fixture(self.tmp)
        cmd = ladder.dispatch_command(m.codex, ladder.parse("codex:gpt-6-astra:low"))
        self.assertIn("--model gpt-6-astra", cmd)
        self.assertIn("--effort low", cmd)
        self.assertNotIn("gpt-5.6-luna", cmd)


if __name__ == "__main__":
    unittest.main()
