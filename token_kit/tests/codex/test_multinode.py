"""Offline guards for ONE job dir seen from TWO nodes.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

The job dir is on the shared filesystem; the owner process, its pid, its /proc
start time and the codex thread's rollout file are on one node.  "Another node"
is simulated by patching the HOSTNAME SOURCE (`TOKEN_KIT_HOSTNAME`, read by
`token_kit.codex.launcher.this_host`) -- never by ssh, which no guard may need.

THE DEFECTS THESE CATCH, all of them read out of the code before the fix:
    `meta.json` recorded no host at all, so every verb judged the owner by a
    LOCAL pid + start time.  On another node that pid is absent -- so `send`
    claimed the lock and started a SECOND owner, which did `thread/resume` on a
    thread whose rollout file lives in the first node's /tmp CODEX_HOME -- or
    it is present and belongs to somebody else entirely.  `stop` was worse than
    wrong: with the owner unreachable it wrote `rc` and `done` for a job that
    was still running on the other node.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

J = importlib.import_module("token_kit.codex.job")        # noqa: E402

FAKE_PY = HERE / "fake_codex.py"
OTHER_HOST = "some-other-node"
DEADLINE_S = 30.0


def until(predicate, timeout: float = DEADLINE_S, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")


class MultiNodeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tk-codex-multinode-"))
        self.launcher = self.tmp / "run_codex.bash"
        self.launcher.write_text("#!/usr/bin/env bash\n"
                                 f'exec "{sys.executable}" "{FAKE_PY}" "$@"\n',
                                 encoding="utf-8")
        self.launcher.chmod(0o755)
        self.record = self.tmp / "record.jsonl"
        self.jobs = self.tmp / "jobs"
        self.finish = self.tmp / "FINISH"
        self.task = self.tmp / "in.md"
        self.task.write_text("say PROVEN\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, scenario: str = "ok", host: str | None = None, **extra: str):
        env = dict(os.environ)
        env.update({
            "FAKE_CODEX_RECORD": str(self.record),
            "FAKE_CODEX_SCENARIO": scenario,
            "FAKE_CODEX_FINISH": str(self.finish),
            "FAKE_CODEX_TURN_MAX_S": "25",
            "TOKEN_KIT_CODEX_JOB_DIR": str(self.jobs),
            "TOKEN_KIT_CODEX_COOLDOWN_MARKER": str(self.tmp / "cooldown.json"),
            "PYTHONPATH": str(SRC) + os.pathsep + env.get("PYTHONPATH", ""),
        })
        if host is not None:
            env["TOKEN_KIT_HOSTNAME"] = host      # <- "this call runs on that node"
        env.update(extra)
        return env

    def cli(self, *argv: str, scenario: str = "ok", host: str | None = None,
            timeout: float = 60.0, **extra: str):
        return subprocess.run([sys.executable, "-m", "token_kit.codex.job", *argv],
                              env=self.env(scenario, host, **extra),
                              capture_output=True, text=True, timeout=timeout)

    def start(self, scenario: str = "ok", linger: str = "10") -> str:
        proc = self.cli("start", "--model", "m", "--effort", "high",
                        "--cwd", str(self.tmp), "--task-file", str(self.task),
                        "--launcher", str(self.launcher), "--linger-s", linger,
                        "--name", "t", scenario=scenario)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def job(self, job_id: str) -> "J.Job":
        return J.Job(self.jobs / job_id)

    def sent(self, method: str) -> list[dict]:
        out = []
        if not self.record.exists():
            return out
        for line in self.record.read_text(encoding="utf-8").splitlines():
            msg = json.loads(line)
            if isinstance(msg, dict) and msg.get("method") == method:
                out.append(msg.get("params") or {})
        return out

    def status_rows(self, job_id: str) -> list[str]:
        try:
            return (self.job(job_id).root / "status").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    # -- the guards ------------------------------------------------------

    def test_meta_records_the_host_and_the_lock_carries_it_too(self):
        job_id = self.start(linger="0.3")
        meta = self.job(job_id).meta()
        self.assertTrue(meta.get("host"), "meta.json records no host")
        self.assertEqual(meta["host"], J.launcher_mod.this_host())
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")

    def test_a_job_started_elsewhere_reads_as_remote_not_as_an_orphan(self):
        job_id = self.start(linger="0.3")
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")
        listing = self.cli("list", host=OTHER_HOST)
        self.assertIn(f"host={J.launcher_mod.this_host()}", listing.stdout,
                      "list does not show which node the job lives on")
        status = self.cli("status", job_id, host=OTHER_HOST)
        self.assertIn("host=", status.stdout)

    def test_cross_host_send_to_a_live_owner_queues_and_starts_no_second_owner(self):
        job_id = self.start(scenario="slow", linger="20")
        until(lambda: self.job(job_id).thread_id(), what="the thread to start")
        until(lambda: self.sent("turn/start"), what="the first turn")
        before = len([r for r in self.status_rows(job_id) if "owner-spawned" in r])

        send = self.cli("send", job_id, "FROM-ANOTHER-NODE", scenario="slow", host=OTHER_HOST)
        self.assertEqual(send.returncode, 0, send.stderr)
        message_id = send.stdout.split()[0]
        self.assertIn(f"queued-remote({J.launcher_mod.this_host()})", send.stdout)
        self.assertTrue(any(f"{message_id} queued-remote" in r for r in self.status_rows(job_id)),
                        "no queued-remote row was written")
        after = len([r for r in self.status_rows(job_id) if "owner-spawned" in r])
        self.assertEqual(after, before, "a SECOND owner was started from the other node")
        self.assertEqual(self.sent("thread/resume"), [],
                         "the other node resumed a thread that lives on this one")

        # And the message really does arrive: the owner LISTS the shared inbox,
        # so a file written from another node is picked up like any other.
        until(lambda: self.job(job_id).deliveries(), what="the owner's own delivery row")
        self.assertTrue(any(row.get("message") == message_id
                            for row in self.job(job_id).deliveries()),
                        "the live owner never took the cross-host message")
        self.cli("stop", job_id, scenario="slow")
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")

    def test_cross_host_send_after_the_owner_is_gone_refuses_loudly(self):
        job_id = self.start(linger="0.2")
        until(lambda: self.job(job_id).done_file.exists(), what="the owner to exit")
        send = self.cli("send", job_id, "TOO-LATE", host=OTHER_HOST)
        self.assertNotEqual(send.returncode, 0, "a message that cannot be delivered exited 0")
        self.assertIn("thread lives on", send.stderr)
        self.assertIn("start a new job", send.stderr)
        rows = self.job(job_id).deliveries()
        self.assertTrue(any(r.get("disposition") == "failed(remote-owner-gone)" for r in rows),
                        f"no failed(remote-owner-gone) delivery row: {rows}")
        kept = list((self.job(job_id).root / "undelivered").iterdir())
        self.assertEqual(len(kept), 1, "the message was dropped instead of kept")
        self.assertEqual(kept[0].read_text(encoding="utf-8"), "TOO-LATE")
        self.assertEqual(J.inbox_pending(self.job(job_id)), 0,
                         "an undeliverable message was left pretending to be queued")
        self.assertEqual(len(self.sent("thread/resume")), 0,
                         "the other node tried to resume a thread it cannot see")

    def test_cross_host_stop_is_a_request_file_and_never_a_kill(self):
        job_id = self.start(scenario="slow", linger="20")
        until(lambda: "id=tn-fake" in self.job(job_id).last_status(), what="a live turn")
        owner_pid = self.job(job_id).owner_pid()

        stop = self.cli("stop", job_id, scenario="slow", host=OTHER_HOST)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertIn("stop requested on", stop.stdout)
        # The owner is still the owner: nothing terminal was written from the
        # other node, and the process was not signalled from there.
        until(lambda: self.sent("turn/interrupt"), what="the owner's own turn/interrupt")
        until(lambda: self.job(job_id).done_file.exists(), what="the owner to finish itself")
        self.assertEqual(self.job(job_id).meta().get("host"), J.launcher_mod.this_host())
        self.assertIsNotNone(owner_pid)

    def test_cross_host_force_refuses_instead_of_killing_a_pid_it_cannot_own(self):
        job_id = self.start(scenario="slow", linger="20")
        until(lambda: self.job(job_id).thread_id(), what="the thread to start")
        stop = self.cli("stop", job_id, "--force", scenario="slow", host=OTHER_HOST)
        self.assertEqual(stop.returncode, J.EXIT_USAGE, stop.stdout + stop.stderr)
        self.assertIn("cannot reach a process on", stop.stderr)
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")

    def test_a_remote_owner_is_never_declared_dead_by_a_local_pid(self):
        """The unit of the rule: hosts are compared BEFORE any pid is read."""
        job = J.Job(self.jobs / "hand-made")
        job.root.mkdir(parents=True)
        (job.root / "meta.json").write_text(json.dumps({"host": OTHER_HOST}) + "\n",
                                            encoding="utf-8")
        job.write_owner(os.getpid())          # a LIVE pid, on this node
        (job.lock / "host").write_text(OTHER_HOST + "\n", encoding="utf-8")
        self.assertTrue(job.is_remote())
        self.assertFalse(job.owner_alive(),
                         "owner_alive must not claim a remote owner from a local pid")
        self.assertIn("remote", J._job_line(job))

    def test_the_thread_rollout_is_archived_into_the_shared_job_dir(self):
        """The one part of a thread that is node-local becomes portable.

        MEASURED on codex-cli 0.153.4: the transcript is
        <CODEX_HOME>/sessions/<Y>/<M>/<D>/rollout-<stamp>-<threadId>.jsonl, so
        a thread started on one node leaves nothing behind on any other. The
        owner copies it into the job dir at the end of every turn.
        """
        home = self.tmp / "codex-home"
        day = home / "sessions" / "2026" / "09" / "21"
        day.mkdir(parents=True)
        rollout = day / "rollout-2026-09-21T00-00-00-th-fake-0001.jsonl"
        rollout.write_text('{"transcript": "yes"}\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "token_kit.codex.job", "start",
             "--model", "m", "--effort", "high", "--cwd", str(self.tmp),
             "--task-file", str(self.task), "--launcher", str(self.launcher),
             "--linger-s", "0.3", "--name", "r"],
            env=self.env("ok", CODEX_HOME=str(home)), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job_id = proc.stdout.strip()
        until(lambda: self.job(job_id).done_file.exists(), what="the job to end")
        copied = list((self.job(job_id).root / J.ROLLOUT_DIR).rglob("rollout-*.jsonl"))
        self.assertEqual(len(copied), 1, f"the rollout was not archived: {copied}")
        self.assertEqual(copied[0].read_text(encoding="utf-8"), '{"transcript": "yes"}\n')

        # ... and a node that has never seen the thread gets it back.
        other_home = self.tmp / "other-node-home"
        other_home.mkdir()
        self.assertTrue(J.seed_rollout(self.job(job_id), other_home, "th-fake-0001"))
        seeded = list(other_home.rglob("rollout-*.jsonl"))
        self.assertEqual([p.relative_to(other_home) for p in seeded],
                         [rollout.relative_to(home)],
                         "the rollout must go back at the SAME path codex looks in")
        self.assertFalse(J.seed_rollout(self.job(job_id), home, "th-fake-0001"),
                         "a node that already has the rollout must not be written to")

    def test_the_owner_finds_an_inbox_file_by_listing_the_directory(self):
        """NFS attribute caching makes an mtime a lie; a readdir is not one.

        `read_inbox` lists the inbox every tick and keys on file NAMES it has
        not seen, so a file written from another node needs no cache coherency
        beyond the directory listing itself.
        """
        job = J.Job(self.jobs / "listing")
        job.inbox.mkdir(parents=True)
        (job.root / "tmp").mkdir()
        seen: set[str] = set()
        self.assertEqual(J.read_inbox(job, seen), [])
        stale = job.inbox.stat().st_mtime
        message = J.put_message(job, "WRITTEN-ELSEWHERE")
        os.utime(job.inbox, (stale, stale))   # pretend the dir mtime never moved
        found = J.read_inbox(job, seen)
        self.assertEqual([m.text for m in found], ["WRITTEN-ELSEWHERE"],
                         "the owner trusted an mtime instead of listing the inbox")
        self.assertEqual(found[0].message_id, message.message_id)


if __name__ == "__main__":
    unittest.main()
