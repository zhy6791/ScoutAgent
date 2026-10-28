"""质检与反馈闭环（对应需求 12：真实可触发的返工闭环）。

evaluate_quality：基于 claims/evidences/structured 计算可量化质量指标 + 暴露问题（Issue）。
decide_rework：根据规则问题打回 collect（补采）或 analyze（重分析）。
build_rework_envelopes：将 LLM 反馈分类为 collect/analyze/write，并生成对应 Envelope。
"""
from __future__ import annotations

import uuid
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.core.fetcher import domain_of
from app.core.models import Envelope


REVIEW_MAX_CLAIMS_PER_BATCH = 6
REVIEW_MAX_EVIDENCES_PER_CLAIM = 4
REVIEW_EVIDENCE_CHARS = 1200
_CLAIM_REVIEW_VERDICTS = {
    "supported", "partially_supported", "unrelated", "contradicted", "insufficient",
}


def _value(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def build_claim_evidence_review_context(
    claims: List[Dict[str, Any]], evidences: List[Any],
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Build bounded, claim-scoped evidence context for semantic review."""
    evidence_by_id = {
        str(_value(e, "evidence_id", "")): e for e in evidences
        if _value(e, "evidence_id", "")
    }
    contexts: List[Dict[str, Any]] = []
    attached_ids = set()
    truncated_evidences = 0
    missing_ids = 0
    excerpt_only_evidences = 0
    for index, claim in enumerate(claims):
        claim_id = str(claim.get("claim_id") or f"claim_{index + 1}")
        claim_eids = list(dict.fromkeys(str(eid) for eid in (claim.get("evidence_ids") or []) if eid))
        selected_ids = claim_eids[:REVIEW_MAX_EVIDENCES_PER_CLAIM]
        if len(claim_eids) > len(selected_ids):
            truncated_evidences += len(claim_eids) - len(selected_ids)
        attached = []
        for evidence_id in selected_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                missing_ids += 1
                continue
            source_url = str(_value(evidence, "source_url", ""))
            excerpt = str(_value(evidence, "excerpt", "") or "")
            stored_review_text = str(_value(evidence, "review_text", "") or _value(evidence, "_full_text", "") or "")
            review_text = stored_review_text or excerpt
            if not stored_review_text:
                excerpt_only_evidences += 1
            if len(review_text) > REVIEW_EVIDENCE_CHARS:
                review_text = review_text[:REVIEW_EVIDENCE_CHARS]
                truncated_evidences += 1
            attached.append({
                "evidence_id": evidence_id,
                "brand": str(_value(evidence, "brand", "")),
                "source_type": str(_value(evidence, "source_type", "")),
                "domain": str(_value(evidence, "domain", "") or domain_of(source_url)),
                "title": str(_value(evidence, "title", "")),
                "excerpt": excerpt[:400],
                "review_text": review_text,
                "text_source": "review_text" if stored_review_text else "excerpt",
                "source_url": source_url,
                "captured_at": str(_value(evidence, "captured_at", "")),
            })
            attached_ids.add(evidence_id)
        contexts.append({
            "claim_id": claim_id,
            "claim": str(claim.get("text", "")),
            "field": str(claim.get("field", "")),
            "confidence": str(claim.get("confidence", "")),
            "evidence_ids": claim_eids,
            "missing_evidence_ids": [eid for eid in claim_eids if eid not in evidence_by_id],
            "evidences": attached,
        })
    return contexts, {
        "claims_total": len(claims),
        "evidence_attached": len(attached_ids),
        "evidence_links_attached": sum(len(c["evidences"]) for c in contexts),
        "unreviewed_claims": 0,
        "truncated_evidences": truncated_evidences,
        "excerpt_only_evidences": excerpt_only_evidences,
        "missing_evidence_ids": sum(len(c["missing_evidence_ids"]) for c in contexts),
        "batch_count": max(1, (len(contexts) + REVIEW_MAX_CLAIMS_PER_BATCH - 1) // REVIEW_MAX_CLAIMS_PER_BATCH),
    }


def normalize_claim_evidence_reviews(
    raw_reviews: Any, contexts: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Keep only model assessments that refer to the supplied claim/evidence pairs."""
    raw_by_claim = {}
    for item in raw_reviews if isinstance(raw_reviews, list) else []:
        if isinstance(item, dict) and item.get("claim_id"):
            raw_by_claim[str(item["claim_id"])] = item
    normalized = []
    blockers = []
    for context in contexts:
        claim_id = context["claim_id"]
        raw = raw_by_claim.get(claim_id)
        allowed_eids = {ev["evidence_id"] for ev in context["evidences"]}
        if not raw:
            verdict = "unreviewed"
            assessments = []
        else:
            verdict = str(raw.get("verdict", "")).strip().lower()
            if verdict not in _CLAIM_REVIEW_VERDICTS:
                verdict = "unreviewed"
            assessments = []
            for assessment in raw.get("evidence_assessments", []) if isinstance(raw.get("evidence_assessments"), list) else []:
                if not isinstance(assessment, dict):
                    continue
                evidence_id = str(assessment.get("evidence_id", ""))
                evidence_verdict = str(assessment.get("verdict", "")).lower().strip()
                if evidence_id not in allowed_eids or evidence_verdict not in {
                    "supports", "partially_supports", "unrelated", "contradicts",
                }:
                    continue
                assessments.append({
                    "evidence_id": evidence_id,
                    "verdict": evidence_verdict,
                    "reason": str(assessment.get("reason", ""))[:500],
                    "support_quote": str(assessment.get("support_quote", ""))[:400],
                })
            evidence_verdicts = {item["verdict"] for item in assessments}
            assessed_ids = {item["evidence_id"] for item in assessments}
            expected_ids = {item["evidence_id"] for item in context["evidences"]}
            if expected_ids - assessed_ids:
                verdict = "unreviewed"
            elif "contradicts" in evidence_verdicts:
                verdict = "contradicted"
            elif evidence_verdicts and evidence_verdicts <= {"unrelated"}:
                verdict = "unrelated"
            elif not evidence_verdicts.intersection({"supports", "partially_supports"}):
                if verdict == "supported":
                    verdict = "insufficient" if not context["evidences"] else "unreviewed"
            elif "partially_supports" in evidence_verdicts and "supports" not in evidence_verdicts:
                verdict = "partially_supported"
            if not context["evidences"] and verdict == "supported":
                verdict = "insufficient"
            if context.get("missing_evidence_ids"):
                verdict = "unreviewed"
            if not context["evidences"] and verdict == "supported":
                verdict = "insufficient"
        reason = str((raw or {}).get("issue") or "")[:1000]
        suggestion = str((raw or {}).get("suggestion") or "")[:1000]
        result = {
            "claim_id": claim_id,
            "claim": context["claim"],
            "verdict": verdict,
            "evidence_assessments": assessments,
            "issue": reason,
            "suggestion": suggestion,
        }
        normalized.append(result)
        if verdict in {"unrelated", "contradicted", "insufficient", "unreviewed"} and context["evidences"] and not context.get("missing_evidence_ids"):
            category = "collect" if verdict in {"insufficient", "unreviewed", "unrelated"} else "analyze"
            blockers.append({
                "category": category,
                "issue": reason or f"Claim {claim_id} 的证据复审结果为 {verdict}",
                "suggestion": suggestion,
                "claim_id": claim_id,
                "evidence_ids": context["evidence_ids"],
                "verdict": verdict,
            })
        elif verdict == "partially_supported" and (context["confidence"] == "high" or raw.get("importance") == "core"):
            blockers.append({
                "category": "analyze",
                "issue": reason or f"核心 Claim {claim_id} 仅得到部分证据支持",
                "suggestion": suggestion,
                "claim_id": claim_id,
                "evidence_ids": context["evidence_ids"],
                "verdict": verdict,
            })
    return normalized, blockers


def validate_claim_review_response(data: Any, contexts: List[Dict[str, Any]]) -> List[str]:
    """Validate the LLM envelope without interpreting semantic verdicts."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["LLM response is not a JSON object."]
    allowed_top_level = {"claim_reviews", "blocking_issues", "advisories", "scores", "review"}
    unknown_top_level = set(data) - allowed_top_level
    if unknown_top_level:
        errors.append(f"LLM returned illegal top-level fields: {', '.join(sorted(unknown_top_level))}.")
    for key, expected_type in (("blocking_issues", list), ("advisories", list), ("scores", dict), ("review", str)):
        if key in data and not isinstance(data.get(key), expected_type):
            errors.append(f"{key} has an invalid type.")
    reviews = data.get("claim_reviews")
    if not isinstance(reviews, list):
        return ["claim_reviews is missing or is not an array."]
    expected = {str(item["claim_id"]): item for item in contexts}
    actual = {}
    for item in reviews:
        if not isinstance(item, dict) or not item.get("claim_id"):
            errors.append("claim_reviews contains an item without claim_id.")
            continue
        claim_id = str(item["claim_id"])
        if claim_id not in expected:
            errors.append(f"LLM returned unknown claim_id: {claim_id}.")
            continue
        if claim_id in actual:
            errors.append(f"LLM returned duplicate claim_id: {claim_id}.")
        actual[claim_id] = item
        allowed_review_fields = {"claim_id", "verdict", "importance", "evidence_assessments", "issue", "suggestion"}
        unknown_review_fields = set(item) - allowed_review_fields
        if unknown_review_fields:
            errors.append(f"Claim {claim_id} has illegal fields: {', '.join(sorted(unknown_review_fields))}.")
        verdict = str(item.get("verdict", "")).lower().strip()
        if verdict not in _CLAIM_REVIEW_VERDICTS:
            errors.append(f"Claim {claim_id} has an invalid verdict.")
        assessments = item.get("evidence_assessments")
        if not isinstance(assessments, list):
            errors.append(f"Claim {claim_id} is missing evidence_assessments.")
            continue
        allowed = {str(ev["evidence_id"]) for ev in expected[claim_id]["evidences"]}
        returned = set()
        for assessment in assessments:
            if not isinstance(assessment, dict) or not assessment.get("evidence_id"):
                errors.append(f"Claim {claim_id} has an invalid evidence assessment.")
                continue
            evidence_id = str(assessment["evidence_id"])
            allowed_assessment_fields = {"evidence_id", "verdict", "reason", "support_quote"}
            if set(assessment) - allowed_assessment_fields:
                errors.append(f"Evidence assessment for {claim_id} has illegal fields.")
            if evidence_id not in allowed:
                errors.append(f"Claim {claim_id} returned an unknown evidence_id: {evidence_id}.")
            returned.add(evidence_id)
            if str(assessment.get("verdict", "")).lower().strip() not in {
                "supports", "partially_supports", "unrelated", "contradicts",
            }:
                errors.append(f"Evidence {evidence_id} has an invalid verdict.")
        if allowed - returned:
            errors.append(f"Claim {claim_id} did not assess every attached evidence.")
    missing = set(expected) - set(actual)
    if missing:
        errors.append(f"LLM omitted claim_ids: {', '.join(sorted(missing))}.")
    for index, issue in enumerate(data.get("blocking_issues", []) if isinstance(data.get("blocking_issues"), list) else []):
        if not isinstance(issue, dict) or not {"category", "issue", "suggestion"}.issubset(issue):
            errors.append(f"blocking_issues[{index}] is incomplete or invalid.")
            continue
        if set(issue) - {"category", "issue", "suggestion"}:
            errors.append(f"blocking_issues[{index}] has illegal fields.")
        if issue.get("category") not in {"collect", "analyze", "write"} or not isinstance(issue.get("issue"), str):
            errors.append(f"blocking_issues[{index}] has invalid values.")
    if any(not isinstance(item, str) for item in data.get("advisories", []) if isinstance(data.get("advisories"), list)):
        errors.append("advisories must contain only strings.")
    return list(dict.fromkeys(errors))


@dataclass
class QualityReport:
    coverage_by_dimension: Dict[str, bool] = field(default_factory=dict)
    coverage_by_brand: Dict[str, Dict[str, int]] = field(default_factory=dict)
    confidence_ratio: float = 0.0
    schema_completeness: float = 0.0
    dimension_coverage_rate: float = 0.0
    brand_coverage_rate: float = 0.0
    issues: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def rule_issues(self) -> List[Dict[str, Any]]:
        return self.issues

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage_by_dimension": self.coverage_by_dimension,
            "coverage_by_brand": self.coverage_by_brand,
            "confidence_ratio": self.confidence_ratio,
            "schema_completeness": self.schema_completeness,
            "dimension_coverage_rate": self.dimension_coverage_rate,
            "brand_coverage_rate": self.brand_coverage_rate,
            "issues": self.issues,
            "rule_issues": self.rule_issues,
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

    # 6. Claim-Evidence integrity rules; these do not require an LLM.
    evidence_by_id = {
        str(_value(e, "evidence_id", "")): e for e in evidences
        if _value(e, "evidence_id", "")
    }
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "unknown")
        claim_text = str(claim.get("text") or "")
        evidence_ids = list(dict.fromkeys(str(eid) for eid in (claim.get("evidence_ids") or []) if eid))
        if not evidence_ids:
            qr.issues.append({
                "issue_id": "is_" + uuid.uuid4().hex[:8],
                "target": f"claim_evidence:{claim_id}",
                "rule_type": "claim_no_evidence",
                "category": "collect",
                "severity": "high",
                "reason": f"Claim {claim_id} 没有绑定任何 Evidence。",
                "raised_by": "L3-003",
            })
            continue

        missing_ids = [eid for eid in evidence_ids if eid not in evidence_by_id]
        if missing_ids:
            qr.issues.append({
                "issue_id": "is_" + uuid.uuid4().hex[:8],
                "target": f"claim_evidence:{claim_id}",
                "rule_type": "evidence_id_missing",
                "category": "analyze",
                "severity": "high",
                "reason": f"Claim {claim_id} 引用了本次 Evidence 集合中不存在的 ID：{', '.join(missing_ids)}。",
                "raised_by": "L3-003",
            })

        linked = [evidence_by_id[eid] for eid in evidence_ids if eid in evidence_by_id]
        mentioned_brands = [brand for brand in brands if brand and brand.casefold() in claim_text.casefold()]
        linked_brands = {str(_value(item, "brand", "")) for item in linked}
        mismatched = [brand for brand in mentioned_brands if brand not in linked_brands]
        if mismatched:
            qr.issues.append({
                "issue_id": "is_" + uuid.uuid4().hex[:8],
                "target": f"claim_evidence:{claim_id}",
                "rule_type": "brand_mismatch",
                "category": "analyze",
                "severity": "high",
                "reason": f"Claim {claim_id} 提及品牌 {', '.join(mismatched)}，但绑定证据中没有对应品牌来源。",
                "raised_by": "L3-003",
            })

    return qr


def _llm_quality_review_legacy(
    query: str,
    brands: List[str],
    focus: List[str],
    claims: List[Dict[str, Any]],
    structured: Dict[str, Any],
    qr: "QualityReport",
    model: str = None,
) -> Dict[str, Any]:
    """质检官用 LLM 对当前分析做真实『审阅』（非纯规则）。

    只有影响核心结论可靠性的阻断问题才进入 issues 并触发返工；非阻断问题进入
    advisories。无阻断问题时即使仍有改进建议，也应判定为 pass。
    """
    from app.core.llm import chat_json

    claim_lines = "\n".join(
        f"- [{c.get('confidence','?')}|{c.get('field','')}|"
        f"证据{len(c.get('evidence_ids') or [])}条|"
        f"交叉验证:{'是' if c.get('cross_validated') else '否'}] {c.get('text','')}"
        for c in claims[:18]
    ) or "（暂无论点）"
    sc_dims = "、".join(f"{k}:{'已覆盖' if v else '缺失'}"
                       for k, v in qr.coverage_by_dimension.items()) or "无"
    brand_cov = "、".join(f"{b}({v.get('domains',0)}域/{v.get('evidence',0)}证据)"
                         for b, v in qr.coverage_by_brand.items()) or "无"
    fallback = {
        "verdict": "rework",
        "scores": {
            "证据充分性": round(qr.brand_coverage_rate * 100),
            "维度完整性": round(qr.dimension_coverage_rate * 100),
            "结论置信度": round(qr.confidence_ratio * 100),
            "结构化完整度": round(qr.schema_completeness * 100),
        },
        "review": "LLM 复审未成功返回可解析结果，无法确认本轮质检通过。",
        "issues": ["LLM 复审输出无效或调用失败，无法确认核心结论已通过语义质检。"],
        "suggestions": ["重新执行 LLM 复审；在获得有效复审结果前不要标记为质检通过。"],
        "advisories": [],
        "review_failed": True,
    }
    fallback["issue_routes"] = [{
        "category": "analyze",
        "issue": fallback["issues"][0],
        "suggestion": fallback["suggestions"][0],
    }]
    messages = [
        {"role": "system", "content": (
            "你是竞品分析报告的质检官（L3 决策层）。请审阅下面的『分析中间产物』，"
            "逐维度打分（0-100 整数），并区分阻断问题与非阻断建议。"
            "只有会实质影响核心结论可靠性的问题才是阻断问题：核心结论无证据或与证据矛盾；"
            "关键量化断言来自低可信单一来源且仍标为高置信；用户明确要求的关键维度完全缺失且未说明数据不可得；"
            "待验证假设被当作高置信事实；严重逻辑矛盾或关键 Schema 缺失。"
            "以下情况不得单独作为阻断问题：调研主体证据数少于某个竞品但已达到可用覆盖；"
            "权威官方一手数据只有一个来源；非核心数字只有单一来源；表达或可读性可优化；"
            "精确市场数据确实不公开但已披露数据缺口、代理指标和推断边界。"
            "没有阻断问题时必须 verdict=pass；一般改进项放入 advisories，不得因此判 rework。"
            "最多输出 5 个 blocking_issues 和 6 个 advisories。"
            '只输出 JSON：{"verdict":"pass|rework",'
            '"scores":{"证据充分性":int,"维度完整性":int,"结论置信度":int,"结构化完整度":int,"交叉验证":int},'
            '"review":"总体评审意见",'
            '"blocking_issues":[{"category":"collect|analyze|write","issue":"阻断问题","suggestion":"必要修复动作"}],'
            '"advisories":["非阻断改进建议"]}。'
            "问题路由规则：证据不足/缺少来源/交叉验证/数据复现归 collect；"
            "论点逻辑/维度遗漏/Schema/置信度/结论结构归 analyze；"
            "表达、段落、标题、可读性、重复或写作风格归 write。"
        )},
        {"role": "user", "content": (
            f"调研主题：{query}\n竞品：{'、'.join(brands)}\n重点维度：{'、'.join(focus)}\n"
            f"规则侧指标 → 维度覆盖：{sc_dims}；品牌证据覆盖：{brand_cov}；"
            f"高置信占比：{round(qr.confidence_ratio*100)}%；结构化完整度：{round(qr.schema_completeness*100)}%\n"
            f"已提炼论点：\n{claim_lines}"
        )},
    ]
    # JSON 偶尔会因模型输出异常而无法解析。复审失败不能静默放行，因此在函数内重试一次。
    for _ in range(2):
        try:
            data = chat_json(
                messages,
                max_tokens=6000, temperature=0.3, model=model,
                purpose="质检官审阅：区分阻断问题与非阻断建议",
            )
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("scores"):
            continue

        raw_blocking = data.get("blocking_issues")
        if raw_blocking is None:  # 兼容旧模型响应格式
            raw_blocking = data.get("issues") or []
        blocking: List[Dict[str, str]] = []
        for item in raw_blocking if isinstance(raw_blocking, list) else []:
            if isinstance(item, dict):
                issue = str(item.get("issue") or item.get("text") or "").strip()
                suggestion = str(item.get("suggestion") or "").strip()
                category = str(item.get("category") or "").lower().strip()
            else:
                issue = str(item).strip()
                suggestion = ""
                category = ""
            if not issue:
                continue
            if category not in {"collect", "analyze", "write"}:
                category = classify_review_issue(issue)
            blocking.append({"category": category, "issue": issue, "suggestion": suggestion})
        blocking = blocking[:5]
        advisories = [
            str(item).strip() for item in (data.get("advisories") or [])
            if str(item).strip()
        ][:6]
        issues = [item["issue"] for item in blocking]
        suggestions = [item["suggestion"] for item in blocking if item["suggestion"]]
        scores = {str(k): _clamp_score(v) for k, v in (data.get("scores") or {}).items()}
        return {
            # 严重性分层后的阻断问题是唯一的 LLM 返工依据。
            "verdict": "rework" if issues else "pass",
            "scores": scores or fallback["scores"],
            "review": str(data.get("review") or ""),
            "issues": issues,
            "suggestions": suggestions,
            "advisories": advisories,
            "issue_routes": blocking,
            "review_failed": False,
        }
    return fallback


def llm_quality_review_with_evidence(
    query: str,
    brands: List[str],
    focus: List[str],
    claims: List[Dict[str, Any]],
    structured: Dict[str, Any],
    qr: "QualityReport",
    model: str = None,
    evidences: List[Any] = None,
) -> Dict[str, Any]:
    """Run claim-scoped semantic review and fail closed on missing reviews."""
    from app.core.llm import chat_json

    legacy_mode = evidences is None
    if legacy_mode:
        contexts = [{
            "claim_id": str(c.get("claim_id") or f"claim_{i + 1}"),
            "claim": str(c.get("text", "")),
            "field": str(c.get("field", "")),
            "confidence": str(c.get("confidence", "")),
            "evidence_ids": list(c.get("evidence_ids") or []),
            "evidences": [],
        } for i, c in enumerate(claims[:18])]
        context_stats = {"claims_total": len(contexts), "evidence_attached": 0,
                         "evidence_links_attached": 0, "unreviewed_claims": 0,
                         "truncated_evidences": 0, "missing_evidence_ids": 0, "batch_count": 1}
    else:
        contexts, context_stats = build_claim_evidence_review_context(claims, evidences or [])

    fallback = {
        "verdict": "rework",
        "scores": {
            "evidence": round(qr.brand_coverage_rate * 100),
            "dimensions": round(qr.dimension_coverage_rate * 100),
            "confidence": round(qr.confidence_ratio * 100),
            "schema": round(qr.schema_completeness * 100),
        },
        "review": "Semantic review failed; the quality gate cannot be passed.",
        "issues": ["LLM semantic review failed or returned an unusable response."],
        "suggestions": ["Retry semantic review before publishing the report."],
        "advisories": [],
        "issue_routes": [{"category": "analyze", "issue": "LLM semantic review failed or returned an unusable response.", "suggestion": "Retry semantic review before publishing the report."}],
        "claim_evidence_reviews": [],
        "review_context_stats": context_stats,
        "review_failed": True,
        "review_valid": False,
    }
    batches = [contexts[i:i + REVIEW_MAX_CLAIMS_PER_BATCH]
               for i in range(0, len(contexts), REVIEW_MAX_CLAIMS_PER_BATCH)] or [[]]
    raw_claim_reviews: List[Any] = []
    raw_blocking: List[Any] = []
    advisories: List[str] = []
    scores: Dict[str, int] = {}
    review_texts: List[str] = []
    review_failed = False
    review_validation_errors: List[str] = []
    for batch in batches:
        payload = {
            "query": query,
            "brands": brands,
            "focus": focus,
            "rule_metrics": {
                "coverage_by_dimension": qr.coverage_by_dimension,
                "coverage_by_brand": qr.coverage_by_brand,
                "confidence_ratio": qr.confidence_ratio,
                "schema_completeness": qr.schema_completeness,
            },
            "claims": batch,
            "structured_fields": list((structured or {}).keys()),
        }
        messages = [
            {"role": "system", "content": (
                "You are an evidence-grounded competitive research reviewer. "
                "Review every claim against only its attached evidence. Same-brand evidence is not proof. "
                "Use verdict supported, partially_supported, unrelated, contradicted, or insufficient. "
                "Return JSON only. Every claim_id must appear in claim_reviews; every evidence_id must be "
                "one of that claim's attached evidence. Include a short reason and quote when possible. "
                "Only substantive factual or logical problems belong in blocking_issues; writing suggestions "
                "belong in advisories. Each claim_reviews item must contain claim_id, verdict, importance, "
                "evidence_assessments (one item per attached evidence, with evidence_id, verdict, reason, "
                "support_quote), issue, and suggestion. Evidence verdict must be supports, partially_supports, "
                "unrelated, or contradicts. Output claim_reviews, blocking_issues, advisories, scores and review."
            )},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        data = None
        batch_validation_errors: List[str] = []
        for _ in range(2):
            try:
                data = chat_json(messages, max_tokens=6000, temperature=0.2, model=model,
                                 purpose="claim-evidence semantic quality review")
            except Exception:
                data = None
            batch_validation_errors = validate_claim_review_response(data, batch)
            if not batch_validation_errors:
                break
        if batch_validation_errors:
            review_validation_errors.extend(batch_validation_errors)
            review_failed = True
            continue
        if not isinstance(data, dict):
            review_failed = True
            continue
        if isinstance(data.get("claim_reviews"), list):
            raw_claim_reviews.extend(data["claim_reviews"])
        raw_items = data.get("blocking_issues")
        if raw_items is None:
            raw_items = data.get("issues") or []
        if isinstance(raw_items, list):
            raw_blocking.extend(raw_items)
        advisories.extend(str(x).strip() for x in (data.get("advisories") or []) if str(x).strip())
        scores.update({str(k): _clamp_score(v) for k, v in (data.get("scores") or {}).items()})
        if data.get("review"):
            review_texts.append(str(data["review"]))

    if review_failed and not raw_claim_reviews and not raw_blocking:
        fallback["review_validation_errors"] = list(dict.fromkeys(review_validation_errors))
        fallback["issues"] = []
        fallback["semantic_issues"] = []
        fallback["rule_issues"] = qr.rule_issues
        fallback["issue_routes"] = []
        fallback["review_valid"] = False
        return fallback

    claim_reviews, semantic_blockers = normalize_claim_evidence_reviews(
        raw_claim_reviews, contexts
    ) if not legacy_mode else ([], [])
    context_stats["unreviewed_claims"] = sum(
        1 for item in claim_reviews if item.get("verdict") == "unreviewed"
    )
    blockers: List[Dict[str, Any]] = list(semantic_blockers)
    for item in raw_blocking:
        if isinstance(item, dict):
            issue = str(item.get("issue") or item.get("text") or "").strip()
            suggestion = str(item.get("suggestion") or "").strip()
            category = str(item.get("category") or "").lower().strip()
        else:
            issue, suggestion, category = str(item).strip(), "", ""
        if not issue:
            continue
        if category not in {"collect", "analyze", "write"}:
            category = classify_review_issue(issue)
        blockers.append({"category": category, "issue": issue, "suggestion": suggestion})
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in blockers:
        key = (item.get("category"), item.get("issue"), item.get("claim_id", ""))
        if item.get("issue") and key not in seen:
            seen.add(key)
            deduped.append(item)
    blockers = deduped[:12]
    issues = [item["issue"] for item in blockers]
    suggestions = [item["suggestion"] for item in blockers if item.get("suggestion")]
    return {
        "verdict": "rework" if issues else "pass",
        "scores": scores or fallback["scores"],
        "review": "\n".join(review_texts),
        "issues": issues,
        "rule_issues": qr.rule_issues,
        "semantic_issues": issues,
        "review_validation_errors": list(dict.fromkeys(review_validation_errors)),
        "suggestions": suggestions,
        "advisories": list(dict.fromkeys(advisories))[:12],
        "issue_routes": blockers,
        "claim_evidence_reviews": claim_reviews,
        "review_context_stats": context_stats,
        "review_failed": review_failed or bool(review_validation_errors),
        "review_valid": not review_failed and not review_validation_errors,
    }


llm_quality_review = _llm_quality_review_legacy


def _clamp_score(v) -> int:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def quality_gate_passed(qr: QualityReport, review: Dict[str, Any]) -> bool:
    """Return true only when rules pass and a validated semantic review passes."""
    return (
        not qr.rule_issues
        and str((review or {}).get("verdict", "")).lower() == "pass"
        and not (review or {}).get("semantic_issues", (review or {}).get("issues", []))
        and not (review or {}).get("review_validation_errors")
        and (review or {}).get("review_valid", not (review or {}).get("review_failed", False)) is True
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


def normalize_issue_routes(raw_routes: Any, issues: Any) -> List[Dict[str, Any]]:
    """Normalize model-provided routes while preserving compatibility with string issues."""
    normalized: List[Dict[str, Any]] = []
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
            route = {
                "category": category,
                "issue": issue,
                "suggestion": str(item.get("suggestion") or "").strip(),
            }
            for key in ("claim_id", "evidence_ids", "verdict"):
                if key in item:
                    route[key] = item[key]
            normalized.append(route)
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

    # Claim-Evidence integrity issues use an explicit rule category.
    collect_targets = [iss for iss in qr.issues if iss["target"].startswith("brand:") or iss.get("rule_type") == "claim_no_evidence"]
    if collect_targets:
        brands_to_recollect = [iss["target"].split(":", 1)[1] for iss in collect_targets if iss["target"].startswith("brand:")]
        envelopes.append(Envelope(
            msg_id="env_" + uuid.uuid4().hex[:8],
            sender="L3-003",
            receiver="collect",
            task_type="REWORK",
            payload={"brands": brands_to_recollect, "reason": "Claim-Evidence 规则检查失败，补充采集"},
            issues=collect_targets,
        ))

    # 维度缺失 / schema 不足 → 打回 analyze 重分析
    analyze_targets = [iss for iss in qr.issues
                       if iss["target"].startswith("dimension:") or iss["target"] == "schema"
                       or iss.get("rule_type") in {"evidence_id_missing", "brand_mismatch"}]
    if analyze_targets:
        envelopes.append(Envelope(
            msg_id="env_" + uuid.uuid4().hex[:8],
            sender="L3-003",
            receiver="analyze",
            task_type="REWORK",
            payload={"reason": "规则检查发现 Evidence 绑定或维度问题，重新分析补全"},
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
    if not routes and (review or {}).get("review_validation_errors") and not qr.rule_issues:
        # Invalid LLM output was retried inside the review function. Do not
        # misroute a protocol error to collect/analyze.
        return envelopes
    if not routes:
        routes = [{"category": "analyze", "issue": str((review or {}).get("review") or "LLM 复审未通过"), "suggestion": ""}]

    by_receiver = {env.receiver: env for env in envelopes}
    for category in ("collect", "analyze", "write"):
        category_routes = [route for route in routes if route["category"] == category]
        if not category_routes:
            continue
        issues = [{
            "target": "review", "severity": "medium", "reason": route["issue"],
            "raised_by": "L3-003", "claim_id": route.get("claim_id", ""),
            "evidence_ids": route.get("evidence_ids", []), "verdict": route.get("verdict", ""),
        } for route in category_routes[:8]]
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
    return ensure_collect_reanalysis(envelopes)


def ensure_collect_reanalysis(envelopes: List[Envelope]) -> List[Envelope]:
    """Ensure evidence collection rework is consumed by the analysis stage.

    A collect-only review would otherwise update evidence coverage while leaving
    Claims and structured knowledge generated from the previous evidence set.
    Insert a synthetic analyze Envelope immediately after the collect Envelopes
    when the current round did not already schedule analysis.
    """
    if not any(env.receiver == "collect" for env in envelopes):
        return envelopes
    if any(env.receiver == "analyze" for env in envelopes):
        return envelopes

    collect_envelopes = [env for env in envelopes if env.receiver == "collect"]
    feedback: List[str] = []
    issues: List[Dict[str, Any]] = []
    for env in collect_envelopes:
        issues.extend(env.issues)
        for key in ("feedback", "suggestions"):
            values = env.payload.get(key, [])
            if not isinstance(values, list):
                values = [values]
            feedback.extend(str(item).strip() for item in values if str(item).strip())
    feedback = list(dict.fromkeys(feedback))
    if not issues:
        issues = [{
            "target": "review",
            "severity": "medium",
            "reason": "采集返工已补充证据，必须重新分析以消费新增证据。",
            "raised_by": "L3-003",
        }]
    analyze_env = Envelope(
        msg_id="env_" + uuid.uuid4().hex[:8],
        sender="L3-003",
        receiver="analyze",
        task_type="REWORK",
        payload={
            "reason": "采集返工完成后强制重新分析，确保新增证据进入 Claims 和结构化知识。",
            "feedback": feedback,
            "source": "collect_rework",
        },
        issues=issues,
    )
    insert_at = max(index for index, env in enumerate(envelopes) if env.receiver == "collect") + 1
    return envelopes[:insert_at] + [analyze_env] + envelopes[insert_at:]


def partition_rework_envelopes(envelopes: List[Envelope]) -> tuple[List[Envelope], List[Envelope]]:
    """Split material analysis rework from pre-write constraints."""
    analysis_rework = [env for env in envelopes if env.receiver in {"collect", "analyze"}]
    write_constraints = [env for env in envelopes if env.receiver == "write"]
    return analysis_rework, write_constraints


def accept_write_constraints(review: Dict[str, Any]) -> Dict[str, Any]:
    """Mark a write-only review as passed by the analysis quality gate.

    Write issues cannot be verified before a report exists. They are preserved as
    writing constraints and must not consume an analysis rework round.
    """
    updated = dict(review or {})
    routes = normalize_issue_routes(updated.get("issue_routes"), updated.get("issues", []))
    write_routes = [route for route in routes if route["category"] == "write"]
    constraints: List[str] = []
    for route in write_routes:
        constraints.append(route["issue"])
        if route.get("suggestion"):
            constraints.append(route["suggestion"])
    constraints = list(dict.fromkeys(item for item in constraints if item))
    updated["original_verdict"] = updated.get("verdict")
    updated["verdict"] = "pass"
    updated["issues"] = []
    updated["suggestions"] = []
    updated["issue_routes"] = []
    updated["writing_constraints"] = constraints
    updated["write_constraints_deferred"] = bool(constraints)
    return updated
