"""Shell setup uses disposable homes, never the user's startup files."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1]


class ShellSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "checkout with spaces ' and $sign"
        self.checkout.mkdir()
        self.script = self.checkout / "add_to_bashrc.bash"
        shutil.copyfile(KIT / "add_to_bashrc.bash", self.script)
        self.bin = self.checkout / "src/token_kit/bin"
        self.bin.mkdir(parents=True)
        launcher = self.bin / "token-kit"
        launcher.write_text("#!/bin/bash\nprintf 'launcher-ok\\n'\n")
        launcher.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.env.pop("BASH_ENV", None)

    def shell(self, command):
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", command,
                               "test", str(self.script)], env=self.env,
                              text=True, capture_output=True, check=True)

    def test_source_updates_current_shell_and_preserves_existing_rc(self):
        rc = self.home / ".bashrc"
        rc.write_text("# Existing settings without final newline")
        result = self.shell('source "$1"; token-kit')
        self.assertIn("launcher-ok", result.stdout)
        self.assertTrue(rc.read_text().startswith("# Existing settings without final newline\n"))
        self.assertEqual(self.shell('source "$HOME/.bashrc"; command -v token-kit').stdout.strip(),
                         str(self.bin / "token-kit"))

    def test_repeated_source_does_not_duplicate_rc_or_path(self):
        self.shell('source "$1"')
        before = (self.home / ".bashrc").read_bytes()
        result = self.shell('source "$1" >/dev/null; source "$1" >/dev/null; printf "%s" "$PATH"')
        self.assertEqual((self.home / ".bashrc").read_bytes(), before)
        self.assertEqual(result.stdout.split(":"), [str(self.bin), "/usr/bin", "/bin"])

    def test_executed_script_sets_up_future_shell(self):
        result = self.shell('bash "$1"')
        self.assertIn("new Bash terminal", result.stdout)
        self.assertIn("launcher-ok", self.shell('source "$HOME/.bashrc"; token-kit').stdout)

    def test_missing_launcher_fails_without_writing_rc(self):
        (self.bin / "token-kit").unlink()
        result = self.shell('if source "$1"; then exit 9; fi; printf "still-alive"')
        self.assertIn("still-alive", result.stdout)
        self.assertFalse((self.home / ".bashrc").exists())


if __name__ == "__main__":
    unittest.main()
