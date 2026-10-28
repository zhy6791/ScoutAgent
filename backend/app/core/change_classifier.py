"""使用 LLM 解释已经由确定性规则确认的实质页面变化。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from app.core.change_detector import ChangeDetection
from app.core.config import get_settings
from app.core.llm import chat_json


_CHANGE_TYPES = {"pricing", "feature", "product", "blog", "jobs", "policy", "unknown"}
_SEVERITIES = {"critical", "high", "medium", "low", "info"}


@dataclass(frozen=True)
class ChangeClassification:
    material: bool
    change_type: str
    severity: str
    summary: str
    facts: List[str] = field(default_factory=list)
    llm_status: str = "success"
    llm_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fallback(
    detection: ChangeDetection,
    status: str = "failed",
    llm_tokens: int = 0,
) -> ChangeClassification:
    return ChangeClassification(
        material=detection.material,
        change_type=detection.keyword_hits[0] if detection.keyword_hits else "unknown",
        severity="medium" if detection.material else "info",
        summary=(
            f"页面发生实质变化：新增 {detection.added_chars} 字符，"
            f"删除 {detection.deleted_chars} 字符。"
            if detection.material else "页面存在轻微变化，未达到 LLM 分类阈值。"
        ),
        facts=[],
        llm_status=status,
        llm_tokens=max(0, int(llm_tokens)),
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def classify_change(
    brand: str,
    target_type: str,
    source_url: str,
    detection: ChangeDetection,
    *,
    model: Optional[str] = None,
) -> ChangeClassification:
    if not detection.changed or not detection.material:
        return _fallback(detection, status="skipped")

    usage: Dict[str, int] = {}
    settings = get_settings()
    try:
        data = chat_json(
            [
                {"role": "system", "content": (
                    "你是竞品变化审查员。只解释给定的确定性网页 Diff，不补充外部事实。"
                    "判断变化是否具有竞争情报意义，并输出严格 JSON："
                    '{"material":true,"change_type":"pricing|feature|product|blog|jobs|policy|unknown",'
                    '"severity":"critical|high|medium|low|info","summary":"不超过120字",'
                    '"facts":["仅来自Diff的事实，最多5条"]}。'
                    "critical 仅用于停售、重大降价、核心产品撤回或重大合规变化；"
                    "high 用于定价模式、新产品或核心功能变化；一般更新不得判为 high。"
                )},
                {"role": "user", "content": (
                    f"品牌：{brand}\n页面类型：{target_type}\nURL：{source_url}\n"
                    f"变化比例：{detection.change_ratio:.4f}\n"
                    f"规则命中：{', '.join(detection.keyword_hits) or '无'}\n"
                    f"Diff：\n{detection.diff_summary}"
                )},
            ],
            temperature=0.1,
            max_tokens=1200,
            model=model or settings.zhipu_model_fast,
            purpose="竞品订阅监控：解释页面实质变化",
            usage_out=usage,
        )
    except Exception:
        data = None

    if not isinstance(data, dict):
        return _fallback(detection, llm_tokens=int(usage.get("total_tokens", 0)))
    change_type = str(data.get("change_type") or "unknown").lower().strip()
    severity = str(data.get("severity") or "info").lower().strip()
    summary = str(data.get("summary") or "").strip()[:500]
    facts = [str(item).strip()[:500] for item in (data.get("facts") or [])
             if str(item).strip()][:5]
    if change_type not in _CHANGE_TYPES or severity not in _SEVERITIES or not summary:
        return _fallback(detection, llm_tokens=int(usage.get("total_tokens", 0)))

    material = _coerce_bool(data.get("material"), detection.material)
    if not material:
        severity = "info"
    return ChangeClassification(
        material=material,
        change_type=change_type,
        severity=severity,
        summary=summary,
        facts=facts,
        llm_status="success",
        llm_tokens=int(usage.get("total_tokens", 0)),
    )
