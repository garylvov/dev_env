"""Offline rollover state machine and usage fixtures; no paid model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import runtime, workflow
from token_kit.core import ledger
from token_kit.core.store import Store, read_json


def claude_row(identifier="message-1", tokens=100, output=10, model="opus"):
    return {"type": "assistant", "message": {"id": identifier, "model": model,
            "usage": {"input_tokens": tokens, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 5, "output_tokens": output}}}


def codex_row(total=200, context=110):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": total - 20, "output_tokens": 20,
                              "cached_input_tokens": 50, "reasoning_output_tokens": 5,
                              "total_tokens": total},
        "last_token_usage": {"total_tokens": context}, "model_context_window": 1000}}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store.create(self.root / "tasks", "test", self.root)
        self.agent = "coordinator"
        self.run = self.store.claim_run(self.agent, "claude", True)
        self.transcript = self.root / "transcript.jsonl"
        self.write_rows(claude_row())
        runtime.initialize(self.store, self.agent, self.run, "claude", 100)

    def write_rows(self, *rows):
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def hook(self, event, **fields):
        return runtime.handle(self.store, self.agent, self.run, {
            "hook_event_name": event, "session_id": "session1",
            "transcript_path": str(self.transcript), **fields})

    def control(self):
        return read_json(self.run / "runtime.json")

    def test_claude_streaming_duplicates_and_cache_subsets(self):
        self.write_rows(claude_row(output=1), claude_row(output=10),
                        {**claude_row("child", 900), "isSidechain": True})
        usage = ledger.summarize(self.transcript, "claude")
        self.assertEqual(usage["models"]["opus"]["total"], 135)
        self.assertEqual(usage["models"]["opus"]["input"], 125)
        self.assertEqual(usage["context_tokens"], 135)

    def test_codex_cumulative_is_not_summed_and_context_is_separate(self):
        self.write_rows({"type": "turn_context", "payload": {"model": "astra"}},
                        codex_row(200), codex_row(200), codex_row(300, 50))
        usage = ledger.summarize(self.transcript, "codex")
        self.assertEqual(usage["models"]["astra"]["total"], 300)
        self.assertEqual(usage["context_tokens"], 50)
        self.assertEqual(usage["models"]["astra"]["reasoning"], 5)

    def test_counter_reset_is_unknown_not_fabricated_spend(self):
        self.write_rows(codex_row(300), codex_row(200))
        with self.assertRaisesRegex(ValueError, "reset"):
            ledger.summarize(self.transcript, "codex")

    def test_repeated_hooks_do_not_double_charge(self):
        self.hook("SessionStart")
        self.hook("PostToolUse")
        self.hook("PostToolUse")
        report = ledger.refresh(self.store)
        self.assertIn("**135**", report)
        self.assertNotIn("message-1", report)

    def test_stop_requires_new_checkpoint_then_marks_ready(self):
        self.hook("SessionStart")
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.assertEqual(self.control()["phase"], "checkpoint_requested")
        checkpoint = self.store.checkpoint(self.agent)
        self.assertFalse(self.hook("Stop")["continue"])
        self.assertEqual(self.control()["phase"], "ready")
        self.assertEqual(self.control()["checkpoint"], str(checkpoint))

    def test_failed_checkpoint_stops_without_infinite_retry(self):
        self.hook("SessionStart")
        self.hook("Stop")
        self.assertFalse(self.hook("Stop")["continue"])
        self.assertEqual(self.control()["phase"], "halted")

    def test_tool_boundary_nudges_checkpoint_before_turn_ends(self):
        self.hook("SessionStart")
        response = self.hook("PostToolUse")
        self.assertIn("checkpoint", response["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("PostToolUse"), {})
        self.store.checkpoint(self.agent)
        self.hook("Stop")
        self.assertEqual(self.control()["phase"], "ready")

    def test_incremental_usage_handles_partial_lines_without_double_counting(self):
        cache = self.root / "usage-cache.json"
        first = ledger.summarize(self.transcript, "claude", cache=cache)
        self.assertEqual(first["models"]["opus"]["total"], 135)
        next_row = json.dumps(claude_row("m2"))
        with self.transcript.open("a") as stream:
            stream.write(next_row[:10])
        self.assertEqual(ledger.summarize(self.transcript, "claude", cache=cache)["models"], first["models"])
        with self.transcript.open("a") as stream:
            stream.write(next_row[10:] + "\n")
        self.assertEqual(ledger.summarize(self.transcript, "claude", cache=cache)["models"]["opus"]["total"], 270)

    def test_active_child_prevents_rollover(self):
        self.hook("SessionStart")
        self.hook("SubagentStart", agent_id="child")
        self.hook("Stop")
        self.store.checkpoint(self.agent)
        self.hook("Stop")
        self.assertEqual(self.control()["phase"], "halted")

    def test_precompact_is_vetoed_and_not_automatically_replayed(self):
        self.assertFalse(self.hook("PreCompact")["continue"])
        self.assertEqual(self.control()["phase"], "halted")

    def test_unknown_telemetry_stops_at_turn_boundary(self):
        self.write_rows({"type": "assistant", "message": {}})
        self.hook("SessionStart")
        self.assertFalse(self.hook("Stop")["continue"])
        self.assertIn("unavailable", self.control()["reason"])

    def test_below_threshold_does_not_force_an_extra_turn(self):
        self.write_rows(claude_row(tokens=1, output=1))
        self.hook("SessionStart")
        self.assertEqual(self.hook("Stop"), {})

    def test_child_usage_is_separate(self):
        self.hook("SessionStart")
        self.hook("SubagentStart", agent_id="child")
        self.hook("SubagentStop", agent_id="child", agent_transcript_path=str(self.transcript))
        self.assertEqual(self.control()["active_children"], [])
        self.assertIn("coordinator/native:child", ledger.refresh(self.store))

    def test_supervisor_stops_old_process_before_restarting(self):
        self.hook("SessionStart")
        self.hook("Stop")
        self.store.checkpoint(self.agent)
        self.hook("Stop")
        child = Mock()
        child.wait.return_value = -15
        stop = Mock()
        rc, control = runtime.wait_segment(child, self.store, self.agent, self.run, stop)
        stop.assert_called_once_with(child)
        self.assertEqual(rc, -15)
        self.assertEqual(control["phase"], "ready")

    def test_missing_startup_hook_fails_closed(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.return_value = -15
        _, control = runtime.wait_segment(child, self.store, self.agent, self.run, Mock(), startup_timeout=-1)
        self.assertEqual(control["phase"], "halted")

    def test_same_engine_restart_and_limit(self):
        ready = {"phase": "ready", "sample": {"current_model": "opus"}}
        with patch.object(workflow, "_launch_segment", side_effect=[(0, ready), (0, {})]) as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, self.agent, "claude", rollover_tokens=100), 0)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(launch.call_args.args[2:4], ("claude", "opus"))
        with patch.object(workflow, "_launch_segment", return_value=(0, ready)) as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(self.store, self.agent, "claude", rollover_tokens=100, max_rollovers=0), 75)
        self.assertEqual(launch.call_count, 1)

    def test_codex_protected_plan_and_cli_preview(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = workflow.main(["launch", str(self.store.path), "--engine", "codex",
                                "--rollover-tokens", "500k", "--dry-run"])
        self.assertEqual(rc, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["rollover_tokens"], 500000)
        self.assertTrue(any("hooks.PreCompact=" in arg for arg in value["argv"]))

    def test_wire_cumulative_updates_preserve_model_breakdown(self):
        def usage(total):
            return {"total": {"inputTokens": total - 10, "outputTokens": 10, "totalTokens": total}}
        for model, total in (("luna", 100), ("luna", 100), ("sol", 150)):
            ledger.record_wire("thread1", model, usage(total), task=str(self.store.path), agent="coordinator")
        rows = read_json(self.store.path / "token-ledger.json")
        self.assertEqual(rows["codex-thread:thread1"]["models"]["luna"]["total"], 100)
        self.assertEqual(rows["codex-thread:thread1"]["models"]["sol"]["total"], 50)
        self.assertIn("**150**", ledger.refresh(self.store))

    def test_both_engines_roll_over_with_real_fake_client_processes(self):
        fixture = Path(__file__).with_name("fake_rollover_client.py")
        binary = self.root / "bin"
        binary.mkdir()
        for engine in ("claude", "codex"):
            # A tiny executable wrapper ensures actual Popen/wait/termination,
            # environment binding, immutable checkpoints and ledger publication.
            script = binary / engine
            import shlex
            script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(fixture)]) + ' "$@"\n')
            script.chmod(0o755)
            store = Store.create(self.root / engine, "rollover", self.root)
            with patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")}), \
                 patch.object(runtime, "verify_codex"), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(workflow.launch(store, "coordinator", engine, rollover_tokens=100), 0)
            runs = [read_json(path) for path in store.agent_path("coordinator").glob("runs/*/run.json")]
            self.assertEqual(len(runs), 2)
            self.assertEqual(sorted(row["status"] for row in runs), ["exited", "reconciled"])
            closed = next(row for row in runs if row["status"] == "reconciled")
            self.assertIs(closed["planned_rollover"]["external_completion_inferred"], False)
            self.assertEqual(sum(bool(row.get("rollover_checkpoint")) for row in runs), 1)
            self.assertIn("**230**", (store.path / "TOKEN_LEDGER.md").read_text())

    def test_codex_preflight_requires_every_hook_to_be_trusted(self):
        command = runtime.hooks()["Stop"][0]["hooks"][0]["command"]
        rows = [{"eventName": event[0].lower() + event[1:], "command": command,
                 "source": "sessionFlags", "enabled": True, "trustStatus": "trusted"}
                for event in runtime.EVENTS]
        runtime.validate_hooks({"data": [{"hooks": rows}]})
        rows[-1]["trustStatus"] = "untrusted"
        with self.assertRaisesRegex(ValueError, "need trust"):
            runtime.validate_hooks({"data": [{"hooks": rows}]})
        with self.assertRaisesRegex(ValueError, "all required"):
            runtime.validate_hooks({"data": [{"hooks": rows[:-1]}]})

    def test_untrusted_codex_fails_before_creating_task(self):
        tasks = self.root / "not-created"
        with patch.object(runtime, "verify_codex", side_effect=ValueError("hooks need trust")), \
             patch.object(workflow.shutil, "which", return_value="codex"), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = workflow.main(["run", "test", "--engine", "codex", "--root", str(tasks),
                                "--workspace", str(self.root), "--rollover-tokens", "500k"])
        self.assertEqual(rc, 2)
        self.assertFalse(tasks.exists())


if __name__ == "__main__":
    unittest.main()
