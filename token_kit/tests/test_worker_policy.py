"""Offline policy delivery checks: no installed client or model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import worker_policy as policy, workflow
from token_kit.core.store import Store


class WorkerPolicyTests(unittest.TestCase):
    def test_orchestrator_role_and_blocked_delegation_are_explicit(self):
        matrix = (Path(__file__).resolve().parents[1] / "agent_trigger_matrix.md").read_text()
        self.assertIn("orchestrator, not an execution worker", matrix)
        self.assertIn("before substantial execution", matrix)
        self.assertIn("Every logical agent, including workers", matrix)
        self.assertIn("orchestrator only", policy.POLICY)
        self.assertIn("ask before execution", policy.POLICY)

    def test_session_map_is_the_only_model_source(self):
        from token_kit.project_install import INSTRUCTIONS
        self.assertIn("TASK/trigger_pyramid.md", INSTRUCTIONS)
        self.assertIn("Map supersedes default prose, not explicit assignments", policy.POLICY)
        self.assertIn("checkpoint/report to the main thread", policy.POLICY)
        self.assertIn("Announce route/tier/model/reason/changes", policy.POLICY)
        self.assertIn("Escalate for complexity, not delay", policy.POLICY)
        for text in (policy.POLICY, INSTRUCTIONS):
            self.assertNotIn("Opus medium -> Astra medium", text)
            self.assertNotIn("Never auto-fallback to Fable", text)

    def test_compaction_recovery_and_history_policy_are_bounded(self):
        self.assertIn("Managed recovery: structured compaction or exact legacy run/runtime evidence",
                      policy.POLICY)
        self.assertIn("never unknown stops/errors/manual interrupts",
                      policy.POLICY)
        self.assertIn("no initial/routine checkpoint/recovery append", policy.POLICY)
        self.assertIn("Dead PID proves no external\ncompletion; parent exit no native closure",
                      policy.POLICY)

    def test_native_nested_parent_tracking_is_in_all_shared_guidance(self):
        from token_kit.project_install import INSTRUCTIONS
        matrix = (Path(__file__).resolve().parents[1] / "agent_trigger_matrix.md").read_text()
        for text in (policy.POLICY, INSTRUCTIONS, matrix):
            self.assertIn("native", text)
            self.assertIn("--parent", text)
            self.assertIn("status TASK", text)
        self.assertIn("not implemented", matrix)

    def test_matrix_separates_worktree_indexes_and_source_integration(self):
        matrix = (Path(__file__).resolve().parents[1] / "agent_trigger_matrix.md").read_text()
        section = matrix.split("### CodeGraph: worktree ownership and freshness", 1)[1]
        for requirement in ("One index per worktree", "--absolute-git-dir",
                            "dirty and untracked", "Never merge SQLite",
                            "separate tasks with explicit --workspace", "one coordinated writer",
                            "not an implemented Token Kit index supervisor"):
            self.assertIn(requirement, section)

    def test_brief_is_compact_idempotent_and_preserves_assignment(self):
        text = 'Only src/parser.py. Use Codex. $(echo nope)\n"quoted"'
        result = policy.brief(text, "/tasks/one", "parser")
        self.assertTrue(result.endswith(text))
        self.assertLess(len(result.encode()), 2500)
        self.assertEqual(policy.brief(result, "/tasks/one"), result)
        self.assertIn('"agent": "parser"', result)

    def test_managed_transport_is_opt_in_and_never_copies_parent_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(policy.managed_brief("work"), "work")
        with patch.dict(os.environ, {"TOKEN_KIT_TASK": "/task", "TOKEN_KIT_AGENT": "parent-secret"}):
            result = policy.managed_brief("work")
        self.assertIn(policy._OPEN, result)
        self.assertNotIn("parent-secret", result)

    def test_reused_brief_gets_new_registered_identity(self):
        parent = policy.brief("work", "/task", "parent")
        child = policy.brief(parent, "/task", "child")
        self.assertNotIn('"agent": "parent"', child)
        self.assertIn('"agent": "child"', child)
        self.assertEqual(child.count(policy._OPEN), 1)
        self.assertTrue(child.endswith("work"))

    def test_legacy_and_standalone_prefixes_are_refreshed(self):
        legacy = "<!-- token-kit worker policy v0 -->\nOld rules.\n<!-- /token-kit worker policy -->"
        body = "  Work exactly here.\n\nKeep trailing space.  \n"
        for wrapper in (legacy + "\n\n", legacy + "\n", policy.POLICY + "\n\n",
                        legacy + '\nToken Kit record: {"task": "/task", "agent": "worker"}\n\n'):
            with self.subTest(wrapper=wrapper[:50]):
                result = policy.brief(wrapper + body, "/task", "worker")
                self.assertEqual(result, policy.brief(body, "/task", "worker"))
                self.assertEqual(policy.brief(result, "/task"), result)

    def test_stacked_prefixes_preserve_outer_identity_and_exact_body(self):
        body = "\nAssignment.\n\n" + policy.POLICY + "\nThis is an interior example.\n"
        legacy = "<!-- token-kit worker policy v2.1 -->\nLegacy.\n<!-- /token-kit worker policy -->"
        inner = policy.brief(body, "/old-task", "old-worker")
        stacked = legacy + '\nToken Kit record: {"task": "/task", "agent": "worker"}\n\n' + inner
        self.assertEqual(policy.brief(stacked, "/task"), policy.brief(body, "/task", "worker"))
        self.assertEqual(policy.brief(stacked, "/task", "new"), policy.brief(body, "/task", "new"))
        self.assertEqual(policy.brief(stacked, "/other"), policy.brief(body, "/other"))
        standalone = legacy + "\n\n" + policy.POLICY + "\n\n" + inner
        self.assertEqual(policy.brief(standalone, "/task"), policy.brief(body, "/task"))

    def test_policy_examples_and_malformed_wrappers_are_not_erased(self):
        for body in ("Example:\n" + policy.POLICY,
                     "```\n" + policy.POLICY + "\n```",
                     "> " + policy.POLICY.replace("\n", "\n> "),
                     "<!-- token-kit worker policy v0 -->\nDo this task.",
                     "<!-- token-kit worker policy v0 -->\nDo this task.\n" + policy.POLICY,
                     policy.POLICY + '\nToken Kit record: {broken}\n\nDo this task.',
                     policy.POLICY + '\nToken Kit record: []\n\nDo this task.',
                     policy.POLICY + '\nToken Kit record: {"task": 123}\n\nDo this task.',
                     policy.POLICY + '\nToken Kit record: {"task": "/task", "agent": 1}\n\nTask.',
                     policy.POLICY + '\nToken Kit record: {"task": "/task"}\nTask.'):
            with self.subTest(body=body[-80:]):
                expected = policy.brief("", "/task") + body
                self.assertEqual(policy.brief(body, "/task"), expected)
                self.assertEqual(policy.brief(expected, "/task"), expected)

    def test_hook_changes_only_prompt_without_approving_tool(self):
        for name in ("Agent", "Task"):
            args = {"prompt": "Scout only; Luna xhigh", "model": "sonnet", "resume": "native-id",
                    "run_in_background": True}
            payload = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}
            output = policy.rewrite(payload, "/task")["hookSpecificOutput"]
            self.assertNotIn("permissionDecision", output)
            self.assertEqual({**output["updatedInput"], "prompt": args["prompt"]}, args)
            self.assertEqual(args["prompt"], "Scout only; Luna xhigh")
            payload["tool_input"] = output["updatedInput"]
            self.assertEqual(policy.rewrite(payload, "/task"), {})

    def test_other_tools_events_and_invalid_shapes_are_untouched(self):
        for payload in (None, [], {}, {"hook_event_name": "PostToolUse"},
                        {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                        {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": []}):
            self.assertEqual(policy.rewrite(payload, "/task"), {})

    def test_hook_command_runs_without_package_path_and_quotes_task(self):
        task = "/task space/'quoted;literal"
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"prompt": "work"}}
        result = subprocess.run(shlex.split(policy.hook_command(task)), input=json.dumps(payload),
                                text=True, capture_output=True, env={"PATH": os.defpath}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["prompt"]
        self.assertIn(json.dumps(task), prompt)

    def test_malformed_hook_input_fails_visibly(self):
        result = subprocess.run(shlex.split(policy.hook_command("/task")), input="{broken",
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not be applied", result.stderr)

    def test_saved_assignment_and_worker_launch_have_policy_without_project_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "source"
            workspace.mkdir()
            store = Store.create(root / "tasks", "Fix parser", workspace)
            agent = store.add_agent("parser", "Fix only parser. Use Codex.")
            self.assertIn(policy._OPEN, (agent / "in.md").read_text())
            self.assertNotIn(policy._OPEN, (agent / "STATE.md").read_text())
            bundle = store.resume_bundle("parser")
            self.assertEqual(Path(bundle["assignment"]).read_text(), (agent / "in.md").read_text())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(workflow.launch(store, "parser", "claude", dry_run=True), 0)
            argv = json.loads(output.getvalue())["argv"]
            self.assertIn(policy._OPEN, argv[-1])
            settings = json.loads(argv[argv.index("--settings") + 1])
            self.assertEqual(settings["env"], {"DISABLE_COMPACT": "1"})
            hook = settings["hooks"]["PreToolUse"][0]
            self.assertEqual(hook["matcher"], "^(Agent|Task)$")
            self.assertIn(str(store.path), hook["hooks"][0]["command"])
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
