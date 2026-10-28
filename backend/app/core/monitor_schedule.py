"""订阅监控计划的 UTC 计算。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> dt.datetime:
    if not value:
        raise ValueError("UTC timestamp is required")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _schedule_config(subscription: Dict[str, Any]) -> Dict[str, Any]:
    value = subscription.get("schedule_config")
    return value if isinstance(value, dict) else {}


def next_scheduled_run(
    subscription: Dict[str, Any],
    *,
    after: Optional[dt.datetime] = None,
) -> str:
    after = (after or utc_now()).astimezone(dt.timezone.utc)
    schedule_type = subscription.get("schedule_type") or "interval"
    config = _schedule_config(subscription)
    if schedule_type == "interval":
        minutes = int(config.get("interval_minutes") or 1440)
        if minutes < 1:
            raise ValueError("interval_minutes must be at least 1")
        return format_utc(after + dt.timedelta(minutes=minutes))
    if schedule_type != "daily":
        raise ValueError(f"unsupported schedule_type: {schedule_type}")

    timezone_name = subscription.get("timezone") or "Asia/Shanghai"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    raw_time = str(config.get("time") or config.get("daily_time") or "09:00")
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise ValueError("daily time must use HH:MM") from exc

    local_after = after.astimezone(zone)
    candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_after:
        candidate += dt.timedelta(days=1)
    return format_utc(candidate)


def next_retry_run(
    subscription: Dict[str, Any],
    *,
    failures: int,
    after: Optional[dt.datetime] = None,
) -> str:
    after = (after or utc_now()).astimezone(dt.timezone.utc)
    regular = parse_utc(next_scheduled_run(subscription, after=after))
    retry_minutes = min(360, 5 * (2 ** max(0, min(int(failures), 6))))
    retry = after + dt.timedelta(minutes=retry_minutes)
    return format_utc(min(regular, retry))
