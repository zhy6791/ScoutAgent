"""监控正文快照的压缩存储和数据库登记。"""
from __future__ import annotations

import gzip
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.core import db
from app.core.config import get_settings
from app.core.monitor_fetcher import MonitorFetchResult


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _root_path(root: Optional[Path] = None) -> Path:
    return (root or get_settings().monitor_snapshot_path).expanduser().resolve()


def _component(value: str, label: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(value or ""):
        raise ValueError(f"invalid {label}")
    return value


def _resolve_relative(relative_path: str, root: Optional[Path] = None) -> Path:
    base = _root_path(root)
    candidate = (base / relative_path).resolve()
    if os.path.commonpath((str(base), str(candidate))) != str(base):
        raise ValueError("snapshot path escapes the configured root")
    return candidate


def write_snapshot(
    snapshot_id: str,
    target_id: str,
    text: str,
    *,
    root: Optional[Path] = None,
) -> str:
    snapshot_id = _component(snapshot_id, "snapshot_id")
    target_id = _component(target_id, "target_id")
    base = _root_path(root)
    target_dir = base / target_id
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"{snapshot_id}.txt.gz"
    if destination.exists():
        raise FileExistsError(f"snapshot already exists: {snapshot_id}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{snapshot_id}.", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                compressed.write((text or "").encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return destination.relative_to(base).as_posix()


def read_snapshot(relative_path: str, *, root: Optional[Path] = None) -> str:
    path = _resolve_relative(relative_path, root)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def delete_snapshot_file(relative_path: str, *, root: Optional[Path] = None) -> None:
    path = _resolve_relative(relative_path, root)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def save_fetch_snapshot(
    snapshot_id: str,
    run_id: str,
    target_id: str,
    result: MonitorFetchResult,
    *,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """保存一次有效抓取；首次保存只建立基线，不产生变化事件。"""
    if not result.ok or result.not_modified or not result.text or not result.content_hash:
        raise ValueError("only a successful modified response can be saved as a snapshot")
    target = db.get_subscription_target(target_id)
    if not target:
        raise ValueError("subscription target does not exist")
    previous_snapshot_id = target.get("last_snapshot_id", "")
    relative_path = write_snapshot(snapshot_id, target_id, result.text, root=root)
    try:
        snapshot = db.create_page_snapshot(
            snapshot_id,
            run_id,
            target_id,
            result.source_url,
            previous_snapshot_id=previous_snapshot_id,
            final_url=result.final_url,
            http_status=result.http_status,
            content_hash=result.content_hash,
            content_length=result.content_length,
            content_excerpt=result.text[:1000],
            snapshot_path=relative_path,
            etag=result.etag,
            last_modified=result.last_modified,
            captured_at=result.captured_at,
        )
    except Exception:
        delete_snapshot_file(relative_path, root=root)
        raise
    return {"snapshot": snapshot, "is_baseline": not bool(previous_snapshot_id)}


def prune_target_snapshots(
    target_id: str,
    *,
    keep: Optional[int] = None,
    root: Optional[Path] = None,
) -> int:
    """清理超过保留数量且没有被变化事件或调研任务引用的快照。"""
    retention = max(1, int(keep or get_settings().monitor_snapshot_retention))
    snapshots = db.list_page_snapshots(target_id, limit=500)
    removed = 0
    for snapshot in snapshots[retention:]:
        deleted = db.delete_page_snapshot_if_unreferenced(snapshot["snapshot_id"])
        if not deleted:
            continue
        if deleted.get("snapshot_path"):
            delete_snapshot_file(deleted["snapshot_path"], root=root)
        removed += 1
    return removed


def snapshot_disk_usage(*, root: Optional[Path] = None) -> int:
    base = _root_path(root)
    if not base.exists():
        return 0
    total = 0
    for path in base.rglob("*.txt.gz"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total
