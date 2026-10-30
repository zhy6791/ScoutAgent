"""Deterministic post-write checks for generated report sections."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


_EVIDENCE_REF = re.compile(r"\[(e_[A-Za-z0-9_-]+)\]")
_PLACEHOLDER_HINTS = ("生成失败", "调用失败", "请重新运行", "模型错误", "暂无内容")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}")


def _paragraphs(value: Any) -> List[str]:
    if isinstance(value, dict):
        value = value.get("paragraphs", [])
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def report_completeness_audit(
    sections: Dict[str, Any],
    claims: Iterable[Dict[str, Any]],
    evidences: Iterable[Any],
    brands: Iterable[str],
    required_section_ids: Iterable[str],
) -> Dict[str, Any]:
    """Check report output without asking an LLM to approve its own text."""
    evidence_ids = {
        str(item.get("evidence_id")) if isinstance(item, dict)
        else str(getattr(item, "evidence_id", ""))
        for item in evidences
    }
    evidence_ids.discard("")
    claims_list = [c for c in claims if isinstance(c, dict)]
    section_map = sections if isinstance(sections, dict) else {}
    issues: List[Dict[str, Any]] = []
    missing_sections = [sid for sid in required_section_ids if sid not in section_map]
    for sid in missing_sections:
        issues.append({"section_id": sid, "category": "write", "issue": "required section missing",
                       "suggestion": "generate this section once"})

    all_text_parts: List[str] = []
    invalid_evidence_ids: List[str] = []
    empty_sections: List[str] = []
    seen_paragraphs: Dict[str, str] = {}
    for sid, value in section_map.items():
        paragraphs = _paragraphs(value)
        all_text_parts.extend(paragraphs)
        if not paragraphs:
            empty_sections.append(str(sid))
            issues.append({"section_id": sid, "category": "write", "issue": "section is empty",
                           "suggestion": "rewrite the section with its linked claims"})
            continue
        refs = _EVIDENCE_REF.findall("\n".join(paragraphs))
        bad = sorted({ref for ref in refs if ref not in evidence_ids})
        if bad:
            invalid_evidence_ids.extend(bad)
            issues.append({"section_id": sid, "category": "write",
                           "issue": f"invalid evidence ids: {', '.join(bad)}",
                           "suggestion": "replace them with supplied evidence ids"})
        for paragraph in paragraphs:
            if any(hint in paragraph for hint in _PLACEHOLDER_HINTS):
                issues.append({"section_id": sid, "category": "write",
                               "issue": "model failure placeholder in section",
                               "suggestion": "rewrite the section"})
            normalized = re.sub(r"\s+", " ", paragraph).strip().casefold()
            if normalized in seen_paragraphs:
                issues.append({"section_id": sid, "category": "write",
                               "issue": f"duplicate paragraph with {seen_paragraphs[normalized]}",
                               "suggestion": "remove repeated paragraph"})
            else:
                seen_paragraphs[normalized] = str(sid)

    combined = "\n".join(all_text_parts)
    missing_brands = [brand for brand in brands if brand and str(brand).casefold() not in combined.casefold()]
    for brand in missing_brands:
        issues.append({"section_id": "__report__", "category": "write",
                       "issue": f"brand missing from report: {brand}",
                       "suggestion": "verify the brand is covered in the report"})

    missing_claims = []
    for claim in claims_list:
        if claim.get("confidence") in {"high", "medium"} and claim.get("text"):
            claim_terms = {term.casefold() for term in _WORD.findall(str(claim["text"])) if len(term) > 1}
            overlap = sum(1 for term in claim_terms if term in combined.casefold())
            if claim_terms and overlap / len(claim_terms) < 0.5:
                missing_claims.append(str(claim.get("claim_id", "")))
    if missing_claims:
        issues.append({"section_id": "__report__", "category": "write",
                       "issue": f"key claims missing: {', '.join(x for x in missing_claims if x)}",
                       "suggestion": "ensure key evidence-backed claims appear in report text"})

    return {
        "passed": not issues,
        "issues": issues,
        "checked_sections": len(section_map),
        "missing_sections": missing_sections,
        "empty_sections": empty_sections,
        "invalid_evidence_ids": sorted(set(invalid_evidence_ids)),
        "missing_claims": missing_claims,
    }
