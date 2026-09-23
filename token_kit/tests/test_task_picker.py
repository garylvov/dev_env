"""Task selection stays read-only and never launches a client."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.workflow import main, ranked_tasks, task_next_preview, picker_created


class TaskPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tasks with spaces"
        self.root.mkdir()

    def task(self, task_id, title="Finish parser", created="2026-09-23T12:00:00+00:00", **extra):
        path = self.root / task_id
        path.mkdir()
        meta = dict(schema_version=1, task_id=task_id, title=title,
                    created_at=created, workspace="/tmp/project", **extra)
        (path / "task.json").write_text(json.dumps(meta))
        state = path / "agents" / "coordinator"
        state.mkdir(parents=True)
        (state / "STATE.md").write_text("## Objective\nOld objective\n## Next\nVerify " + task_id + "\n## Evidence\nHidden\n")
        return path

    def invoke(self, *args, tty=False, answer=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("sys.stdin.isatty", return_value=tty), \
                patch("builtins.input", side_effect=answer if isinstance(answer, BaseException) else None,
                      return_value=answer), patch("token_kit.workflow.launch") as launch:
            rc = main(["pick", "--root", str(self.root), *args])
            launch.assert_not_called()
        return rc, out.getvalue(), err.getvalue()

    def test_typo_matches_and_exact_precedes_fuzzy(self):
        self.task("older", created="2026-09-22T12:00:00+00:00")
        self.task("exact", "Finish parsr")
        self.assertEqual([r["task_id"] for r in ranked_tasks(self.root, ["PARSR"])], ["exact", "older"])
        self.assertEqual(len(ranked_tasks(self.root, ["fnish", "parser"])), 2)

    def test_duplicates_use_timestamp_and_state_preview(self):
        self.task("old", created="2026-09-22T12:00:00+00:00")
        newest = self.task("new")
        rc, out, err = self.invoke("parser", "--select", "1")
        self.assertEqual(rc, 0)
        self.assertIn(str(newest), shlex.split(out))
        self.assertLess(err.index("Verify new"), err.index("Verify old"))
        self.assertNotIn("Hidden", err)
        self.assertIn("Created", err)
        self.assertIn("Sep 2026", err)

    def test_no_match_and_non_tty_require_explicit_selection(self):
        self.task("first")
        self.task("second")
        rc, out, err = self.invoke("unrelatedxyz")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no matching", err)
        rc, out, err = self.invoke("parser")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("--select N", err)

    def test_interactive_cancel_eof_invalid_and_selection(self):
        self.task("first")
        for answer in ("", "q", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                rc, out, err = self.invoke(tty=True, answer=answer)
                self.assertEqual(rc, 1)
                self.assertEqual(out, "")
                self.assertIn("cancelled", err)
        self.assertEqual(self.invoke(tty=True, answer="abc")[0], 2)
        self.assertEqual(self.invoke(tty=True, answer="1")[0], 0)
        self.assertEqual(self.invoke("--select", "0")[0], 2)
        self.assertEqual(self.invoke("--select", "2")[0], 2)

    def test_command_shell_quotes_paths_and_preserves_flags(self):
        path = self.task("first")
        rc, out, _ = self.invoke("--select", "1", "--engine", "claude", "--yolo", "--rollover-tokens", "500k")
        self.assertEqual(rc, 0)
        self.assertEqual(shlex.split(out), ["token-kit", "run", "--task", str(path), "--engine", "claude",
                                           "--rollover-tokens", "500000", "--yolo"])

    def test_preview_bounded_and_missing_state_allowed(self):
        path = self.task("first")
        state = path / "agents/coordinator/STATE.md"
        state.write_text("x" * 32768 + "\n## Next\nOutside bounded read\n")
        self.assertEqual(task_next_preview({"path": str(path)}), "unavailable")
        state.unlink()
        self.assertEqual(self.invoke("--select", "1")[0], 0)

    def test_find_keeps_substring_only_json(self):
        self.task("first")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["find", "parsr", "--root", str(self.root)]), 0)
        self.assertEqual(json.loads(out.getvalue()), [])

    def test_default_limit_bounds_state_reads(self):
        for number in range(22):
            self.task(f"task-{number:02d}")
        with patch("token_kit.workflow.task_next_preview", return_value="Next") as preview:
            rc, out, err = self.invoke()
            self.assertEqual(preview.call_count, 20)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("2 more matching tasks", err)
        self.assertIn("--limit", err)
        self.assertEqual(self.invoke("--limit", "0")[0], 2)
        self.assertEqual(self.invoke("--limit", "22", "--select", "22")[0], 0)

    def run_record(self, task, name, **fields):
        directory = task / "agents/coordinator/runs" / name
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps(fields))

    def test_latest_run_settings_used_chronologically_with_observed_model(self):
        task = self.task("first")
        self.run_record(task, "zzz", engine="claude", model="sonnet", created_at="2026-09-22T12:00:00+00:00")
        self.run_record(task, "aaa", engine="codex", model=None, usage={"current_model": "gpt-6-astra"},
                        rollover_tokens=500000, yolo=True, created_at="2026-09-23T12:00:00+00:00")
        rc, out, _ = self.invoke("--select", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(shlex.split(out), ["token-kit", "run", "--task", str(task), "--engine", "codex",
                                           "--model", "gpt-6-astra", "--rollover-tokens", "500000", "--yolo"])
        rc, out, _ = self.invoke("--select", "1", "--engine", "claude", "--model", "opus",
                                 "--no-yolo", "--rollover-tokens", "100k")
        self.assertEqual(rc, 0)
        self.assertEqual(shlex.split(out), ["token-kit", "run", "--task", str(task), "--engine", "claude",
                                           "--model", "opus", "--rollover-tokens", "100000"])
        self.assertNotIn("--model", shlex.split(self.invoke("--select", "1", "--engine", "claude")[1]))

    def test_missing_run_settings_fallback_and_model_fallback(self):
        task = self.task("first")
        expected = ["token-kit", "run", "--task", str(task), "--engine", "claude"]
        self.assertEqual(shlex.split(self.invoke("--select", "1")[1]), expected)
        self.run_record(task, "old", engine="claude", model="sonnet", usage={"current_model": "unknown"})
        self.assertEqual(shlex.split(self.invoke("--select", "1")[1]), expected + ["--model", "sonnet"])

    def test_run_metadata_bound_reports_error_without_command(self):
        task = self.task("first")
        self.run_record(task, "large", engine="claude", extra="x" * 65536)
        rc, out, err = self.invoke("--select", "1")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("exceeds 64 KiB", err)

    def test_local_created_dates_handle_daylight_saving_and_unknown_timezone(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                self.assertEqual(picker_created("2026-09-23T17:39:00Z"), "Wed 23 Sep 2026, 1:39pm EDT")
                self.assertEqual(picker_created("2026-01-23T17:39:00+00:00"), "Fri 23 Jan 2026, 12:39pm EST")
                self.assertEqual(picker_created("2026-09-23T01:39:00Z"), "Tue 22 Sep 2026, 9:39pm EDT")
                self.assertEqual(picker_created("2026-09-23T17:39:00"),
                                 "Wed 23 Sep 2026, 5:39pm (timezone unknown)")
                self.assertEqual(picker_created(None), "Unknown date")
                self.assertEqual(picker_created("bad stamp"), "Unknown date")
        finally:
            time.tzset()

    def test_human_status_labels_leave_machine_metadata_unchanged(self):
        self.task("first", status="open")
        self.task("second", status="done")
        self.task("third", status="unexpected", created="invalid")
        _, _, err = self.invoke()
        self.assertIn("[Unfinished] Created", err)
        self.assertIn("[Finished] Created", err)
        self.assertIn("[Unknown status] Created Unknown date", err)
        for command in (["list"], ["find", "parser"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([*command, "--root", str(self.root)]), 0)
            rows = {row["task_id"]: row for row in json.loads(out.getvalue())}
            self.assertEqual(rows["first"]["created_at"], "2026-09-23T12:00:00+00:00")
            self.assertEqual(rows["first"]["status"], "open")
            self.assertEqual(rows["second"]["status"], "done")
            self.assertEqual(rows["third"]["created_at"], "invalid")

    def test_malformed_settings_do_not_enable_permissions_or_emit_command(self):
        task = self.task("first")
        directory = task / "agents/coordinator/runs/old"
        directory.mkdir(parents=True)
        for field, value in (("yolo", "false"), ("usage", ["bad"]), ("model", ["bad"])):
            with self.subTest(field=field):
                (directory / "run.json").write_text(json.dumps({"engine": "claude", field: value}))
                rc, out, err = self.invoke("--select", "1")
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("invalid recorded", err)


if __name__ == "__main__":
    unittest.main()
