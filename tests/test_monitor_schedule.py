import datetime as dt
import unittest

from app.core.monitor_schedule import next_retry_run, next_scheduled_run


UTC = dt.timezone.utc


class MonitorScheduleTests(unittest.TestCase):
    def test_interval_schedule_advances_from_completion_time(self):
        subscription = {
            "schedule_type": "interval",
            "schedule_config": {"interval_minutes": 90},
        }
        actual = next_scheduled_run(
            subscription,
            after=dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(actual, "2026-10-25T01:30:00Z")

    def test_daily_schedule_uses_subscription_timezone(self):
        subscription = {
            "schedule_type": "daily",
            "schedule_config": {"time": "09:00"},
            "timezone": "Asia/Shanghai",
        }
        same_day = next_scheduled_run(
            subscription,
            after=dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        )
        next_day = next_scheduled_run(
            subscription,
            after=dt.datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
        )
        self.assertEqual(same_day, "2026-10-25T01:00:00Z")
        self.assertEqual(next_day, "2026-10-26T01:00:00Z")

    def test_retry_uses_exponential_backoff_without_passing_regular_run(self):
        subscription = {
            "schedule_type": "interval",
            "schedule_config": {"interval_minutes": 1440},
        }
        actual = next_retry_run(
            subscription,
            failures=2,
            after=dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(actual, "2026-10-25T00:20:00Z")

    def test_invalid_schedule_is_rejected(self):
        with self.assertRaises(ValueError):
            next_scheduled_run({"schedule_type": "cron"})
        with self.assertRaises(ValueError):
            next_scheduled_run({
                "schedule_type": "daily",
                "schedule_config": {"time": "25:00"},
            })


if __name__ == "__main__":
    unittest.main()
