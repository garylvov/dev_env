"""Task pyramid snapshots remain current without rewriting recovery history."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit import pyramid
from token_kit.core.store import Store


class PyramidStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.defaults = self.root / "defaults.md"
        self.original = "# Trigger pyramid\r\n\r\nSmart: Astra medium → Opus medium\r\n"
        self.defaults.write_bytes(self.original.encode())
        mocked = patch.object(pyramid, "REPOSITORY_PATH", self.defaults)
        mocked.start()
        self.addCleanup(mocked.stop)

    def create(self):
        return Store.create(self.root / "tasks", "Pyramid", self.root)

    def test_create_copies_exact_defaults_and_isolates_tasks(self):
        first = self.create()
        second = self.create()
        target = first.path / pyramid.NAME
        self.assertEqual(target.read_bytes(), self.defaults.read_bytes())
        target.write_text("Smart: Opus medium\n")
        self.defaults.write_text("Smart: Astra high\n")
        self.assertEqual(second.trigger_pyramid()["content"], self.original)
        self.assertEqual(first.trigger_pyramid(seed=True)["content"], "Smart: Opus medium\n")
        self.assertEqual(target.read_text(), "Smart: Opus medium\n")

    def test_old_task_read_only_fallback_then_explicit_seed(self):
        store = self.create()
        target = store.path / pyramid.NAME
        target.unlink()
        recovered = Store(store.path)
        metadata = recovered.resume_bundle("coordinator")["trigger_pyramid"]
        self.assertEqual(metadata, recovered.trigger_pyramid())
        self.assertEqual(metadata["source"], "repository")
        self.assertFalse(target.exists())
        seeded = recovered.trigger_pyramid(seed=True)
        self.assertEqual(seeded["source"], "task")
        self.assertEqual(seeded["path"], str(target))
        self.assertEqual(target.read_bytes(), self.defaults.read_bytes())

    def test_current_map_reaches_new_workers_and_resume_without_rewriting_history(self):
        store = self.create()
        before = store.resume_bundle("coordinator")
        assignment = Path(before["assignment"])
        immutable = assignment.read_bytes()
        manifest = Path(before["checkpoint"]) / "manifest.json"
        committed = manifest.read_bytes()
        override = "Smartest: Opus medium; Smart: Astra high; Medium: Sol medium; Mid: Luna xhigh\n"
        (store.path / pyramid.NAME).write_text(override)
        worker = store.add_agent("worker", "Implement bounded change")
        self.assertIn(override, (worker / "in.md").read_text())
        after = Store(store.path).resume_bundle("coordinator")
        self.assertEqual(after["trigger_pyramid"]["content"], override)
        self.assertEqual(after["changed_evidence"], [])
        self.assertEqual(assignment.read_bytes(), immutable)
        self.assertEqual(manifest.read_bytes(), committed)

    def test_invalid_defaults_do_not_reserve_task(self):
        self.defaults.write_bytes(b"")
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse((self.root / "tasks").exists())

    def test_invalid_map_does_not_leave_orphan_agent(self):
        store = self.create()
        target = store.path / pyramid.NAME
        for content in (b"", b" \n", b"\xff", b"x" * (pyramid.MAX_BYTES + 1)):
            with self.subTest(content_length=len(content)):
                target.write_bytes(content)
                with self.assertRaises(ValueError):
                    store.add_agent("worker", "Implement bounded change")
                self.assertFalse((store.path / "agents" / "worker").exists())

    def test_symlink_snapshot_is_rejected_without_overwriting_target(self):
        store = self.create()
        target = store.path / pyramid.NAME
        target.unlink()
        target.symlink_to(self.defaults)
        with self.assertRaises(ValueError):
            store.trigger_pyramid(seed=True)
        self.assertEqual(self.defaults.read_bytes(), self.original.encode())


if __name__ == "__main__":
    unittest.main()
