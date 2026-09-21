"""Guard for the rollover supervisor.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

Nothing real is launched or killed by the threshold cases: `claude` and `tmux`
are recorder scripts written by the test, the session registry is a scratch
directory, and the rollover is redirected to a recording stub. The two kill
cases DO use a real process -- but one the test spawned itself, and the kill is
verified the only way the doctrine allows: the pid's kernel start time no
longer matching, never an exit code.

Cases, in order below:
  1 context_tokens takes the LAST main-thread assistant usage record and sums
    input + cache_read + cache_creation; a sidechain record must not move it
  2 a live session is one whose registry procStart still matches /proc
  3 the transcript path is derived from sessionId + the cwd slug
  4 below SOFT nothing happens beyond one poll row
  5 crossing SOFT emits exactly ONE soft action, never two
  6 in the drain band a BUSY session is a hold row with a reason, not a rollover
  7 in the drain band an IDLE session rolls over (reason drain_idle)
  8 a drain wait that has expired rolls over (reason drain_timeout)
  9 crossing HARD rolls over regardless (reason hard)
 10 a rollover is never repeated for the same pid
 11 launch records its flags, hands the seed as ONE argument naming the seed
    FILE, and resolves the new session's pid by descending from the pane
 12 seed_as_arg=0 removes the seed argument -- the knob this guard fails on
 13 an unresolvable pid is a loud row, not a crash, and autowatch=0 spawns nothing
 14 a kill that does not land is verified dead by START TIME and refuses
 15 a kill that lands is confirmed by the start time no longer matching
 16 a live watcher's lock is refused with a reason row; a stale one is a takeover
 17 status reports ALIVE on a fresh heartbeat and DEAD on a stale one
 18 `once` is one bounded pass: it returns without a second poll row
 19 dials come from TOML and CLI only; an unknown key refuses loudly
 20 the state file is an ARGUMENT: two state files in two directories get two
    run directories, two locks and two logs, and neither names a project
 21 the default state file is ./STATE.md in the current directory
 22 the SOFT request lands beside the state file, and the seed names it
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT_DIR / "src"))

from token_kit.supervisor import config as cfgmod  # noqa: E402
from token_kit.supervisor import main as sup  # noqa: E402
from token_kit.supervisor import session as S  # noqa: E402

PID = 4242


def usage(tokens, sidechain=False):
    return json.dumps({"type": "assistant", "isSidechain": sidechain,
                       "message": {"usage": {"input_tokens": tokens,
                                             "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": 0}}})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.home = self.root / "claude_home"
        (self.home / "sessions").mkdir(parents=True)
        self.work.mkdir()
        self.state_file = self.work / "STATE.md"
        self.state_file.write_text("# handoff\n")
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text("")
        self.rolled = self.root / "rolled.tsv"
        self.stub = self.recorder("rollover_stub", self.rolled)

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self, name, target):
        """A script that records the argv it was called with and exits 0."""
        path = self.root / name
        path.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "'
                        + str(target) + '"\nexit 0\n')
        path.chmod(0o755)
        return str(path)

    def register(self, pid, start, status="busy", sid="sess-1", cwd="/work/x"):
        (self.home / "sessions" / f"{pid}.json").write_text(json.dumps(
            {"sessionId": sid, "cwd": cwd, "status": status, "procStart": start}))

    def cfg(self, **over):
        dials = dict(state_file=str(self.state_file), run_root=str(self.root / "runs"),
                     claude_home=str(self.home),
                     soft_tokens=100, hard_tokens=1000, drain_wait_secs=600,
                     poll_secs=0, rollover_cmd=self.stub, launch_pid_wait_secs=2)
        dials.update(over)
        return cfgmod.load(None, dials)

    def rows(self, cfg, event):
        return [r for r in cfg.log.read_text().splitlines()
                if r.split("\t")[1] == event]

    def grow(self, tokens, sidechain=False):
        with self.transcript.open("a") as fh:
            fh.write(usage(tokens, sidechain) + "\n")


class Measurement(Fixture):
    def test_1_context_tokens_last_main_thread_record(self):
        self.grow(10)
        self.grow(120)
        self.grow(999999, sidechain=True)        # a subagent must not move it
        self.assertEqual(120, S.context_tokens(self.transcript))
        self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {"usage": {
                "input_tokens": 1, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 300}}}) + "\n")
        self.assertEqual(321, S.context_tokens(self.transcript))

    def test_2_live_session_is_pid_plus_matching_start_time(self):
        mine = os.getpid()
        self.register(mine, S.proc_start(mine))
        self.assertTrue(S.is_live_session(self.home, mine))
        self.register(mine, "999999999")          # pid reuse: same pid, new boot
        self.assertFalse(S.is_live_session(self.home, mine))
        self.assertEqual("", S.proc_start(999999))

    def test_3_transcript_derived_from_session_id_and_cwd_slug(self):
        self.register(PID, "1", cwd="/some_root/path-x", sid="abc")
        got = S.transcript_of_pid(self.home, PID)
        self.assertEqual(self.home / "projects" / "-some-root-path-x" / "abc.jsonl", got)


class Thresholds(Fixture):
    def setUp(self):
        super().setUp()
        self.register(PID, S.proc_start(os.getpid()))

    def test_4_below_soft_is_one_poll_and_nothing_else(self):
        cfg = self.cfg()
        self.grow(50)
        self.assertEqual("below", sup.one_pass(cfg, PID, self.transcript))
        self.assertEqual(1, len(self.rows(cfg, "poll")))
        self.assertFalse(cfg.request.exists())

    def test_5_soft_fires_exactly_once(self):
        cfg = self.cfg()
        self.grow(150)
        self.assertEqual("soft", sup.one_pass(cfg, PID, self.transcript, "busy"))
        self.assertEqual("drain", sup.one_pass(cfg, PID, self.transcript, "busy"))
        self.assertEqual(1, len(self.rows(cfg, "soft_request")))
        self.assertIn("SOFT threshold crossed", cfg.request.read_text())

    def test_6_busy_drain_is_a_hold_row_with_a_reason(self):
        cfg = self.cfg()
        self.grow(150)
        sup.one_pass(cfg, PID, self.transcript, "busy")
        sup.one_pass(cfg, PID, self.transcript, "busy")
        hold = self.rows(cfg, "drain_hold")
        self.assertEqual(1, len(hold))
        self.assertIn("reason=work_in_flight", hold[0])
        self.assertIn("release=idle_or_age_ge_600s", hold[0])
        self.assertFalse(self.rolled.exists())

    def test_7_idle_drain_rolls_over(self):
        cfg = self.cfg()
        self.grow(150)
        sup.one_pass(cfg, PID, self.transcript, "busy")
        self.assertEqual("rolled", sup.one_pass(cfg, PID, self.transcript, "idle"))
        self.assertIn("drain_idle", self.rolled.read_text())
        self.assertEqual(1, len(self.rows(cfg, "rollover_begin")))

    def test_8_expired_drain_wait_rolls_over(self):
        cfg = self.cfg(drain_wait_secs=0)
        self.grow(150)
        sup.one_pass(cfg, PID, self.transcript, "busy")
        self.assertEqual("rolled", sup.one_pass(cfg, PID, self.transcript, "busy"))
        self.assertIn("drain_timeout", self.rolled.read_text())

    def test_9_hard_rolls_over_regardless(self):
        cfg = self.cfg()
        self.grow(5000)
        self.assertEqual("rolled", sup.one_pass(cfg, PID, self.transcript, "busy"))
        self.assertIn("hard", self.rolled.read_text())

    def test_10_a_rollover_is_never_repeated(self):
        cfg = self.cfg()
        self.grow(5000)
        sup.one_pass(cfg, PID, self.transcript, "busy")
        sup.one_pass(cfg, PID, self.transcript, "busy")
        self.assertEqual(1, len(self.rows(cfg, "rollover_begin")))
        self.assertEqual(1, len(self.rows(cfg, "rollover_skipped")))
        self.assertEqual(1, len(self.rolled.read_text().splitlines()))

    def test_18_once_is_one_bounded_pass(self):
        cfg = self.cfg()
        self.grow(50)
        self.assertEqual(0, sup.cmd_watch(cfg, PID, str(self.transcript), "", once=True))
        self.assertEqual(1, len(self.rows(cfg, "poll")))
        self.assertFalse(cfg.lock_dir.exists())


class Launch(Fixture):
    """`claude` and `tmux` are recorders; a real `sleep` child stands in for the
    session so the pane->subtree->registry resolution has something to find."""

    def setUp(self):
        super().setUp()
        self.argv = self.root / "tmux.argv"
        self.child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap)
        self.pane_pid = self.child.pid

    def _reap(self):
        self.child.kill()
        self.child.wait()

    def fake_tmux(self, pane=True):
        path = self.root / "tmux"
        pane_line = f'echo {self.pane_pid}' if pane else 'true'
        path.write_text(
            '#!/usr/bin/env bash\n'
            f'printf "%s\\n" "$*" >> {self.argv}\n'
            f'if [ "$1" = list-panes ]; then {pane_line}; fi\n'
            'exit 0\n')
        path.chmod(0o755)
        return str(path)

    def test_11_launch_records_flags_and_hands_the_seed_as_one_argument(self):
        self.register(self.pane_pid, S.proc_start(self.pane_pid))
        cfg = self.cfg(tmux_bin=self.fake_tmux(), claude_bin="claude-recorder",
                       autowatch=False)
        cfg.flags_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.flags_file.write_text("--model haiku --dangerously-skip-permissions\n")
        pid = sup.launch_from_file(cfg)

        self.assertEqual(self.pane_pid, pid)
        argv = self.argv.read_text()
        self.assertIn("--model haiku --dangerously-skip-permissions", argv)
        self.assertIn("Rollover seed: read the file", argv)
        seed = [t for t in argv.split() if t.startswith(str(cfg.run_dir))][-1]
        self.assertTrue(Path(seed.rstrip("'\"")).is_file(), seed)
        self.assertNotIn("send-keys", argv)
        self.assertEqual(str(pid), cfg.pid_file.read_text().strip())
        self.assertEqual(1, len(self.rows(cfg, "launch_pid")))
        self.assertEqual([], self.rows(cfg, "watch_spawned"))

    def test_12_seed_as_arg_off_removes_the_seed_argument(self):
        self.register(self.pane_pid, S.proc_start(self.pane_pid))
        cfg = self.cfg(tmux_bin=self.fake_tmux(), autowatch=False, seed_as_arg=False)
        cfg.flags_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.flags_file.write_text("--model haiku\n")
        sup.launch_from_file(cfg)
        self.assertNotIn("Rollover seed", self.argv.read_text())

    def prompts_fixture(self, text="the operator's own words"):
        """A transcript for the work dir, under a projects root of our own."""
        from token_kit.router.hook import slug

        projects = self.root / "projects"
        d = projects / slug(str(self.work.resolve()))
        d.mkdir(parents=True)
        (d / "sess.jsonl").write_text(json.dumps({
            "type": "user", "uuid": "u1", "timestamp": "2026-01-31T14:05:00.000Z",
            "sessionId": "sess", "isSidechain": False, "promptSource": "typed",
            "message": {"role": "user", "content": text}}) + "\n")
        os.environ["LANE_RECYCLER_PROJECTS_ROOT"] = str(projects)
        self.addCleanup(os.environ.pop, "LANE_RECYCLER_PROJECTS_ROOT", None)

    def test_23_the_relaunch_refreshes_the_verbatim_prompt_file_and_names_it(self):
        """The handoff says what was decided; this says what was ASKED."""
        self.prompts_fixture()
        self.register(self.pane_pid, S.proc_start(self.pane_pid))
        cfg = self.cfg(tmux_bin=self.fake_tmux(), autowatch=False)
        cfg.flags_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.flags_file.write_text("--model haiku\n")
        sup.launch_from_file(cfg)

        prompts = self.state_file.parent / "PROMPTS.md"
        self.assertTrue(prompts.is_file(), "no PROMPTS.md beside the state file")
        self.assertIn("the operator's own words", prompts.read_text())
        self.assertEqual(0o600, prompts.stat().st_mode & 0o777)
        self.assertEqual(1, len(self.rows(cfg, "prompts_refreshed")))
        seed = sorted(cfg.run_dir.glob("seed.*.md"))[-1].read_text()
        self.assertIn(f"What the user said, verbatim: {prompts}", seed)

    def test_24_a_failed_extraction_is_a_row_and_the_rollover_still_launches(self):
        """FAIL OPEN: a nice-to-have file may never block a relaunch."""
        from token_kit import prompts as P

        self.register(self.pane_pid, S.proc_start(self.pane_pid))
        cfg = self.cfg(tmux_bin=self.fake_tmux(), autowatch=False)
        cfg.flags_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.flags_file.write_text("--model haiku\n")
        real = P.collect
        P.collect = lambda *a, **k: (_ for _ in ()).throw(OSError("transcripts unreadable"))
        self.addCleanup(setattr, P, "collect", real)

        pid = sup.launch_from_file(cfg)
        self.assertEqual(self.pane_pid, pid, "a failed extraction blocked the relaunch")
        rows = self.rows(cfg, "prompts_failed")
        self.assertEqual(1, len(rows))
        self.assertIn("transcripts unreadable", rows[0])
        seed = sorted(cfg.run_dir.glob("seed.*.md"))[-1].read_text()
        self.assertNotIn("What the user said", seed)
        self.assertIn("Rollover:", seed)

    def test_13_unresolvable_pid_is_a_loud_row_not_a_crash(self):
        cfg = self.cfg(tmux_bin=self.fake_tmux(pane=False), autowatch=True)
        cfg.flags_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.flags_file.write_text("--model haiku\n")
        self.assertIsNone(sup.launch_from_file(cfg))
        self.assertEqual(1, len(self.rows(cfg, "launch_pid_unresolved")))
        self.assertEqual([], self.rows(cfg, "watch_spawned"))


class Kill(Fixture):
    def test_14_a_kill_that_does_not_land_refuses(self):
        mine = os.getpid()
        self.register(mine, S.proc_start(mine))
        recorded = self.root / "kills.tsv"
        cfg = self.cfg(kill_cmd=self.recorder("killer", recorded),
                       term_wait_secs=0, kill_poll_secs=0, rollover_cmd="")
        self.assertFalse(sup.end_session(cfg, mine))     # the recorder kills nothing
        self.assertIn("TERM", recorded.read_text())
        self.assertIn("KILL", recorded.read_text())
        self.assertFalse(sup.rollover(cfg, mine, 5000, "hard"))
        self.assertEqual(1, len(self.rows(cfg, "rollover_failed")))
        self.assertEqual([], self.rows(cfg, "rollover_ended"))

    def test_15_a_kill_that_lands_is_confirmed_by_start_time(self):
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # auto-reap
        self.addCleanup(signal.signal, signal.SIGCHLD, previous)
        child = subprocess.Popen(["sleep", "30"])
        start = S.proc_start(child.pid)
        self.register(child.pid, start)
        cfg = self.cfg(term_wait_secs=10, kill_poll_secs=1, rollover_cmd="")
        self.assertTrue(sup.end_session(cfg, child.pid))
        self.assertNotEqual(start, S.proc_start(child.pid))
        self.assertFalse(S.is_live_session(self.home, child.pid))


class LockAndStatus(Fixture):
    def test_16_live_lock_refused_stale_lock_taken_over(self):
        cfg = self.cfg(heartbeat_stale_secs=300)
        self.assertTrue(sup.acquire_lock(cfg, PID))
        cfg.heartbeat.touch()
        self.assertFalse(sup.acquire_lock(cfg, PID))
        refused = self.rows(cfg, "watch_refused")
        self.assertEqual(1, len(refused))
        self.assertIn("reason=live_watcher_holds_lock", refused[0])
        old = time.time() - 4000
        os.utime(cfg.heartbeat, (old, old))
        self.assertTrue(sup.acquire_lock(cfg, PID))
        self.assertEqual(1, len(self.rows(cfg, "watch_takeover")))

    def test_17_status_alive_then_dead(self):
        cfg = self.cfg(heartbeat_stale_secs=300)
        cfg.run_dir.mkdir(parents=True, exist_ok=True)
        cfg.heartbeat.touch()
        self.assertEqual(0, sup.cmd_status(cfg))
        old = time.time() - 4000
        os.utime(cfg.heartbeat, (old, old))
        self.assertEqual(1, sup.cmd_status(cfg))


class RunsFromAnywhere(Fixture):
    """The state file is an argument; its absolute path is the identity."""

    # 20
    def test_20_two_state_files_never_share_a_run_dir(self):
        other = self.root / "other"
        other.mkdir()
        (other / "STATE.md").write_text("# a different piece of work\n")
        a = self.cfg()
        b = self.cfg(state_file=str(other / "STATE.md"))
        self.assertNotEqual(a.session_key, b.session_key)
        self.assertNotEqual(a.run_dir, b.run_dir)
        self.assertNotEqual(a.log, b.log)
        self.assertNotEqual(a.lock_dir, b.lock_dir)
        # and the key is a hash: no part of anyone's directory names leaks
        self.assertNotIn("other", a.session_key + b.session_key)
        self.assertTrue(sup.acquire_lock(a, PID))
        self.assertTrue(sup.acquire_lock(b, PID),
                        "a lock in one directory blocked an unrelated one")

    # 21
    def test_21_the_default_state_file_is_state_md_in_the_cwd(self):
        here = Path.cwd()
        cfg = cfgmod.Config()
        self.assertEqual("STATE.md", cfg.state_file)
        self.assertEqual(here / "STATE.md", cfg.state_path)
        self.assertEqual(here, cfg.work_dir)

    # 22
    def test_22_the_soft_request_lands_beside_the_state_file(self):
        cfg = self.cfg()
        self.register(PID, S.proc_start(os.getpid()))
        self.grow(150)
        sup.one_pass(cfg, PID, self.transcript, "busy")
        self.assertEqual(self.work / "ROLLOVER_REQUEST.md", cfg.request)
        self.assertTrue(cfg.request.is_file())
        self.assertIn(str(self.state_file), cfg.request.read_text())
        self.assertIn(str(self.state_file), sup.seed_text(cfg))
        self.assertIn(str(self.work), sup.seed_text(cfg))


class Dials(Fixture):
    def test_19_toml_and_cli_only_and_unknown_keys_refuse(self):
        toml = self.root / "sup.toml"
        toml.write_text('[supervisor]\nsoft_tokens = 111\nhard_tokens = 222\n')
        cfg = cfgmod.load(toml, {})
        self.assertEqual((111, 222), (cfg.soft_tokens, cfg.hard_tokens))
        cfg = cfgmod.load(toml, {"soft_tokens": "5"})        # CLI wins
        self.assertEqual(5, cfg.soft_tokens)
        toml.write_text('[supervisor]\nSOFT_TOKENS = 111\n')
        with self.assertRaises(SystemExit):
            cfgmod.load(toml, {})
        with self.assertRaises(SystemExit):
            cfgmod.load(None, {"soft": 1})
        # the ceiling is a COST decision: the shipped defaults stay where the
        # main thread put them back after a lane raised them to "use the window"
        default = cfgmod.Config()
        self.assertEqual((180000, 235000), (default.soft_tokens, default.hard_tokens))


if __name__ == "__main__":
    unittest.main()
