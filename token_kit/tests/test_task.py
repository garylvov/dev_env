"""Guard for token-kit-task: the working folder that titles itself.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

Nothing here touches a real home: XDG_STATE_HOME points at a scratch directory,
so the machine-wide index under test is this test's own file.

Cases, in order below:
  1 `new` names the folder <date>_<time>_<slug>, writes the four header lines
    with a HUMAN start time, makes lanes/, and prints the absolute path only
  2 the slug is ascii, hyphenated and bounded; a collision gets -2
  3 `lane` makes v0, then v1 on a retry, never an overwrite; --under nests;
    a name with a slash is refused rather than guessed at
  4 `retitle` keeps the date_time prefix, changes the slug, rewrites the title
    and summary, and leaves a RELATIVE symlink at the old name
  5 THE SYMLINK GUARD: a file reached through the old path after a retitle is
    the same inode, and a SECOND retitle re-points the first symlink instead
    of chaining it
  6 `retitle` refuses loudly when the target name is taken
  7 the index: one row per event, a torn last line is dropped, and concurrent
    appends interleave whole rows
  8 `list` reads the local root; `list --all` reads the index, says how many
    paths have gone away, and `--open` filters
  9 `find` matches titles first and bodies second, case insensitively
 10 `resume` prints the exact supervise command, refuses to guess between two
    matches (exit 2), and marks the task open
 11 `done` and `reopen` flip Status and log a row
 12 the header parser reads ONLY its four keys and leaves the body alone
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit import task as T  # noqa: E402

ENTRY = KIT_DIR / "src" / "token_kit" / "bin" / "token-kit-task"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tasks = self.root / "tasks"
        self.state_home = self.root / "state"
        os.environ["XDG_STATE_HOME"] = str(self.state_home)
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.addCleanup(self.tmp.cleanup)

    def run_task(self, *argv) -> tuple[int, str]:
        """The module's own main, with stdout captured."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = T.main([str(a) for a in argv])
        return rc, buf.getvalue()

    def new(self, title="migrate the date parsing", **kw):
        rc, out = self.run_task("new", title, "--root", self.tasks,
                                *sum([["--" + k.replace("_", "-"), v]
                                      for k, v in kw.items()], []))
        self.assertEqual(0, rc)
        return Path(out.strip())


class NewFolder(Base):
    def test_1_the_folder_titles_itself_and_the_header_is_human(self):
        task = self.new()
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}_\d{4}_migrate-the-date-parsing$",
                                 task.name), task.name)
        self.assertTrue(task.is_absolute())
        self.assertTrue((task / "lanes").is_dir(), "no empty lanes/ folder")
        head = T.Header(task / "STATE.md")
        self.assertEqual("migrate the date parsing", head.title)
        self.assertEqual("open", head.status)
        self.assertEqual(str(Path.cwd()), head["Cwd"])
        # "Mon 21 Sep 2026, 10:42pm": a person reads this, so it is not ISO.
        self.assertRegex(head.started,
                         r"^[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2}[ap]m$")
        body = (task / "STATE.md").read_text()
        for section in ("## Decisions", "## In flight", "## Next"):
            self.assertIn(section, body)

    def test_1b_it_prints_the_path_and_nothing_else(self):
        rc, out = self.run_task("new", "a title", "--root", self.tasks)
        self.assertEqual(0, rc)
        self.assertEqual(1, len(out.strip().splitlines()))
        self.assertTrue(Path(out.strip()).is_dir())

    def test_2_the_slug_is_ascii_hyphenated_and_bounded(self):
        self.assertEqual("cafe-resume-work", T.slug("Café: RÉSUMÉ work!"))
        self.assertEqual("task", T.slug("!!!"))
        long = T.slug("a " * 60)
        self.assertLessEqual(len(long), T.SLUG_MAX)
        self.assertFalse(long.endswith("-"))

    def test_2b_a_collision_gets_a_numbered_suffix(self):
        a = self.new("same title")
        b = self.new("same title")
        self.assertNotEqual(a, b)
        self.assertTrue(b.name.endswith("-2"), b.name)
        self.assertTrue((b / "STATE.md").is_file())


class Lanes(Base):
    def test_3_a_retry_is_a_new_version_never_an_overwrite(self):
        task = self.new()
        rc, first = self.run_task("lane", task, "reader")
        self.assertEqual(0, rc)
        first = Path(first.strip())
        self.assertEqual("v0", first.name)
        self.assertIn("every claim below is a lead to verify",
                      (first / "in.md").read_text())
        (first / "in.md").write_text("MY BRIEF")
        rc, second = self.run_task("lane", task, "reader")
        self.assertEqual("v1", Path(second.strip()).name)
        self.assertEqual("MY BRIEF", (first / "in.md").read_text())

    def test_3b_under_nests_a_worker_inside_its_lead(self):
        task = self.new()
        _rc, lead = self.run_task("lane", task, "lead")
        lead = Path(lead.strip())
        rc, worker = self.run_task("lane", task, "worker", "--under", lead)
        self.assertEqual(0, rc)
        self.assertEqual(lead / "lanes" / "worker" / "v0", Path(worker.strip()))

    def test_3c_a_slash_in_a_lane_name_is_refused(self):
        task = self.new()
        rc, _ = self.run_task("lane", task, "lead/worker")
        self.assertEqual(1, rc)

    def test_3d_a_directory_that_is_not_a_task_is_refused(self):
        rc, _ = self.run_task("lane", self.root, "reader")
        self.assertEqual(1, rc)


