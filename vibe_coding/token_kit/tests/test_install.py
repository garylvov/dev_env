"""Guard for the installer. Runs the REAL install against a scratch HOME.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

Cases, in the order they appear below:
  1 fresh install ends PASS, reports changes, writes a manifest
  2 a second run is a no-op and says NO CHANGES
  3 a pre-existing settings.json keeps its keys, its key ORDER, and its
    unrelated hook entry; our hook appears exactly once even on a re-run;
    a dated backup exists
  4 a real (non-symlink) agent file is a CONFLICT and its bytes do not change
  5 uninstall removes every symlink and restores settings.json
  6 the SAME assertions as case 3, run against a deliberately broken merge,
    FAIL -- so this guard is known to be capable of failing
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import cli  # noqa: E402
from token_kit.settings_merge import merge, unmerge  # noqa: E402

CLI = KIT_DIR / "src" / "token_kit" / "cli.py"

EXISTING_SETTINGS = {
    "model": "opusplan",
    "alwaysThinkingEnabled": True,
    "env": {"MY_OWN_VAR": "keep-me"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "/somewhere/block_irreversible.py"}]}
        ]
    },
    "subagentPromptCacheTtl": "5m",
}


def run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, env=env, cwd=str(KIT_DIR))


class ScratchHome(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="token_kit_guard_"))
        self.home = self.tmp / "home"
        (self.home / ".claude" / "agents").mkdir(parents=True)
        (self.home / ".config").mkdir(parents=True)
        self.settings = self.home / ".claude" / "settings.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, *args):
        p = run_cli(self.home, "install", "--profile", "workstation", *args)
        self.assertNotIn("Traceback", p.stderr, msg=p.stderr)
        return p

    def read_settings(self):
        return json.loads(self.settings.read_text())


class TestFreshAndIdempotent(ScratchHome):
    def test_1_fresh_install(self):
        p = self.install()
        self.assertIn("install = PASS", p.stdout, msg=p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"installed: \d+ change")
        self.assertTrue((self.home / ".config/token_kit/manifest.tsv").is_file())

    def test_2_rerun_is_a_noop(self):
        self.install()
        p = self.install()
        changes = [l for l in p.stdout.splitlines() if l.startswith("  CHANGE")]
        self.assertEqual(changes, [], msg="re-run changed things:\n" + "\n".join(changes))
        self.assertIn("NO CHANGES", p.stdout)
        self.assertIn("install = PASS", p.stdout)


class TestPreservesExistingSettings(ScratchHome):
    def setUp(self):
        super().setUp()
        self.settings.write_text(json.dumps(EXISTING_SETTINGS, indent=2) + "\n")
        self.before = self.read_settings()

    def assert_preserved(self, after: dict):
        """The assertions case 6 re-runs against a broken merge."""
        for key, value in self.before.items():
            if key == "hooks":
                continue
            self.assertEqual(after.get(key), value,
                             f"pre-existing key {key} was changed")
        before_order = list(self.before)
        self.assertEqual(list(after)[:len(before_order)], before_order,
                         "key order was not preserved")
        bash_before = [e for e in self.before["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
        bash_after = [e for e in after["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
        self.assertEqual(bash_after, bash_before, "the unrelated Bash hook entry was altered")

    def kit_hooks(self, settings: dict):
        return [h for e in settings["hooks"]["PreToolUse"]
                for h in e.get("hooks", []) if "token_kit_hook" in h["command"]]

    def test_3_unrelated_parts_survive(self):
        self.install()
        after = self.read_settings()
        self.assert_preserved(after)
        self.assertEqual(len(self.kit_hooks(after)), 1, "kit hook not registered exactly once")

    def test_3_rerun_does_not_duplicate_the_hook(self):
        self.install()
        self.install()
        self.assertEqual(len(self.kit_hooks(self.read_settings())), 1)

    def test_3_conflicting_key_is_reported_not_overwritten(self):
        p = self.install()
        self.assertIn("CONFLICT key subagentPromptCacheTtl", p.stdout)
        self.assertEqual(self.read_settings()["subagentPromptCacheTtl"], "5m")

    def test_3_dated_backup_exists(self):
        self.install()
        backups = list((self.home / ".claude").glob("settings.json.token_kit-backup-*"))
        self.assertEqual(len(backups), 1, f"expected one dated backup, got {backups}")
        self.assertEqual(json.loads(backups[0].read_text()), self.before)

    def test_5_uninstall_restores(self):
        self.install()
        p = run_cli(self.home, "uninstall")
        self.assertNotIn("Traceback", p.stderr, msg=p.stderr)
        self.assertEqual(list((self.home / ".claude/agents").iterdir()), [],
                         "agent symlinks left behind")
        self.assertFalse((self.home / ".config/token_kit/bin").exists(),
                         "bin dir left behind")
        self.assertEqual(self.read_settings(), self.before,
                         "settings.json was not restored to its pre-install state")

    def test_7_every_event_is_registered_once_and_removed(self):
        """The four events, and the symmetry: uninstall leaves none behind.

        The defect this catches is specific: `manifest.hook_command()` used to
        return the LAST hook row, so an uninstall removed one event and left
        the other three pointing at a command that no longer exists.
        """
        self.install()
        after = self.read_settings()
        hooks = after["hooks"]
        shim = str(self.home / ".config/token_kit/bin/token_kit_hook.sh")
        reader = str(self.home / ".config/token_kit/bin/respawn-reader")
        for event, command in (("PreToolUse", shim), ("SessionStart", shim),
                               ("PostToolUse", reader), ("Stop", reader)):
            found = [h for e in hooks.get(event, [])
                     for h in e.get("hooks", []) if h["command"] == command]
            self.assertEqual(len(found), 1,
                             f"{event} -> {command}: registered {len(found)} time(s)")
        # A Stop entry carries NO matcher key at all.
        stop_entry = [e for e in hooks["Stop"] if any(
            h["command"] == reader for h in e.get("hooks", []))][0]
        self.assertNotIn("matcher", stop_entry,
                         "a Stop entry must not carry a matcher key")

        run_cli(self.home, "uninstall")
        self.assertEqual(self.read_settings(), self.before,
                         "an event was left behind by uninstall")


class TestConflictFileUntouched(ScratchHome):
    def test_4_real_agent_file_is_never_overwritten(self):
        mine = self.home / ".claude/agents/architect.md"
        mine.write_text("MY OWN ARCHITECT PROMPT -- DO NOT TOUCH\n")
        p = self.install()
        self.assertEqual(mine.read_text(), "MY OWN ARCHITECT PROMPT -- DO NOT TOUCH\n")
        self.assertIn("CONFLICT architect.md", p.stdout)
        # the conflict must not stop the rest of the install
        self.assertTrue((self.home / ".claude/agents/codex-log-read.md").is_symlink())


class TestEveryComponentLands(ScratchHome):
    """One assertion per component in cli.COMPONENTS: it is installed, it is
    reachable, and uninstall takes it away again. A component with no check
    here is a capability nobody reads."""

    def test_8_executables_are_linked_and_answer_help(self):
        self.install()
        bin_dir = self.home / ".config/token_kit/bin"
        for name, _rel in cli.EXECUTABLES:
            p = bin_dir / name
            self.assertTrue(p.is_symlink(), f"{name} is not a symlink")
            self.assertTrue(p.resolve().is_file(), f"{name} does not resolve")
            self.assertTrue(os.access(p, os.X_OK), f"{name} is not executable")

    def test_8b_a_linked_shim_resolves_its_own_clone(self):
        """A shim linked into bin/ must find the kit through its LINK TARGET.

        `dirname $BASH_SOURCE/../../..` from the link's own directory points at
        ~/.config, not at the clone, so the import fails with a message nobody
        reads. The shims resolve themselves first; this is that guard.
        """
        self.install()
        shim = self.home / ".config/token_kit/bin/respawn-reader"
        p = subprocess.run([str(shim)], input="", capture_output=True, text=True,
                           env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("No module named", p.stderr)

    def test_9_generated_row_agents_land_one_per_candidate(self):
        self.install()
        agents = self.home / ".claude/agents"
        generated = sorted(p for p in agents.glob("row-*.md") if p.is_symlink())
        self.assertTrue(generated, "no generated row agent was installed")
        for p in generated:
            head = p.read_text().splitlines()
            self.assertEqual(head[0], "---")
            self.assertTrue(any(l.startswith("effort: ") for l in head[:8]),
                            f"{p.name} carries no effort: frontmatter key")
            self.assertTrue(any(l.startswith("model: ") for l in head[:8]),
                            f"{p.name} carries no model: frontmatter key")

    def test_9b_uninstall_removes_the_generated_files_too(self):
        self.install()
        run_cli(self.home, "uninstall")
        left = list((self.home / ".claude/agents").glob("row-*.md"))
        self.assertEqual(left, [], "generated row agent links left behind")
        self.assertEqual(sorted((cli.KIT_DIR / "agents" / "generated").glob("row-*.md")), [],
                         "generated row agent files left in the clone")

    def test_10_the_installed_matrix_validates(self):
        p = self.install()
        self.assertNotIn("CONFLICT matrix:", p.stdout,
                         "the table the installer links is not internally sound")
        self.assertTrue((self.home / ".config/token_kit/agent_trigger_matrix.toml").is_symlink())


class TestAdoptPersonas(ScratchHome):
    """--adopt-personas: a real file is MOVED aside, never deleted, and comes
    back where it was on uninstall. Off by default."""

    def setUp(self):
        super().setUp()
        self.mine = self.home / ".claude/agents/architect.md"
        self.mine.write_text("MY OWN ARCHITECT PROMPT\n")
        self.aside = self.home / ".claude/agents" / cli.ADOPT_DIR / "architect.md"

    def test_11_off_by_default_the_file_is_a_conflict(self):
        p = self.install()
        self.assertIn("CONFLICT architect.md", p.stdout)
        self.assertEqual(self.mine.read_text(), "MY OWN ARCHITECT PROMPT\n")
        self.assertFalse(self.aside.exists(), "nothing may move without the flag")

    def test_12_adopt_moves_aside_and_links(self):
        p = self.install("--adopt-personas")
        self.assertNotIn("CONFLICT architect.md", p.stdout)
        self.assertTrue(self.aside.is_file(), "the operator's file was not preserved")
        self.assertEqual(self.aside.read_text(), "MY OWN ARCHITECT PROMPT\n",
                         "the operator's bytes changed")
        self.assertTrue(self.mine.is_symlink(), "the kit's persona was not linked")
        self.assertEqual(Path(os.readlink(self.mine)), cli.KIT_DIR / "agents" / "architect.md")

    def test_13_uninstall_puts_it_back(self):
        self.install("--adopt-personas")
        run_cli(self.home, "uninstall")
        self.assertFalse(self.mine.is_symlink())
        self.assertEqual(self.mine.read_text(), "MY OWN ARCHITECT PROMPT\n",
                         "the adopted file did not come back")

    def test_14_dry_run_moves_nothing(self):
        p = self.install("--adopt-personas", "--dry-run")
        self.assertIn("would    adopt architect.md", p.stdout)
        self.assertFalse(self.aside.exists())
        self.assertEqual(self.mine.read_text(), "MY OWN ARCHITECT PROMPT\n")


class TestMergeUnit(unittest.TestCase):
    """Case 6: the guard shown to FAIL, then to pass again.

    `broken_merge` is the obvious wrong implementation -- assign every key,
    replace the hook list. The very assertions the installer's guard makes are
    run against it and must fail; the real merge must then pass them.
    """

    HOOK = "/home/me/.config/token_kit/bin/token_kit_hook.sh"

    @staticmethod
    def broken_merge(settings, add_keys, hook_command, matcher, event="PreToolUse"):
        out = copy.deepcopy(settings)
        for key, want in add_keys.items():
            out[key] = want                                   # clobbers a deliberate choice
        out.setdefault("hooks", {})[event] = [                # drops every unrelated entry
            {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command}]}]
        return out, None

    def check(self, after):
        before = EXISTING_SETTINGS
        self.assertEqual(after["subagentPromptCacheTtl"], before["subagentPromptCacheTtl"])
        self.assertEqual(list(after)[:len(before)], list(before))
        self.assertIn({"matcher": "Bash",
                       "hooks": [{"type": "command",
                                  "command": "/somewhere/block_irreversible.py"}]},
                      after["hooks"]["PreToolUse"])

    def test_6a_broken_merge_fails_the_guard(self):
        after, _ = self.broken_merge(EXISTING_SETTINGS, {"subagentPromptCacheTtl": "1h"},
                                     self.HOOK, "*")
        with self.assertRaises(AssertionError):
            self.check(after)

    def test_6b_real_merge_passes_the_guard(self):
        after, rep = merge(EXISTING_SETTINGS, {"subagentPromptCacheTtl": "1h"}, self.HOOK, "*")
        self.check(after)
        self.assertTrue(rep.hook_added)
        self.assertEqual(rep.keys_added, [])
        self.assertEqual(rep.key_conflicts, [("subagentPromptCacheTtl", "5m", "1h")])

    def test_6c_merge_then_unmerge_is_identity(self):
        after, rep = merge(EXISTING_SETTINGS, {"newKey": 1}, self.HOOK, "*")
        back = unmerge(after, rep.keys_added, self.HOOK)
        self.assertEqual(back, EXISTING_SETTINGS)

    def test_6d_matcher_collision_extends_instead_of_replacing(self):
        after, _ = merge(EXISTING_SETTINGS, {}, self.HOOK, "Bash")
        entries = [e for e in after["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
        self.assertEqual(len(entries), 1)
        self.assertEqual([h["command"] for h in entries[0]["hooks"]],
                         ["/somewhere/block_irreversible.py", self.HOOK])

    def test_6e_settings_with_no_hooks_at_all_comes_back_clean(self):
        plain = {"model": "opusplan"}
        after, rep = merge(plain, {}, self.HOOK, "*")
        self.assertEqual(unmerge(after, rep.keys_added, self.HOOK), plain)


if __name__ == "__main__":
    unittest.main()
