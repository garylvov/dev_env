"""The live-path census, as a guard rather than as a report.

The kit is published to a PUBLIC repo. A cluster path, a username, a hostname
or a job id in a module that gets INSTALLED is therefore a defect, not a
worklist item: every machine fact belongs in a profile TOML that
`token_kit.profiles` reads.

Three cases:
  1 the installed modules carry zero violations (the number that must be 0);
  2 the detector can FAIL -- a planted line is caught, kind by kind, so case 1
    is not passing because the scanner is blind;
  3 the detector does not match its own pattern file (the self-match trap: the
    patterns used to live in cli.py, where they censused themselves).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import cli  # noqa: E402

PLANTED = {
    "oscar_path": "ROOT = '/oscar/data/somewhere/someone'",
    "home_path": "HOME = '/users/someone'",
    "login_host": "HOST = 'login009'",
    "node_name": "NODE = 'gpu3204'",
    "username": "USER = 'glvov'",
    "slurm_call": "subprocess.run(['squeue', '-u', 'me'])",
    "email": "OWNER = 'someone@example.edu'",
}


class TestModulesAreClean(unittest.TestCase):
    def test_1_installed_modules_have_no_live_paths(self):
        violations = cli.census_modules()
        self.assertEqual(
            [], violations,
            "a module that gets installed carries a machine fact:\n" +
            "\n".join(f"  {k:<12} {f}:{n}  {t}" for k, f, n, t in violations))

    def test_1b_the_scan_actually_looked_at_the_modules(self):
        names = {p.name for p in cli.module_files()}
        for expected in ("cli.py", "profiles.py", "hook.py", "reader.py",
                         "main.py", "dispatch.py", "job.py", "install.sh"):
            self.assertIn(expected, names, f"{expected} was not censused")

    def test_2_the_detector_can_fail(self):
        patterns = cli.census_patterns()
        with tempfile.TemporaryDirectory() as tmp:
            for kind, line in PLANTED.items():
                planted = Path(tmp) / f"{kind}.py"
                planted.write_text(f"# harmless\n{line}\n")
                rows = cli.scan([planted], patterns)
                self.assertTrue(any(r[0] == kind for r in rows),
                                f"the census did not catch a planted {kind}: {line}")

    def test_3_the_pattern_file_is_not_censused_by_itself(self):
        self.assertNotIn(cli.CENSUS_PATTERNS_FILE, cli.module_files())
        rows = cli.scan([cli.CENSUS_PATTERNS_FILE], cli.census_patterns())
        self.assertTrue(rows, "the pattern file DOES match its own patterns -- "
                              "that is exactly why it must be excluded")


if __name__ == "__main__":
    unittest.main()
