import tempfile
import threading
import unittest
from pathlib import Path

from app.core import db
from app.core.monitor_fetcher import MonitorFetchResult
from app.core.snapshot_store import (
    prune_target_snapshots,
    read_snapshot,
    save_fetch_snapshot,
    snapshot_disk_usage,
    write_snapshot,
)


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snapshots"
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "snapshot-test.db"
        db._LOCAL = threading.local()
        db._SCHEMA_READY = False

        db.create_subscription("sub_1", "Track Trae", ["Trae"])
        db.create_subscription_target(
            "target_1", "sub_1", "Trae", "pricing", "https://example.com/pricing"
        )
        db.create_monitor_run("run_1", "sub_1", "manual")

    def tearDown(self):
        conn = getattr(db._LOCAL, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH = self.old_path
        db._LOCAL = self.old_local
        db._SCHEMA_READY = self.old_ready
        self.tmp.cleanup()

    def result(self, text, content_hash):
        return MonitorFetchResult(
            source_url="https://example.com/pricing",
            final_url="https://example.com/pricing",
            ok=True,
            http_status=200,
            content_type="text/html",
            text=text,
            content_hash=content_hash,
            content_length=len(text),
            etag='"v1"',
            captured_at="2026-10-25T08:00:00Z",
        )

    def test_first_capture_is_baseline_and_second_links_to_it(self):
        first = save_fetch_snapshot(
            "snapshot_1", "run_1", "target_1", self.result("first body", "hash-1"),
            root=self.root,
        )
        second = save_fetch_snapshot(
            "snapshot_2", "run_1", "target_1", self.result("second body", "hash-2"),
            root=self.root,
        )

        self.assertTrue(first["is_baseline"])
        self.assertFalse(second["is_baseline"])
        self.assertEqual(second["snapshot"]["previous_snapshot_id"], "snapshot_1")
        self.assertEqual(read_snapshot(first["snapshot"]["snapshot_path"], root=self.root), "first body")
        self.assertEqual(db.list_competitor_changes("sub_1"), [])

    def test_failed_or_not_modified_fetch_cannot_replace_baseline(self):
        save_fetch_snapshot(
            "snapshot_1", "run_1", "target_1", self.result("baseline", "hash-1"),
            root=self.root,
        )
        failed = MonitorFetchResult(
            source_url="https://example.com/pricing", ok=False, error_code="timeout"
        )
        unchanged = MonitorFetchResult(
            source_url="https://example.com/pricing", ok=True, not_modified=True
        )

        with self.assertRaises(ValueError):
            save_fetch_snapshot("snapshot_failed", "run_1", "target_1", failed, root=self.root)
        with self.assertRaises(ValueError):
            save_fetch_snapshot("snapshot_304", "run_1", "target_1", unchanged, root=self.root)

        target = db.get_subscription_target("target_1")
        self.assertEqual(target["last_snapshot_id"], "snapshot_1")
        self.assertEqual(target["last_hash"], "hash-1")

    def test_snapshot_ids_cannot_escape_root_or_overwrite(self):
        relative = write_snapshot("snapshot_1", "target_1", "body", root=self.root)
        self.assertEqual(read_snapshot(relative, root=self.root), "body")
        with self.assertRaises(FileExistsError):
            write_snapshot("snapshot_1", "target_1", "new body", root=self.root)
        with self.assertRaises(ValueError):
            write_snapshot("../escape", "target_1", "body", root=self.root)
        self.assertEqual(read_snapshot(relative, root=self.root), "body")

    def test_retention_removes_only_unreferenced_old_snapshots(self):
        snapshots = []
        for index in range(1, 5):
            result = self.result(f"body {index}", f"hash-{index}")
            result.captured_at = f"2026-10-25T08:0{index}:00Z"
            snapshots.append(save_fetch_snapshot(
                f"snapshot_{index}", "run_1", "target_1", result, root=self.root,
            )["snapshot"])
        db.create_competitor_change(
            "change_1", "sub_1", "run_1", "target_1", "snapshot_1", "snapshot_2",
            material=True,
        )

        removed = prune_target_snapshots("target_1", keep=1, root=self.root)

        self.assertEqual(removed, 1)
        self.assertIsNone(db.get_page_snapshot("snapshot_3"))
        self.assertIsNotNone(db.get_page_snapshot("snapshot_1"))
        self.assertIsNotNone(db.get_page_snapshot("snapshot_2"))
        self.assertIsNotNone(db.get_page_snapshot("snapshot_4"))
        self.assertGreater(snapshot_disk_usage(root=self.root), 0)


if __name__ == "__main__":
    unittest.main()
