import unittest
from unittest.mock import patch

from app.core.subscription_scheduler import SubscriptionScheduler


class SubscriptionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_submits_only_atomically_claimed_subscriptions(self):
        scheduler = SubscriptionScheduler(worker_id="worker-test")
        claimed = [
            {"sub_id": "sub_1", "lease_owner": "worker-test"},
            {"sub_id": "sub_2", "lease_owner": "worker-test"},
        ]
        with patch(
            "app.core.subscription_scheduler.db.claim_due_subscriptions",
            return_value=claimed,
        ) as claim, patch.object(scheduler, "submit_claimed") as submit:
            count = await scheduler.tick()

        self.assertEqual(count, 2)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(submit.call_args_list[0].kwargs["trigger_type"], "scheduled")
        self.assertEqual(claim.call_args.args[0], "worker-test")

    async def test_submit_tracks_task_until_completion(self):
        scheduler = SubscriptionScheduler(worker_id="worker-test")

        async def no_op(subscription, *, trigger_type):
            return None

        with patch.object(scheduler, "_run", side_effect=no_op):
            task = scheduler.submit_claimed(
                {"sub_id": "sub_1", "lease_owner": "worker-test"},
                trigger_type="scheduled",
            )
            self.assertEqual(scheduler.active_tasks, 1)
            await task
            await __import__("asyncio").sleep(0)
            self.assertEqual(scheduler.active_tasks, 0)


if __name__ == "__main__":
    unittest.main()
