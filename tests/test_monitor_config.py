import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings


class MonitorConfigTests(unittest.TestCase):
    def test_scheduler_is_available_only_for_long_running_backend(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(subscription_scheduler_enabled=True)
            self.assertTrue(settings.subscription_scheduler_active)

        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            settings = Settings(subscription_scheduler_enabled=True)
            self.assertTrue(settings.is_serverless)
            self.assertFalse(settings.subscription_scheduler_active)
            self.assertEqual(settings.monitor_snapshot_path, Path("/tmp/monitor_snapshots"))

    def test_explicit_snapshot_directory_wins(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            settings = Settings(monitor_snapshot_dir="D:/monitor-data")
            self.assertEqual(settings.monitor_snapshot_path, Path("D:/monitor-data"))


if __name__ == "__main__":
    unittest.main()
