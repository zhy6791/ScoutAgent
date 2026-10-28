import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.core import db


class SubscriptionMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_without_losing_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE subscriptions (sub_id TEXT PRIMARY KEY,query TEXT,brands TEXT,"
                "created_at TEXT,last_run_at TEXT,last_report_id TEXT,run_count INTEGER)"
            )
            conn.execute(
                "INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?)",
                ("sub_legacy", "legacy query", '["A"]', "2026-01-01T00:00:00", "", "", 2),
            )
            conn.commit()

            db._init_schema(conn)
            db._init_schema(conn)  # migrations must be idempotent

            versions = [row["version"] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()]
            columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(subscriptions)"
            ).fetchall()}
            legacy = conn.execute(
                "SELECT * FROM subscriptions WHERE sub_id='sub_legacy'"
            ).fetchone()
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            indexes = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}

            self.assertEqual(versions, [1, 2, 3, 4, 5, 6])
            self.assertIn("schedule_config", columns)
            self.assertIn("lease_expires_at", columns)
            self.assertEqual(legacy["query"], "legacy query")
            self.assertEqual(legacy["run_count"], 2)
            self.assertEqual(legacy["last_status"], "needs_configuration")
            self.assertTrue({
                "subscription_targets", "monitor_runs", "page_snapshots", "competitor_changes",
                "task_seed_evidences",
            }.issubset(tables))
            self.assertTrue({
                "idx_subscriptions_due", "idx_subscription_targets_active",
                "idx_monitor_runs_subscription", "idx_page_snapshots_target",
                "idx_competitor_changes_subscription", "idx_competitor_changes_inbox",
            }.issubset(indexes))
            conn.close()


class SubscriptionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "subscriptions.db"
        db._LOCAL = threading.local()
        db._SCHEMA_READY = False

    def tearDown(self):
        conn = getattr(db._LOCAL, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH = self.old_path
        db._LOCAL = self.old_local
        db._SCHEMA_READY = self.old_ready
        self.tmp.cleanup()

    def test_subscription_target_run_snapshot_and_change_lifecycle(self):
        subscription = db.create_subscription(
            "sub_1",
            "Track Trae",
            ["Trae"],
            schedule_config={"interval_minutes": 1440},
        )
        self.assertEqual(db.schema_version(), 6)
        self.assertFalse(subscription["enabled"])
        self.assertEqual(subscription["last_status"], "needs_configuration")
        self.assertEqual(subscription["schedule_config"], {"interval_minutes": 1440})
        with self.assertRaises(ValueError):
            db.update_subscription("sub_1", schedule_type="cron")
        with self.assertRaises(ValueError):
            db.set_subscription_enabled("sub_1", True)

        target = db.create_subscription_target(
            "target_1",
            "sub_1",
            "Trae",
            "pricing",
            "HTTPS://Example.COM:443/pricing/?b=2&a=1#plans",
        )
        self.assertEqual(target["url"], "https://example.com/pricing?a=1&b=2")
        self.assertEqual(db.get_subscription("sub_1")["last_status"], "paused")

        with self.assertRaises(ValueError):
            db.create_subscription_target(
                "target_duplicate", "sub_1", "Trae", "pricing",
                "https://example.com/pricing?a=1&b=2",
            )

        enabled = db.set_subscription_enabled("sub_1", True)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["last_status"], "active")

        run = db.create_monitor_run("run_1", "sub_1", "manual")
        self.assertEqual(run["status"], "pending")
        self.assertEqual(run["target_count"], 1)
        db.update_monitor_run("run_1", status="running", started_at="2026-10-25T00:00:00Z")

        first = db.create_page_snapshot(
            "snapshot_1", "run_1", "target_1", target["url"],
            http_status=200, content_hash="hash-1", content_length=100,
        )
        second = db.create_page_snapshot(
            "snapshot_2", "run_1", "target_1", target["url"],
            previous_snapshot_id="snapshot_1", http_status=200,
            content_hash="hash-2", content_length=120,
        )
        self.assertEqual(first["previous_snapshot_id"], "")
        self.assertEqual(second["previous_snapshot_id"], "snapshot_1")
        self.assertEqual(db.get_subscription_target("target_1")["last_hash"], "hash-2")

        change = db.create_competitor_change(
            "change_1", "sub_1", "run_1", "target_1", "snapshot_1", "snapshot_2",
            change_type="pricing", severity="high", material=True,
            change_ratio=0.2, diff_summary="Price changed",
        )
        self.assertTrue(change["material"])
        self.assertEqual(change["status"], "new")
        self.assertEqual(db.update_competitor_change_status("change_1", "read")["status"], "read")

        db.delete_subscription_target("target_1")
        subscription = db.get_subscription("sub_1")
        self.assertFalse(subscription["enabled"])
        self.assertEqual(subscription["last_status"], "needs_configuration")
        self.assertEqual(db.list_page_snapshots("target_1"), [])
        self.assertEqual(db.list_competitor_changes("sub_1"), [])

        db.delete_subscription("sub_1")
        self.assertIsNone(db.get_subscription("sub_1"))
        self.assertEqual(db.list_monitor_runs("sub_1"), [])
        self.assertEqual(db._connect().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_target_validation_and_pause_clear_runtime_lease(self):
        db.create_subscription("sub_2", "Track Cursor", ["Cursor"])
        with self.assertRaises(ValueError):
            db.create_subscription_target(
                "target_bad_type", "sub_2", "Cursor", "social", "https://cursor.com"
            )
        with self.assertRaises(ValueError):
            db.create_subscription_target(
                "target_bad_url", "sub_2", "Cursor", "official", "file:///etc/passwd"
            )

        db.create_subscription_target(
            "target_2", "sub_2", "Cursor", "official", "https://cursor.com"
        )
        db.set_subscription_enabled("sub_2", True)
        conn = db._connect()
        conn.execute(
            "UPDATE subscriptions SET lease_owner='worker-1',lease_expires_at='2099-01-01T00:00:00Z' "
            "WHERE sub_id='sub_2'"
        )
        conn.commit()

        paused = db.set_subscription_enabled("sub_2", False)
        self.assertFalse(paused["enabled"])
        self.assertEqual(paused["last_status"], "paused")
        self.assertEqual(paused["lease_owner"], "")
        self.assertEqual(paused["lease_expires_at"], "")


if __name__ == "__main__":
    unittest.main()
