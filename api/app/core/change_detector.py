"""页面快照的确定性差异检测与实质变化过滤。"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from app.core.config import get_settings


_BUSINESS_PATTERNS: Dict[str, re.Pattern] = {
    "pricing": re.compile(
        r"(?:价格|定价|收费|免费|订阅|套餐|折扣|涨价|降价|price|pricing|plan|subscription|"
        r"\$|¥|￥|usd|cny|rmb|/month|/year)", re.IGNORECASE
    ),
    "feature": re.compile(
        r"(?:功能|能力|支持|新增|发布|上线|移除|下线|集成|feature|launch|release|"
        r"introduc|integration|deprecated|discontinued)", re.IGNORECASE
    ),
    "product": re.compile(
        r"(?:产品|版本|型号|企业版|专业版|product|version|enterprise|professional|pro\b)",
        re.IGNORECASE,
    ),
    "jobs": re.compile(r"(?:招聘|岗位|职位|人才|hiring|career|job opening|vacanc)", re.IGNORECASE),
    "policy": re.compile(
        r"(?:条款|隐私|合规|政策|许可|terms|privacy|compliance|policy|license)", re.IGNORECASE
    ),
}
_VOLATILE_LINE = re.compile(
    r"^(?:updated|last updated|更新时间|更新于|截至)[:：]?\s*"
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChangeDetection:
    changed: bool
    material: bool
    change_ratio: float
    added_chars: int
    deleted_chars: int
    changed_lines: int
    keyword_hits: List[str] = field(default_factory=list)
    diff_summary: str = ""
    before_excerpt: str = ""
    after_excerpt: str = ""

    def to_dict(self):
        return asdict(self)


def _stable_lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").splitlines()
            if line.strip() and not _VOLATILE_LINE.match(line.strip())]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [diff truncated]"


def detect_change(
    before: str,
    after: str,
    *,
    min_change_ratio: Optional[float] = None,
    min_changed_chars: Optional[int] = None,
    max_diff_chars: Optional[int] = None,
) -> ChangeDetection:
    settings = get_settings()
    min_change_ratio = (
        settings.monitor_min_change_ratio if min_change_ratio is None else min_change_ratio
    )
    min_changed_chars = (
        settings.monitor_min_changed_chars if min_changed_chars is None else min_changed_chars
    )
    max_diff_chars = settings.monitor_diff_max_chars if max_diff_chars is None else max_diff_chars

    before_lines = _stable_lines(before)
    after_lines = _stable_lines(after)
    before_stable = "\n".join(before_lines)
    after_stable = "\n".join(after_lines)
    if before_stable == after_stable:
        return ChangeDetection(False, False, 0.0, 0, 0, 0)

    matcher = difflib.SequenceMatcher(None, before_stable, after_stable, autojunk=False)
    ratio = round(1.0 - matcher.ratio(), 6)
    added = deleted = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1

    unified_lines = list(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="before",
        tofile="after",
        lineterm="",
        n=2,
    ))
    changed_lines = sum(
        1 for line in unified_lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    changed_text = "\n".join(
        line[1:] for line in unified_lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    keyword_hits = sorted(
        category for category, pattern in _BUSINESS_PATTERNS.items() if pattern.search(changed_text)
    )
    changed_chars = added + deleted
    material = bool(keyword_hits) or (
        changed_chars >= max(1, min_changed_chars) and ratio >= max(0.0, min_change_ratio)
    )

    removed = [line[1:] for line in unified_lines
               if line.startswith("-") and not line.startswith("---")]
    inserted = [line[1:] for line in unified_lines
                if line.startswith("+") and not line.startswith("+++")]
    return ChangeDetection(
        changed=True,
        material=material,
        change_ratio=ratio,
        added_chars=added,
        deleted_chars=deleted,
        changed_lines=changed_lines,
        keyword_hits=keyword_hits,
        diff_summary=_clip("\n".join(unified_lines), max(500, max_diff_chars)),
        before_excerpt=_clip("\n".join(removed), 2000),
        after_excerpt=_clip("\n".join(inserted), 2000),
    )
