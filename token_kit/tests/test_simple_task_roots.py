"""Task folders inside the working repo: creation, exclusion, search and relocation, on real git repos."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from token_kit import simple_cli as cli
from token_kit import simple_task_roots as roots
from token_kit.task_files import load_task


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout


class TaskRootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "a.py").write_text("x = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.home = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.visibility = patch.object(roots, "visibility", return_value="PRIVATE")
        self.visibility.start()
        self.addCleanup(self.visibility.stop)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)

    def call(self, cwd, *args):
        os.chdir(cwd)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("token_kit.simple_runner.run_session", return_value=0) as run:
            code = cli.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue(), run

    def create(self, cwd, title="Fix parser"):
        code, out, err, run = self.call(cwd, "run", title)
        self.assertEqual(code, 0, err)
        return run.call_args.args[0].root, err

    def exclude(self, cwd):
        return Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip()).read_text()

    def test_run_creates_task_in_repo_with_runtime_excluded(self):
        (self.repo / "sub").mkdir()
        task, err = self.create(self.repo / "sub")
        self.assertEqual(task.parent, self.repo / "agent_tasks")
        self.assertTrue((task / ".token-kit/session.json").is_file())
        self.assertEqual(self.exclude(self.repo).count(roots.RUNTIME_EXCLUDE), 1)
        status = git(self.repo, "status", "--porcelain", "--untracked-files=all")
        self.assertIn(f"agent_tasks/{task.name}/asks.md", status)
        self.assertNotIn(".token-kit", status)
        self.assertEqual(err, "")
        self.create(self.repo, "Second")
        self.assertEqual(self.exclude(self.repo).count(roots.RUNTIME_EXCLUDE), 1)
        self.assertFalse(self.home.exists())

    def test_outside_git_uses_config_root_and_explicit_root_wins(self):
        plain = self.root / "plain"
        plain.mkdir()
        task, _ = self.create(plain)
        self.assertEqual(task.parent, self.home / "token_kit")
        code, out, err, run = self.call(self.repo, "run", "Elsewhere", "--root", self.root / "chosen")
        self.assertEqual(run.call_args.args[0].root.parent, self.root / "chosen")
        self.assertFalse((self.repo / "agent_tasks").exists())

    def test_public_repo_keeps_task_local(self):
        self.visibility.stop()
        with patch.object(roots, "visibility", return_value="PUBLIC"):
            task, err = self.create(self.repo)
        self.visibility.start()
        self.assertIn(f"/agent_tasks/{task.name}/", self.exclude(self.repo).splitlines())
        self.assertIn("public repo", err)
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=all"), "")

    def test_existing_ignore_rule_is_reported_not_removed(self):
        path = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
        path.write_text(path.read_text() + "agent_tasks/\n")
        task, err = self.create(self.repo)
        self.assertIn("agent_tasks/\n", self.exclude(self.repo))
        self.assertIn("git-ignored", err)
        self.assertNotIn("(in the repo: commit it", self.guidance(task))
        code, out, err, _ = self.call(self.repo, "continue", task.name)
        self.assertNotIn("git-ignored", err)

    def guidance(self, task):
        from token_kit.simple_adapters import folder_guidance
        return folder_guidance(load_task(task))

    def test_guidance_says_commit_only_for_committable_folders(self):
        task, _ = self.create(self.repo)
        self.assertIn("(in the repo: commit it with your work commits)", self.guidance(task))
        plain = self.root / "plain"
        plain.mkdir()
        other, _ = self.create(plain)
        self.assertNotIn("commit it", self.guidance(other))

    def test_continue_from_linked_worktree_finds_main_task(self):
        task, _ = self.create(self.repo)
        tree = self.root / "tree"
        git(self.repo, "worktree", "add", "-q", str(tree))
        code, out, err, run = self.call(tree, "continue", "parser")
        self.assertEqual(code, 0, err)
        self.assertEqual(run.call_args.args[0].root, task)
        self.assertEqual(run.call_args.args[0].workspace, self.repo)
        code, out, err, _ = self.call(tree, "status", "parser")
        self.assertIn(str(task / f"{task.name}_out.md"), out)

    def test_old_config_root_tasks_still_found(self):
        old = self.home / "token_kit"
        from token_kit.task_files import create_task
        view = create_task(old, "rsl rl recover", self.root)
        code, out, err, run = self.call(self.repo, "continue", "recover")
        self.assertEqual(code, 0, err)
        self.assertEqual(run.call_args.args[0].root, view.root)

    def test_committed_task_in_several_worktrees(self):
        early = self.root / "early"
        git(self.repo, "worktree", "add", "-q", str(early))
        task, _ = self.create(self.repo)
        git(self.repo, "add", "agent_tasks")
        git(self.repo, "commit", "-qm", "task")
        late = self.root / "late"
        git(self.repo, "worktree", "add", "-q", "--detach", str(late))
        # The current worktree's copy shadows the others.
        code, out, err, run = self.call(late, "continue", task.name)
        self.assertEqual(run.call_args.args[0].root, late / "agent_tasks" / task.name)
        self.assertEqual(run.call_args.args[0].workspace, late)
        # From a worktree without a copy the name is ambiguous.
        code, out, err, run = self.call(early, "continue", task.name)
        self.assertEqual(code, 2)
        self.assertIn("--root", err)
        self.assertIn(str(self.repo / "agent_tasks"), err)
        self.assertIn(str(late / "agent_tasks"), err)
        run.assert_not_called()
        code, out, err, run = self.call(early, "continue", task.name, "--root", late / "agent_tasks")
        self.assertEqual(run.call_args.args[0].root, late / "agent_tasks" / task.name)

    def test_moved_or_cloned_task_follows_its_checkout(self):
        task, _ = self.create(self.repo)
        git(self.repo, "add", "agent_tasks")
        git(self.repo, "commit", "-qm", "task")
        clone = self.root / "clone"
        git(self.root, "clone", "-q", "--local", str(self.repo), str(clone))
        copy = load_task(clone / "agent_tasks" / task.name)
        self.assertFalse((copy.root / ".token-kit").exists())
        self.assertEqual(copy.workspace, clone)
        self.assertEqual(copy.title, "fix parser")
        moved = self.root / "moved"
        shutil.move(self.repo, moved)
        view = load_task(moved / "agent_tasks" / task.name)
        self.assertEqual(view.workspace, moved)
        self.assertEqual(view.title, "Fix parser")

    def committed(self, title="Fix parser"):
        task, _ = self.create(self.repo, title)
        git(self.repo, "add", "agent_tasks")
        git(self.repo, "commit", "-qm", "task")
        return task

    def test_continue_in_fresh_clone_excludes_runtime_records(self):
        task = self.committed()
        clone = self.root / "clone"
        git(self.root, "clone", "-q", "--local", str(self.repo), str(clone))
        code, out, err, run = self.call(clone, "continue", task.name)
        self.assertEqual(code, 0, err)
        (clone / "agent_tasks" / task.name / ".token-kit").mkdir()
        (clone / "agent_tasks" / task.name / ".token-kit/session.json").write_text("{}")
        self.assertEqual(git(clone, "status", "--porcelain", "--untracked-files=all"), "")

    def test_missing_relative_workspace_never_falls_back_to_old_checkout(self):
        (self.repo / "sub").mkdir()
        task, _ = self.create(self.repo / "sub")
        copy = self.root / "copy"
        shutil.copytree(self.repo, copy)
        shutil.rmtree(copy / "sub")
        view = load_task(copy / "agent_tasks" / task.name)
        self.assertIsNone(view.workspace)
        self.assertTrue(any(str(copy / "sub") in note for note in view.diagnostics))

    def test_worker_relative_workspace_resolves_first(self):
        from token_kit.task_files import save_settings
        task, _ = self.create(self.repo)
        (self.repo / "w").mkdir()
        save_settings(load_task(task, workspace=self.repo / "w", agent="editor"), {"model": "m"})
        copy = self.root / "copy"
        shutil.copytree(self.repo, copy)
        self.assertEqual(load_task(copy / "agent_tasks" / task.name, agent="editor").workspace, copy / "w")

    def test_current_copy_survives_limit_and_other_choices_are_not_blocked(self):
        early = self.root / "early"
        git(self.repo, "worktree", "add", "-q", str(early))
        task = self.committed()
        late = self.root / "late"
        git(self.repo, "worktree", "add", "-q", "--detach", str(late))
        os.utime(task, (2_000_000_000, 2_000_000_000))  # main's copy sorts first
        code, out, err, run = self.call(late, "continue", task.name, "--limit", "1")
        self.assertEqual(run.call_args.args[0].root, late / "agent_tasks" / task.name)
        other, _ = self.create(self.repo, "Fix parser two")
        os.chdir(early)
        args = cli.parser_for("continue").parse_args(["parser"])
        rows = cli.task_candidates(cli.search_roots(args), ["parser"])
        self.assertEqual(sorted(view.root.name for view in rows), sorted([task.name, other.name]))
        args.select = [view.root for view in rows].index(other) + 1
        self.assertEqual(cli.select_task(args), other)
        args.select = [view.root.name for view in rows].index(task.name) + 1
        with self.assertRaisesRegex(ValueError, "--root"):
            cli.select_task(args)

    def test_visibility_uses_origin_and_treats_failures_as_unknown(self):
        self.visibility.stop()
        self.assertIsNone(roots.visibility(self.repo))  # no origin remote: gh is not asked
        git(self.repo, "remote", "add", "origin", "https://example.invalid/o/r.git")
        real = subprocess.run
        def fake(argv, **kw):
            if argv[0] != "gh":
                return real(argv, **kw)
            self.assertEqual(argv[:4], ["gh", "repo", "view", "https://example.invalid/o/r.git"])
            self.assertEqual(kw["timeout"], 5)
            return subprocess.CompletedProcess(argv, 0, "PUBLIC\n", "")
        with patch.object(roots.subprocess, "run", side_effect=fake):
            self.assertEqual(roots.visibility(self.repo), "PUBLIC")
        def slow(argv, **kw):
            if argv[0] != "gh":
                return real(argv, **kw)
            raise subprocess.TimeoutExpired(argv, 5)
        with patch.object(roots.subprocess, "run", side_effect=slow):
            self.assertIsNone(roots.visibility(self.repo))
        self.visibility.start()


if __name__ == "__main__":
    unittest.main()
