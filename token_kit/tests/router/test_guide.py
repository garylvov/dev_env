"""The SessionStart injection, and the guard that keeps the examples honest.

Two different failures are pinned here.

The first is DRIFT BETWEEN THE GUIDE AND ITSELF: the injected context is the
document, so a row added to the chart is in the delegating thread's context
without anybody regenerating anything.

The second is DRIFT BETWEEN THE EXAMPLES AND THE SHIMS.  The guide this
replaced shipped a dispatch template reading `codex-dispatch --model ...
--effort ... --message '<one sub-step>'`, and `--message` is not a flag
`codex-dispatch` has ever had: it takes `--cwd --task-file --out`.  Nobody
noticed, because prose is not executed.  So every fenced `codex-dispatch` /
`codex-job` line in the Examples section is parsed HERE by the shims' own
argparse parsers -- the same objects the shims call at runtime.  An example
that could not run is a failing test, which matters more than the prose.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import tempfile
import unittest
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import hook as hook_mod, matrix as matrix_mod
from .runner import FIXTURE_MATRIX

SHIPPED = KIT_DIR / "agent_trigger_matrix.md"


def fenced_lines(text: str, heading: str = matrix_mod.EXAMPLES_HEADING) -> list[str]:
    """Every line inside a ``` fence, below `heading`."""
    tail = text.split(heading, 1)
    if len(tail) < 2:
        return []
    out, inside = [], False
    for line in tail[1].splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside and line.strip():
            out.append(line.strip())
    return out


class TestExampleCommandsParse(unittest.TestCase):
    def setUp(self):
        self.lines = fenced_lines(SHIPPED.read_text(encoding="utf-8"))

    def test_the_examples_section_actually_has_commands(self):
        """A guard that cannot fail is a defect; this is what arms the next one."""
        self.assertTrue(any(ln.startswith("codex-") for ln in self.lines),
                        "no fenced codex command in Examples -- the drift guard is inert")

    def test_every_codex_command_line_parses_with_the_shims_own_parser(self):
        from token_kit.codex import dispatch, job

        parsers = {"codex-dispatch": dispatch.build_parser(), "codex-job": job.build_parser()}
        checked = 0
        for line in self.lines:
            line = line.split("#")[0].strip()
            argv = shlex.split(line, comments=True)
            if not argv or argv[0] not in parsers:
                continue
            try:
                parsers[argv[0]].parse_args(argv[1:])
            except SystemExit as exc:      # argparse's way of saying "no such flag"
                self.fail(f"the guide ships a command the CLI would refuse:\n"
                          f"  {line}\n  argparse exited {exc.code}")
            checked += 1
        self.assertGreaterEqual(checked, 4, "too few commands checked to call this a guard")

    def test_the_guard_notices_the_exact_drift_that_shipped_before(self):
        from token_kit.codex import dispatch

        with contextlib.redirect_stderr(io.StringIO()):   # argparse's usage spew
            with self.assertRaises(SystemExit):
                dispatch.build_parser().parse_args(shlex.split(
                    "--model gpt-5.6-luna --effort high --message 'one sub-step'"))


def all_fenced(text: str) -> list[str]:
    """Every line inside a ``` fence, anywhere in the file.

    The Examples guard above starts at one heading. The working-folder commands
    are NOT in Examples, and a command line that could not run is exactly as
    wrong there: prose is not executed, so it is parsed here instead.
    """
    out, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside and line.strip():
            out.append(line.strip())
    return out


class TestTaskCommandLinesParse(unittest.TestCase):
    """Legacy matrix examples remain executable through the explicit namespace."""

    DOCS = (KIT_DIR / "agent_trigger_matrix.md",)

    def lines(self):
        out = []
        for doc in self.DOCS:
            out += [ln for ln in all_fenced(doc.read_text(encoding="utf-8"))
                    if ln.split("#")[0].strip().startswith("token-kit legacy task")]
        return out

    def test_the_docs_actually_ship_task_commands(self):
        """Arms the next test: a guard with nothing to check is inert."""
        self.assertGreaterEqual(len(self.lines()), 3,
                                "no fenced legacy task command in the matrix")

    def test_every_task_command_line_parses(self):
        from token_kit import task as task_mod

        parser = task_mod.build_parser()
        for line in self.lines():
            argv = shlex.split(line.split("#")[0].strip(), comments=True)
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(argv[3:])
                except SystemExit as exc:
                    self.fail(f"the docs ship a command the CLI would refuse:\n"
                              f"  {line}\n  argparse exited {exc.code}")

    def test_the_guard_notices_a_flag_that_does_not_exist(self):
        from token_kit import task as task_mod

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                task_mod.build_parser().parse_args(
                    shlex.split("new 'a title' --titled-by-hand"))


class TestSharedReadmeCommands(unittest.TestCase):
    def test_workflow_examples_use_the_real_parser(self):
        from token_kit.workflow import build_parser

        parser = build_parser()
        checked = 0
        for line in all_fenced((KIT_DIR / "README.md").read_text()):
            argv = shlex.split(line, comments=True)
            if not argv or argv[0] != "token-kit":
                continue
            if argv[1] in ("--help", "install", "uninstall"):
                continue
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(argv[1:])
                except SystemExit as exc:
                    self.fail(f"Invalid README command: {line}: exit {exc.code}")
            checked += 1
        self.assertGreaterEqual(checked, 1, "quickstart must contain an executable workflow command")

    def test_readme_stays_a_short_quickstart(self):
        self.assertLessEqual(len((KIT_DIR / "README.md").read_text().split()), 250)

    def test_readme_explains_resume_layout(self):
        text = (KIT_DIR / "README.md").read_text()
        for entry in ("agents/<id>/", "in.md", "STATE.md", "out.md", "checkpoints/", "assignments/"):
            self.assertIn(entry, text)
        self.assertIn("committed checkpoints, not transcripts", text)

    def test_fable_is_visible_as_an_explicit_override(self):
        text = SHIPPED.read_text().split("## Explicit user overrides", 1)[1].split("## Call budget", 1)[0]
        self.assertIn('"Have Fable red-team this" | `claude:fable:medium`', text)
        self.assertIn('"Opus while we have it" | `claude:opus:medium`', text)


class TestInjection(unittest.TestCase):
    def test_the_injection_is_the_document_itself(self):
        m = matrix_mod.load(SHIPPED)
        text = hook_mod.guide_for_injection(m)
        self.assertIn("| kind | use when | who does it | prefer | done when |", text)
        self.assertIn("Never spawn a model to wait", text)
        self.assertIn("| warn |", text)

    def test_a_small_guide_is_injected_whole(self):
        m = matrix_mod.load(FIXTURE_MATRIX)
        self.assertLessEqual(matrix_mod.approx_tokens(m.text), hook_mod.GUIDE_TOKEN_BUDGET,
                             "the fixture is the SMALL case; keep it small")
        self.assertIn(matrix_mod.EXAMPLES_HEADING, hook_mod.guide_for_injection(m))

    def test_a_large_guide_drops_Examples_and_says_where_they_are(self):
        m = matrix_mod.load(SHIPPED)
        whole = matrix_mod.guide_text(m, with_examples=True)
        trimmed = matrix_mod.guide_text(m, with_examples=False)
        self.assertGreater(matrix_mod.approx_tokens(whole), hook_mod.GUIDE_TOKEN_BUDGET,
                           "if the shipped guide ever fits, this test must be re-decided")
        self.assertEqual(hook_mod.guide_for_injection(m), trimmed)
        self.assertNotIn("codex-dispatch --model", trimmed)
        self.assertIn("Examples", trimmed)
        self.assertIn(str(SHIPPED), trimmed, "the pointer must name the file")
        self.assertIn("| kind | use when |", trimmed, "the chart always rides along")

    def test_changing_a_row_changes_the_injection(self):
        """The guide cannot go stale: it IS the file, not a copy of it."""
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "m.md"
            p.write_text(FIXTURE_MATRIX.read_text(encoding="utf-8").replace(
                "| write-doc |", "| CHANGED-BY-THE-GUARD |"), encoding="utf-8")
            m = matrix_mod.load(p)
            self.assertIn("CHANGED-BY-THE-GUARD", hook_mod.guide_for_injection(m))
            self.assertNotIn("| write-doc |", hook_mod.guide_for_injection(m))


if __name__ == "__main__":
    unittest.main()
