"""把监控变化转换为带快照种子证据的完整调研任务。"""
from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlsplit

from app.core import db
from app.core.credibility import score_evidence
from app.core.orchestrator import create_task


_FOCUS_BY_CHANGE = {
    "pricing": ["定价策略", "商业模式", "市场份额"],
    "feature": ["功能对比", "技术架构", "用户口碑"],
    "product": ["功能对比", "增长趋势", "SWOT"],
    "blog": ["增长趋势", "舆情趋势", "SWOT"],
    "jobs": ["增长趋势", "技术架构", "生态壁垒"],
    "policy": ["SWOT", "风险判断", "市场份额"],
    "unknown": ["功能对比", "SWOT", "增长趋势"],
}


def create_research_from_change(change_id: str, *, mode: str = "deep") -> Dict[str, Any]:
    if mode not in {"quick", "deep", "expert"}:
        raise ValueError("unsupported research mode")
    change = db.get_competitor_change(change_id)
    if not change:
        raise LookupError("competitor change not found")
    target = db.get_subscription_target(change["target_id"])
    snapshot = db.get_page_snapshot(change["current_snapshot_id"])
    subscription = db.get_subscription(change["sub_id"])
    if not target or not snapshot or not subscription:
        raise LookupError("monitor source data is incomplete")

    brand = target.get("brand") or (subscription.get("brands") or [""])[0]
    summary = change.get("diff_summary") or "页面内容发生变化"
    query = (
        f"分析 {brand} 最新「{change.get('change_type', 'unknown')}」变化的竞争影响："
        f"{summary}。核实变化事实，并与主要竞品进行对比。"
    )
    response = create_task(query, mode=mode)
    task_id = response["taskId"]
    focus = _FOCUS_BY_CHANGE.get(change.get("change_type"), _FOCUS_BY_CHANGE["unknown"])
    prefill = {
        "competitors": [brand] if brand else [],
        "focus": focus,
        "freshness": "优先最新（近一月）",
        "extra": (
            f"监控变化 ID：{change_id}；来源页面：{snapshot.get('final_url') or snapshot.get('source_url')}"
        ),
    }
    task = db.get_task(task_id) or {}
    context = {
        **(task.get("clarifications") or {}),
        **prefill,
        "_mode": mode,
        "_monitor_context": {
            "change_id": change_id,
            "snapshot_id": snapshot["snapshot_id"],
            "subscription_id": change["sub_id"],
            "brand": brand,
            "change_type": change.get("change_type", "unknown"),
            "severity": change.get("severity", "info"),
            "source_url": snapshot.get("final_url") or snapshot.get("source_url"),
        },
    }
    db.update_task_context(task_id, context)
    db.set_task_monitor_source(task_id, change_id, snapshot["snapshot_id"])

    source_url = snapshot.get("final_url") or snapshot.get("source_url") or target["url"]
    source_type = (
        "official" if target.get("target_type") in {"official", "pricing"}
        else "news" if target.get("target_type") == "blog"
        else "web"
    )
    excerpt = "\n".join(part for part in (
        summary,
        snapshot.get("content_excerpt", ""),
    ) if part).strip()[:1000]
    evidence_id = "ev_mon_" + snapshot["snapshot_id"].replace("snap_", "", 1)
    seed = {
        "evidence_id": evidence_id,
        "source_url": source_url,
        "source_type": source_type,
        "domain": urlsplit(source_url).netloc.lower().replace("www.", ""),
        "title": f"{brand} {target.get('target_type', 'custom')} 页面监控变化",
        "excerpt": excerpt,
        "credibility": score_evidence(
            source_url,
            source_type,
            captured_at=snapshot.get("captured_at", ""),
            ok_fetch=True,
            excerpt=excerpt,
        ),
        "collected_by": "monitor-agent",
        "brand": brand,
        "captured_at": snapshot.get("captured_at", ""),
        "snapshot_path": snapshot.get("snapshot_path", ""),
    }
    db.save_task_seed_evidence(
        task_id,
        evidence_id,
        snapshot["snapshot_id"],
        change_id,
        seed,
    )
    return {
        **response,
        "query": query,
        "prefill": prefill,
        "monitorContext": context["_monitor_context"],
        "evidenceId": evidence_id,
    }

