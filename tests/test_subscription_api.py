import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import db
from app.core.subscription_api import router


class SubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "api.db"
        db._LOCAL = threading.local()
        db._SCHEMA_READY = False
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        db.close_all_connections()
        db._DB_PATH = self.old_path
        db._LOCAL = self.old_local
        db._SCHEMA_READY = self.old_ready
        self.tmp.cleanup()

    def create_active_subscription(self):
        response = self.client.post(
            "/api/subscriptions",
            json={
                "query": "Track Trae",
                "brands": ["Trae"],
                "timezone": "Asia/Shanghai",
                "schedule": {"type": "interval", "interval_minutes": 60},
                "targets": [{
                    "brand": "Trae",
                    "target_type": "official",
                    "url": "https://example.com/trae",
                }],
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_get_patch_pause_and_resume(self):
        created = self.create_active_subscription()
        sub_id = created["sub_id"]
        self.assertTrue(created["enabled"])
        self.assertEqual(created["enabled_target_count"], 1)
        self.assertTrue(created["next_run_at"])

        patched = self.client.patch(
            f"/api/subscriptions/{sub_id}",
            json={
                "schedule": {"type": "daily", "time": "09:30"},
                "timezone": "Asia/Shanghai",
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["schedule_type"], "daily")
        self.assertEqual(patched.json()["schedule_config"], {"time": "09:30"})

        paused = self.client.post(f"/api/subscriptions/{sub_id}/pause")
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(paused.json()["enabled"])
        self.assertEqual(paused.json()["last_status"], "paused")

        resumed = self.client.post(f"/api/subscriptions/{sub_id}/resume")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.json()["enabled"])
        self.assertEqual(resumed.json()["last_status"], "active")

    def test_draft_requires_brand_and_target_before_resume(self):
        response = self.client.post(
            "/api/subscriptions",
            json={"query": "Draft", "brands": [], "enabled": True},
        )
        self.assertEqual(response.status_code, 201)
        draft = response.json()
        self.assertFalse(draft["enabled"])
        self.assertEqual(draft["last_status"], "needs_configuration")

        failed = self.client.post(f"/api/subscriptions/{draft['sub_id']}/resume")
        self.assertEqual(failed.status_code, 409)

        target = self.client.post(
            f"/api/subscriptions/{draft['sub_id']}/targets",
            json={
                "brand": "Cursor",
                "target_type": "pricing",
                "url": "https://example.com/pricing",
            },
        )
        self.assertEqual(target.status_code, 201, target.text)
        resumed = self.client.post(f"/api/subscriptions/{draft['sub_id']}/resume")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["brands"], ["Cursor"])

    def test_invalid_schedule_timezone_and_url_return_4xx(self):
        invalid_timezone = self.client.post(
            "/api/subscriptions",
            json={"query": "Bad timezone", "timezone": "Mars/Olympus"},
        )
        self.assertEqual(invalid_timezone.status_code, 422)

        invalid_schedule = self.client.post(
            "/api/subscriptions",
            json={
                "query": "Bad schedule",
                "schedule": {"type": "daily", "time": "25:00"},
            },
        )
        self.assertEqual(invalid_schedule.status_code, 422)

        invalid_url = self.client.post(
            "/api/subscriptions",
            json={
                "query": "Bad URL",
                "brands": ["Trae"],
                "targets": [{
                    "brand": "Trae",
                    "target_type": "official",
                    "url": "file:///etc/passwd",
                }],
            },
        )
        self.assertEqual(invalid_url.status_code, 422)
        self.assertEqual(db.list_subscriptions(), [])

    def test_manual_run_returns_202_and_persists_pending_run(self):
        subscription = self.create_active_subscription()
        stub = AsyncMock(return_value=None)
        with patch("app.core.subscription_api.run_claimed_subscription", stub):
            response = self.client.post(
                f"/api/subscriptions/{subscription['sub_id']}/run"
            )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        run = db.get_monitor_run(payload["run_id"])
        self.assertIsNotNone(run)
        self.assertEqual(run["trigger_type"], "manual")
        stub.assert_awaited_once()

        duplicate = self.client.post(
            f"/api/subscriptions/{subscription['sub_id']}/run"
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_target_runs_changes_and_status_endpoints(self):
        subscription = self.create_active_subscription()
        sub_id = subscription["sub_id"]
        target = subscription["targets"][0]
        db.create_monitor_run("run_1", sub_id, "manual")
        db.create_page_snapshot(
            "snapshot_1",
            "run_1",
            target["target_id"],
            target["url"],
            content_hash="hash-1",
        )
        db.create_page_snapshot(
            "snapshot_2",
            "run_1",
            target["target_id"],
            target["url"],
            previous_snapshot_id="snapshot_1",
            content_hash="hash-2",
        )
        db.create_competitor_change(
            "change_1",
            sub_id,
            "run_1",
            target["target_id"],
            "snapshot_1",
            "snapshot_2",
            change_type="pricing",
            severity="high",
            material=True,
        )

        runs = self.client.get(f"/api/subscriptions/{sub_id}/runs")
        changes = self.client.get(f"/api/subscriptions/{sub_id}/changes")
        run = self.client.get("/api/monitor-runs/run_1")
        updated = self.client.patch(
            "/api/competitor-changes/change_1",
            json={"status": "read"},
        )

        self.assertEqual(runs.status_code, 200)
        self.assertEqual(run.status_code, 200)
        self.assertEqual(changes.json()[0]["brand"], "Trae")
        self.assertEqual(changes.json()[0]["source_url"], target["url"])
        self.assertEqual(updated.json()["status"], "read")


if __name__ == "__main__":
    unittest.main()
