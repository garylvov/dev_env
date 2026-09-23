"""A display name must never become an implicitly submitted task."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import runtime, workflow
from token_kit.adapters import claude, codex
from token_kit.adapters.base import LaunchRequest
from token_kit.core.store import Store, read_json


class IdleStartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tasks = self.root / "tasks"

    def idle_runtime(self):
        store = Store.create(self.tasks, "Label", self.root)
        run = store.claim_run("coordinator", "codex", True)
        runtime.initialize(store, "coordinator", run, "codex", 500000,
                           session_context="New idle Token Kit session. No task has been submitted.")
        return store, run

    def test_idle_missing_hook_survives_startup_timeout(self):
        store, run = self.idle_runtime()
        child = Mock()
        child.poll.side_effect = [None, None, 0]
        child.wait.side_effect = subprocess.TimeoutExpired("client", 0.5)
        stop = Mock()
        with patch.object(runtime.time, "monotonic", side_effect=[0, 31, 120]), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            rc, control = runtime.wait_segment(child, store, "coordinator", run, stop)
        self.assertEqual(rc, 0)
        self.assertEqual(control["phase"], "running")
        self.assertTrue(control["awaiting_input"])
        stop.assert_not_called()
        self.assertEqual(output.getvalue().count("keeping the idle session open"), 1)

    def test_first_submission_is_fallback_handshake_and_delivers_context_once(self):
        store, run = self.idle_runtime()
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "parent"}
        result = runtime.handle(store, "coordinator", run, payload)
        self.assertIn("first instruction", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("No task has been submitted", str(result))
        control = read_json(run / "runtime.json")
        self.assertEqual(control["session_id"], "parent")
        self.assertFalse(control["awaiting_input"])
        self.assertEqual(runtime.handle(store, "coordinator", run, payload), {})
        self.assertEqual(runtime.handle(store, "coordinator", run,
                         {**payload, "hook_event_name": "SessionStart"}), {})

    def test_work_without_parent_handshake_fails_closed(self):
        for payload in ({"hook_event_name": "PostToolUse", "session_id": "parent"},
                        {"hook_event_name": "UserPromptSubmit"},
                        {"hook_event_name": "UserPromptSubmit", "session_id": "parent",
                         "agent_id": "child"}):
            store, run = self.idle_runtime()
            result = runtime.handle(store, "coordinator", run, payload)
            self.assertFalse(result["continue"])
            self.assertEqual(read_json(run / "runtime.json")["phase"], "halted")

    def test_halted_session_is_not_blindly_restarted(self):
        store = Store.create(self.tasks, "Label", self.root)
        with patch.object(workflow, "_launch_segment", return_value=(75, {"phase": "halted"})) as segment:
            self.assertEqual(workflow.launch(store, "coordinator", "claude",
                                             rollover_tokens=100, idle=True), 75)
        segment.assert_called_once()

    def test_adapters_omit_initial_message_for_idle_launch(self):
        for adapter in (claude, codex):
            plan = adapter.prepare_launch(LaunchRequest(self.root, None, managed_hooks=True), environ={})
            self.assertNotIn("--", plan.argv)
            self.assertNotIn(None, plan.argv)

    def test_session_title_is_metadata_not_assignment(self):
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.main(["run", "Delete all source files", "--workspace", str(self.root),
                                           "--root", str(self.tasks)]), 0)
        store = launch.call_args.args[0]
        self.assertTrue(launch.call_args.kwargs["idle"])
        self.assertIsNone(launch.call_args.kwargs["initial_prompt"])
        self.assertEqual(read_json(store.path / "task.json")["title"], "Delete all source files")
        for name in ("in.md", "STATE.md", "assignments/0001.md"):
            text = (store.agent_path("coordinator") / name).read_text()
            self.assertNotIn("Delete all source files", text)
            self.assertIn("No task assigned", text)

    def test_explicit_prompt_starts_only_the_requested_work(self):
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.main(["run", "Display label", "--prompt", "Explain parser.py only",
                                           "--workspace", str(self.root), "--root", str(self.tasks)]), 0)
        store = launch.call_args.args[0]
        self.assertFalse(launch.call_args.kwargs["idle"])
        self.assertEqual(launch.call_args.kwargs["initial_prompt"], "Explain parser.py only")
        text = (store.agent_path("coordinator") / "in.md").read_text()
        self.assertIn("Explain parser.py only", text)
        self.assertNotIn("Display label", text)

    def test_preview_is_idle_and_writes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(["run", "A label", "--workspace", str(self.root),
                                           "--root", str(self.tasks), "--dry-run"]), 0)
        self.assertEqual(json.loads(output.getvalue())["startup"], "idle")
        self.assertFalse(self.tasks.exists())

    def test_empty_prompt_and_resume_prompt_are_rejected_without_creation(self):
        for extra in (["--prompt", " "], ["--task", str(self.root / "missing"), "--prompt", "work"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(workflow.main(["run", *extra]), 2)
        self.assertFalse(self.tasks.exists())

    def test_idle_process_has_no_prompt_and_receives_compact_hook_context_once(self):
        for engine in ("claude", "codex"):
            store = Store.create(self.tasks, engine, self.root, assignment="No task assigned.")
            bundle = store.resume_bundle("coordinator")
            child = Mock(pid=99999999)
            with patch.object(store, "resume_bundle", return_value=bundle), \
                 patch.object(workflow.shutil, "which", return_value="/bin/client"), \
                 patch.object(workflow.subprocess, "Popen", return_value=child) as popen, \
                 patch.object(runtime, "ensure_codex_hooks"), \
                 patch.object(runtime, "wait_segment", return_value=(0, {})):
                self.assertEqual(workflow.launch(store, "coordinator", engine, rollover_tokens=100, idle=True), 0)
            self.assertNotIn("--", popen.call_args.args[0])
            run, = (store.agent_path("coordinator") / "runs").iterdir()
            self.assertFalse((run / "prompt.md").exists())
            self.assertEqual(read_json(run / "run.json")["startup"], "idle")
            payload = {"hook_event_name": "SessionStart", "session_id": "session"}
            result = runtime.handle(store, "coordinator", run, payload)
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("No task has been submitted", context)
            self.assertNotIn("Continue logical agent", context)
            self.assertLess(len(context.encode()), 4000)
            self.assertEqual(runtime.handle(store, "coordinator", run, payload), {})

    def test_rollover_after_idle_start_resumes_instead_of_waiting_again(self):
        store = Store.create(self.tasks, "Label", self.root)
        with patch.object(workflow, "_launch_segment", side_effect=[(0, {"phase": "ready"}), (0, {})]) as segment, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.launch(store, "coordinator", "claude", rollover_tokens=100, idle=True), 0)
        self.assertTrue(segment.call_args_list[0].kwargs["idle"])
        self.assertFalse(segment.call_args_list[1].kwargs["idle"])

    def test_explicit_resume_is_still_active(self):
        store = Store.create(self.tasks, "Real assignment", self.root)
        with patch.object(workflow.shutil, "which", return_value="/bin/claude"), \
             patch.object(workflow, "launch", return_value=0) as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.main(["run", "--task", str(store.path)]), 0)
        self.assertFalse(launch.call_args.kwargs["idle"])

    def test_real_offline_client_process_opens_without_submitted_message(self):
        # Exercise Popen/env/startup hooks without invoking either real client.
        binaries = self.root / "bin"
        binaries.mkdir()
        program = f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
from token_kit import runtime
from token_kit.core.store import Store
store = Store(os.environ["TOKEN_KIT_TASK"])
run = store.agent_path("coordinator") / "runs" / os.environ["TOKEN_KIT_RUN"]
assert "--" not in sys.argv, "An initial message was submitted"
result = runtime.handle(store, "coordinator", run,
                        {"hook_event_name": "SessionStart", "session_id": "offline-idle"})
assert "No task has been submitted" in result["hookSpecificOutput"]["additionalContext"]
(run / "offline-idle-ok.json").write_text(json.dumps({"argv": sys.argv}))
'''
        for engine in ("claude", "codex"):
            binary = binaries / engine
            binary.write_text(program)
            binary.chmod(0o755)
            store = Store.create(self.tasks, "Label", self.root, assignment="No task assigned.")
            with patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}):
                rc, control = workflow._launch_segment(store, "coordinator", engine, None,
                                                       False, False, 500000, idle=True)
            self.assertEqual(rc, 0)
            self.assertEqual(control["phase"], "running")
            run, = (store.agent_path("coordinator") / "runs").iterdir()
            self.assertTrue((run / "offline-idle-ok.json").is_file())
