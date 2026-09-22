"""Project installer ownership and non-destructive merge regressions."""

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.project_install import configure, InstallConflict, MANIFEST
from token_kit import project_install


class ProjectInstallTests(unittest.TestCase):
    def test_role_overrides_are_installed_for_both_clients(self):
        configure(self.project, engine="both")
        text = (self.project / "AGENTS.md").read_text()
        for phrase in ("Explicit user task/role model requests override routing defaults",
                       "Opus for the big stuff", "Luna for run loops", "Sol for run loops",
                       "Keep simultaneous role choices separate", "ask before substituting"):
            self.assertIn(phrase, text)
        self.assertIn("@AGENTS.md", (self.project / "CLAUDE.md").read_text())
        self.assertIn("Opus medium -> Astra medium", text)
        self.assertIn("Sonnet medium -> Luna high", text)
        self.assertIn("excludes every Claude candidate", text)
        self.assertIn("Routine execution loops/job coordination/docs: Sol medium -> Luna high -> Sonnet medium", text)
        self.assertIn("Choose the\ntier by the actual assignment, not hierarchy depth", text)
        self.assertNotIn("Prefer Astra (medium) for planning", text)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def write(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read(self, name):
        return (self.project / name).read_text()

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob("*") if p.is_file()}

    def test_both_clients_idempotent_and_partial_uninstall(self):
        self.write("AGENTS.md", "User rules\n")
        self.write("CLAUDE.md", "User Claude rules\n")
        self.write(".codex/config.toml", 'model = "existing"\n[mcp_servers.other]\ncommand = "other"\n')
        self.write(".mcp.json", '{"extra": true, "mcpServers": {"other": {"command": "other"}}}')
        configure(self.project, codegraph=True)
        first = self.snapshot()
        self.assertEqual(configure(self.project, codegraph=True), [])
        self.assertEqual(first, self.snapshot())
        self.assertEqual(self.read("CLAUDE.md").count("@AGENTS.md"), 1)
        codex = tomllib.loads(self.read(".codex/config.toml"))
        self.assertEqual(codex["mcp_servers"]["codegraph"]["args"], ["serve", "--mcp"])
        self.assertEqual(codex["model"], "existing")
        configure(self.project, engine="claude", uninstall=True)
        self.assertEqual(self.read("CLAUDE.md"), "User Claude rules\n")
        self.assertIn("token-kit --help", self.read("AGENTS.md"))
        self.assertEqual(json.loads(self.read(".mcp.json")), {
            "extra": True, "mcpServers": {"other": {"command": "other"}}})
        configure(self.project, engine="codex", uninstall=True)
        self.assertEqual(self.read("AGENTS.md"), "User rules\n")
        self.assertEqual(self.read(".codex/config.toml"), 'model = "existing"\n[mcp_servers.other]\ncommand = "other"\n')
        self.assertEqual(configure(self.project, uninstall=True), [])

    def test_dry_run_no_files_or_directories(self):
        self.assertTrue(configure(self.project, codegraph=True, dry_run=True))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_known_owned_legacy_instructions_upgrade(self):
        for legacy in project_install._LEGACY_INSTRUCTION_VERSIONS:
            with self.subTest(legacy=legacy.splitlines()[1]), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                agents = project / "AGENTS.md"
                agents.write_text("User instructions\n")
                with patch.object(project_install, "INSTRUCTIONS", legacy):
                    configure(project, codegraph=True)
                agents.write_text(agents.read_text() + "Appended user instructions\n")
                before = {str(p.relative_to(project)): p.read_bytes()
                          for p in project.rglob("*") if p.is_file()}
                self.assertEqual(set(configure(project, dry_run=True)), {"AGENTS.md", MANIFEST})
                self.assertEqual(before, {str(p.relative_to(project)): p.read_bytes()
                                          for p in project.rglob("*") if p.is_file()})
                self.assertEqual(set(configure(project)), {"AGENTS.md", MANIFEST})
                text = agents.read_text()
                self.assertTrue(text.startswith("User instructions\n"))
                self.assertTrue(text.endswith("Appended user instructions\n"))
                self.assertNotIn("token-kit-workflow", text)
                self.assertIn("checkpoint, resume, launch", text)
                self.assertEqual(configure(project, codegraph=True), [])
                for relative, content in before.items():
                    if relative not in {"AGENTS.md", MANIFEST}:
                        self.assertEqual((project / relative).read_bytes(), content)

    def test_edited_legacy_instructions_refuse_upgrade(self):
        with patch.object(project_install, "INSTRUCTIONS", project_install._LEGACY_INSTRUCTIONS):
            configure(self.project)
        self.write("AGENTS.md", self.read("AGENTS.md").replace("meaningful", "custom"))
        before = self.snapshot()
        with self.assertRaises(InstallConflict):
            configure(self.project)
        self.assertEqual(before, self.snapshot())

    def test_existing_claude_import_preserved(self):
        self.write("CLAUDE.md", "Instructions\n@./AGENTS.md\n")
        configure(self.project)
        self.assertEqual(self.read("CLAUDE.md"), "Instructions\n@./AGENTS.md\n")
        configure(self.project, uninstall=True)
        self.assertEqual(self.read("CLAUDE.md"), "Instructions\n@./AGENTS.md\n")

    def test_owned_text_edit_blocks_install_and_uninstall(self):
        configure(self.project)
        self.write("AGENTS.md", self.read("AGENTS.md").replace("meaningful", "custom"))
        before = self.snapshot()
        for uninstall in [False, True]:
            with self.assertRaises(InstallConflict):
                configure(self.project, uninstall=uninstall, codegraph=True)
            self.assertEqual(before, self.snapshot())

    def test_owned_json_edit_blocks_uninstall(self):
        configure(self.project, codegraph=True)
        value = json.loads(self.read(".mcp.json"))
        value["mcpServers"]["codegraph"]["command"] = "my-codegraph"
        self.write(".mcp.json", json.dumps(value))
        before = self.snapshot()
        with self.assertRaises(InstallConflict):
            configure(self.project, uninstall=True)
        self.assertEqual(before, self.snapshot())

    def test_unrelated_new_edits_survive_uninstall(self):
        configure(self.project, codegraph=True)
        self.write("AGENTS.md", self.read("AGENTS.md") + "New user instruction\n")
        value = json.loads(self.read(".mcp.json"))
        value["newSetting"] = "preserved"
        self.write(".mcp.json", json.dumps(value))
        configure(self.project, uninstall=True)
        self.assertEqual(self.read("AGENTS.md"), "New user instruction\n")
        self.assertEqual(json.loads(self.read(".mcp.json")), {"newSetting": "preserved"})

    def test_unmanaged_codegraph_conflict_is_preflighted(self):
        self.write(".mcp.json", '{"mcpServers":{"codegraph":{"command":"mine"}}}')
        before = self.snapshot()
        with self.assertRaises(InstallConflict):
            configure(self.project, codegraph=True)
        self.assertEqual(before, self.snapshot())

    def test_quoted_toml_unmanaged_entry_conflict(self):
        self.write(".codex/config.toml", '[mcp_servers."codegraph"]\ncommand = "mine"\n')
        with self.assertRaises(InstallConflict):
            configure(self.project, codegraph=True)
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_symlink_refused(self):
        self.write("user.md", "User")
        (self.project / "AGENTS.md").symlink_to(self.project / "user.md")
        with self.assertRaises(InstallConflict):
            configure(self.project)
        self.assertEqual(self.read("user.md"), "User")

    def test_installs_additive(self):
        configure(self.project, engine="codex")
        self.assertFalse((self.project / "CLAUDE.md").exists())
        configure(self.project, engine="claude")
        self.assertEqual(json.loads(self.read(MANIFEST))["engines"], ["claude", "codex"])

    def test_existing_text_line_endings_roundtrip(self):
        original = b"User rules\r\nKeep this formatting\r\n"
        path = self.project / "AGENTS.md"
        path.write_bytes(original)
        configure(self.project)
        self.assertTrue(path.read_bytes().startswith(original))
        configure(self.project, uninstall=True)
        self.assertEqual(path.read_bytes(), original)

    def test_concurrent_installs_preserve_both_engine_registrations(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        processes = []
        for engine in ["claude", "codex"] * 4:
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "token_kit.project_install", "install",
                 "--project", str(self.project), "--engine", engine, "--codegraph"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(json.loads(self.read(MANIFEST))["engines"], ["claude", "codex"])
        self.assertEqual(configure(self.project, codegraph=True), [])


if __name__ == "__main__":
    unittest.main()
