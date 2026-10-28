import tempfile
import threading
import unittest
from pathlib import Path

from app.core import db
from app.core.monitor_fetcher import MonitorFetchResult
from app.core.monitor_runner import run_claimed_subscription


class MonitorRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db._DB_PATH
        self.old_local = db._LOCAL
        self.old_ready = db._SCHEMA_READY
        db._DB_PATH = Path(self.tmp.name) / "runner.db"
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

    def create_claimed_subscription(self, target_count=1):
        db.create_subscription(
            "sub_1",
            "Track Trae",
            ["Trae"],
            schedule_config={"interval_minutes": 60},
            next_run_at="2026-10-25T00:00:00Z",
        )
        for index in range(target_count):
            db.create_subscription_target(
                f"target_{index}",
                "sub_1",
                "Trae",
                "official",
                f"https://example.com/page-{index}",
            )
        db.set_subscription_enabled("sub_1", True)
        return db.claim_subscription(
            "sub_1",
            "worker-1",
            now="2026-10-25T00:00:00Z",
            lease_expires_at="2099-01-01T00:00:00Z",
        )

    async def test_all_targets_success_finishes_run_and_releases_lease(self):
        subscription = self.create_claimed_subscription()

        def fetcher(url, **kwargs):
            return MonitorFetchResult(
                source_url=url,
                final_url=url,
                ok=True,
                not_modified=True,
                http_status=304,
                captured_at="2026-10-25T00:01:00Z",
            )

        result = await run_claimed_subscription(
            subscription,
            "worker-1",
            fetcher=fetcher,
        )

        self.assertEqual(result["run"]["status"], "succeeded")
        self.assertEqual(result["run"]["success_count"], 1)
        self.assertEqual(result["run"]["not_modified_count"], 1)
        self.assertGreaterEqual(result["run"]["duration_ms"], 0)
        self.assertEqual(result["subscription"]["lease_owner"], "")
        self.assertEqual(result["subscription"]["consecutive_failures"], 0)
        self.assertTrue(result["subscription"]["next_run_at"])

    async def test_one_target_failure_produces_partial_run(self):
        subscription = self.create_claimed_subscription(target_count=2)

        def fetcher(url, **kwargs):
            if url.endswith("page-0"):
                return MonitorFetchResult(
                    source_url=url,
                    final_url=url,
                    ok=True,
                    not_modified=True,
                    http_status=304,
                    captured_at="2026-10-25T00:01:00Z",
                )
            return MonitorFetchResult(
                source_url=url,
                ok=False,
                error_code="timeout",
                error_message="request timed out",
            )

        result = await run_claimed_subscription(
            subscription,
            "worker-1",
            fetcher=fetcher,
        )

        self.assertEqual(result["run"]["status"], "partial")
        self.assertEqual(result["run"]["success_count"], 1)
        self.assertEqual(result["failed_targets"], 1)
        self.assertEqual(db.get_subscription_target("target_1")["failure_count"], 1)
        self.assertIn("target_1: timeout", result["run"]["error"])

        metrics = db.monitoring_metrics()
        self.assertEqual(metrics["fetch_attempts"], 2)
        self.assertEqual(metrics["fetch_successes"], 1)
        self.assertEqual(metrics["fetch_success_rate"], 0.5)

    async def test_no_enabled_targets_is_a_failed_terminal_run(self):
        subscription = self.create_claimed_subscription()
        db.update_subscription_target("target_0", enabled=False)
        # Preserve the already acquired lease to simulate a target being disabled mid-run.
        conn = db._connect()
        conn.execute(
            "UPDATE subscriptions SET lease_owner='worker-1',lease_expires_at='2099-01-01T00:00:00Z' "
            "WHERE sub_id='sub_1'"
        )
        conn.commit()
        subscription = db.get_subscription("sub_1")

        result = await run_claimed_subscription(subscription, "worker-1")

        self.assertEqual(result["run"]["status"], "failed")
        self.assertIn("no enabled targets", result["run"]["error"])
        self.assertEqual(result["subscription"]["last_status"], "needs_configuration")
        self.assertEqual(result["subscription"]["lease_owner"], "")

    def test_target_failure_threshold_disables_last_target_and_subscription(self):
        self.create_claimed_subscription()
        for index in range(3):
            target = db.record_target_fetch_failure(
                "target_0",
                f"2026-10-25T00:0{index}:00Z",
                disable_at=3,
            )
        self.assertFalse(target["enabled"])
        self.assertEqual(target["failure_count"], 3)
        subscription = db.get_subscription("sub_1")
        self.assertFalse(subscription["enabled"])
        self.assertEqual(subscription["last_status"], "needs_configuration")

    def test_due_claim_is_atomic_respects_pause_and_recovers_expired_lease(self):
        self.create_claimed_subscription()
        # Release the helper's lease, then exercise due scanning.
        conn = db._connect()
        conn.execute(
            "UPDATE subscriptions SET lease_owner='',lease_expires_at='' WHERE sub_id='sub_1'"
        )
        conn.commit()

        first = db.claim_due_subscriptions(
            "worker-a",
            now="2026-10-25T00:00:00Z",
            lease_expires_at="2026-10-25T00:10:00Z",
        )
        blocked = db.claim_due_subscriptions(
            "worker-b",
            now="2026-10-25T00:05:00Z",
            lease_expires_at="2026-10-25T00:15:00Z",
        )
        recovered = db.claim_due_subscriptions(
            "worker-b",
            now="2026-10-25T00:11:00Z",
            lease_expires_at="2026-10-25T00:21:00Z",
        )

        self.assertEqual([item["sub_id"] for item in first], ["sub_1"])
        self.assertEqual(blocked, [])
        self.assertEqual(recovered[0]["lease_owner"], "worker-b")

        db.set_subscription_enabled("sub_1", False)
        paused = db.claim_due_subscriptions(
            "worker-c",
            now="2026-10-26T00:00:00Z",
            lease_expires_at="2026-10-26T00:10:00Z",
        )
        self.assertEqual(paused, [])


if __name__ == "__main__":
    unittest.main()
