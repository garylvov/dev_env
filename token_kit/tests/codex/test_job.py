"""Offline guards for token_kit.codex.job -- messaging a LIVE codex job.

    uv run --python '>=3.11' --no-project python -m unittest discover -s tests -t .

Everything runs against the fake codex from `fake_codex.py`, launched through a
`.bash` launcher shaped like ~/run_codex.bash, in real detached owner
processes.  The things under test are the wire (`turn/steer` with an
`expectedTurnId`), the delivery rows, and the no-lost-message ordering.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

J = importlib.import_module("token_kit.codex.job")          # noqa: E402
E = importlib.import_module("token_kit.codex.errors")       # noqa: E402

FAKE_PY = HERE / "fake_codex.py"
DEADLINE_S = 30.0


def until(predicate, timeout: float = DEADLINE_S, what: str = "condition"):
    """Wait for a real subprocess to get somewhere.  Returns the value."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")


class JobCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tk-codex-job-"))
        self.launcher = self.tmp / "run_codex.bash"
        self.launcher.write_text(
            "#!/usr/bin/env bash\n" f'exec "{sys.executable}" "{FAKE_PY}" "$@"\n',
            encoding="utf-8")
        self.launcher.chmod(0o755)
        self.record = self.tmp / "record.jsonl"
        self.jobs = self.tmp / "jobs"
        self.marker = self.tmp / "codex-cooldown.json"
        self.finish = self.tmp / "FINISH"
        self.task = self.tmp / "in.md"
        self.task.write_text("say PROVEN\n", encoding="utf-8")

    def env(self, scenario: str = "ok", **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        # Explicit test-local overrides below are the only managed contexts.
        for name in ("TOKEN_KIT_TASK", "TOKEN_KIT_AGENT", "TOKEN_KIT_RUN"):
            env.pop(name, None)
        env.update({
            "FAKE_CODEX_RECORD": str(self.record),
            "FAKE_CODEX_SCENARIO": scenario,
            "FAKE_CODEX_FINISH": str(self.finish),
            "FAKE_CODEX_TURN_MAX_S": "25",
            "TOKEN_KIT_CODEX_JOB_DIR": str(self.jobs),
            "TOKEN_KIT_CODEX_COOLDOWN_MARKER": str(self.marker),
            "PYTHONPATH": str(SRC) + os.pathsep + env.get("PYTHONPATH", ""),
        })
        env.update(extra)
        return env

    def cli(self, *argv: str, scenario: str = "ok", timeout: float = 60.0, **extra: str):
        return subprocess.run(
            [sys.executable, "-m", "token_kit.codex.job", *argv],
            env=self.env(scenario, **extra), capture_output=True, text=True, timeout=timeout)

    def start(self, scenario: str = "ok", linger: str = "10", **extra: str) -> str:
        proc = self.cli("start", "--model", "gpt-5.6-luna", "--effort", "high",
                        "--cwd", str(self.tmp), "--task-file", str(self.task),
                        "--launcher", str(self.launcher), "--linger-s", linger,
                        "--name", "t", scenario=scenario, **extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job_id = proc.stdout.strip()
        self.assertTrue(job_id, "start must print a job id immediately")
        return job_id

    # -- readers ---------------------------------------------------------
    def job(self, job_id: str) -> "J.Job":
        return J.Job(self.jobs / job_id)

    def deliveries(self, job_id: str) -> list[dict]:
        return self.job(job_id).deliveries()

    def disposition(self, job_id: str, message_id: str) -> str:
        for row in self.deliveries(job_id):
            if row.get("message") == message_id:
                return row.get("disposition", "")
        return ""

    def sent(self, method: str) -> list[dict]:
        out = []
        if not self.record.exists():
            return out
        for line in self.record.read_text(encoding="utf-8").splitlines():
            msg = json.loads(line)
            if isinstance(msg, dict) and msg.get("method") == method:
                out.append(msg.get("params") or {})
        return out

    # -- the guards ------------------------------------------------------

    def test_managed_job_persists_policy_context_and_sends_compact_brief(self):
        from token_kit.pyramid import read_pyramid
        from token_kit.core.store import Store
        store = Store.create(self.tmp / "tasks", "test", self.tmp)
        job_id = self.start(linger="0", TOKEN_KIT_TASK=str(store.path), TOKEN_KIT_AGENT="parent-secret")
        result = self.cli("wait", job_id, "--timeout-s", "15")
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = json.loads((self.job(job_id).root / "meta.json").read_text())
        self.assertEqual(meta["token_kit_task"], str(store.path))
        prompt = self.sent("turn/start")[0]["input"][0]["text"]
        self.assertIn("<!-- token-kit worker policy v2 -->", prompt)
        self.assertIn(read_pyramid(store.path)["content"].rstrip(), prompt)
        self.assertTrue(prompt.endswith(self.task.read_text()))
        self.assertNotIn("parent-secret", prompt)
        self.assertIn("codex-worker", (store.path / "TOKEN_LEDGER.md").read_text())

    def test_start_returns_before_the_turn_ends_and_wait_reports_it(self):
        started = time.monotonic()
        job_id = self.start(scenario="slow", linger="3")
        self.assertLess(time.monotonic() - started, 10.0, "start must not block on the turn")
        until(lambda: self.sent("turn/started") or self.job(job_id).thread_id(),
              what="the turn to start")
        self.finish.write_text("go\n", encoding="utf-8")
        proc = self.cli("wait", job_id, "--timeout-s", "30", "--poll-s", "0.1")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PROVEN", proc.stdout)

    def test_mid_turn_message_is_steered_into_the_live_turn(self):
        job_id = self.start(scenario="slow", linger="3")
        until(lambda: self.job(job_id).thread_id(), what="thread/started")
        until(lambda: self.sent("turn/started") or self.sent("turn/start"),
              what="turn/start on the wire")
        send = self.cli("send", job_id, "MARKER-DELTA")
        self.assertEqual(send.returncode, 0, send.stderr)
        message_id = send.stdout.split()[0]
        until(lambda: self.disposition(job_id, message_id) == "steered",
              what="a steered delivery row")
        steers = self.sent("turn/steer")
        self.assertTrue(steers, "no turn/steer reached the wire")
        # The steer key is the LIVE turn id the owner learned from
        # `turn/started`.params.turn.id -- the field that cost the survey two
        # probes -- and the fake refuses any other value.
        self.assertEqual(steers[0].get("expectedTurnId"), "tn-fake-0001")
        self.assertEqual(steers[0]["input"][0]["text"], "MARKER-DELTA")
        # one turn, and the steered text is IN it
        self.finish.write_text("go\n", encoding="utf-8")
        proc = self.cli("wait", job_id, "--timeout-s", "30", "--poll-s", "0.1")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("MARKER-DELTA", proc.stdout)
        self.assertEqual(len(self.sent("turn/start")), 1, "a steer must not start a new turn")

    def test_late_message_becomes_the_next_turn_on_the_same_thread(self):
        job_id = self.start(scenario="ok", linger="8")
        until(lambda: len(self.sent("turn/start")) == 1, what="the first turn")
        until(lambda: "idle" in self.job(job_id).last_status(), what="the owner to go idle")
        send = self.cli("send", job_id, "SECOND-MESSAGE")
        message_id = send.stdout.split()[0]
        self.assertIn("queued", send.stdout)
        until(lambda: self.disposition(job_id, message_id) == "queued-next-turn",
              what="a queued-next-turn row")
        starts = until(lambda: self.sent("turn/start") if len(self.sent("turn/start")) == 2
                       else None, what="a second turn/start")
        self.assertEqual(starts[1]["threadId"], starts[0]["threadId"], "same thread")
        self.assertEqual(starts[1]["input"][0]["text"], "SECOND-MESSAGE")
        self.assertEqual(self.sent("thread/resume"), [], "a live owner must not resume")

    def test_message_after_the_owner_exits_resumes_the_thread_by_id(self):
        job_id = self.start(scenario="ok", linger="0.2")
        until(lambda: self.job(job_id).done_file.exists(), what="the owner to exit")
        thread = self.job(job_id).thread_id()
        send = self.cli("send", job_id, "AFTER-EXIT")
        self.assertIn("resumed", send.stdout)
        message_id = send.stdout.split()[0]
        until(lambda: self.disposition(job_id, message_id) == "resumed",
              what="a resumed delivery row")
        resumes = until(lambda: self.sent("thread/resume"), what="thread/resume")
        self.assertEqual(resumes[0]["threadId"], thread)
        self.assertEqual(len(self.sent("thread/start")), 1,
                         "a resume must not start a second thread")

    def test_no_message_is_lost_across_the_completion_race(self):
        """Sends fired at an owner that is expiring: every one gets a row."""
        job_id = self.start(scenario="ok", linger="0.3")
        ids = []
        for index in range(6):
            send = self.cli("send", job_id, f"RACE-{index}")
            self.assertEqual(send.returncode, 0, send.stderr)
            ids.append(send.stdout.split()[0])
            time.sleep(0.25)   # straddles the 0.3 s linger: some land mid-exit
        for message_id in ids:
            until(lambda mid=message_id: self.disposition(job_id, mid),
                  what=f"a delivery row for {message_id}")
        until(lambda: J.inbox_pending(self.job(job_id)) == 0, what="an empty inbox")
        self.assertEqual(len(self.deliveries(job_id)), len(ids))

    def test_a_send_inside_the_exit_window_is_not_lost(self):
        """The deterministic version of the race, via the EXIT_RACE seam.

        The owner has scanned its inbox, found it empty, and is about to drop
        `owner.lock`.  A `send` lands NOW: it writes its file and then sees a
        still-live owner, so it queues rather than taking over.  Only the
        owner's rescan-after-release saves the message.
        """
        job_id = self.start(scenario="ok", linger="0.3",
                            **{J.EXIT_RACE_ENV: "2.5"})
        until(lambda: "exit-window" in self.job(job_id).last_status(),
              what="the owner's exit window")
        send = self.cli("send", job_id, "IN-THE-WINDOW", **{J.EXIT_RACE_ENV: "2.5"})
        self.assertEqual(send.returncode, 0, send.stderr)
        message_id = send.stdout.split()[0]
        until(lambda: self.disposition(job_id, message_id),
              what=f"any delivery row for {message_id} (a dropped message has none)")
        self.assertEqual(J.inbox_pending(self.job(job_id)), 0)

    def test_quota_is_rc42_boards_the_marker_and_blocks_the_next_start(self):
        job_id = self.start(scenario="quota", linger="1")
        proc = self.cli("wait", job_id, "--timeout-s", "30", "--poll-s", "0.1",
                        scenario="quota")
        self.assertEqual(proc.returncode, J.EXIT_BUSY_OR_ABSENT, proc.stdout + proc.stderr)
        self.assertIn("reason=quota", self.job(job_id).last_status() + proc.stderr)
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(marker["reason"], "quota")
        self.assertIn("usageLimitExceeded", marker["detail"])
        blocked = self.cli("start", "--model", "gpt-5.6-luna", "--effort", "high",
                           "--cwd", str(self.tmp), "--task-file", str(self.task),
                           "--launcher", str(self.launcher), scenario="quota")
        self.assertEqual(blocked.returncode, J.EXIT_BUSY_OR_ABSENT, blocked.stderr)
        self.assertIn("reason=quota", blocked.stderr)

    def test_stop_interrupts_the_live_turn_and_ends_the_job(self):
        job_id = self.start(scenario="slow", linger="30")
        until(lambda: "id=tn-fake" in self.job(job_id).last_status(), what="a live turn")
        proc = self.cli("stop", job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")
        self.assertTrue(self.sent("turn/interrupt"), "stop must send turn/interrupt")

    def test_status_and_list_name_the_job_without_touching_codex(self):
        job_id = self.start(scenario="ok", linger="0.2")
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")
        status = self.cli("status", job_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(job_id, status.stdout)
        self.assertIn("PROVEN", status.stdout)
        listing = self.cli("list")
        self.assertIn(job_id, listing.stdout)

    def test_a_pid_whose_start_time_moved_is_not_the_owner(self):
        """Doctrine: a pid is not an identity.  Liveness needs the start time."""
        job = J.Job(self.jobs / "fake-job")
        job.root.mkdir(parents=True)
        job.write_owner(os.getpid())
        self.assertTrue(job.owner_alive())
        (job.lock / "starttime").write_text("1\n", encoding="utf-8")
        self.assertFalse(job.owner_alive(), "a recycled pid must not read as the owner")

    def test_wait_shows_the_LAST_answer_not_the_tail_of_the_log(self):
        """A short follow-up answer must not be buried by the first turn."""
        job = J.Job(self.jobs / "answers")
        job.root.mkdir(parents=True)
        (job.root / "answer.md").write_text(
            "\n## turn 1 (t1)\n\n" + ("FIRST " * 400) + "\n\n## turn 2 (t2)\n\nSECOND\n",
            encoding="utf-8")
        self.assertEqual(job.last_answer(), "SECOND")
        self.assertIn("FIRST", job.answer_tail(4000))

    def test_errors_classifies_the_real_0_153_4_vocabulary(self):
        self.assertTrue(E.is_quota({"codexErrorInfo": "usageLimitExceeded",
                                    "message": "Usage limit reached."}))
        self.assertTrue(E.is_quota({"codexErrorInfo": "rateLimitExceeded", "message": ""}))
        self.assertTrue(E.is_quota({"message": "You've hit your usage limit."}))
        self.assertFalse(E.is_quota({"codexErrorInfo": "contextWindowExceeded",
                                     "message": "too long"}))
        self.assertFalse(E.is_quota({"codexErrorInfo": {"activeTurnNotSteerable":
                                                        {"turnKind": "review"}},
                                     "message": "not steerable"}))


if __name__ == "__main__":
    unittest.main()
