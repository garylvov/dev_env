"""Offline guards for token_kit.codex.dispatch, against a FAKE codex.

    uv run --python 3.11 --no-project python -m unittest discover \
        -s tests/codex -t .

Every test drives the real CLI in a child process, through a launcher script
shaped exactly like the kit's own, so argv, env and the wire are the things
under test -- not a mock of them.
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

# NB: the package re-exports the `dispatch` FUNCTION, so import the module by
# name rather than `from token_kit.codex import dispatch`, which binds the
# function and turns every constant lookup into an AttributeError.
D = importlib.import_module("token_kit.codex.dispatch")  # noqa: E402

FAKE_PY = HERE / "fake_codex.py"


def write_launcher(path: Path) -> Path:
    """A .bash launcher, like ~/run_codex.bash, that execs the fake codex."""
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{sys.executable}" "{FAKE_PY}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class DispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tk-codex-test-"))
        self.launcher = write_launcher(self.tmp / "run_codex.bash")
        self.record = self.tmp / "record.jsonl"
        self.task = self.tmp / "in.md"
        self.task.write_text("reply with the word PROVEN\n", encoding="utf-8")
        self.out = self.tmp / "out.md"
        self.slots = self.tmp / "slots"

    def run_cli(self, *extra: str, scenario: str = "ok", answer: str | None = None,
                launcher: str | None = None, env_extra: dict[str, str] | None = None):
        env = dict(os.environ)
        env.update({
            "FAKE_CODEX_RECORD": str(self.record),
            "FAKE_CODEX_SCENARIO": scenario,
            "TOKEN_KIT_CODEX_SLOT_DIR": str(self.slots),
            "PYTHONPATH": str(SRC) + os.pathsep + env.get("PYTHONPATH", ""),
        })
        if answer is not None:
            env["FAKE_CODEX_ANSWER"] = answer
        if env_extra:
            env.update(env_extra)
        argv = [sys.executable, "-m", "token_kit.codex.dispatch",
                "--model", "gpt-5.6-luna", "--effort", "high",
                "--cwd", str(self.tmp), "--task-file", str(self.task),
                "--out", str(self.out),
                "--launcher", launcher if launcher is not None else str(self.launcher),
                "--wait-s", "0.3", *extra]
        started = time.monotonic()
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        return proc, time.monotonic() - started

    def recorded(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def sent(self, method: str) -> dict:
        for msg in self.recorded():
            if msg.get("method") == method:
                return msg.get("params") or {}
        self.fail(f"the dispatcher never sent {method}: {self.recorded()}")

    # -- the guards ------------------------------------------------------

    def test_model_and_effort_reach_the_wire(self):
        proc, _ = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sent("thread/start").get("model"), "gpt-5.6-luna")
        turn = self.sent("turn/start")
        self.assertEqual(turn.get("model"), "gpt-5.6-luna")
        self.assertEqual(turn.get("effort"), "high")
        self.assertEqual(proc.stdout.strip(), "PROVEN")
        self.assertEqual(self.out.read_text(encoding="utf-8").strip(), "PROVEN")
        self.assertEqual(Path(f"{self.out}.thread").read_text(encoding="utf-8").strip(),
                         "th-fake-0001")
        self.assertTrue(Path(f"{self.out}.log.jsonl").exists())

    def test_tokio_worker_floor_reaches_the_child(self):
        """<=4 makes the real app-server exit rc 0 with no output (measured)."""
        proc, _ = self.run_cli(env_extra={"TOKIO_WORKER_THREADS": "4"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        child_env = self.recorded()[0]["env"]
        self.assertGreaterEqual(int(child_env["TOKIO_WORKER_THREADS"]),
                                D.MIN_TOKIO_WORKER_THREADS)

    def test_byte_cap_truncates_and_names_the_full_file(self):
        long_answer = "x" * 5000
        proc, _ = self.run_cli("--max-bytes", "500", answer=long_answer)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[truncated at 500 of 5000 bytes; full answer:", proc.stdout)
        self.assertIn(str(self.out), proc.stdout)
        self.assertLess(len(proc.stdout.encode()), 800)
        self.assertEqual(self.out.read_text(encoding="utf-8").strip(), long_answer)

    def test_byte_cap_below_floor_is_usage(self):
        proc, _ = self.run_cli("--max-bytes", "10")
        self.assertEqual(proc.returncode, D.EXIT_USAGE, proc.stderr)

    def test_missing_model_is_usage_not_a_default(self):
        env = dict(os.environ, TOKEN_KIT_CODEX_SLOT_DIR=str(self.slots),
                   PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.run(
            [sys.executable, "-m", "token_kit.codex.dispatch", "--effort", "high",
             "--cwd", str(self.tmp), "--task-file", str(self.task), "--out", str(self.out)],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, D.EXIT_USAGE)

    def test_absent_launcher_is_rc42_fast(self):
        proc, secs = self.run_cli(launcher=str(self.tmp / "no-such-codex.bash"))
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertIn("reason=absent", proc.stderr)
        self.assertLess(secs, 2.0, f"rc 42 took {secs:.2f}s")

    def test_auth_refused_is_rc42(self):
        proc, _ = self.run_cli(scenario="authfail")
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertIn("reason=auth", proc.stderr)

    def test_silent_server_is_rc42_not_an_empty_success(self):
        proc, _ = self.run_cli(scenario="silent")
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_bound_full_is_rc42_busy(self):
        self.slots.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            slot = self.slots / f"slot-{index}"
            slot.mkdir()
            (slot / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        proc, secs = self.run_cli("--max-children", "2")
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertIn("reason=busy", proc.stderr)
        self.assertLess(secs, 10.0)
        self.assertEqual(self.recorded(), [], "a busy dispatch must not spawn codex")

    def test_dead_holder_slot_is_reclaimed(self):
        self.slots.mkdir(parents=True, exist_ok=True)
        dead = subprocess.run([sys.executable, "-c", "pass"])
        slot = self.slots / "slot-0"
        slot.mkdir()
        (slot / "pid").write_text(f"{dead.returncode + 999999}\n", encoding="utf-8")
        proc, _ = self.run_cli("--max-children", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_resume_sends_the_stored_thread_id(self):
        proc, _ = self.run_cli("--resume", "th-stored-42")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sent("thread/resume").get("threadId"), "th-stored-42")
        self.assertEqual(self.sent("turn/start").get("threadId"), "th-stored-42")
        for msg in self.recorded():
            self.assertNotEqual(msg.get("method"), "thread/start",
                                "a resume must not start a new thread")
        self.assertEqual(Path(f"{self.out}.thread").read_text(encoding="utf-8").strip(),
                         "th-stored-42")

    def test_codex_failure_is_rc3_with_empty_stdout(self):
        proc, _ = self.run_cli(scenario="fail")
        self.assertEqual(proc.returncode, D.EXIT_CODEX_FAILED, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("CODEX_FAILED", proc.stderr)

    def test_quota_is_rc42_not_rc3(self):
        """A maxed-out account is codex UNAVAILABLE, not a turn that failed.

        rc 3 invites a retry into the same wall; rc 42 hands the step to a
        Claude model and boards the cooldown marker for the next caller.
        """
        marker = self.tmp / "cooldown.json"
        proc, _ = self.run_cli(scenario="quota",
                               env_extra={"TOKEN_KIT_CODEX_COOLDOWN_MARKER": str(marker)})
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertIn("reason=quota", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("usageLimitExceeded",
                      json.loads(marker.read_text(encoding="utf-8"))["detail"])

    def test_slot_is_released_so_a_second_call_runs(self):
        first, _ = self.run_cli("--max-children", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        second, _ = self.run_cli("--max-children", "1")
        self.assertEqual(second.returncode, 0, second.stderr)


if __name__ == "__main__":
    unittest.main()
