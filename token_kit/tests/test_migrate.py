"""Legacy migration uses only temporary folders and makes no model calls."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_kit.core import migrate
from token_kit.core.store import read_json


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.workspace = self.base / "source with spaces"
        self.workspace.mkdir()
        self.source = self.base / "legacy task with spaces"
        self.source.mkdir()
        self.root = self.base / "new tasks"
        (self.source / "STATE.md").write_bytes((
            f"# Improve parser\r\n\r\nCwd: {self.workspace}\r\nStatus: done\r\n"
            "\r\n## Decisions\r\nKept interface.\r\n## In flight\r\nUnknown job.\r\n"
            "## Next\r\nCheck tests.\r\n"
        ).encode())
        (self.source / "PROMPTS.md").write_bytes(b"# Original requests\nPlease fix it.\n")

    def lane(self, relative="lanes/parser/v0"):
        path = self.source / relative
        path.mkdir(parents=True)
        (path / "in.md").write_bytes(b"Fix parser\r\n")
        (path / "out.md").write_bytes(b"Everything complete!\n\xff")
        (path / "RESPAWN_REQUEST.md").write_bytes(b"Restart after inspecting running job.\n")
        return path

    def source_bytes(self):
        return {p.relative_to(self.source).as_posix(): p.read_bytes()
                for p in self.source.rglob("*") if p.is_file()}

    def test_preserves_source_bytes_and_snapshots_all_provenance(self):
        self.lane()
        self.lane("lanes/parser/v0/lanes/check/v1")
        before = self.source_bytes()
        store = migrate.migrate_task(self.source, self.root)
        self.assertEqual(before, self.source_bytes())
        self.assertEqual(self.workspace.resolve(), store.workspace)
        provenance = read_json(store.path / "migration.json")
        self.assertEqual(str(self.source.resolve()), provenance["source"])
        self.assertEqual("complete", provenance["status"])
        self.assertEqual({".", "lanes/parser/v0", "lanes/parser/v0/lanes/check/v1"},
                         set(provenance["agents"]))
        observed = set()
        for record in provenance["agents"].values():
            self.assertTrue(record["recovery_required"])
            agent = store.agent_path(record["agent_id"])
            state = (agent / "STATE.md").read_text()
            self.assertIn("UNVERIFIED", state)
            self.assertIn("RECOVERY REQUIRED", state)
            self.assertIn("No task work or completion has been verified", state)
            self.assertFalse((agent / "out.md").exists())
            self.assertEqual([], store.resume_bundle(record["agent_id"])["runs"])
            for snapshot in record["files"]:
                data = (store.path / snapshot["artifact"]).read_bytes()
                self.assertEqual(before[snapshot["source"]], data)
                self.assertEqual(hashlib.sha256(data).hexdigest(), snapshot["sha256"])
                observed.add(snapshot["source"])
        self.assertEqual(set(before), observed)

    def test_import_is_idempotent_and_does_not_resync_source_changes(self):
        lane = self.lane()
        first = migrate.migrate_task(self.source, self.root)
        initial = (first.path / "migration.json").read_bytes()
        (lane / "out.md").write_text("New source result")
        second = migrate.migrate_task(self.source, self.root)
        self.assertEqual(first.path, second.path)
        self.assertEqual(initial, (second.path / "migration.json").read_bytes())
        self.assertEqual(1, len(list(self.root.glob("*/task.json"))))

    def test_similar_lane_names_have_distinct_safe_stable_ids(self):
        self.lane("lanes/a b/v0")
        self.lane("lanes/a-b/v0")
        store = migrate.migrate_task(self.source, self.root)
        records = read_json(store.path / "migration.json")["agents"]
        identifiers = [record["agent_id"] for record in records.values()]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for identifier in identifiers:
            self.assertRegex(identifier, r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

    def test_invalid_source_metadata_does_not_create_destination(self):
        for text in ("# title\n", "Cwd: /tmp\n", "# title\nCwd: relative/path\n",
                     f"# title\nCwd: {self.base / 'missing'}\n"):
            with self.subTest(text=text):
                (self.source / "STATE.md").write_text(text)
                with self.assertRaises(ValueError):
                    migrate.migrate_task(self.source, self.root)
                self.assertFalse(self.root.exists())

    def test_symlinked_evidence_is_rejected_before_writes(self):
        lane = self.lane()
        (lane / "link").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            migrate.migrate_task(self.source, self.root)
        self.assertFalse(self.root.exists())

    def test_source_and_lanes_symlink_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            migrate.migrate_task(alias, self.root)
        (self.source / "lanes").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            migrate.migrate_task(self.source, self.root)
        self.assertFalse(self.root.exists())

    def test_bounded_traversal_rejects_before_writes(self):
        self.lane()
        for setting, value in (("MAX_ENTRIES", 1), ("MAX_DEPTH", 0),
                               ("MAX_FILE_BYTES", 8), ("MAX_TOTAL_BYTES", 1)):
            with self.subTest(setting=setting), patch.object(migrate, setting, value):
                with self.assertRaisesRegex(ValueError, "limit"):
                    migrate.migrate_task(self.source, self.root)
                self.assertFalse(self.root.exists())

    def test_destination_inside_source_rejected(self):
        before = self.source_bytes()
        with self.assertRaisesRegex(ValueError, "outside"):
            migrate.migrate_task(self.source, self.source / "new")
        self.assertEqual(before, self.source_bytes())

    def test_failed_import_is_visible_and_never_silently_duplicated(self):
        self.lane()
        with patch.object(migrate, "_atomic_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migrate.migrate_task(self.source, self.root)
        with self.assertRaisesRegex(ValueError, "Incomplete legacy import"):
            migrate.migrate_task(self.source, self.root)
        self.assertEqual(1, len(list(self.root.glob("*/task.json"))))

    def test_creation_failure_leaves_intent_record_for_inspection(self):
        with patch.object(migrate.Store, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migrate.migrate_task(self.source, self.root)
        with self.assertRaisesRegex(ValueError, "Incomplete legacy import"):
            migrate.migrate_task(self.source, self.root)
        self.assertEqual([], list(self.root.glob("*/task.json")))


if __name__ == "__main__":
    unittest.main()
