"""长驻后端中的竞品订阅调度器。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import socket
import uuid
from typing import Any, Dict, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core import db
from app.core.config import get_settings
from app.core.monitor_runner import run_claimed_subscription
from app.core.monitor_schedule import format_utc, utc_now


logger = logging.getLogger(__name__)


class SubscriptionScheduler:
    """周期扫描到期订阅，并在当前事件循环中提交已领取的运行。"""

    def __init__(self, *, worker_id: str = "") -> None:
        settings = get_settings()
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._semaphore = asyncio.Semaphore(max(1, settings.monitor_max_concurrency))
        self._tasks: Set[asyncio.Task[Any]] = set()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_tasks(self) -> int:
        return len(self._tasks)

    def start(self) -> None:
        if self._started:
            return
        settings = get_settings()
        self._scheduler.add_job(
            self.tick,
            "interval",
            seconds=max(1, settings.monitor_tick_seconds),
            id="subscription-monitor-tick",
            max_instances=1,
            coalesce=True,
            next_run_time=utc_now(),
            replace_existing=True,
        )
        self._scheduler.start()
        self._started = True
        logger.info("subscription scheduler started worker_id=%s", self.worker_id)

    async def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        logger.info("subscription scheduler stopped worker_id=%s", self.worker_id)

    async def tick(self) -> int:
        settings = get_settings()
        now = utc_now()
        lease_expires_at = now + dt.timedelta(seconds=max(1, settings.monitor_lease_seconds))
        claimed = await asyncio.to_thread(
            db.claim_due_subscriptions,
            self.worker_id,
            now=format_utc(now),
            lease_expires_at=format_utc(lease_expires_at),
            limit=settings.monitor_due_batch_size,
        )
        for subscription in claimed:
            self.submit_claimed(subscription, trigger_type="scheduled")
        return len(claimed)

    def submit_claimed(
        self,
        subscription: Dict[str, Any],
        *,
        trigger_type: str,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(
            self._run(subscription, trigger_type=trigger_type),
            name=f"monitor:{subscription['sub_id']}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(self, subscription: Dict[str, Any], *, trigger_type: str) -> None:
        try:
            await run_claimed_subscription(
                subscription,
                self.worker_id,
                trigger_type=trigger_type,
                semaphore=self._semaphore,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "subscription monitor failed sub_id=%s trigger=%s",
                subscription.get("sub_id", ""),
                trigger_type,
            )


_scheduler: Optional[SubscriptionScheduler] = None


def get_subscription_scheduler() -> SubscriptionScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = SubscriptionScheduler()
    return _scheduler
