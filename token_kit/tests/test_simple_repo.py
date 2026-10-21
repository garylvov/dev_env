import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from token_kit import simple_adapters as adapters
from token_kit import simple_codegraph as codegraph
from token_kit import simple_repo_docs as repo_docs
from token_kit.simple_cli import main
from token_kit.simple_types import LaunchOptions, TaskView

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts)


class RepoDocsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "a.py").write_text("x = 1\n")
        (self.repo / "b.py").write_text("y = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        docs = self.repo / "docs/agents"
        docs.mkdir(parents=True)
        (docs / "README.md").write_text("- [run](run.md) -- how to test\n")
        (docs / "run.md").write_text(f"<!-- verified: {self.base} files: a.py -->\nuse uv\n")
        (docs / "other.md").write_text(f"<!-- verified: {self.base} files: b.py -->\nsecret body\n")
        (docs / "unmarked.md").write_text("no marker\n")

    def test_stale_only_when_covered_file_changed(self):
        self.assertEqual(repo_docs.stale_docs(self.repo), ["unmarked.md"])
        (self.repo / "a.py").write_text("x = 2\n")
        git(self.repo, "commit", "-qam", "change a")
        self.assertEqual(repo_docs.stale_docs(self.repo), ["run.md", "unmarked.md"])

    def test_unknown_commit_is_stale(self):
        (self.repo / "docs/agents/run.md").write_text("<!-- verified: deadbeef files: a.py -->\n")
        self.assertIn("run.md", repo_docs.stale_docs(self.repo))

    def test_kickoff_names_index_and_stale_docs_without_contents(self):
        (self.repo / "b.py").write_text("y = 2\n")
        git(self.repo, "commit", "-qam", "change b")
        view = TaskView(self.repo / "task", self.repo, "t", self.repo / "task/in.md",
                        self.repo / "task/STATE.md", self.repo / "task/out.md")
        (self.repo / "task").mkdir()
        with patch.dict(os.environ, {"HOME": str(self.repo)}, clear=False):
            plan = adapters.prepare(view, LaunchOptions(rollover="off"), "go", self.repo / "run")
        guidance = plan.argv[plan.argv.index("--append-system-prompt") + 1]
        self.assertIn(f"Repo docs index: {self.repo.resolve() / 'docs/agents/README.md'}", guidance)
        self.assertIn("verify before trusting): other.md, unmarked.md\n", guidance)
        self.assertNotIn("secret body", guidance)
        self.assertNotIn("run.md,", guidance)

    def test_no_index_no_note_and_no_git(self):
        (self.repo / "docs/agents/README.md").unlink()
        with patch.object(repo_docs.subprocess, "run") as run:
            self.assertEqual(repo_docs.kickoff_note(self.repo), "")
        run.assert_not_called()


class CodegraphTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        (self.ws / ".git").mkdir()
        self.view = TaskView(self.root / "task", self.ws, "t", self.root / "task/in.md",
                             self.root / "task/STATE.md", self.root / "task/out.md")
        (self.root / "task").mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def index(self, root):
        (root / ".codegraph").mkdir(parents=True, exist_ok=True)
        (root / ".codegraph/codegraph.db").write_bytes(b"db")

    def prepare(self, **kwargs):
        return adapters.prepare(self.view, LaunchOptions(rollover="off", **kwargs), "go", self.root / "run")

    def ok(self, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_auto_attach_claude_mcp_config(self):
        self.index(self.ws)
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=self.ok) as run:
            plan = self.prepare()
        self.assertEqual(run.call_args_list[0].args[0], ["/x/codegraph", "sync", "--quiet", str(self.ws.resolve())])
        self.assertEqual(run.call_args_list[0].kwargs["env"]["CODEGRAPH_TELEMETRY"], "0")
        config = json.loads(plan.argv[plan.argv.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["codegraph"]["command"], "/x/codegraph")
        self.assertEqual(config["mcpServers"]["codegraph"]["env"], {"CODEGRAPH_TELEMETRY": "0"})
        self.assertEqual(config["mcpServers"]["codegraph"]["args"], ["serve", "--mcp", "--path", str(self.ws.resolve())])
        self.assertLess(plan.argv.index("--mcp-config"), plan.argv.index("--"))
        guidance = plan.argv[plan.argv.index("--append-system-prompt") + 1]
        self.assertIn(f"CodeGraph MCP is attached for {self.ws.resolve()}", guidance)
        self.assertIn("pass its root as projectPath", guidance)

    def test_sync_failure_continues_without_server(self):
        self.index(self.ws)
        failed = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "locked\n")
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=failed):
            plan = self.prepare()
        self.assertNotIn("--mcp-config", plan.argv)
        self.assertNotIn("CodeGraph MCP", plan.argv[plan.argv.index("--append-system-prompt") + 1])
        diagnostics = json.loads(plan.env["TOKEN_KIT_SIMPLE_CAPABILITIES"])["diagnostics"]
        self.assertTrue(any("sync failed (exit 1: locked)" in d for d in diagnostics))

    def test_timeout_continues_without_server(self):
        self.index(self.ws)
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=slow):
            result = codegraph.attach(self.ws, None)
        self.assertFalse(result.attached)
        self.assertIn("timed out", result.diagnostics[0])

    def test_missing_executable_warns_with_install_hint(self):
        with patch.object(codegraph.shutil, "which", return_value=None):
            result = codegraph.attach(self.ws, True)
        self.assertFalse(result.attached)
        self.assertIn("npm install -g --prefix ~/.local @colbymchenry/codegraph", result.diagnostics[0])

    def test_no_index_auto_and_explicit_off_do_nothing(self):
        with patch.object(codegraph.subprocess, "run") as run, patch.object(codegraph.shutil, "which") as which:
            self.assertFalse(codegraph.attach(self.ws, None).attached)
            self.index(self.ws)
            self.assertFalse(codegraph.attach(self.ws, False).attached)
        run.assert_not_called()
        which.assert_not_called()

    def test_seeds_worktree_from_main_index(self):
        main = self.root / "main"
        self.index(main)
        (main / ".codegraph/daemon.pid").write_text("1")
        listing = f"worktree {main}\nHEAD abc\n\nworktree {self.ws}\nHEAD def\n"
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, listing if "worktree" in argv else "", "")
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=run) as called:
            result = codegraph.attach(self.ws, True)
        self.assertTrue(result.attached)
        self.assertEqual((self.ws / ".codegraph/codegraph.db").read_bytes(), b"db")
        self.assertFalse((self.ws / ".codegraph/daemon.pid").exists())
        self.assertEqual(called.call_args_list[1].args[0][1], "sync")

    def test_builds_fresh_without_any_index(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, f"worktree {self.ws}\n" if "worktree" in argv else "", "")
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=run) as called:
            result = codegraph.attach(self.ws, True)
        init = called.call_args_list[1]
        self.assertEqual(init.args[0], ["/x/codegraph", "init", "--yes", str(self.ws)])
        self.assertEqual(init.kwargs["timeout"], codegraph.INIT_TIMEOUT)
        self.assertTrue(result.attached)

    def test_launch_writes_no_config_files(self):
        self.index(self.ws)
        before_ws, before_home = snapshot(self.ws), snapshot(self.root)
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph.subprocess, "run", side_effect=self.ok):
            self.prepare()
            self.prepare(headless=True)
        self.assertEqual(snapshot(self.ws), before_ws)
        self.assertEqual(snapshot(self.root), before_home)

    def test_cli_flag_saved_and_validated(self):
        from token_kit.simple_cli import parser_for, resolve_options
        args = parser_for("continue").parse_args(["x", "--codegraph"])
        self.assertIs(resolve_options(args).codegraph, True)
        args = parser_for("continue").parse_args(["x"])
        self.assertIs(resolve_options(args, {"codegraph": False}).codegraph, False)
        self.assertIs(resolve_options(args, {}).codegraph, True)  # operator default
        with self.assertRaises(ValueError):
            resolve_options(args, {"codegraph": 1})

    def test_hook_lines_printed_not_installed(self):
        git(self.ws.parent, "init", "-q", str(self.root / "repo"))
        repo = self.root / "repo"
        with patch("sys.stdout") as out:
            self.assertEqual(main(["codegraph-hooks", str(repo)]), 0)
        text = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertIn("post-checkout", text)
        self.assertIn("codegraph sync --quiet .", text)
        self.assertEqual(sorted(p.name for p in (repo / ".git/hooks").iterdir() if not p.name.endswith(".sample")), [])

    def test_exclude_appends_once_to_local_exclude_of_linked_worktree(self):
        main_repo = self.root / "main"
        git(self.root, "init", "-q", str(main_repo))
        (main_repo / ".gitignore").write_text("build/\n")
        (main_repo / "a.py").write_text("x = 1\n")
        git(main_repo, "add", ".")
        git(main_repo, "commit", "-qm", "base")
        worktree = self.root / "fork"
        git(main_repo, "worktree", "add", "-q", str(worktree))
        self.index(worktree)
        self.assertIn("?? .codegraph/", git(worktree, "status", "--porcelain"))
        with patch.object(codegraph.shutil, "which", return_value="/x/codegraph"), \
                patch.object(codegraph, "_run", return_value=None):
            self.assertTrue(codegraph.attach(worktree, None).attached)
            codegraph.attach(worktree, None)
        exclude = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
        self.assertEqual(exclude.read_text().count("/.codegraph/"), 1)
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.assertEqual((main_repo / ".gitignore").read_text(), "build/\n")
        self.assertEqual((worktree / ".gitignore").read_text(), "build/\n")
