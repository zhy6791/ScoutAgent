"""竞品订阅、监控目标、运行和变化事件 API。"""
from __future__ import annotations

import datetime as dt
import asyncio
import os
import socket
import uuid
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.core import db
from app.core.config import get_settings
from app.core.monitor_runner import run_claimed_subscription
from app.core.monitor_schedule import format_utc, next_scheduled_run, parse_utc, utc_now
from app.core.search import search


router = APIRouter(prefix="/api", tags=["subscriptions"])
_MANUAL_TASKS: set[asyncio.Task[Any]] = set()


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interval", "daily"] = "interval"
    interval_minutes: Optional[int] = Field(default=1440, ge=1, le=525_600)
    time: Optional[str] = "09:00"


class TargetCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str = Field(min_length=1, max_length=120)
    target_type: Literal["official", "pricing", "blog", "jobs", "custom"] = "custom"
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True


class TargetPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_type: Optional[Literal["official", "pricing", "blog", "jobs", "custom"]] = None
    url: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    enabled: Optional[bool] = None


class SubscriptionCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    brands: List[str] = Field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    schedule: ScheduleBody = Field(default_factory=ScheduleBody)
    targets: List[TargetCreateBody] = Field(default_factory=list)
    enabled: bool = False


class SubscriptionPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    brands: Optional[List[str]] = None
    timezone: Optional[str] = None
    schedule: Optional[ScheduleBody] = None


class ChangeStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["new", "read", "ignored"]


class DiscoverTargetsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=1000)
    brands: List[str] = Field(default_factory=list)
    limit_per_brand: int = Field(default=5, ge=1, le=10)


class ResearchFromChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["quick", "deep", "expert"] = "deep"


def _subscription_or_404(sub_id: str) -> Dict[str, Any]:
    subscription = db.get_subscription(sub_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="subscription not found")
    return subscription


def _target_or_404(sub_id: str, target_id: str) -> Dict[str, Any]:
    target = db.get_subscription_target(target_id)
    if not target or target.get("sub_id") != sub_id:
        raise HTTPException(status_code=404, detail="subscription target not found")
    return target


