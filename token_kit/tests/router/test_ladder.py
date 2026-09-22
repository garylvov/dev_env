"""The `prefer` ladder: grammar, availability, the cooldown, the dispatch line.

Each case here is one branch of the CLOSED availability set.  The point of the
ladder is that anyone can see WHY a candidate was skipped, so every test
asserts the reason word, not just the choice.

Codex settings are code defaults now, overridable from the `[router]` table of
one optional config file.  `settings()` below writes that file, so every case
also exercises the reader production uses -- and the file being ABSENT is the
normal case, pinned by its own test.
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
    path = tmp / "m.md"
    path.write_text(text, encoding="utf-8")
    return matrix_mod.load(path)


def settings(tmp: Path, **overrides) -> dict:
    """codex settings with `[router]` overrides, through the real reader."""
    if not overrides:
        os.environ["TOKEN_KIT_CONFIG"] = str(tmp / "absent.toml")
        return ladder.codex_settings()
    lines = ["[router]"]
    for k, v in overrides.items():
        lines.append(f"{k} = {v}" if isinstance(v, (int, float)) and not isinstance(v, bool)
                     else f'{k} = "{v}"')
    path = tmp / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["TOKEN_KIT_CONFIG"] = str(path)
    return ladder.codex_settings()


class TestGrammar(unittest.TestCase):
    def test_parses_engine_model_effort(self):
        c = ladder.parse("codex:gpt-6-astra:high")
        self.assertEqual((c.engine, c.model, c.effort), ("codex", "gpt-6-astra", "high"))
        self.assertTrue(c.is_codex)

    def test_rejects_anything_else(self):
        for bad in ("opus", "claude:opus", "claude:opus:enormous", "gemini:x:high", ""):
            self.assertIsNone(ladder.parse(bad), bad)

    def test_every_fixture_row_is_valid_and_ends_in_claude(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(matrix_mod.validate(load_fixture(Path(t))), [])

    def test_validate_accepts_a_codex_only_ladder(self):
        with tempfile.TemporaryDirectory() as t:
            m = load_fixture(Path(t), (r"`codex:gpt-5\.6-luna:high` > `claude:sonnet:low`",
                                       "`codex:gpt-5.6-luna:high`"))
            problems = matrix_mod.validate(m)
            self.assertEqual(problems, [])


class TestSettings(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_an_absent_config_file_is_the_normal_case(self):
        c = settings(self.tmp)
        self.assertEqual(c["binary"], ladder.CODEX_DEFAULTS["binary"])
        self.assertEqual(c["refusal_code"], 42)

    def test_the_router_table_overrides_a_default(self):
        c = settings(self.tmp, binary="/bin/sh", quota_cooldown_s=60)
        self.assertEqual(c["binary"], "/bin/sh")
        self.assertEqual(c["quota_cooldown_s"], 60)

    def test_an_unknown_key_is_ignored_rather_than_inventing_a_setting(self):
        c = settings(self.tmp, binary="/bin/sh", nonsense="yes")
        self.assertNotIn("nonsense", c)


class TestAvailability(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["TOKEN_KIT_CODEX_SLOT_DIR"] = str(self.tmp / "slots")
        os.environ["TOKEN_KIT_CODEX_COOLDOWN_MARKER"] = str(self.tmp / "cooldown.json")
        (self.tmp / "agents").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def here(self, **over) -> dict:
        over.setdefault("binary", "sh")
        over.setdefault("agents_dir", str(self.tmp / "agents"))
        return settings(self.tmp, **over)

    def test_codex_available_when_the_binary_is_here(self):
        self.assertIsNone(ladder.codex_unavailable_reason(self.here(), self.tmp / "s"))

    def test_profile_decides_when_the_setting_names_plain_codex(self):
        from unittest import mock
        c = self.here(binary="codex")
        with mock.patch.object(ladder, "profile_codex_reachable", return_value=False):
            self.assertEqual(ladder.codex_unavailable_reason(c, self.tmp / "s"), "binary_absent")

    def test_profile_with_a_missing_launcher_path_is_unreachable(self):
        from unittest import mock
        from token_kit.codex import launcher as launcher_mod
        cfg = launcher_mod.CodexConfig(launcher=str(self.tmp / "no-such-launcher"))
        with mock.patch.object(launcher_mod, "load_config", return_value=cfg):
            self.assertFalse(ladder.profile_codex_reachable())

    def test_binary_absent(self):
        self.assertEqual(
            ladder.codex_unavailable_reason(self.here(binary="codex-no-such-binary"),
                                            self.tmp / "s"),
            "binary_absent")

    def test_wrapper_dir_absent(self):
        self.assertEqual(
            ladder.codex_unavailable_reason(self.here(agents_dir="/nonexistent-agents-dir"),
                                            self.tmp / "s"),
            "wrapper_unavailable")

    def test_per_host_bound_full_reads_the_dispatchers_own_lock_dir(self):
        from token_kit.codex import dispatch
        c = self.here()
        root = dispatch.slot_root()
        self.assertEqual(root, self.tmp / "slots", "the override env must be honoured")
        for i in range(dispatch.DEFAULT_MAX_CHILDREN):
            d = root / f"slot-{i}"
            d.mkdir(parents=True)
            (d / "pid").write_text(f"{os.getpid()}\n")   # a pid that IS alive
        self.assertEqual(ladder.codex_unavailable_reason(c, self.tmp / "s"), "codex_bound_full")

    def test_a_dead_holders_slot_does_not_count(self):
        from token_kit.codex import dispatch
        c = self.here()
        for i in range(dispatch.DEFAULT_MAX_CHILDREN):
            d = dispatch.slot_root() / f"slot-{i}"
            d.mkdir(parents=True)
            (d / "pid").write_text("2147480000\n")       # a pid that is not
        self.assertIsNone(ladder.codex_unavailable_reason(c, self.tmp / "s"))

    def test_quota_cooldown_is_live_then_expires(self):
        c = self.here()
        state = self.tmp / "sess"
        marker = ladder.arm_cooldown(c, state)
        self.assertEqual(ladder.codex_unavailable_reason(c, state), "quota_cooldown")
        old = time.time() - (float(c["quota_cooldown_s"]) + 60)
        os.utime(marker, (old, old))
        self.assertIsNone(ladder.codex_unavailable_reason(c, state))

    def test_the_dispatchers_own_quota_marker_reaches_the_router(self):
        """The marker `codex.errors.board_quota()` writes must be READ here.

        These were two different files: the dispatcher boarded a real
        out-of-quota refusal into a machine-wide marker, and the router only
        ever looked at a per-session one that nothing but a manual verb wrote.
        Every spawn after a real refusal therefore paid for it again.
        """
        from token_kit.codex import errors as codex_errors

        c = self.here()
        state = self.tmp / "sess-no-marker-of-its-own"
        self.assertIsNone(ladder.codex_unavailable_reason(c, state))

        codex_errors.board_quota("usageLimitExceeded", retry_after_s=900.0)
        self.assertEqual(ladder.codex_unavailable_reason(c, state), "quota_cooldown")

        # and it lets go when the boarded window passes
        self.assertEqual(0.0, codex_errors.cooldown_active(now=time.time() + 1000))
        self.assertIsNone(ladder.codex_unavailable_reason(c, state, now=time.time() + 1000))


class TestChoice(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["TOKEN_KIT_CODEX_SLOT_DIR"] = str(self.tmp / "slots")
        os.environ["TOKEN_KIT_CODEX_COOLDOWN_MARKER"] = str(self.tmp / "cooldown.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_first_available_wins_and_the_skip_is_named(self):
        m = load_fixture(self.tmp)
        c = settings(self.tmp, binary="codex-no-such-binary",
                     agents_dir=str(self.tmp / "agents"))
        chosen, skipped = ladder.choose(m.row("lookup"), c, self.tmp / "s")
        self.assertEqual(chosen.text, "claude:sonnet:low")
        self.assertEqual([(x.text, r) for x, r in skipped],
                         [("codex:gpt-5.6-luna:high", "binary_absent")])

    def test_a_claude_candidate_is_ALWAYS_available(self):
        """The deleted concurrency table, pinned as deleted.

        It used to be possible for a tier to be 'full', which meant a kind
        with only Claude candidates could resolve to nothing.  There is no
        live count of running Claude subagents to build that on, so the
        criterion is gone: every Claude candidate is available, always.
        """
        m = load_fixture(self.tmp)
        c = settings(self.tmp, binary="codex-no-such-binary")
        for kind in ("design", "write-doc", "implement"):
            chosen, skipped = ladder.choose(m.row(kind), c, self.tmp / "s")
            self.assertIsNotNone(chosen, kind)
            self.assertEqual(skipped, [], kind)
        self.assertFalse(hasattr(ladder, "claude_unavailable_reason"),
                         "the concurrency criterion had no live producer and was deleted")

    def test_the_dispatch_line_carries_THIS_candidates_model_and_effort(self):
        cmd = ladder.dispatch_command(settings(self.tmp), ladder.parse("codex:gpt-6-astra:low"))
        self.assertIn("--model gpt-6-astra", cmd)
        self.assertIn("--effort low", cmd)
        self.assertNotIn("gpt-5.6-luna", cmd)


if __name__ == "__main__":
    unittest.main()
