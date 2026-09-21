"""The live-path census, as a guard rather than as a report.

The kit is published to a PUBLIC repo and runs from anywhere. A machine path,
a user name, a host name or a job id in a module that gets INSTALLED is
therefore a defect (machine facts are DETECTED at run time), and so is the name
of a project, a repository or a document tree ANYWHERE in the kit.

Cases:
  1 the installed modules carry zero machine facts (the number that must be 0);
  2 no file in the kit names a project (the other number that must be 0);
  3 the detector can FAIL -- a planted line is caught, kind by kind, so cases 1
    and 2 are not passing because the scanner is blind;
  4 the detector does not match its own pattern file (the self-match trap: the
    patterns used to live in cli.py, where they censused themselves);
  5 the word scan really did look at the kit's own files.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import cli  # noqa: E402

import getpass  # noqa: E402

PLANTED = {
    "abs_site_path": "ROOT = '/somefs/data/group/person/tree'",
    "home_path": "HOME = '/users/someone'",
    "host_name": "HOST = 'login009'",
    "scheduler_call": "subprocess.run(['s" + "queue', '-u', 'me'])",
    "email": "OWNER = 'someone@example.edu'",
    "job_id": "JOB = 'jobid 4863416'",
    "username": f"USER = {getpass.getuser()!r}",
}

#: Project words, written in halves so that this guard does not become the
#: thing it guards against. Each pair joins to one banned word.
PLANTED_WORDS = [("agres", "cap"), ("st", "ellex"), ("o", "scar"),
                 ("imp", "rint"), ("w", "bc"), ("ret", "read"),
                 ("proto", "motions"), ("m", "ega")]


class TestModulesAreClean(unittest.TestCase):
    def test_1_installed_modules_have_no_live_paths(self):
        violations = cli.census_modules()
        self.assertEqual(
            [], violations,
            "a module that gets installed carries a machine fact:\n" +
            "\n".join(f"  {k:<12} {f}:{n}  {t}" for k, f, n, t in violations))

    def test_2_no_file_in_the_kit_names_a_project(self):
        words = cli.census_project_words()
        self.assertEqual(
            [], words,
            "a file in the kit names a project:\n" +
            "\n".join(f"  {k:<14} {f}:{n}  {t}" for k, f, n, t in words))

    def test_5_the_word_scan_looked_at_the_kit(self):
        names = {p.name for p in cli.kit_files()}
        for expected in ("cli.py", "config.py", "canary.py", "README.md",
                         "install.sh", "test_census.py"):
            self.assertIn(expected, names, f"{expected} was not word-scanned")

    def test_1b_the_scan_actually_looked_at_the_modules(self):
        names = {p.name for p in cli.module_files()}
        for expected in ("cli.py", "config.py", "canary.py", "reader.py",
                         "main.py", "dispatch.py", "job.py", "install.sh"):
            self.assertIn(expected, names, f"{expected} was not censused")
        # and the router's package is skipped ON PURPOSE: the lane that owns
        # it guards it, and two scans over one file report one defect twice.
        self.assertNotIn("hook.py", names)

    def test_3_the_detector_can_fail(self):
        patterns = cli.census_patterns()
        with tempfile.TemporaryDirectory() as tmp:
            for kind, line in PLANTED.items():
                planted = Path(tmp) / f"{kind}.py"
                planted.write_text(f"# harmless\n{line}\n")
                rows = cli.scan([planted], patterns)
                self.assertTrue(any(r[0] == kind for r in rows),
                                f"the census did not catch a planted {kind}: {line}")
            for head, tail in PLANTED_WORDS:
                planted = Path(tmp) / "word.py"
                planted.write_text(f"# a note about the {head + tail} tree\n")
                rows = cli.scan([planted], patterns)
                self.assertTrue(any(r[0] == "project_word" for r in rows),
                                f"the census did not catch {head + tail}")

    def test_4_the_pattern_file_is_not_censused_by_itself(self):
        self.assertNotIn(cli.CENSUS_PATTERNS_FILE, cli.module_files())
        self.assertNotIn(cli.CENSUS_PATTERNS_FILE, cli.kit_files())
        rows = cli.scan([cli.CENSUS_PATTERNS_FILE], cli.census_patterns())
        self.assertTrue(rows, "the pattern file DOES match its own patterns -- "
                              "that is exactly why it must be excluded")


if __name__ == "__main__":
    unittest.main()
