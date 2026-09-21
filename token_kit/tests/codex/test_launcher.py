"""Offline guards for token_kit.codex.launcher -- the kit's OWN codex launcher.

    uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .

Everything runs against a SCRATCH profile, a scratch "shared" (stand-in for
NFS) codex home and a scratch node-local tmp root, so no test reads or writes
the operator's ~/.codex, ~/run_codex.bash or /tmp/codex-home-<user>.

What is under test is the behaviour the operator's wrapper has and the two
races it cannot fix:
    the home is prepared BEFORE the child is spawned;
    auth.json is newer-wins in BOTH directions, and `logout` removes both;
    a copy onto the shared home is never visible half written;
    two first runs on one node seed the 32 MB store once;
    the exit sync runs even when the process is SIGTERMed;
    a profile that requires a node-local home never falls through to a bare
    `codex` against the shared one -- it refuses, rc 42, reason=absent.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

L = importlib.import_module("token_kit.codex.launcher")   # noqa: E402
D = importlib.import_module("token_kit.codex.dispatch")   # noqa: E402

FAKE_PY = HERE / "fake_codex.py"

PROFILE_TEMPLATE = """\
name = "{name}"

[detect]
priority = 0

[paths]
data_root = "{root}"

[codex]
launcher = "codex"
node_local_home = {node_local}
node_local_tmp_root = "{tmp_root}"
node_local_home_prefix = "codex-home"
binary = "{binary}"
bypass_approvals = {bypass}
"""


class LauncherCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tk-launcher-"))
        self.shared = self.tmp / "nfs-home" / ".codex"
        self.shared.mkdir(parents=True)
        self.tmp_root = self.tmp / "node-tmp"
        self.tmp_root.mkdir()
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self.binary = self.tmp / "fake-codex"
        self.binary.write_text("#!/usr/bin/env bash\n"
                               f'exec "{sys.executable}" "{FAKE_PY}" "$@"\n',
                               encoding="utf-8")
        self.binary.chmod(0o755)
        self.write_profile()
        self._saved_env = {k: os.environ.get(k) for k in
                           (L.SHARED_HOME_ENV, L.PROFILES_DIR_ENV, L.PROFILE_NAME_ENV)}
        os.environ[L.SHARED_HOME_ENV] = str(self.shared)
        os.environ[L.PROFILES_DIR_ENV] = str(self.profiles)
        os.environ[L.PROFILE_NAME_ENV] = "scratch"

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_profile(self, *, node_local: bool = True, binary: str | None = None,
                      bypass: bool = True, name: str = "scratch") -> None:
        (self.profiles / f"{name}.toml").write_text(PROFILE_TEMPLATE.format(
            name=name, root=self.tmp, node_local=str(node_local).lower(),
            tmp_root=self.tmp_root,
            binary=self.binary if binary is None else binary,
            bypass=str(bypass).lower()), encoding="utf-8")

    def config(self) -> "L.CodexConfig":
        return L.load_config(self.profiles, "scratch")

    def env_for_child(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    # -- the guards ------------------------------------------------------

    def test_profile_is_the_authority_for_every_key(self):
        cfg = self.config()
        self.assertTrue(cfg.node_local_home)
        self.assertEqual(cfg.tmp_root, self.tmp_root)
        self.assertEqual(cfg.binary, str(self.binary))
        self.assertTrue(cfg.bypass_approvals)
        self.write_profile(node_local=False, bypass=False)
        cfg = self.config()
        self.assertFalse(cfg.node_local_home)
        self.assertFalse(cfg.bypass_approvals)

    def test_the_home_is_ready_before_the_child_could_exist(self):
        (self.shared / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
        (self.shared / "auth.json").write_text('{"t": 1}', encoding="utf-8")
        launch = L.prepare(config=self.config(), base_env={}, serialise_startup=False)
        home = L.node_local_home(self.config())
        self.assertTrue(str(home).startswith(str(self.tmp_root)),
                        "the node-local home must live under the profile's tmp root")
        self.assertEqual(launch.env["CODEX_HOME"], str(home))
        self.assertTrue((home / "config.toml").is_file(),
                        "config was not refreshed into the node-local home")
        self.assertEqual((home / "auth.json").read_text(), '{"t": 1}')
        self.assertEqual(launch.argv[0], str(self.binary))
        self.assertIn(L.BYPASS_FLAG, launch.argv)
        self.assertEqual(launch.argv[-2:], ["app-server", "--stdio"])
        self.assertTrue(launch.internal)

    def test_bypass_flag_is_a_profile_key(self):
        self.write_profile(bypass=False)
        launch = L.prepare(config=self.config(), base_env={}, serialise_startup=False)
        self.assertNotIn(L.BYPASS_FLAG, launch.argv)

    def test_yolo_is_a_synonym_and_never_appears_twice(self):
        launch = L.prepare(config=self.config(), base_env={},
                           subcommand=("--yolo", "exec", "hi"), serialise_startup=False)
        self.assertEqual(launch.argv.count(L.BYPASS_FLAG), 1, launch.argv)
        self.assertNotIn("--yolo", launch.argv)
        self.assertEqual(launch.argv[-2:], ["exec", "hi"])

    def test_update_runs_against_the_shared_home(self):
        launch = L.prepare(config=self.config(), base_env={}, subcommand=("update",),
                           serialise_startup=False)
        self.assertEqual(launch.env["CODEX_HOME"], str(self.shared),
                         "`codex update` must see the standalone install, "
                         "which lives in the shared home")

    def test_thread_caps_are_defaults_not_overrides(self):
        launch = L.prepare(config=self.config(),
                           base_env={"TOKIO_WORKER_THREADS": "12"},
                           serialise_startup=False)
        self.assertEqual(launch.env["TOKIO_WORKER_THREADS"], "12")
        self.assertEqual(launch.env["RAYON_NUM_THREADS"], L.THREAD_CAPS["RAYON_NUM_THREADS"])
        self.assertEqual(launch.env["NODE_OPTIONS"], L.THREAD_CAPS["NODE_OPTIONS"])

    def test_auth_newer_wins_in_both_directions(self):
        home = L.node_local_home(self.config())
        home.mkdir(parents=True, exist_ok=True)
        (self.shared / "auth.json").write_text("SHARED-NEW", encoding="utf-8")
        (home / "auth.json").write_text("LOCAL-OLD", encoding="utf-8")
        os.utime(home / "auth.json", (1000, 1000))
        os.utime(self.shared / "auth.json", (2000, 2000))
        self.assertEqual(L.sync_auth(self.shared, home), "shared->local")
        self.assertEqual((home / "auth.json").read_text(), "SHARED-NEW")
        # ... and back the other way: a token refreshed on this node wins.
        (home / "auth.json").write_text("LOCAL-FRESH", encoding="utf-8")
        os.utime(home / "auth.json", (3000, 3000))
        self.assertEqual(L.sync_auth(self.shared, home), "local->shared")
        self.assertEqual((self.shared / "auth.json").read_text(), "LOCAL-FRESH")

    def test_logout_removes_both_copies(self):
        home = L.prepare(config=self.config(), base_env={}, serialise_startup=False).home
        (self.shared / "auth.json").write_text("TOKEN", encoding="utf-8")
        (home / "auth.json").write_text("TOKEN", encoding="utf-8")
        L.sync_back(self.config(), logout=True)
        self.assertFalse((self.shared / "auth.json").exists())
        self.assertFalse((home / "auth.json").exists())

    def test_a_copy_onto_the_shared_home_is_never_written_in_place(self):
        """No reader can ever see a half-written auth.json.

        `cp -f` (the wrapper) opens the DESTINATION and truncates it, so a
        concurrent reader sees an empty or partial token file. The guard is
        exact: copy2 must never be handed the final path -- it writes a temp
        file in the same directory and the rename does the rest.
        """
        targets: list[str] = []
        real = shutil.copy2

        def watched(src, dst, *a, **kw):
            targets.append(str(dst))
            return real(src, dst, *a, **kw)

        home = L.node_local_home(self.config())
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("NEW", encoding="utf-8")
        (self.shared / "auth.json").write_text("OLD", encoding="utf-8")
        os.utime(self.shared / "auth.json", (1000, 1000))
        os.utime(home / "auth.json", (2000, 2000))
        L.shutil.copy2 = watched
        try:
            self.assertEqual(L.sync_auth(self.shared, home), "local->shared")
        finally:
            L.shutil.copy2 = real
        self.assertTrue(targets, "no copy happened at all")
        final = str(self.shared / "auth.json")
        self.assertNotIn(final, targets,
                         "the destination was written in place -- a reader can see a torn file")
        for target in targets:
            self.assertEqual(str(Path(target).parent), str(self.shared),
                             "the temp file must be in the destination's own directory, "
                             "or the rename is not atomic")
        self.assertEqual((self.shared / "auth.json").read_text(), "NEW")

    def test_two_first_runs_on_one_node_seed_the_store_once(self):
        """The 32 MB seed is serialised: two children, one copy, no interleave."""
        (self.shared / "state_5.sqlite").write_bytes(b"S" * 4096)
        (self.shared / "memories_1.sqlite").write_bytes(b"M" * 128)
        seen: list[str] = []
        real = L.atomic_copy

        def slow(src: Path, dst: Path) -> bool:
            if src.name.endswith(".sqlite"):
                seen.append(src.name)
                time.sleep(0.4)      # wide enough for the other thread to race in
            return real(src, dst)

        L.atomic_copy = slow
        cfg = self.config()
        try:
            threads = [threading.Thread(target=L.prepare_home, args=(cfg,)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            L.atomic_copy = real
        self.assertEqual(sorted(seen), ["memories_1.sqlite", "state_5.sqlite"],
                         f"each store must be seeded exactly once per node, got {seen}")
        home = L.node_local_home(cfg)
        self.assertEqual((home / "state_5.sqlite").read_bytes(), b"S" * 4096)
        self.assertFalse((cfg.tmp_root / f"{cfg.home_prefix}-"
                          f"{L.getpass.getuser()}.seed.lock").exists(),
                         "the seed lock was not released")

    def test_the_startup_lock_covers_startup_and_is_released(self):
        cfg = self.config()
        first = L.prepare(config=cfg, base_env={})
        self.assertIsNotNone(first.startup_lock)
        lock = L.NodeLock(cfg.tmp_root / f"{cfg.home_prefix}-{L.getpass.getuser()}.startup.lock")
        self.assertFalse(lock.acquire(0.2), "a second start took the lock while one was starting")
        first.release_startup()
        self.assertTrue(lock.acquire(2.0), "the lock outlived startup")
        lock.release()

    def test_a_dead_lock_holder_is_reclaimed(self):
        path = self.tmp_root / "stale.lock"
        path.mkdir()
        (path / "pid").write_text("2\n", encoding="utf-8")      # pid 2 is kthreadd's
        (path / "starttime").write_text("999999999\n", encoding="utf-8")
        self.assertTrue(L.NodeLock(path).acquire(1.0),
                        "a holder whose start time moved must be reclaimed, not waited on")

    def test_the_exit_sync_runs_on_SIGTERM(self):
        """Not exec'd, so the sync always runs -- the wrapper's whole reason."""
        home = L.node_local_home(self.config())
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("FRESH-ON-THIS-NODE", encoding="utf-8")
        os.utime(home / "auth.json", (3000, 3000))
        script = self.tmp / "sigterm_child.py"
        script.write_text(
            "import os, sys, time\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from token_kit.codex import launcher as L\n"
            f"cfg = L.load_config({str(self.profiles)!r}, 'scratch')\n"
            "launch = L.prepare(config=cfg, base_env={}, serialise_startup=False)\n"
            "L.install_exit_sync(launch)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script)], env=self.env_for_child(),
                                stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:  # pragma: no cover -- defensive
                proc.kill()
        self.assertEqual((self.shared / "auth.json").read_text(), "FRESH-ON-THIS-NODE",
                         "a SIGTERMed owner lost the token it refreshed on this node")

    # -- lead 1: the profile refuses instead of falling through ----------

    def test_a_required_launcher_that_cannot_run_is_rc42_absent_and_spawns_nothing(self):
        """No silent bare-`codex` against the shared home.  Ever.

        THE DEFECT THIS CATCHES: resolution used to be detection -- is
        ~/run_codex.bash there? no? then plain `codex` -- so on a cluster whose
        wrapper was missing the dispatcher quietly ran codex with CODEX_HOME on
        NFS, which is the exact SQLite corruption the wrapper exists to avoid.
        """
        self.write_profile(binary="/nonexistent/codex")
        record = self.tmp / "record.jsonl"
        env = self.env_for_child()
        env.update({"FAKE_CODEX_RECORD": str(record),
                    "TOKEN_KIT_CODEX_SLOT_DIR": str(self.tmp / "slots")})
        task = self.tmp / "in.md"
        task.write_text("hello\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "token_kit.codex.dispatch",
             "--model", "m", "--effort", "high", "--cwd", str(self.tmp),
             "--task-file", str(task), "--out", str(self.tmp / "out.md")],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, D.EXIT_BUSY_OR_ABSENT, proc.stderr)
        self.assertIn("reason=absent", proc.stderr)
        self.assertIn("scratch", proc.stderr, "the refusal must name the profile that required it")
        self.assertFalse(record.exists(), "a codex child was spawned anyway")

    def test_an_inherited_shared_CODEX_HOME_is_refused_under_a_node_local_profile(self):
        with self.assertRaises(L.LauncherUnavailable) as caught:
            L.prepare(config=self.config(), override=str(self.binary),
                      base_env={"CODEX_HOME": str(self.shared)}, serialise_startup=False)
        self.assertEqual(caught.exception.reason, "absent")
        self.assertIn(str(self.shared), caught.exception.detail)

    def test_an_explicit_launcher_that_is_not_there_is_absent_not_a_fallback(self):
        with self.assertRaises(L.LauncherUnavailable) as caught:
            L.prepare(config=self.config(), override=str(self.tmp / "no-such-wrapper.bash"),
                      base_env={}, serialise_startup=False)
        self.assertEqual(caught.exception.reason, "absent")

    def test_a_workstation_profile_uses_plain_codex_and_prepares_nothing(self):
        self.write_profile(node_local=False)
        launch = L.prepare(config=self.config(), base_env={}, serialise_startup=False)
        self.assertFalse(launch.internal)
        self.assertIsNone(launch.home)
        self.assertNotIn("CODEX_HOME", launch.env)

    def test_a_real_turn_runs_through_the_prepared_home(self):
        """End to end against the fake: prepared home, real argv, real answer."""
        record = self.tmp / "record.jsonl"
        env = self.env_for_child()
        env.update({"FAKE_CODEX_RECORD": str(record), "FAKE_CODEX_SCENARIO": "ok",
                    "FAKE_CODEX_ANSWER": "PROVEN-VIA-KIT-LAUNCHER",
                    "TOKEN_KIT_CODEX_SLOT_DIR": str(self.tmp / "slots")})
        env.pop("CODEX_HOME", None)
        task = self.tmp / "in.md"
        task.write_text("hello\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "token_kit.codex.dispatch",
             "--model", "m", "--effort", "high", "--cwd", str(self.tmp),
             "--task-file", str(task), "--out", str(self.tmp / "out.md")],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PROVEN-VIA-KIT-LAUNCHER", proc.stdout)
        first = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["env"]["CODEX_HOME"], str(L.node_local_home(self.config())),
                         "the child did not run with the node-local home")
        self.assertGreaterEqual(int(first["env"]["TOKIO_WORKER_THREADS"]),
                                D.MIN_TOKIO_WORKER_THREADS,
                                "the measured tokio floor was lost")


if __name__ == "__main__":
    unittest.main()
