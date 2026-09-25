"""Unlimited restarts keep lifecycle gates and explicit finite choices intact."""
import contextlib
import io
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from token_kit import workflow
from token_kit.core.store import Store, read_json


class UnlimitedRolloverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store.create(self.root / "tasks", "Continuation", self.root)

    def test_cli_defaults_and_explicit_choices(self):
        for prefix in (["run"], ["launch", str(self.store.path), "--engine", "claude"],
                       ["continue"], ["pick"]):
            args = workflow.build_parser().parse_args(prefix)
            self.assertIsNone(args.max_rollovers)
            self.assertFalse(args.max_rollovers_explicit)
            for value, expected in (("unlimited", None), ("infinite", None), ("0", 0), ("2", 2), ("10", 10)):
                args = workflow.build_parser().parse_args([*prefix, "--max-rollovers", value])
                self.assertEqual(args.max_rollovers, expected)
                self.assertTrue(args.max_rollovers_explicit)
            for value in ("-1", "1.5", "none"):
                with self.subTest(prefix=prefix, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    workflow.build_parser().parse_args([*prefix, "--max-rollovers", value])

    def test_unlimited_crosses_old_cap_for_both_restart_types(self):
        for control in ({"phase": "ready"}, {"phase": "halted", "halt_kind": "compaction"}):
            # Each fake segment represents a completed checkpoint/reconciliation cycle.
            with self.subTest(control=control), patch.object(workflow, "_launch_segment", side_effect=[(75, control)] * 12 + [(0, {})]) as segment, contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(workflow.launch(self.store, "coordinator", "claude"), 0)
            self.assertEqual(segment.call_count, 13)
            self.assertTrue(all(call.kwargs["allow_recovery"] for call in segment.call_args_list))
            self.assertIn("(12)", output.getvalue())
            self.assertIn("--max-rollovers unlimited", output.getvalue())
            self.assertNotIn("None", output.getvalue())

    def test_finite_caps_stop_and_arbitrary_errors_do_not_retry(self):
        for limit in (0, 2):
            for control in ({"phase": "ready"}, {"phase": "halted", "halt_kind": "compaction"}):
                with self.subTest(limit=limit, control=control), patch.object(workflow, "_launch_segment", return_value=(75, control)) as segment, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(workflow.launch(self.store, "coordinator", "claude", max_rollovers=limit), 75)
                self.assertEqual(segment.call_count, limit + 1)
        for rc, control in ((2, {}), (130, {}), (75, {"phase": "halted", "halt_kind": "worker"})):
            with patch.object(workflow, "_launch_segment", return_value=(rc, control)) as segment, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(workflow.launch(self.store, "coordinator", "claude"), rc)
            segment.assert_called_once()
        with patch.object(workflow, "_launch_segment", side_effect=ValueError("unresolved recovery")) as segment, contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "unresolved recovery"):
            workflow.launch(self.store, "coordinator", "claude")
        segment.assert_called_once()

    def test_continue_inheritance_migration_and_override(self):
        cases = [({}, None, False), ({"max_rollovers": 10}, None, False),
                 ({"max_rollovers": 2}, 2, True), ({"max_rollovers": 0}, 0, True),
                 ({"max_rollovers": 10, "max_rollovers_explicit": True}, 10, True),
                 ({"max_rollovers": None, "max_rollovers_explicit": False}, None, False)]
        for previous, expected, explicit in cases:
            with self.subTest(previous=previous), patch.object(workflow, "latest_picker_run", return_value=previous), patch.object(workflow, "run", return_value=0) as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(workflow.main(["continue", "--task", str(self.store.path)]), 0)
                args = run.call_args.args[0]
                self.assertEqual(args.max_rollovers, expected)
                self.assertEqual(args.max_rollovers_explicit, explicit)
                self.assertEqual(workflow.main(["continue", "--task", str(self.store.path), "--max-rollovers", "unlimited"]), 0)
                self.assertIsNone(run.call_args.args[0].max_rollovers)
                self.assertTrue(run.call_args.args[0].max_rollovers_explicit)

    def test_run_record_preserves_explicit_ten_and_unlimited(self):
        child = Mock(pid=99999999)
        for limit, explicit in ((10, True), (None, False), (None, True)):
            with patch.object(workflow.shutil, "which", return_value="/bin/client"), patch.object(workflow, "subprocess", Mock(Popen=Mock(return_value=child), TimeoutExpired=subprocess.TimeoutExpired)), patch.object(workflow.runtime, "wait_segment", return_value=(0, {})), patch.object(workflow.ledger, "refresh"):
                rc, _ = workflow._launch_segment(self.store, "coordinator", "claude", None, False, False, None,
                                                  max_rollovers=limit, max_rollovers_explicit=explicit)
            self.assertEqual(rc, 0)
            previous = workflow.latest_picker_run(str(self.store.path))
            self.assertEqual(previous["max_rollovers"], limit)
            self.assertEqual(previous["max_rollovers_explicit"], explicit)

    def test_startup_and_picker_display_unlimited(self):
        args = workflow.build_parser().parse_args(["run", "--task", str(self.store.path)])
        with patch.object(workflow.shutil, "which", return_value="/bin/client"), patch.object(workflow, "launch", return_value=0) as launch, contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(workflow.run(args), 0)
        self.assertIn("enabled (unlimited managed restarts)", output.getvalue())
        self.assertIn("--max-rollovers unlimited", output.getvalue())
        self.assertIsNone(launch.call_args.kwargs["max_rollovers"])
        args.dry_run = True
        with contextlib.redirect_stdout(io.StringIO()) as dry_output:
            self.assertEqual(workflow.run(args), 0)
        self.assertTrue(json.loads(dry_output.getvalue())["compaction_recovery"])
        self.assertIsNone(json.loads(dry_output.getvalue())["max_rollovers"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(["pick", "--root", str(self.root / "tasks"), "--select", "1", "--print", "--max-rollovers", "unlimited"]), 0)
        self.assertIn("--max-rollovers unlimited", output.getvalue())


if __name__ == "__main__":
    unittest.main()