def _clean_brands(values: List[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values or []:
        brand = str(value).strip()
        key = brand.casefold()
        if brand and key not in seen:
            seen.add(key)
            output.append(brand)
    return output[:50]


def _validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"unknown timezone: {name}") from exc
    return name


def _schedule_values(schedule: ScheduleBody, timezone_name: str) -> Dict[str, Any]:
    if schedule.type == "interval":
        config = {"interval_minutes": int(schedule.interval_minutes or 1440)}
    else:
        daily_time = str(schedule.time or "09:00")
        config = {"time": daily_time}
    candidate = {
        "schedule_type": schedule.type,
        "schedule_config": config,
        "timezone": timezone_name,
    }
    try:
        next_scheduled_run(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return candidate


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise HTTPException(status_code=422, detail="target URL must use http or https")


def _enrich_subscription(subscription: Dict[str, Any]) -> Dict[str, Any]:
    sub_id = subscription["sub_id"]
    targets = db.list_subscription_targets(sub_id)
    runs = db.list_monitor_runs(sub_id, limit=1)
    changes = db.list_competitor_changes(sub_id, limit=500)
    return {
        **subscription,
        "targets": targets,
        "target_count": len(targets),
        "enabled_target_count": sum(1 for target in targets if target.get("enabled")),
        "latest_run": runs[0] if runs else None,
        "unread_change_count": sum(1 for item in changes if item.get("status") == "new"),
    }


def _guess_target_type(url: str, title: str) -> str:
    text = f"{url} {title}".lower()
    if any(token in text for token in ("pricing", "price", "plans", "定价", "价格")):
        return "pricing"
    if any(token in text for token in ("blog", "news", "release", "更新", "博客")):
        return "blog"
    if any(token in text for token in ("jobs", "careers", "招聘")):
        return "jobs"
    return "official"


@router.get("/subscriptions")
def list_subscriptions():
    return [_enrich_subscription(item) for item in db.list_subscriptions()]


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
def create_subscription(body: SubscriptionCreateBody):
    timezone_name = _validate_timezone(body.timezone)
    schedule = _schedule_values(body.schedule, timezone_name)
    brands = _clean_brands(body.brands)
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    subscription = db.create_subscription(
        sub_id,
        body.query,
        brands,
        schedule_type=schedule["schedule_type"],
        schedule_config=schedule["schedule_config"],
        timezone=timezone_name,
    )
    try:
        for item in body.targets:
            _validate_url(item.url)
            db.create_subscription_target(
                f"target_{uuid.uuid4().hex[:12]}",
                sub_id,
                item.brand,
                item.target_type,
                item.url,
                enabled=item.enabled,
            )
        if body.enabled and brands and any(item.enabled for item in body.targets):
            next_run_at = next_scheduled_run(db.get_subscription(sub_id) or subscription)
            db.update_subscription(sub_id, next_run_at=next_run_at)
            db.set_subscription_enabled(sub_id, True)
    except Exception:
        db.delete_subscription(sub_id)
        raise
    return _enrich_subscription(_subscription_or_404(sub_id))


@router.get("/subscriptions/{sub_id}")
def get_subscription(sub_id: str):
    return _enrich_subscription(_subscription_or_404(sub_id))


@router.patch("/subscriptions/{sub_id}")
def patch_subscription(sub_id: str, body: SubscriptionPatchBody):
    current = _subscription_or_404(sub_id)
    fields = body.model_fields_set
    changes: Dict[str, Any] = {}
    if "query" in fields:
        changes["query"] = body.query
    if "brands" in fields:
        changes["brands"] = _clean_brands(body.brands or [])
    timezone_name = (
        _validate_timezone(body.timezone)
        if "timezone" in fields and body.timezone is not None
        else current.get("timezone") or "Asia/Shanghai"
    )
    if "timezone" in fields:
        changes["timezone"] = timezone_name
    if "schedule" in fields and body.schedule is not None:
        changes.update(_schedule_values(body.schedule, timezone_name))
    updated = db.update_subscription(sub_id, **changes) or current
    if updated.get("enabled"):
        updated = db.update_subscription(
            sub_id,
            next_run_at=next_scheduled_run(updated, after=utc_now()),
        ) or updated
    return _enrich_subscription(updated)


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    subscription = _subscription_or_404(sub_id)
    lease_owner = subscription.get("lease_owner", "")
    lease_expires_at = subscription.get("lease_expires_at", "")
    if lease_owner and lease_expires_at:
        try:
            lease_active = parse_utc(lease_expires_at) > utc_now()
        except ValueError:
            lease_active = True
        if lease_active:
            raise HTTPException(status_code=409, detail="subscription is currently running")
    db.set_subscription_enabled(sub_id, False)
    db.delete_subscription(sub_id)
    return {"ok": True}


@router.post("/subscriptions/{sub_id}/pause")
def pause_subscription(sub_id: str):
    _subscription_or_404(sub_id)
    return _enrich_subscription(db.set_subscription_enabled(sub_id, False) or {})


@router.post("/subscriptions/{sub_id}/resume")
def resume_subscription(sub_id: str):
    subscription = _subscription_or_404(sub_id)
    try:
        next_run_at = next_scheduled_run(subscription, after=utc_now())
        db.update_subscription(sub_id, next_run_at=next_run_at)
        resumed = db.set_subscription_enabled(sub_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _enrich_subscription(resumed or subscription)


@router.post(
    "/subscriptions/{sub_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_subscription(sub_id: str):
    current = _subscription_or_404(sub_id)
    if not current.get("enabled"):
        raise HTTPException(status_code=409, detail="subscription must be resumed before it can run")
    settings = get_settings()
    now = utc_now()
    worker_id = f"manual:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    claimed = db.claim_subscription(
        sub_id,
        worker_id,
        now=format_utc(now),
        lease_expires_at=format_utc(
            now + dt.timedelta(seconds=max(1, settings.monitor_lease_seconds))
        ),
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="subscription is already running")
    run_id = f"mon_{uuid.uuid4().hex[:12]}"
    run = db.create_monitor_run(
        run_id,
        sub_id,
        "manual",
        scheduled_at=format_utc(now),
    )
    task = asyncio.create_task(
        run_claimed_subscription(
            claimed,
            worker_id,
            trigger_type="manual",
            run_id=run_id,
        ),
        name=f"manual-monitor:{sub_id}",
    )
    _MANUAL_TASKS.add(task)
    task.add_done_callback(_MANUAL_TASKS.discard)
    return {"ok": True, "run_id": run_id, "status": run["status"]}


@router.get("/subscriptions/{sub_id}/targets")
def list_targets(sub_id: str):
    _subscription_or_404(sub_id)
    return db.list_subscription_targets(sub_id)


@router.post(
    "/subscriptions/{sub_id}/targets",
    status_code=status.HTTP_201_CREATED,
)
def create_target(sub_id: str, body: TargetCreateBody):
    subscription = _subscription_or_404(sub_id)
    _validate_url(body.url)
    brands = _clean_brands([*subscription.get("brands", []), body.brand])
    if brands != subscription.get("brands", []):
        db.update_subscription(sub_id, brands=brands)
    try:
        return db.create_subscription_target(
            f"target_{uuid.uuid4().hex[:12]}",
            sub_id,
            body.brand,
            body.target_type,
            body.url,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/subscriptions/{sub_id}/targets/{target_id}")
def patch_target(sub_id: str, target_id: str, body: TargetPatchBody):
    current = _target_or_404(sub_id, target_id)
    changes = body.model_dump(exclude_unset=True)
    if "url" in changes:
        _validate_url(changes["url"])
    if "brand" in changes:
        subscription = _subscription_or_404(sub_id)
        brands = _clean_brands([*subscription.get("brands", []), changes["brand"]])
        db.update_subscription(sub_id, brands=brands)
    try:
        return db.update_subscription_target(target_id, **changes) or current
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/subscriptions/{sub_id}/targets/{target_id}")
def delete_target(sub_id: str, target_id: str):
    _target_or_404(sub_id, target_id)
    db.delete_subscription_target(target_id)
    return {"ok": True}


@router.get("/subscriptions/{sub_id}/runs")
def list_runs(
    sub_id: str,
    limit: int = Query(default=100, ge=1, le=500),
):
    _subscription_or_404(sub_id)
    return db.list_monitor_runs(sub_id, limit=limit)


@router.get("/subscriptions/{sub_id}/changes")
def list_changes(
    sub_id: str,
    change_status: str = Query(default="", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
):
    _subscription_or_404(sub_id)
    if change_status and change_status not in {"new", "read", "ignored"}:
        raise HTTPException(status_code=422, detail="unsupported change status")
    targets = {
        item["target_id"]: item for item in db.list_subscription_targets(sub_id)
    }
    output = []
    for item in db.list_competitor_changes(
        sub_id,
        status=change_status,
        limit=limit,
    ):
        target = targets.get(item["target_id"], {})
        output.append({
            **item,
            "brand": target.get("brand", ""),
            "target_type": target.get("target_type", ""),
            "source_url": target.get("url", ""),
        })
    return output


@router.get("/monitor-runs/{run_id}")
def get_monitor_run(run_id: str):
    run = db.get_monitor_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="monitor run not found")
    return run


@router.get("/monitoring/metrics")
def get_monitoring_metrics():
    from app.core.snapshot_store import snapshot_disk_usage

    return {**db.monitoring_metrics(), "snapshot_disk_bytes": snapshot_disk_usage()}


@router.patch("/competitor-changes/{change_id}")
def patch_change(change_id: str, body: ChangeStatusBody):
    if not db.get_competitor_change(change_id):
        raise HTTPException(status_code=404, detail="competitor change not found")
    return db.update_competitor_change_status(change_id, body.status)


@router.post(
    "/competitor-changes/{change_id}/research",
    status_code=status.HTTP_201_CREATED,
)
def research_from_change(change_id: str, body: ResearchFromChangeBody):
    from app.core.monitor_research import create_research_from_change

    try:
        return create_research_from_change(change_id, mode=body.mode)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/subscriptions/targets/discover")
def discover_targets(body: DiscoverTargetsBody):
    brands = _clean_brands(body.brands)
    if not brands and not body.query.strip():
        raise HTTPException(status_code=422, detail="query or brands is required")
    search_brands = brands or [body.query.strip()]
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for brand in search_brands:
        query = f"{brand} {body.query} 官网 定价 产品更新".strip()
        try:
            results = search(query, num=body.limit_per_brand)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"target discovery failed: {exc}") from exc
        for result in results:
            url = str(result.get("url") or result.get("link") or "").strip()
            title = str(result.get("title") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            candidates.append({
                "brand": brand,
                "target_type": _guess_target_type(url, title),
                "url": url,
                "title": title,
                "snippet": str(result.get("snippet") or result.get("summary") or "")[:500],
            })
    return {"items": candidates}