class Retitle(Base):
    def test_4_the_stamp_stays_and_the_slug_changes(self):
        task = self.new("fix date parsing")
        stamp = task.name.rsplit("_", 1)[0]
        rc, out = self.run_task("retitle", task, "replace the date helper",
                                "--summary", "it was the helper all along")
        self.assertEqual(0, rc)
        new = Path(out.strip())
        self.assertEqual(f"{stamp}_replace-the-date-helper", new.name)
        head = T.Header(new / "STATE.md")
        self.assertEqual("replace the date helper", head.title)
        self.assertEqual("it was the helper all along", head["Summary"])
        self.assertEqual("open", head.status, "retitle must not change Status")

    def test_5_the_old_path_still_resolves_to_the_same_inode(self):
        task = self.new("fix date parsing")
        (task / "lanes" / "a").mkdir()
        (task / "lanes" / "a" / "out.md").write_text("RESULT: done\n")
        _rc, out = self.run_task("retitle", task, "replace the date helper")
        new = Path(out.strip())
        self.assertTrue(task.is_symlink(), "no symlink left at the old name")
        self.assertEqual(new.name, os.readlink(task),
                         "the symlink must be RELATIVE, so the pair can be moved")
        old_file = task / "lanes" / "a" / "out.md"
        self.assertEqual(old_file.stat().st_ino,
                         (new / "lanes" / "a" / "out.md").stat().st_ino)

    def test_5b_a_second_retitle_repoints_the_first_link_instead_of_chaining(self):
        task = self.new("one")
        _rc, out = self.run_task("retitle", task, "two")
        second = Path(out.strip())
        _rc, out = self.run_task("retitle", second, "three")
        third = Path(out.strip())
        self.assertEqual(third.name, os.readlink(task),
                         "the FIRST name still points at the second: a chain")
        self.assertEqual(third.name, os.readlink(second))
        self.assertTrue((task / "STATE.md").is_file())
        self.assertEqual("three", T.Header(task / "STATE.md").title)

    def test_5c_retitling_through_an_old_name_moves_the_real_directory(self):
        task = self.new("one")
        _rc, out = self.run_task("retitle", task, "two")
        # now retitle by the OLD path, which is a symlink
        rc, out = self.run_task("retitle", task, "three")
        self.assertEqual(0, rc)
        self.assertTrue(Path(out.strip()).is_dir())
        self.assertFalse(Path(out.strip()).is_symlink())

    def test_6_a_taken_name_is_refused_loudly(self):
        task = self.new("one")
        stamp = task.name.rsplit("_", 1)[0]
        (self.tasks / f"{stamp}_two").mkdir()
        rc, _ = self.run_task("retitle", task, "two")
        self.assertEqual(1, rc)
        self.assertEqual("one", T.Header(task / "STATE.md").title,
                         "a refused retitle must change nothing")


class Index(Base):
    def test_7_one_row_per_event_and_a_torn_last_line_is_dropped(self):
        task = self.new("one")
        self.run_task("retitle", task, "two")
        rows = T.index_rows()
        self.assertEqual(["new", "retitle"], [r[1] for r in rows])
        with T.index_path().open("a") as fh:
            fh.write("2026-09-21T22:42:11-0400\tnew\t/half")   # no newline, no title
        self.assertEqual(2, len(T.index_rows()), "a torn last line was not dropped")

    def test_7b_concurrent_appends_interleave_whole_rows(self):
        """One row, one write, O_APPEND: never half a row from each writer."""
        code = ("import os,sys;sys.path.insert(0,%r);"
                "from token_kit import task as T;"
                "from pathlib import Path;"
                "[T.index_append('new', Path('/x/%%d' %% i), 'title %%d' %% i) "
                "for i in range(60)]" % str(KIT_DIR / "src"))
        env = {**os.environ, "XDG_STATE_HOME": str(self.state_home)}
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env)
                 for _ in range(4)]
        for p in procs:
            p.wait()
        text = T.index_path().read_text()
        self.assertEqual(240, len(text.splitlines()))
        for line in text.splitlines():
            self.assertEqual(4, len(line.split("\t")), f"a torn row: {line!r}")


