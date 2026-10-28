"""执行一次已经取得租约的竞品订阅监控。"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from typing import Any, Callable, Dict, Optional

from app.core import db
from app.core.change_classifier import classify_change
from app.core.change_pipeline import process_snapshot_change
from app.core.config import get_settings
from app.core.monitor_fetcher import fetch_monitor_page
from app.core.monitor_schedule import format_utc, next_retry_run, next_scheduled_run, utc_now
from app.core.snapshot_store import prune_target_snapshots, save_fetch_snapshot


def _process_target(
    target: Dict[str, Any],
    run_id: str,
    *,
    fetcher: Callable[..., Any],
    classifier: Callable[..., Any],
) -> Dict[str, Any]:
    settings = get_settings()
    checked_at = format_utc(utc_now())
    latest = db.get_page_snapshot(target.get("last_snapshot_id", "")) if target.get("last_snapshot_id") else None
    try:
        result = fetcher(
            target["url"],
            etag=(latest or {}).get("etag", ""),
            last_modified=(latest or {}).get("last_modified", ""),
        )
        if not result.ok:
            db.record_target_fetch_failure(
                target["target_id"], checked_at,
                disable_at=settings.monitor_target_failure_threshold,
            )
            return {
                "target_id": target["target_id"], "ok": False,
                "error": f"{result.error_code}: {result.error_message}"[:1000],
            }
        if result.not_modified:
            db.record_target_fetch_success(target["target_id"], result.captured_at or checked_at)
            return {"target_id": target["target_id"], "ok": True, "status": "unchanged_304"}

        snapshot_id = "snap_" + uuid.uuid4().hex[:12]
        saved = save_fetch_snapshot(snapshot_id, run_id, target["target_id"], result)
        change_result = process_snapshot_change(snapshot_id, classifier=classifier)
        try:
            prune_target_snapshots(
                target["target_id"],
                keep=settings.monitor_snapshot_retention,
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "target_id": target["target_id"],
            "ok": True,
            "status": change_result["status"],
            "snapshot_id": saved["snapshot"]["snapshot_id"],
            "change_id": (change_result.get("change") or {}).get("change_id", ""),
            "material": bool((change_result.get("change") or {}).get("material")),
            "llm_status": (change_result.get("change") or {}).get("llm_status", "skipped"),
        }
    except Exception as exc:  # noqa: BLE001
        db.record_target_fetch_failure(
            target["target_id"], checked_at,
            disable_at=settings.monitor_target_failure_threshold,
        )
        return {"target_id": target["target_id"], "ok": False, "error": str(exc)[:1000]}
    finally:
        db.close_thread_connection()


async def run_claimed_subscription(
    subscription: Dict[str, Any],
    worker_id: str,
    *,
    trigger_type: str = "scheduled",
    run_id: str = "",
    semaphore: Optional[asyncio.Semaphore] = None,
    fetcher: Callable[..., Any] = fetch_monitor_page,
    classifier: Callable[..., Any] = classify_change,
) -> Dict[str, Any]:
    sub_id = subscription["sub_id"]
    if subscription.get("lease_owner") != worker_id:
        raise ValueError("subscription lease is not owned by this worker")
    targets = db.list_subscription_targets(sub_id, include_disabled=False)
    run_id = run_id or "mon_" + uuid.uuid4().hex[:12]
    started = utc_now()
    monotonic_start = time.monotonic()
    started_at = format_utc(started)
    run = db.get_monitor_run(run_id)
    if run and run.get("sub_id") != sub_id:
        raise ValueError("monitor run belongs to another subscription")
    if not run:
        run = db.create_monitor_run(
            run_id,
            sub_id,
            trigger_type,
            scheduled_at=subscription.get("next_run_at", "") if trigger_type == "scheduled" else started_at,
            target_count=len(targets),
        )
    db.update_monitor_run(run_id, status="running", started_at=started_at)
    limiter = semaphore or asyncio.Semaphore(max(1, get_settings().monitor_max_concurrency))

    async def execute(target):
        async with limiter:
            return await asyncio.to_thread(
                _process_target,
                target,
                run_id,
                fetcher=fetcher,
                classifier=classifier,
            )

    try:
        results = await asyncio.gather(*(execute(target) for target in targets)) if targets else []
        successes = sum(1 for result in results if result.get("ok"))
        failures = len(results) - successes
        if targets and successes == len(targets):
            status = "succeeded"
        elif successes:
            status = "partial"
        else:
            status = "failed"
        errors = "; ".join(
            f"{result.get('target_id', 'unknown')}: {result.get('error', '')}"
            for result in results if result.get("error")
        )
        if not targets:
            errors = "subscription has no enabled targets"

        finished = utc_now()
        finished_at = format_utc(finished)
        duration_ms = max(0, int((time.monotonic() - monotonic_start) * 1000))
        db.update_monitor_run(
            run_id,
            status=status,
            finished_at=finished_at,
            success_count=successes,
            not_modified_count=sum(1 for item in results if item.get("status") == "unchanged_304"),
            minor_change_count=sum(
                1 for item in results if item.get("change_id") and not item.get("material")
            ),
            llm_failure_count=sum(1 for item in results if item.get("llm_status") == "failed"),
            duration_ms=duration_ms,
            error=errors,
        )
        if status == "failed":
            next_run_at = next_retry_run(
                subscription,
                failures=int(subscription.get("consecutive_failures") or 0) + 1,
                after=finished,
            )
        else:
            next_run_at = next_scheduled_run(subscription, after=finished)
        updated_subscription = db.finish_subscription_monitor(
            sub_id,
            worker_id,
            status=status,
            next_run_at=next_run_at,
            checked_at=finished_at,
            success=status == "succeeded",
            error=errors,
        )
        return {
            "run": db.get_monitor_run(run_id) or run,
            "subscription": updated_subscription,
            "targets": results,
            "failed_targets": failures,
        }
    except Exception as exc:
        finished = utc_now()
        finished_at = format_utc(finished)
        db.update_monitor_run(
            run_id,
            status="failed",
            finished_at=finished_at,
            duration_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
            error=str(exc)[:1000],
        )
        try:
            next_run_at = next_retry_run(
                subscription,
                failures=int(subscription.get("consecutive_failures") or 0) + 1,
                after=finished,
            )
        except Exception:
            next_run_at = format_utc(finished + dt.timedelta(hours=1))
        db.finish_subscription_monitor(
            sub_id,
            worker_id,
            status="failed",
            next_run_at=next_run_at,
            checked_at=finished_at,
            success=False,
            error=str(exc),
        )
        raise
