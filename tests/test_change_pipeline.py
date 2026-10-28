import tempfile
import threading
import unittest
from pathlib import Path

from app.core import db
from app.core.change_classifier import ChangeClassification
from app.core.change_pipeline import process_snapshot_change
from app.core.monitor_fetcher import MonitorFetchResult
from app.core.snapshot_store import save_fetch_snapshot


class ChangePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snapshots"
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "changes.db"
        db._LOCAL = threading.local()
        db._SCHEMA_READY = False
        db.create_subscription("sub_1", "Track Trae", ["Trae"])
        db.create_subscription_target(
            "target_1", "sub_1", "Trae", "pricing", "https://example.com/pricing"
        )
        db.create_monitor_run("run_1", "sub_1", "manual")
        db.create_monitor_run("run_2", "sub_1", "manual")

    def tearDown(self):
        conn = getattr(db._LOCAL, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH = self.old_path
        db._LOCAL = self.old_local
        db._SCHEMA_READY = self.old_ready
        self.tmp.cleanup()

    def save(self, snapshot_id, run_id, text, content_hash):
        return save_fetch_snapshot(
            snapshot_id,
            run_id,
            "target_1",
            MonitorFetchResult(
                source_url="https://example.com/pricing",
                final_url="https://example.com/pricing",
                ok=True,
                http_status=200,
                text=text,
                content_hash=content_hash,
                content_length=len(text),
                captured_at="2026-10-25T08:00:00Z",
            ),
            root=self.root,
        )

    def test_baseline_and_identical_content_do_not_create_change(self):
        self.save("snapshot_1", "run_1", "Same product body", "hash-1")
        baseline = process_snapshot_change("snapshot_1", root=self.root)
        self.save("snapshot_2", "run_2", "Same product body", "hash-2")
        unchanged = process_snapshot_change("snapshot_2", root=self.root)

        self.assertEqual(baseline["status"], "baseline")
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(db.list_competitor_changes("sub_1"), [])

    def test_material_change_is_classified_recorded_and_idempotent(self):
        self.save("snapshot_1", "run_1", "Pro plan price USD 20 /month", "hash-1")
        self.save("snapshot_2", "run_2", "Pro plan price USD 25 /month", "hash-2")
        calls = []

        def classifier(brand, target_type, url, detected):
            calls.append((brand, target_type, url, detected.material))
            return ChangeClassification(
                material=True,
                change_type="pricing",
                severity="high",
                summary="月费由 20 美元调整为 25 美元。",
                facts=["旧价格 20 美元", "新价格 25 美元"],
                llm_status="success",
                llm_tokens=456,
            )

        created = process_snapshot_change("snapshot_2", root=self.root, classifier=classifier)
        repeated = process_snapshot_change("snapshot_2", root=self.root, classifier=classifier)

        self.assertEqual(created["status"], "created")
        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(len(calls), 1)
        self.assertTrue(created["change"]["material"])
        self.assertIn("旧价格 20 美元", created["change"]["diff_summary"])
        run = db.get_monitor_run("run_2")
        self.assertEqual(run["changed_count"], 1)
        self.assertEqual(run["llm_tokens"], 456)
        self.assertEqual(len(db.list_competitor_changes("sub_1")), 1)

    def test_minor_change_is_audited_without_llm_tokens_or_changed_count(self):
        self.save("snapshot_1", "run_1", "Welcome to our website.", "hash-1")
        self.save("snapshot_2", "run_2", "Welcome to our website!", "hash-2")
        result = process_snapshot_change("snapshot_2", root=self.root)

        self.assertEqual(result["status"], "created")
        self.assertFalse(result["llm_called"])
        self.assertFalse(result["change"]["material"])
        self.assertEqual(result["change"]["severity"], "info")
        run = db.get_monitor_run("run_2")
        self.assertEqual(run["changed_count"], 0)
        self.assertEqual(run["llm_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