class Listing(Base):
    def test_8_list_reads_the_local_root(self):
        a = self.new("first piece of work")
        self.new("second piece of work")
        self.run_task("lane", a, "reader")
        rc, out = self.run_task("list", "--root", self.tasks)
        self.assertEqual(0, rc)
        lines = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertEqual(2, len(lines))
        self.assertIn("lanes=1", out)
        self.assertRegex(out, r"\d{1,2}:\d{2}[ap]m")

    def test_8b_a_retitle_symlink_is_not_listed_as_a_second_task(self):
        task = self.new("one")
        self.run_task("retitle", task, "two")
        rc, out = self.run_task("list", "--root", self.tasks)
        lines = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertEqual(1, len(lines), out)

    def test_8c_list_all_reads_the_index_and_counts_what_is_gone(self):
        import shutil

        a = self.new("kept work")
        b = self.new("deleted work")
        shutil.rmtree(b)
        rc, out = self.run_task("list", "--all")
        self.assertEqual(0, rc)
        self.assertIn("1 task(s) in the index no longer exist", out)
        self.assertIn(str(a), out)
        self.assertNotIn(str(b), out)

    def test_8d_open_filters(self):
        a = self.new("still going")
        b = self.new("finished")
        self.run_task("done", b)
        rc, out = self.run_task("list", "--root", self.tasks, "--open")
        self.assertIn(str(a), out)
        self.assertNotIn(str(b), out)


class FindAndResume(Base):
    def test_9_titles_rank_before_bodies_and_case_does_not_matter(self):
        body = self.new("unrelated work")
        (body / "PROMPTS.md").write_text("> please fix the DATE parsing\n")
        titled = self.new("date parsing rewrite")
        rc, out = self.run_task("find", "DATE", "parsing", "--all")
        self.assertEqual(0, rc)
        lines = out.splitlines()
        self.assertEqual(2, len(lines))
        self.assertIn(str(titled), lines[0], "a title hit must rank first")
        self.assertIn(str(body), lines[1])

    def test_9b_a_summary_is_searched_too_and_a_miss_is_rc_1(self):
        task = self.new("opaque name")
        self.run_task("retitle", task, "opaque name", "--summary", "the widget cache")
        rc, out = self.run_task("find", "widget", "--all")
        self.assertEqual(0, rc)
        rc, _ = self.run_task("find", "nothing-matches-this", "--all")
        self.assertEqual(1, rc)

    def test_10_resume_prints_the_exact_restart_command(self):
        task = self.new("date parsing rewrite")
        rc, out = self.run_task("resume", "date", "parsing")
        self.assertEqual(0, rc)
        self.assertEqual(
            f"token-kit-supervise launch --state-file {task / 'STATE.md'} "
            f"--cwd {T.Header(task / 'STATE.md')['Cwd']}", out.strip())

    def test_10b_resume_takes_a_path_too(self):
        task = self.new("by path")
        rc, out = self.run_task("resume", str(task))
        self.assertEqual(0, rc)
        self.assertIn(str(task / "STATE.md"), out)

    def test_10c_two_matches_never_guess(self):
        self.new("date parsing one")
        self.new("date parsing two")
        rc, out = self.run_task("resume", "date", "parsing")
        self.assertEqual(2, rc, "resume guessed between two tasks")
        self.assertEqual(2, len(out.splitlines()))

    def test_10d_resume_marks_the_task_open_again(self):
        task = self.new("finished work")
        self.run_task("done", task)
        self.assertEqual("done", T.Header(task / "STATE.md").status)
        self.run_task("resume", str(task))
        self.assertEqual("open", T.Header(task / "STATE.md").status)


class StatusFlips(Base):
    def test_11_done_and_reopen_flip_the_header_and_log_a_row(self):
        task = self.new("work")
        rc, _ = self.run_task("done", task)
        self.assertEqual(0, rc)
        self.assertEqual("done", T.Header(task / "STATE.md").status)
        self.run_task("reopen", task)
        self.assertEqual("open", T.Header(task / "STATE.md").status)
        self.assertEqual(["new", "done", "reopen"], [r[1] for r in T.index_rows()])


class HeaderParsing(Base):
    def test_12_only_the_four_keys_are_read_and_the_body_is_untouched(self):
        task = self.new("work")
        state = task / "STATE.md"
        state.write_text(state.read_text() +
                         "\nStatus: this line is in the BODY and is not a header\n"
                         "some free text a person wrote\n")
        head = T.Header(state)
        self.assertEqual("open", head.status, "a body line was parsed as a header")
        T.rewrite_header(state, Status="done")
        text = state.read_text()
        self.assertIn("Status: this line is in the BODY", text)
        self.assertIn("some free text a person wrote", text)
        self.assertEqual("done", T.Header(state).status)

    def test_12b_a_missing_key_is_inserted_not_appended_to_the_body(self):
        state = self.root / "hand-written.md"
        state.write_text("# hand written\n\nStarted: Mon 21 Sep 2026, 10:42pm\n"
                         "\n## Decisions\nfree text\n")
        T.rewrite_header(state, Status="open")
        lines = state.read_text().splitlines()
        self.assertLess(lines.index("Status: open"), lines.index("## Decisions"))
        self.assertEqual("free text", lines[-1])


class Entrypoint(Base):
    def test_13_the_shim_runs_end_to_end(self):
        env = {**os.environ, "XDG_STATE_HOME": str(self.state_home)}
        p = subprocess.run([str(ENTRY), "new", "through the shim",
                            "--root", str(self.tasks)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertNotIn("No module named", p.stderr)
        self.assertTrue(Path(p.stdout.strip()).is_dir())


if __name__ == "__main__":
    unittest.main()
