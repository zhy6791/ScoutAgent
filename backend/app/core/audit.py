"""质检与反馈闭环（对应需求 12：真实可触发的返工闭环）。

evaluate_quality：基于 claims/evidences/structured 计算可量化质量指标 + 暴露问题（Issue）。
decide_rework：根据规则问题打回 collect（补采）或 analyze（重分析）。
build_rework_envelopes：将 LLM 反馈分类为 collect/analyze/write，并生成对应 Envelope。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.core.fetcher import domain_of
from app.core.models import Envelope


@dataclass
class QualityReport:
    coverage_by_dimension: Dict[str, bool] = field(default_factory=dict)
    coverage_by_brand: Dict[str, Dict[str, int]] = field(default_factory=dict)
    confidence_ratio: float = 0.0
    schema_completeness: float = 0.0
    dimension_coverage_rate: float = 0.0
    brand_coverage_rate: float = 0.0
    issues: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage_by_dimension": self.coverage_by_dimension,
            "coverage_by_brand": self.coverage_by_brand,
            "confidence_ratio": self.confidence_ratio,
            "schema_completeness": self.schema_completeness,
            "dimension_coverage_rate": self.dimension_coverage_rate,
            "brand_coverage_rate": self.brand_coverage_rate,
            "issues": self.issues,
        }

    def summary(self) -> Dict[str, Any]:
        """供前端返工卡片展示的精简指标。"""
        return {
            "confidence_ratio": round(self.confidence_ratio * 100),
            "dimension_coverage": round(self.dimension_coverage_rate * 100),
            "brand_coverage": round(self.brand_coverage_rate * 100),
            "schema_completeness": round(self.schema_completeness * 100),
        }


# field -> focus 维度关键词的粗映射（用于判断某维度是否被覆盖）
_FIELD_KEYWORDS = {
    "pricing_model": ("定价", "价格", "收费", "套餐"),
    "feature_tree": ("功能", "特性", "能力"),
    "user_persona": ("用户", "画像", "人群", "场景"),
    "trend": ("趋势", "发展", "增长"),
    "swot": ("swot", "优势", "劣势"),
}


def evaluate_quality(
    brands: List[str],
    focus: List[str],
    claims: List[Dict[str, Any]],
    evidences: List[Any],
    structured: Dict[str, Any],
    *,
    min_indep_domains: int = 2,
) -> QualityReport:
    qr = QualityReport()

    # 1. 置信度比
    total = len(claims) or 1
    high = sum(1 for c in claims if c.get("confidence") == "high")
    qr.confidence_ratio = round(high / total, 3)

    # 2. 维度覆盖：每个 focus 维度是否有 ≥1 条 medium/high claim
    fields_present = {c.get("field") for c in claims
                      if c.get("confidence") in ("high", "medium")}
    for dim in focus:
        covered = False
        low = dim.lower()
        for f, kws in _FIELD_KEYWORDS.items():
            if f in fields_present and any(k in low for k in kws):
                covered = True
                break
        # 兜底：只要有任意有效 claim 即视为该维度有所触及
        if not covered and fields_present:
            covered = True
        qr.coverage_by_dimension[dim] = covered
    covered_dims = sum(1 for v in qr.coverage_by_dimension.values() if v)
    qr.dimension_coverage_rate = round(covered_dims / (len(focus) or 1), 3)

    # 3. 品牌覆盖：每品牌的证据数与独立域名数
    brand_ok = 0
    for b in brands:
        evs = [e for e in evidences if getattr(e, "brand", "") == b]
        domains = {domain_of(getattr(e, "source_url", "")) for e in evs}
        domains.discard("")
        qr.coverage_by_brand[b] = {"evidence": len(evs), "domains": len(domains)}
        if len(domains) >= min_indep_domains:
            brand_ok += 1
        else:
            qr.issues.append({
                "issue_id": "is_" + uuid.uuid4().hex[:8],
                "target": f"brand:{b}",
                "severity": "high" if len(evs) == 0 else "medium",
                "reason": f"「{b}」独立信源仅 {len(domains)} 个（<{min_indep_domains}），证据不足，建议补充采集。",
                "raised_by": "L3-003",
            })
    qr.brand_coverage_rate = round(brand_ok / (len(brands) or 1), 3)

    # 4. 维度缺失 issue
    for dim, ok in qr.coverage_by_dimension.items():
        if not ok:
            qr.issues.append({
                "issue_id": "is_" + uuid.uuid4().hex[:8],
                "target": f"dimension:{dim}",
                "severity": "medium",
                "reason": f"维度「{dim}」缺少有效论点支撑，建议重新分析。",
                "raised_by": "L3-003",
            })

    # 5. Schema 完整度
    from app.core.schemas import schema_completeness
    qr.schema_completeness = schema_completeness(structured)
    if qr.schema_completeness < 0.34:
        qr.issues.append({
            "issue_id": "is_" + uuid.uuid4().hex[:8],
            "target": "schema",
            "severity": "low",
            "reason": "结构化知识（功能树/定价/画像）填充不足，建议重新分析补全。",
            "raised_by": "L3-003",
        })

    return qr


def llm_quality_review(
    query: str,
    brands: List[str],
    focus: List[str],
    claims: List[Dict[str, Any]],
    structured: Dict[str, Any],
    qr: "QualityReport",
    model: str = None,
) -> Dict[str, Any]:
    """质检官用 LLM 对当前分析做真实『审阅』（非纯规则）：逐维度打分 + 指出问题 + 给改进建议。

    产出结构化评审意见，让「质检审裁」阶段有真实的对比、审阅与可执行的调优建议
    （对应评分维度：反馈闭环真实可触发、重做后有改善）。失败时退回基于规则指标的兜底意见。
    """
    from app.core.llm import chat_json

    claim_lines = "\n".join(
        f"- [{c.get('confidence','?')}|{c.get('field','')}] {c.get('text','')}"
        for c in claims[:18]
    ) or "（暂无论点）"
    sc_dims = "、".join(f"{k}:{'已覆盖' if v else '缺失'}"
                       for k, v in qr.coverage_by_dimension.items()) or "无"
    brand_cov = "、".join(f"{b}({v.get('domains',0)}域/{v.get('evidence',0)}证据)"
                         for b, v in qr.coverage_by_brand.items()) or "无"
    fallback = {
        "verdict": "pass" if not qr.issues else "rework",
        "scores": {
            "证据充分性": round(qr.brand_coverage_rate * 100),
            "维度完整性": round(qr.dimension_coverage_rate * 100),
            "结论置信度": round(qr.confidence_ratio * 100),
            "结构化完整度": round(qr.schema_completeness * 100),
        },
        "review": f"基于规则指标：维度覆盖 {round(qr.dimension_coverage_rate*100)}%、"
                  f"品牌覆盖 {round(qr.brand_coverage_rate*100)}%、"
                  f"高置信占比 {round(qr.confidence_ratio*100)}%。",
        "issues": [i.get("reason", "") for i in qr.issues[:6]],
        "suggestions": [],
    }
    fallback["issue_routes"] = [
        {"category": classify_review_issue(issue.get("reason", "")),
         "issue": issue.get("reason", "")}
        for issue in qr.issues[:6]
        if issue.get("reason")
    ]
    try:
        data = chat_json(
            [
                {"role": "system", "content": (
                    "你是竞品分析报告的质检官（L3 决策层）。请对下面这份『分析中间产物』做严格的质量审阅，"
                    "像券商内核/主编终审一样，逐维度打分（0-100 整数，要有真实差异、不要清一色整十），"
                    "指出具体问题，并给出可执行的改进建议。最后给整体结论 pass（达标）或 rework（需返工）。"
                    '只输出 JSON：{"verdict":"pass|rework",'
                    '"scores":{"证据充分性":int,"维度完整性":int,"结论置信度":int,"结构化完整度":int,"交叉验证":int},'
                    '"review":"一段总体评审意见（点明亮点与短板）",'
                    '"issues":["具体问题1","具体问题2"],'
                    '"issue_routes":[{"category":"collect|analyze|write","issue":"问题原文","suggestion":"对应改进动作"}],'
                    '"suggestions":["可执行改进建议1","改进建议2"]}。只输出 JSON。'
                    "issue_routes 必须逐条为 issues 分类：证据不足/缺少来源/交叉验证/数据复现归 collect；"
                    "论点逻辑/维度遗漏/Schema/置信度/结论结构归 analyze；表达、段落、标题、可读性、重复或写作风格归 write。"
                )},
                {"role": "user", "content": (
                    f"调研主题：{query}\n竞品：{'、'.join(brands)}\n重点维度：{'、'.join(focus)}\n"
                    f"规则侧指标 → 维度覆盖：{sc_dims}；品牌证据覆盖：{brand_cov}；"
                    f"高置信占比：{round(qr.confidence_ratio*100)}%；结构化完整度：{round(qr.schema_completeness*100)}%\n"
                    f"已提炼论点：\n{claim_lines}"
                )},
            ],
            max_tokens=2000, temperature=0.3, model=model,
            purpose="质检官审阅：逐维度打分+问题+改进建议",
        )
        if isinstance(data, dict) and data.get("scores"):
            scores = {str(k): _clamp_score(v) for k, v in (data.get("scores") or {}).items()}
            return {
                "verdict": "rework" if str(data.get("verdict")) == "rework" else "pass",
                "scores": scores or fallback["scores"],
                "review": str(data.get("review") or fallback["review"]),
                "issues": [str(x) for x in (data.get("issues") or []) if str(x).strip()][:8],
                "suggestions": [str(x) for x in (data.get("suggestions") or []) if str(x).strip()][:8],
                "issue_routes": normalize_issue_routes(data.get("issue_routes"), data.get("issues") or []),
            }
    except Exception:
        pass
    return fallback


def _clamp_score(v) -> int:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def quality_gate_passed(qr: QualityReport, review: Dict[str, Any]) -> bool:
    """Return true only when rule checks and LLM review both pass."""
    return (
        not qr.issues
        and str((review or {}).get("verdict", "")).lower() == "pass"
        and not (review or {}).get("issues")
    )


_REVIEW_ROUTE_KEYWORDS = {
    "collect": ("证据", "来源", "信源", "引用", "单源", "交叉验证", "复现", "benchmark",
                 "舆情数据", "声量", "情感数据", "市场份额数据", "样本", "口径未明"),
    "write": ("表达", "表述", "文案", "写作", "段落", "标题", "可读性", "重复", "空泛",
              "叙事", "语言", "格式", "报告结构", "语气"),
    "analyze": ("论点", "逻辑", "维度", "schema", "结构化", "覆盖", "置信度", "结论",
                "映射", "矩阵", "策略", "定性", "定量"),
}


def classify_review_issue(issue: str) -> str:
    """Classify an audit issue into the executable rework owner.

    The audit model supplies the primary category. This keyword fallback keeps old
    model responses routable and deliberately defaults ambiguous feedback to analyze.
    """
    text = str(issue or "").lower()
    for category in ("collect", "write", "analyze"):
        if any(keyword.lower() in text for keyword in _REVIEW_ROUTE_KEYWORDS[category]):
            return category
    return "analyze"


def normalize_issue_routes(raw_routes: Any, issues: Any) -> List[Dict[str, str]]:
    """Normalize model-provided routes while preserving compatibility with string issues."""
    normalized: List[Dict[str, str]] = []
    valid = {"collect", "analyze", "write"}
    if isinstance(raw_routes, list):
        for item in raw_routes:
            if not isinstance(item, dict):
                continue
            issue = str(item.get("issue") or item.get("text") or "").strip()
            if not issue:
                continue
            category = str(item.get("category") or "").lower().strip()
            if category not in valid:
                category = classify_review_issue(issue)
            normalized.append({
                "category": category,
                "issue": issue,
                "suggestion": str(item.get("suggestion") or "").strip(),
            })
    if normalized:
        routed_text = {item["issue"] for item in normalized}
        for issue in issues if isinstance(issues, list) else []:
            text = str(issue).strip()
            if text and text not in routed_text:
                normalized.append({"category": classify_review_issue(text), "issue": text, "suggestion": ""})
        return normalized[:12]
    for issue in issues if isinstance(issues, list) else []:
        text = str(issue).strip()
        if text:
            normalized.append({"category": classify_review_issue(text), "issue": text, "suggestion": ""})
    return normalized[:12]


def decide_rework(qr: QualityReport) -> List[Envelope]:
    """根据质量报告决定返工动作，产出结构化 Envelope 消息。"""
    envelopes: List[Envelope] = []

    # 证据不足 → 打回 collect 补采
    collect_targets = [iss for iss in qr.issues if iss["target"].startswith("brand:")]
    if collect_targets:
        brands_to_recollect = [iss["target"].split(":", 1)[1] for iss in collect_targets]
        envelopes.append(Envelope(
            msg_id="env_" + uuid.uuid4().hex[:8],
            sender="L3-003",
            receiver="collect",
            task_type="REWORK",
            payload={"brands": brands_to_recollect, "reason": "证据不足，补充采集"},
            issues=collect_targets,
        ))

    # 维度缺失 / schema 不足 → 打回 analyze 重分析
    analyze_targets = [iss for iss in qr.issues
                       if iss["target"].startswith("dimension:") or iss["target"] == "schema"]
    if analyze_targets:
        envelopes.append(Envelope(
            msg_id="env_" + uuid.uuid4().hex[:8],
            sender="L3-003",
            receiver="analyze",
            task_type="REWORK",
            payload={"reason": "维度/结构覆盖不足，重新分析补全"},
            issues=analyze_targets,
        ))

    return envelopes


def build_rework_envelopes(qr: QualityReport, review: Dict[str, Any], brands: List[str] = None) -> List[Envelope]:
    """Build rule and categorized LLM rework envelopes for collect/analyze/write."""
    envelopes = decide_rework(qr)
    if quality_gate_passed(qr, review):
        return []

    routes = normalize_issue_routes((review or {}).get("issue_routes"), (review or {}).get("issues", []))
    review_suggestions = [str(item).strip() for item in (review or {}).get("suggestions", []) if str(item).strip()]
    rule_reasons = [str(item.get("reason", "")) for item in qr.issues if item.get("reason")]
    if not routes and rule_reasons:
        routes = [{"category": "analyze", "issue": reason, "suggestion": ""} for reason in rule_reasons]
    if not routes:
        routes = [{"category": "analyze", "issue": str((review or {}).get("review") or "LLM 复审未通过"), "suggestion": ""}]

    by_receiver = {env.receiver: env for env in envelopes}
    for category in ("collect", "analyze", "write"):
        category_routes = [route for route in routes if route["category"] == category]
        if not category_routes:
            continue
        issues = [{"target": "review", "severity": "medium", "reason": route["issue"], "raised_by": "L3-003"}
                  for route in category_routes[:8]]
        suggestions = [route["suggestion"] for route in category_routes if route.get("suggestion")]
        suggestions.extend(
            suggestion for suggestion in review_suggestions
            if classify_review_issue(suggestion) == category
        )
        if category in by_receiver:
            env = by_receiver[category]
            env.issues.extend(issues)
            env.payload.setdefault("feedback", []).extend(suggestions)
            env.payload.setdefault("suggestions", []).extend(suggestions)
            continue
        payload = {"reason": f"质检反馈归类为 {category}，执行对应返工",
                   "feedback": suggestions, "suggestions": suggestions[:4]}
        if category == "collect":
            mentioned = [b for b in (brands or []) if any(b.lower() in route["issue"].lower() for route in category_routes)]
            payload["brands"] = mentioned or list(brands or [])
        envelopes.append(Envelope(
            msg_id="env_" + uuid.uuid4().hex[:8], sender="L3-003", receiver=category,
            task_type="REWORK", payload=payload, issues=issues,
        ))
    return envelopes
