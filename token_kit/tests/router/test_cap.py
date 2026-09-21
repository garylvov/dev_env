"""The per-subagent call cap: counting, the two thresholds, and the floor hole.

The trap this guards: a hard deny at the cap denies the very Write the dying
agent needs to save its state, so the lane dies with nothing.  The floor must
let out.md through and nothing else.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from . import KIT_DIR  # noqa: F401
from token_kit.router import hook as hook_mod
from .runner import FIXTURE_MATRIX


class CapHarness(unittest.TestCase):
    CWD = "/proj/root"          # a synthetic project root; never touched on disk

    def setUp(self):
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lane = self.tmp / "lanes" / "demo" / "v0"
        self.lane.mkdir(parents=True)
        matrix = self.tmp / "m.toml"
        matrix.write_text(
            FIXTURE_MATRIX.read_text(encoding="utf-8").replace("$AGENTS_DIR",
                                                               str(self.tmp / "agents")),
            encoding="utf-8")
        os.environ.update({
            "LANE_RECYCLER_MATRIX": str(matrix),
            "LANE_RECYCLER_STATE": str(self.tmp / "state"),
            "LANE_RECYCLER_PROJECTS_ROOT": str(self.tmp / "projects"),
            "TOKEN_KIT_CODEX_SLOT_DIR": str(self.tmp / "slots"),
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def transcript(self, agent_id: str, calls: int, session: str = "s1",
                   lane_line: str | None = None) -> Path:
        d = (self.tmp / "projects" / hook_mod.slug(self.CWD) / session / "subagents")
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"agent-{agent_id}.jsonl"
        first = lane_line if lane_line is not None else f"LANE_DIR: {self.lane}"
        lines = [json.dumps({"type": "user",
                             "message": {"content": [{"type": "text", "text": first}]}})]
        # two tool_use BLOCKS in ONE record: the count is of blocks, not records
        pair = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"}, {"type": "tool_use", "name": "Read"}]}})
        one = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"}]}})
        lines += [pair] * (calls // 2) + [one] * (calls % 2)
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f

    def call(self, agent_id: str, tool: str = "Bash", target: str = "",
             session: str = "s1") -> dict:
        payload = {"agent_id": agent_id, "session_id": session, "cwd": self.CWD,
                   "hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": {"file_path": target} if target else {}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            hook_mod.handle(payload)
        raw = buf.getvalue().strip()
        return json.loads(raw)["hookSpecificOutput"] if raw else {}


class TestCounting(CapHarness):
    def test_counts_tool_use_blocks_not_records(self):
        f = self.transcript("a1", 10)
        self.assertEqual(hook_mod.count_calls(f), 10)
        self.assertEqual(len(f.read_text().splitlines()), 6)   # 1 brief + 5 records

    def test_lane_dir_from_the_brief_and_from_the_in_md_fallback(self):
        self.assertEqual(hook_mod.lane_dir_of(self.transcript("a2", 2)), str(self.lane))
        f = self.transcript("a3", 2, lane_line=f"your brief is {self.lane}/in.md")
        self.assertEqual(hook_mod.lane_dir_of(f), str(self.lane))

    def test_an_unreadable_transcript_allows_and_says_so(self):
        self.assertEqual(self.call("ghost"), {})
        events = (self.tmp / "state" / "s1" / "ghost" / "events.tsv").read_text()
        self.assertIn("unresolved", events)


class TestThresholds(CapHarness):
    def test_below_the_warn_the_hook_has_no_opinion(self):
        self.transcript("b1", 10)
        self.assertEqual(self.call("b1"), {})

    def test_the_main_thread_is_never_capped(self):
        payload = {"session_id": "s1", "cwd": self.CWD, "tool_name": "Bash", "tool_input": {}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            hook_mod.handle(payload)
        self.assertEqual(buf.getvalue(), "", "no agent_id means the main thread")

    def test_warn_denies_exactly_once(self):
        self.transcript("b2", 160)
        first = self.call("b2")
        self.assertEqual(first["permissionDecision"], "deny")
        self.assertIn("calls=160 warn=150", first["permissionDecisionReason"])
        self.assertEqual(self.call("b2"), {}, "the next call must go through")

    def test_the_floor_permits_only_the_lane_s_own_out_md_and_the_handback(self):
        self.transcript("b3", 235)
        denied = self.call("b3", "Bash")
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("FLOOR", denied["permissionDecisionReason"])
        self.assertEqual(self.call("b3", "Write", str(self.lane / "out.md")), {})
        self.assertEqual(self.call("b3", "SubagentHandback"), {})
        other = self.call("b3", "Write", str(self.lane / "notes.md"))
        self.assertEqual(other["permissionDecision"], "deny")

    def test_the_floor_files_a_respawn_request_once(self):
        self.transcript("b4", 235)
        self.call("b4")
        self.call("b4")
        rows = (self.lane / "RESPAWN_REQUEST.md").read_text().splitlines()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("reason=floor", rows[0])

    def test_past_the_hard_ceiling_the_wording_escalates(self):
        self.transcript("b5", 260)
        r = self.call("b5")
        self.assertIn("PAST HARD CEILING", r["permissionDecisionReason"])
        self.assertIn("calls=260 hard=250", r["permissionDecisionReason"])
        self.assertEqual(self.call("b5", "Write", str(self.lane / "out.md")), {},
                         "the out.md write is STILL never denied")

    def test_the_budget_is_a_READ_of_the_matrix(self):
        """Change the number in the TOML and the cap changes. Nothing else."""
        self.transcript("b6", 10)
        self.assertEqual(self.call("b6"), {})
        path = Path(os.environ["LANE_RECYCLER_MATRIX"])
        path.write_text(path.read_text().replace("warn = 150", "warn = 5"), encoding="utf-8")
        r = self.call("b6")
        self.assertEqual(r["permissionDecision"], "deny")
        self.assertIn("warn=5", r["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
