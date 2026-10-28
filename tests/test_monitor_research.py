import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import db
from app.core.monitor_research import create_research_from_change
from app.core.orchestrator import _load_task_seed_evidences, submit_clarify


class MonitorResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "research.db"
        db._LOCAL = threading.local()
        db._SCHEMA_READY = False
        db.create_subscription("sub_1", "Track Trae", ["Trae"])
        db.create_subscription_target(
            "target_1", "sub_1", "Trae", "pricing", "https://example.com/pricing"
        )
        db.create_monitor_run("run_1", "sub_1", "manual")
        db.create_page_snapshot(
            "snapshot_1",
            "run_1",
            "target_1",
            "https://example.com/pricing",
            content_hash="old",
            content_excerpt="Pro plan costs 20 USD.",
            captured_at="2026-10-24T00:00:00Z",
        )
        db.create_page_snapshot(
            "snapshot_2",
            "run_1",
            "target_1",
            "https://example.com/pricing",
            previous_snapshot_id="snapshot_1",
            content_hash="new",
            content_excerpt="Pro plan costs 25 USD.",
            captured_at="2026-10-25T00:00:00Z",
        )
        db.create_competitor_change(
            "change_1",
            "sub_1",
            "run_1",
            "target_1",
            "snapshot_1",
            "snapshot_2",
            change_type="pricing",
            severity="high",
            material=True,
            diff_summary="Pro plan price changed from 20 to 25 USD.",
        )

    def tearDown(self):
        db.close_all_connections()
        db._DB_PATH = self.old_path
        db._LOCAL = self.old_local
        db._SCHEMA_READY = self.old_ready
        self.tmp.cleanup()

    def test_change_creates_linked_task_and_promoted_seed_evidence(self):
        def create_task_stub(query, mode="deep"):
            db.save_task("task_1", query, {"_mode": mode})
            return {
                "taskId": "task_1",
                "needClarify": True,
                "clarifyQuestions": [{"id": "focus", "type": "multi", "options": []}],
            }

        with patch(
            "app.core.monitor_research.create_task",
            side_effect=create_task_stub,
        ):
            result = create_research_from_change("change_1", mode="deep")

        self.assertEqual(result["taskId"], "task_1")
        self.assertEqual(result["monitorContext"]["snapshot_id"], "snapshot_2")
        task = db.get_task("task_1")
        self.assertEqual(task["source_change_id"], "change_1")
        self.assertEqual(task["source_snapshot_id"], "snapshot_2")
        self.assertEqual(task["clarifications"]["competitors"], ["Trae"])
        self.assertEqual(task["status"], "created")

        seeds = db.list_task_seed_evidences("task_1")
        self.assertEqual(len(seeds), 1)
        self.assertTrue(seeds[0]["evidence_id"].startswith("ev_mon_"))
        self.assertEqual(seeds[0]["brand"], "Trae")
        self.assertEqual(seeds[0]["snapshot_id"], "snapshot_2")
        self.assertEqual(db.query_evidences(), [])

        loaded = _load_task_seed_evidences("task_1")
        self.assertEqual(loaded[0].evidence_id, seeds[0]["evidence_id"])
        self.assertEqual(loaded[0].brand, "Trae")

    def test_hidden_monitor_context_survives_clarification_and_links_report(self):
        db.save_task("task_2", "Research Trae change", {
            "_mode": "deep",
            "_monitor_context": {"change_id": "change_1"},
            "competitors": ["Trae"],
        })
        db.set_task_monitor_source("task_2", "change_1", "snapshot_2")

        submit_clarify("task_2", {"market": "全球"})
        task = db.get_task("task_2")
        self.assertEqual(task["clarifications"]["market"], "全球")
        self.assertEqual(task["clarifications"]["_monitor_context"]["change_id"], "change_1")

        db.save_report({
            "id": "report_1",
            "title": "Trae change",
            "query": "Research Trae change",
            "evidence": [],
            "claims": [],
        }, task_id="task_2")
        db.mark_task_done("task_2", "report_1")
        db.link_monitor_report_for_task("task_2", "report_1")

        report_row = db._connect().execute(
            "SELECT source_change_id,source_snapshot_id FROM reports WHERE report_id='report_1'"
        ).fetchone()
        change = db.get_competitor_change("change_1")
        self.assertEqual(report_row["source_change_id"], "change_1")
        self.assertEqual(report_row["source_snapshot_id"], "snapshot_2")
        self.assertEqual(change["research_report_id"], "report_1")


if __name__ == "__main__":
    unittest.main()
