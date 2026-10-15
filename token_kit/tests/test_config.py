"""Guard for the resolved machine facts.

There are no profile files and no --profile flag, so the thing that has to be
right is DETECTION. It is tested against fake mounts tables rather than against
this machine, because a guard that only passes where it was written proves
nothing about the next machine.

Cases:
  1 a home on a network filesystem => codex needs a node-local home
  2 a home on local disk => it does not
  3 the LONGEST matching mount point wins (mount points nest, and "/" always
    matches, so a shorter match would answer for the whole machine)
  4 no mounts table at all (not Linux) => local disk, never a crash
  5 an absent override file is fine, and every value still resolves
  6 the override file WINS, and says so in the origins
  7 an unknown table is carried through untouched, for the lane that owns it
  8 the detector can FAIL: case 1's assertion run against a scan that only
    looks at the first mount line does not hold
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import config as C  # noqa: E402

NETWORK_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /tmp tmpfs rw 0 0
fileserver:/export/home {home} nfs4 rw,relatime 0 0
"""

LOCAL_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /tmp tmpfs rw 0 0
/dev/sda2 {home} ext4 rw,relatime 0 0
"""


class Detection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home" / "someone"
        self.home.mkdir(parents=True)
        self.mounts = self.root / "mounts"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, template):
        self.mounts.write_text(template.format(home=self.home))
        return self.mounts

    # 1
    def test_network_home_needs_a_node_local_codex_home(self):
        self.assertTrue(C.needs_node_local_codex_home(self.write(NETWORK_MOUNTS),
                                                      home=self.home))

    # 2
    def test_local_home_does_not(self):
        self.assertFalse(C.needs_node_local_codex_home(self.write(LOCAL_MOUNTS),
                                                       home=self.home))

    # 3
    def test_the_longest_mount_point_wins(self):
        self.write(NETWORK_MOUNTS)
        self.assertEqual("nfs4", C.fstype_of(self.home, self.mounts))
        self.assertEqual("ext4", C.fstype_of("/", self.mounts))
        self.assertEqual("nfs4", C.fstype_of(self.home / "deeper", self.mounts))

    # 4
    def test_no_mounts_table_is_local_disk_not_a_crash(self):
        absent = self.root / "nope"
        self.assertEqual([], C.read_mounts(absent))
        self.assertFalse(C.needs_node_local_codex_home(absent, home=self.home))

    # 8 -- the guard can fail: a scan that stops at the first mount line
    #      reports the root filesystem for the home directory.
    def test_the_detector_would_fail_if_it_took_the_first_match(self):
        self.write(NETWORK_MOUNTS)
        first = C.read_mounts(self.mounts)[0][1]
        self.assertNotEqual(first, C.fstype_of(self.home, self.mounts),
                            "taking the first matching mount would have passed "
                            "case 1 for the wrong reason")


class Override(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mounts = self.root / "mounts"
        self.mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")

    def tearDown(self):
        self.tmp.cleanup()

    # 5
    def test_absent_override_is_fine(self):
        res = C.resolve(self.root / "no-such-file.toml", self.mounts)
        self.assertEqual({}, res.raw)
        self.assertEqual("codex", res.codex_launcher)
        self.assertEqual("default", res.origins["codex_launcher"])
        self.assertEqual("detected", res.origins["codex_node_local_home"])
        self.assertTrue(str(res.state_dir).endswith("token_kit"))

    # 6
    def test_the_override_file_wins_and_says_so(self):
        src = self.root / "config.toml"
        src.write_text('[codex]\nnode_local_home = true\nlauncher = "my-codex"\n'
                       '[paths]\nstate_dir = "~/elsewhere"\n')
        res = C.resolve(src, self.mounts)
        self.assertTrue(res.codex_node_local_home)
        self.assertEqual("override", res.origins["codex_node_local_home"])
        self.assertEqual("my-codex", res.codex_launcher)
        self.assertEqual("override", res.origins["state_dir"])
        self.assertIn("describe", dir(C))
        self.assertIn("[override]", C.describe(res))

    # 7
    def test_an_unknown_table_is_carried_through_untouched(self):
        src = self.root / "config.toml"
        src.write_text('[router]\nquota_cooldown_secs = 42\nmax_codex_children = 3\n')
        res = C.resolve(src, self.mounts)
        self.assertEqual({"quota_cooldown_secs": 42, "max_codex_children": 3},
                         res.table("router"))
        self.assertEqual({}, res.table("nothing_here"))

    def test_a_broken_override_refuses_loudly(self):
        src = self.root / "config.toml"
        src.write_text("[codex\n")
        with self.assertRaises(SystemExit):
            C.resolve(src, self.mounts)


if __name__ == "__main__":
    unittest.main()
