"""核心数据模型（第 3 章）：Evidence / Claim / Envelope / 报告结构。

Python 3.9 兼容：使用 typing.Optional / List，避免 `X | None` 运行期解析问题。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    evidence_id: str
    source_url: str
    source_type: str  # official|news|douyin|xiaohongshu|bilibili|weibo|zhihu|review|financial_report
    title: str
    excerpt: str
    captured_at: str
    credibility: float  # 0-100 整数（由 credibility.score_evidence 计算，精确到个位、有差异）
    collected_by: str
    screenshot_path: str = ""
    image_urls: List[str] = field(default_factory=list)
    lang: str = "zh"
    brand: str = ""
    domain: str = ""
    freshness_days: Optional[int] = None  # 距今天数，None=无法解析
    snapshot_id: str = ""
    change_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Claim:
    claim_id: str
    text: str
    field: str  # feature_tree|pricing_model|user_persona|swot|sentiment|overview
    evidence_ids: List[str]
    confidence: str  # high|medium|low|unverified
    cross_validated: bool
    author: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Issue:
    issue_id: str
    target: str  # claim_id / section
    severity: str  # high|medium|low
    reason: str
    raised_by: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Envelope:
    msg_id: str
    sender: str
    receiver: str
    task_type: str  # PRODUCE | REWORK | PASS
    payload: Dict[str, Any] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    trace_ref: str = ""


@dataclass
class Subscription:
    sub_id: str
    query: str
    brands: List[str] = field(default_factory=list)
    enabled: bool = False
    schedule_type: str = "interval"
    schedule_config: Dict[str, Any] = field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    next_run_at: str = ""
    last_run_at: str = ""
    last_report_id: str = ""
    run_count: int = 0
    last_checked_at: str = ""
    last_success_at: str = ""
    last_status: str = "needs_configuration"
    consecutive_failures: int = 0
    monitor_error: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubscriptionTarget:
    target_id: str
    sub_id: str
    brand: str
    target_type: str
    url: str
    enabled: bool = True
    last_snapshot_id: str = ""
    last_hash: str = ""
    last_checked_at: str = ""
    failure_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MonitorRun:
    run_id: str
    sub_id: str
    trigger_type: str
    scheduled_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = "pending"
    target_count: int = 0
    success_count: int = 0
    changed_count: int = 0
    search_calls: int = 0
    llm_tokens: int = 0
    duration_ms: int = 0
    not_modified_count: int = 0
    minor_change_count: int = 0
    llm_failure_count: int = 0
    error: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PageSnapshot:
    snapshot_id: str
    run_id: str
    target_id: str
    source_url: str
    previous_snapshot_id: str = ""
    final_url: str = ""
    http_status: int = 0
    content_hash: str = ""
    content_length: int = 0
    content_excerpt: str = ""
    snapshot_path: str = ""
    etag: str = ""
    last_modified: str = ""
    captured_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CompetitorChange:
    change_id: str
    sub_id: str
    run_id: str
    target_id: str
    previous_snapshot_id: str
    current_snapshot_id: str
    change_type: str = "unknown"
    severity: str = "info"
    material: bool = False
    change_ratio: float = 0.0
    diff_summary: str = ""
    before_excerpt: str = ""
    after_excerpt: str = ""
    llm_status: str = "skipped"
    status: str = "new"
    research_task_id: str = ""
    research_report_id: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_claim(
    claim_id: str,
    text: str,
    field_name: str,
    evidence_ids: List[str],
    author: str,
    independent_domains: int = 0,
) -> Claim:
    """按四铁律计算置信度：无证据→unverified；≥2 独立来源→high。"""
    if not evidence_ids:
        return Claim(claim_id, text, field_name, [], "unverified", False, author)
    cross = independent_domains >= 2
    if cross:
        conf = "high"
    elif len(evidence_ids) >= 2:
        conf = "medium"
    else:
        conf = "low"
    return Claim(claim_id, text, field_name, evidence_ids, conf, cross, author)
