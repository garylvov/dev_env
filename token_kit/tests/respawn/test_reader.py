"""Guard for the lane respawn reader.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

The language-neutral case file is the real contract:

    bash <auto_resume v0>/run_respawn_cases.sh src/token_kit/respawn/bin/respawn-reader

These cases carry the same intent in-process, plus the two the case file cannot
express (fail-open on malformed stdin, and the executable entry point actually
running as a hook would run it).

Cases, in order below:
  1 a spawn with no respawn row says nothing, but the lane IS registered
  2 a recycled lane is reported to the main thread on PostToolUse(Agent)
  3 the same row is never reported twice
  4 consumption is an APPENDED ledger row; the request file is byte-identical
  5 Stop sweeps a lane registered earlier in the session (background case)
  6 another session's Stop sweeps nothing
  7 the 4th recycle flips the message from "respawn" to STOP
  8 stop_hook_active short-circuits -- no turn loop
  9 a non-Agent PostToolUse is ignored
 10 SubagentStop is silent (its additionalContext reaches the SUBAGENT)
 11 an unregistered lane dir is never registered
 12 malformed stdin FAILS OPEN: rc 0, no stdout, one error row
 13 the bin/ entry point works end to end over a pipe
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit.respawn import reader  # noqa: E402

ENTRY = KIT_DIR / "src" / "token_kit" / "respawn" / "bin" / "respawn-reader"
ROW = "2026-09-20T22:00:00-04:00\tlane={lane}\tagent=ag-{n}\tcalls={n}\treason=floor\n"


def post(session, lane, tool="Agent"):
    return {"hook_event_name": "PostToolUse", "session_id": session,
            "tool_name": tool, "tool_input": {"prompt": f"LANE_DIR: {lane}\nbrief"}}


def stop(session, active=False):
    return {"hook_event_name": "Stop", "session_id": session,
            "stop_hook_active": active}


class ReaderCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.lane = root / "lane"
        self.other = root / "other"
        self.lane.mkdir()
        self.other.mkdir()
        self.cfg = reader.Config(state_root=str(root / "state"), max_respawns=3)
        Path(self.cfg.state_root).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def recycle(self, lane, n):
        with (lane / "RESPAWN_REQUEST.md").open("a") as fh:
            fh.write(ROW.format(lane=lane, n=n))

    def ledger_rows(self, lane):
        return reader.rows_of(lane / "RESPAWN_CONSUMED.md")

    # 1
    def test_spawn_with_no_row_is_silent_but_registers(self):
        self.assertEqual("", reader.handle(self.cfg, post("s1", self.lane)))
        self.assertIn(str(self.lane), reader.registered_lanes(self.cfg, "s1"))

    # 2, 3, 4
    def test_reported_once_appended_request_untouched(self):
        self.recycle(self.lane, 231)
        before = (self.lane / "RESPAWN_REQUEST.md").read_bytes()
        msg = reader.handle(self.cfg, post("s1", self.lane))
        self.assertIn("recycled at 231 calls", msg)
        self.assertIn("respawn a fresh agent", msg)
        self.assertEqual(1, self.ledger_rows(self.lane))
        self.assertEqual("", reader.handle(self.cfg, post("s1", self.lane)))
        self.assertEqual(1, self.ledger_rows(self.lane))
        self.assertEqual(before, (self.lane / "RESPAWN_REQUEST.md").read_bytes())

    # 5, 6
    def test_stop_sweeps_only_the_owning_session(self):
        reader.handle(self.cfg, post("s1", self.lane))       # register
        self.recycle(self.lane, 245)
        self.assertEqual("", reader.handle(self.cfg, stop("s9999")))
        self.assertEqual(0, self.ledger_rows(self.lane))
        msg = reader.handle(self.cfg, stop("s1"))
        self.assertIn("recycled at 245 calls", msg)
        self.assertIn("respawn 1 of 3", msg)

    # 7
    def test_fourth_recycle_flips_to_stop(self):
        reader.handle(self.cfg, post("s1", self.lane))
        for n in (1, 2, 3, 4):
            self.recycle(self.lane, n)
            msg = reader.handle(self.cfg, stop("s1"))
        self.assertTrue(msg.startswith("STOP"), msg)
        self.assertIn("recycled 4 times", msg)
        self.assertIn("out.md yourself", msg)
        self.assertNotIn("respawn a fresh agent", msg)
        self.assertEqual(4, self.ledger_rows(self.lane))

    # 8
    def test_stop_hook_active_short_circuits(self):
        reader.handle(self.cfg, post("s1", self.lane))
        self.recycle(self.lane, 12)
        self.assertEqual("", reader.handle(self.cfg, stop("s1", active=True)))
        self.assertEqual(0, self.ledger_rows(self.lane))

    # 9, 10
    def test_non_agent_and_subagentstop_are_ignored(self):
        self.recycle(self.lane, 13)
        self.assertEqual("", reader.handle(self.cfg, post("s1", self.lane, tool="Read")))
        self.assertEqual("", reader.handle(
            self.cfg, {"hook_event_name": "SubagentStop", "session_id": "s1",
                       "agent_id": "ag-1"}))
        self.assertEqual(0, self.ledger_rows(self.lane))

    # 11
    def test_absent_lane_dir_is_not_registered(self):
        ghost = Path(self.tmp.name) / "ghost"
        reader.handle(self.cfg, post("s1", ghost))
        self.assertEqual([], reader.registered_lanes(self.cfg, "s1"))

    # 14 -- THE ROLLOVER CASE. A background lane spawned before a rollover
    #       must still be swept after it, when the session id has changed.
    def test_a_rollover_between_spawn_and_stop_still_sweeps(self):
        campaign = str(Path(self.tmp.name) / "campaign")
        Path(campaign).mkdir()
        spawn = dict(post("s-before", self.lane), cwd=campaign)
        self.assertEqual("", reader.handle(self.cfg, spawn))
        # ... the lane dies in the background, AFTER the supervisor rolled the
        # session over. New session id, same campaign, same cwd.
        self.recycle(self.lane, 244)
        rolled = dict(stop("s-after"), cwd=campaign)
        msg = reader.handle(self.cfg, rolled)
        self.assertIn("recycled at 244 calls", msg,
                      "the sweep lost the lane at the rollover")
        self.assertEqual(1, self.ledger_rows(self.lane))

    # 15 -- and it is still not a global sweep: a DIFFERENT campaign sees
    #       nothing, which is what keeps case 6 honest.
    def test_another_campaign_still_sweeps_nothing(self):
        mine = str(Path(self.tmp.name) / "campaign_a")
        theirs = str(Path(self.tmp.name) / "campaign_b")
        for d in (mine, theirs):
            Path(d).mkdir()
        reader.handle(self.cfg, dict(post("s1", self.lane), cwd=mine))
        self.recycle(self.lane, 12)
        self.assertEqual("", reader.handle(self.cfg, dict(stop("s9999"), cwd=theirs)))
        self.assertEqual(0, self.ledger_rows(self.lane))

    # 12 -- the hook fails OPEN, loudly
    def test_malformed_stdin_fails_open_with_a_row(self):
        proc = subprocess.run(
            [sys.executable, "-m", "token_kit.respawn.reader",
             "--state-root", self.cfg.state_root],
            input="{not json", capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(KIT_DIR / "src"),
                 "HOME": self.tmp.name})
        self.assertEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout.strip())
        self.assertIn("error", (Path(self.cfg.state_root)
                                / "respawn_reader_errors.log").read_text())

    # 13 -- the entry point a hook registration actually names
    def test_entry_point_over_a_pipe(self):
        self.recycle(self.lane, 99)
        proc = subprocess.run(
            [str(ENTRY), "--state-root", self.cfg.state_root],
            input=json.dumps(post("s1", self.lane)),
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual("PostToolUse", out["hookSpecificOutput"]["hookEventName"])
        self.assertIn("recycled at 99 calls",
                      out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
