"""Guard for the Stop-hook handoff nudge.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

THE CONTRACT THIS RELIES ON, verified against the installed CLI's own bundle
rather than from memory: a Stop hook blocks by printing
`{"decision": "block", "reason": "..."}`; the reason reaches the model as
"Stop hook feedback"; the input carries `stop_hook_active`, which the bundle's
own message says a hook must honour ("check stop_hook_active in the input and
return success while it's true"); and consecutive blocks are capped
(CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, default 8) after which the CLI ends the turn
regardless. So this nudge blocks at most once, never while stop_hook_active is
true, and at most once every `nudge_every_mins`.

Cases, in order below:
  1 a state file the supervisor registered for this cwd, plus enough tool calls
    since it was last written, is ONE block with a short reason
  2 the reason names the file, the age, and the retitle escape hatch
  3 below the call floor: silence, and a ledger row saying why
  4 inside the rate-limit window: silence, and a ledger row saying why
  5 stop_hook_active: silence, and NO row (the CLI is already re-running us)
  6 no supervised file and no ./STATE.md: silence, no row, nothing invented
  7 ./STATE.md is the fallback when nothing is registered
  8 calls are counted from the CLI's own transcript, after the file's mtime,
    main thread only: a subagent's calls are not the main thread's to answer for
  9 a lane respawn message and a nudge arrive together, in one block
 10 the executable entry point does all of this over a pipe, and costs what it
    costs (the measurement is printed, not asserted: it is machine dependent)
 11 FAIL OPEN: an unreadable transcript is silence, never a wedged turn
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit.respawn import reader  # noqa: E402

ENTRY = KIT_DIR / "src" / "token_kit" / "respawn" / "bin" / "respawn-reader"


def tool_record(when: datetime, sidechain: bool = False, n: int = 1) -> str:
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain,
        "timestamp": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": {"content": [{"type": "tool_use", "name": "Read", "id": f"t{i}"}
                                for i in range(n)]}})


class NudgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.state = self.cwd / "STATE.md"
        self.state.write_text("# handoff\n")
        self.registry = self.root / "registry.tsv"
        self.registry.write_text(f"2026-09-21T10:00:00-0400\t{self.cwd}\t{self.state}\n")
        self.transcript = self.root / "session.jsonl"
        self.write_calls(30)

    def write_calls(self, n, sidechain=False, before=False):
        when = datetime.now(timezone.utc) + timedelta(seconds=-600 if before else 5)
        self.transcript.write_text("\n".join(tool_record(when, sidechain)
                                             for _ in range(n)) + "\n")

    def cfg(self, **over):
        c = reader.Config(state_root=str(self.root / "state"))
        c.supervise_registry = str(self.registry)
        c.nudge_min_calls = 25
        c.nudge_every_mins = 20
        for key, value in over.items():
            setattr(c, key, value)
        return c

    def event(self, **over):
        out = {"hook_event_name": "Stop", "session_id": "sess-1",
               "cwd": str(self.cwd), "transcript_path": str(self.transcript),
               "stop_hook_active": False}
        out.update(over)
        return out

    def rows(self, cfg):
        try:
            return [r.split("\t") for r in
                    cfg.nudge_ledger.read_text().splitlines()]
        except OSError:
            return []

    # ---------------------------------------------------------------- cases
    def test_1_enough_calls_since_the_last_write_is_one_nudge(self):
        cfg = self.cfg()
        first = reader.nudge(cfg, self.event())
        self.assertTrue(first, "no nudge at 30 calls with a floor of 25")
        self.assertEqual(["blocked"], [r[5] for r in self.rows(cfg)])
        # ONE nudge: the second stop in the window is silent.
        self.assertEqual("", reader.nudge(cfg, self.event()))

    def test_2_the_reason_is_short_and_names_the_file_and_the_way_out(self):
        text = reader.nudge(self.cfg(), self.event())
        self.assertIn(str(self.state), text)
        self.assertIn("30 tool calls", text)
        self.assertIn("token-kit-task retitle", text,
                      "the nudge must say how to fix a title that has gone stale")
        self.assertLess(len(text), 500, "the nudge is a nudge, not a briefing")

    def test_3_below_the_call_floor_is_silence_with_a_reason_row(self):
        cfg = self.cfg(nudge_min_calls=100)
        self.assertEqual("", reader.nudge(cfg, self.event()))
        self.assertEqual(["below_call_floor"], [r[5] for r in self.rows(cfg)])

    def test_4_the_rate_limit_is_ledger_visible(self):
        cfg = self.cfg()
        reader.nudge(cfg, self.event())
        self.assertEqual("", reader.nudge(cfg, self.event()))
        self.assertEqual(["blocked", "rate_limited"], [r[5] for r in self.rows(cfg)])
        # and it lifts once the window has passed: age the "blocked" row by
        # half an hour, which is what waiting would have done.
        old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(
            timespec="seconds")
        aged = [r for r in cfg.nudge_ledger.read_text().splitlines()]
        aged[0] = "\t".join([old] + aged[0].split("\t")[1:])
        cfg.nudge_ledger.write_text("\n".join(aged) + "\n")
        self.assertTrue(reader.nudge(cfg, self.event()))

    def test_5_stop_hook_active_is_honoured_and_leaves_no_row(self):
        cfg = self.cfg()
        self.assertEqual("", reader.nudge(cfg, self.event(stop_hook_active=True)))
        self.assertEqual([], self.rows(cfg))

    def test_6_no_handoff_anywhere_is_complete_silence(self):
        self.state.unlink()
        self.registry.write_text("")
        cfg = self.cfg()
        self.assertEqual("", reader.nudge(cfg, self.event()))
        self.assertEqual([], self.rows(cfg), "a row about a file nobody chose")
        self.assertIsNone(reader.state_file_for(cfg, str(self.cwd)))

    def test_7_state_md_in_the_cwd_is_the_fallback(self):
        self.registry.write_text("")
        cfg = self.cfg()
        self.assertEqual(self.state, reader.state_file_for(cfg, str(self.cwd)))
        self.assertTrue(reader.nudge(cfg, self.event()))

    def test_7b_the_registered_file_wins_over_the_fallback(self):
        other = self.root / "elsewhere.md"
        other.write_text("# the supervised handoff\n")
        self.registry.write_text(f"2026-09-21T11:00:00-0400\t{self.cwd}\t{other}\n")
        self.assertEqual(other, reader.state_file_for(self.cfg(), str(self.cwd)))

    def test_8_only_main_thread_calls_after_the_last_write_are_counted(self):
        mtime = self.state.stat().st_mtime
        self.write_calls(30, sidechain=True)
        self.assertEqual(0, reader.tool_calls_since(str(self.transcript), mtime),
                         "a subagent's calls were counted as the main thread's")
        self.write_calls(30, before=True)
        self.assertEqual(0, reader.tool_calls_since(str(self.transcript), mtime),
                         "calls from before the file was written were counted")
        self.write_calls(7)
        self.assertEqual(7, reader.tool_calls_since(str(self.transcript), mtime))

    def test_9_a_respawn_message_and_a_nudge_arrive_in_one_block(self):
        lane = self.root / "lane"
        lane.mkdir()
        (lane / "RESPAWN_REQUEST.md").write_text(
            "2026-09-21T22:00:00-0400\tlane=x\tagent=ag-1\tcalls=40\treason=floor\n")
        cfg = self.cfg()
        reader.register(cfg, "sess-1", str(lane), str(self.cwd))
        out = self.run_entry(cfg, self.event())
        self.assertEqual("block", out["decision"])
        self.assertIn("respawn a fresh agent", out["reason"])
        self.assertIn("token-kit-task retitle", out["reason"])

    def run_entry(self, cfg, event) -> dict:
        """The executable, exactly as settings.json invokes it."""
        conf = self.root / "reader.toml"
        conf.write_text(f'[respawn]\nstate_root = "{cfg.state_root}"\n'
                        f'supervise_registry = "{self.registry}"\n'
                        f'nudge_min_calls = {cfg.nudge_min_calls}\n'
                        f'nudge_every_mins = {cfg.nudge_every_mins}\n')
        started = time.monotonic()
        p = subprocess.run([str(ENTRY), "--config", str(conf)],
                           input=json.dumps(event), capture_output=True, text=True)
        self.cost_ms = (time.monotonic() - started) * 1000
        self.assertEqual(0, p.returncode, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_10_the_entry_point_blocks_over_a_pipe_and_costs_what_it_costs(self):
        out = self.run_entry(self.cfg(), self.event())
        self.assertEqual("block", out["decision"])
        self.assertIn(str(self.state), out["reason"])
        # MEASURED, not asserted: the number belongs to whatever machine ran it.
        print(f"\n  Stop nudge hook cost: {self.cost_ms:.0f} ms end to end "
              f"(one subprocess, {len(self.transcript.read_text().splitlines())} "
              "transcript lines)")

    def test_10b_silence_is_empty_stdout_not_an_empty_json_object(self):
        out = self.run_entry(self.cfg(nudge_min_calls=1000), self.event())
        self.assertEqual({}, out)

    def test_11_an_unreadable_transcript_fails_open(self):
        cfg = self.cfg()
        self.assertEqual(0, reader.tool_calls_since(str(self.root / "gone.jsonl"), 0))
        self.assertEqual("", reader.nudge(cfg, self.event(transcript_path="")))


if __name__ == "__main__":
    unittest.main()
