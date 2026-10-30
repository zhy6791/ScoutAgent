"""Local ReAct collector for one competitor.

The agent chooses the next search/fetch action, while this module owns all
side effects, Evidence construction, deduplication and stopping decisions.
It deliberately has no dependency on the orchestrator so it can be tested in
isolation and mirrored by the serverless API backend.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from app.core.credibility import freshness_days, score_evidence
from app.core.fetcher import domain_of, fetch_page
from app.core.llm import chat_json
from app.core.models import Evidence
from app.core.search import multi_search
from app.core.textquality import is_relevant_content
from app.core.config import get_settings


def _now() -> str:
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _source_type(url: str) -> str:
    domain = domain_of(url)
    if any(x in domain for x in ("zhihu.com", "zhihuishu.com")):
        return "zhihu"
    if "weibo.com" in domain:
        return "weibo"
    if "bilibili.com" in domain:
        return "bilibili"
    if any(x in domain for x in ("xiaohongshu.com", "xhslink.com")):
        return "xiaohongshu"
    if "douyin.com" in domain:
        return "douyin"
    if any(x in domain for x in ("gov.cn", "edu.cn")):
        return "official"
    path = urlparse(url).path.lower()
    if any(x in path for x in ("news", "article", "report")):
        return "news"
    return "web"


def _url_key(brand: str, url: str) -> Tuple[str, str]:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith(("utm_", "fbclid", "gclid"))
        ]
        normalized = urlunsplit((
            parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
            urlencode(sorted(query), doseq=True), "",
        ))
    except Exception:
        normalized = raw
    return str(brand or "").strip().casefold(), normalized


def _tokens(text: str) -> List[str]:
    import re
    out = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text or "")
    seen: Set[str] = set()
    return [x.lower() for x in out if not (x.lower() in seen or seen.add(x.lower()))]


@dataclass
class CollectState:
    brand: str
    category: str = ""
    focus: List[str] = field(default_factory=list)
    angles: List[str] = field(default_factory=list)
    evidences: List[Evidence] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    candidates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    seen_urls: Set[Tuple[str, str]] = field(default_factory=set)
    covered_dimensions: Set[str] = field(default_factory=set)
    round_index: int = 0
    search_calls: int = 0
    fetch_calls: int = 0
    no_progress_rounds: int = 0
    actions: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


def _dimension_coverage(state: CollectState) -> List[str]:
    blobs = [
        f"{e.title} {e.excerpt} {getattr(e, '_full_text', '')}"
        for e in state.evidences
    ]
    combined = " ".join(blobs).lower()
    missing = []
    for dimension in state.focus:
        terms = _tokens(dimension)
        matched = sum(1 for term in terms if term in combined)
        if terms and matched / len(terms) < 0.5:
            missing.append(dimension)
    state.covered_dimensions = set(state.focus) - set(missing)
    return missing


def stop_gate(
    state: CollectState,
    *,
    finish_requested: bool,
    max_rounds: int,
    max_search_calls: int,
    max_fetch_calls: int,
    max_seconds: float,
    no_progress_limit: int,
) -> Dict[str, Any]:
    """Return PASS, CONTINUE or INCOMPLETE without trusting LLM finish."""
    state.missing = _dimension_coverage(state)
    elapsed = time.monotonic() - state.started_at
    domains = {e.domain or domain_of(e.source_url) for e in state.evidences if e.source_url}
    missing = list(state.missing)
    if not state.evidences:
        missing.insert(0, f"{state.brand} 有效 Evidence")
    if len(domains) < 1 and state.evidences:
        missing.append("至少一个有效来源域名")
    missing = list(dict.fromkeys(missing))
    budget_exhausted = (
        state.round_index >= max_rounds
        or state.search_calls >= max_search_calls
        or state.fetch_calls >= max_fetch_calls
        or elapsed >= max_seconds
    )
    if finish_requested and not missing:
        return {"status": "complete", "missing": [], "reason": "达到最低证据与维度覆盖要求"}
    if state.no_progress_rounds >= no_progress_limit:
        return {"status": "incomplete", "missing": missing,
                "reason": f"连续 {no_progress_limit} 轮没有新增有效 Evidence"}
    if budget_exhausted:
        return {"status": "budget_exhausted", "missing": missing,
                "reason": "达到 ReAct 轮次、搜索、抓取或时间上限"}
    return {"status": "continue", "missing": missing,
            "reason": "仍有可执行的补采方向" if missing else "等待 Agent 请求结束"}


def _make_evidence(
    brand: str,
    collector: str,
    result: Dict[str, Any],
    page: Dict[str, Any],
) -> Optional[Evidence]:
    url = str(result.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return None
    text = str(page.get("text") or result.get("snippet") or "").strip()
    if not text or not is_relevant_content(text, [brand], brand):
        return None
    captured = page.get("captured_at") or result.get("captured_at") or _now()
    source_type = _source_type(url)
    evidence = Evidence(
        evidence_id=f"e_{__import__('uuid').uuid4().hex[:8]}",
        source_url=url,
        source_type=source_type,
        title=str(result.get("title") or brand),
        excerpt=text[:280],
        captured_at=captured,
        credibility=score_evidence(
            url, source_type, captured_at=captured,
            has_publish_date=bool(result.get("captured_at")),
            ok_fetch=bool(page.get("ok")), excerpt=text[:280],
        ),
        collected_by=collector,
        review_text=text[:4000],
        image_urls=[str(x.get("src")) for x in (page.get("images") or []) if x.get("src")][:3],
        brand=brand,
        domain=domain_of(url),
        freshness_days=freshness_days(captured),
    )
    evidence._full_text = text[:1500]  # type: ignore[attr-defined]
    return evidence


def _action_prompt(state: CollectState, observation: str) -> List[Dict[str, str]]:
    candidate_lines = []
    for item in list(state.candidates.values())[-12:]:
        candidate_lines.append(
            f"- {item.get('title','')} | {item.get('url','')} | {item.get('snippet','')[:180]}"
        )
    missing = ", ".join(state.missing) or "none known"
    return [
        {"role": "system", "content": (
            "You are a bounded research collector for one competitor. Choose exactly one next action "
            "as JSON. Allowed actions: search_web {query}, search_platform {query,site}, "
            "fetch_webpage {url}, finish_collection {reason}. Never invent evidence IDs or content. "
            "Prefer official, reputable and independent sources. Finish only when evidence supports "
            "the requested dimensions."
        )},
        {"role": "user", "content": (
            f"brand={state.brand}; category={state.category}; focus={state.focus}; angles={state.angles}\n"
            f"round={state.round_index}; searches={state.search_calls}; fetches={state.fetch_calls}; "
            f"evidence={len(state.evidences)}; missing={missing}\n"
            f"observation={observation}\nCandidates:\n{chr(10).join(candidate_lines)}\n"
            'Return only JSON, e.g. {"action":"search_web","query":"..."}. '
            "Do not use markdown."
        )},
    ]


async def run_collect_agent(
    task_id: str,
    brand: str,
    category: str,
    focus: List[str],
    angles: List[str],
    collector: str,
    fetch_limit: int,
    freshness: str,
    existing_urls: Set[Tuple[str, str]],
    *,
    fallback: Optional[Callable[[], Dict[str, Any]]] = None,
    max_rounds: int = 8,
    max_search_calls: int = 5,
    max_seconds: float = 180.0,
    no_progress_limit: int = 2,
) -> Dict[str, Any]:
    """Run one bounded ReAct loop. Tool failures are observations, not crashes."""
    state = CollectState(brand=brand, category=category, focus=list(focus), angles=list(angles))
    state.seen_urls = set(existing_urls)
    max_fetch_calls = max(1, int(fetch_limit))
    observation = "Start by searching the most important missing dimension."
    model = get_settings().zhipu_model_aux
    try:
        while state.round_index < max_rounds:
            state.round_index += 1
            before = len(state.evidences)
            before_candidates = len(state.candidates)
            response = await asyncio.to_thread(
                chat_json, _action_prompt(state, observation),
                temperature=0.2, max_tokens=900, model=model,
                purpose="collect_react_action",
                evidence_ids=[e.evidence_id for e in state.evidences[-8:]],
            )
            action = response if isinstance(response, dict) else {}
            kind = str(action.get("action") or action.get("type") or "").strip().lower()
            if kind not in {"search_web", "search_platform", "fetch_webpage", "finish_collection"}:
                observation = "Invalid action. Return one allowed action as JSON."
                state.actions.append({"round": state.round_index, "action": "invalid", "result": observation})
                state.no_progress_rounds += 1
                continue
            record: Dict[str, Any] = {"round": state.round_index, "action": kind}
            if kind in {"search_web", "search_platform"}:
                query = str(action.get("query") or "").strip()
                if not query:
                    query = f"{brand} {state.missing[0] if state.missing else state.angles[0] if state.angles else '产品'}"
                site = str(action.get("site") or "").strip() if kind == "search_platform" else None
                if state.search_calls >= max_search_calls:
                    observation = "搜索预算已用尽，请请求 finish_collection。"
                else:
                    state.search_calls += 1
                    results = await asyncio.to_thread(
                        multi_search, [query], num=10, site=site or None, freshness=freshness,
                    )
                    new_candidates = 0
                    for result in results:
                        url = str(result.get("url") or "").strip()
                        if url and url not in state.candidates:
                            state.candidates[url] = result
                            new_candidates += 1
                    record.update({"query": query, "site": site or "", "results": len(results), "new_candidates": new_candidates})
                    observation = f"Search returned {len(results)} results and {new_candidates} new candidates. Fetch a relevant URL next."
            elif kind == "fetch_webpage":
                url = str(action.get("url") or "").strip()
                result = state.candidates.get(url, {"url": url, "title": "", "snippet": ""})
                if not url.startswith(("http://", "https://")):
                    observation = "URL is invalid; choose a URL from candidates."
                elif state.fetch_calls >= max_fetch_calls:
                    observation = "Fetch budget is exhausted; request finish_collection."
                elif _url_key(brand, url) in state.seen_urls:
                    observation = "This URL was already used; choose a different candidate."
                else:
                    state.fetch_calls += 1
                    page = await asyncio.to_thread(fetch_page, url, fallback_snippet=result.get("snippet", ""))
                    evidence = _make_evidence(brand, collector, result, page)
                    if evidence is None:
                        observation = "The page was empty or not relevant to this brand; choose another candidate."
                    else:
                        state.evidences.append(evidence)
                        state.seen_urls.add(_url_key(brand, url))
                        state.images.extend([
                            {"src": page.get("og_image") or item.get("src"),
                             "alt": item.get("alt", ""), "title": evidence.title,
                             "source_url": url, "domain": evidence.domain,
                             "source_type": evidence.source_type, "brand": brand,
                             "evidence_id": evidence.evidence_id}
                            for item in ((page.get("images") or [])[:1])
                            if page.get("og_image") or item.get("src")
                        ])
                        observation = f"Added Evidence {evidence.evidence_id} from {evidence.domain}. Continue or finish when coverage is sufficient."
                    record.update({"url": url, "added_evidence": 1 if evidence else 0})
            else:
                gate = stop_gate(
                    state, finish_requested=True, max_rounds=max_rounds,
                    max_search_calls=max_search_calls, max_fetch_calls=max_fetch_calls,
                    max_seconds=max_seconds, no_progress_limit=no_progress_limit,
                )
                record["stop_check"] = gate
                state.actions.append(record)
                if gate["status"] in {"complete", "incomplete", "budget_exhausted"}:
                    return {"brand": brand, "evidences": state.evidences, "images": state.images,
                            "found": len(state.candidates), "status": gate["status"],
                            "missing": gate["missing"], "reason": gate["reason"],
                            "actions": state.actions, "search_calls": state.search_calls,
                            "fetch_calls": state.fetch_calls, "rounds": state.round_index}
                observation = "Cannot finish yet: " + ", ".join(gate["missing"] or [gate["reason"]])
            state.actions.append(record)
            made_progress = len(state.evidences) > before or len(state.candidates) > before_candidates
            state.no_progress_rounds = 0 if made_progress else state.no_progress_rounds + 1
            if state.no_progress_rounds >= no_progress_limit:
                gate = stop_gate(
                    state, finish_requested=False, max_rounds=max_rounds,
                    max_search_calls=max_search_calls, max_fetch_calls=max_fetch_calls,
                    max_seconds=max_seconds, no_progress_limit=no_progress_limit,
                )
                return {"brand": brand, "evidences": state.evidences, "images": state.images,
                        "found": len(state.candidates), "status": gate["status"],
                        "missing": gate["missing"], "reason": gate["reason"],
                        "actions": state.actions, "search_calls": state.search_calls,
                        "fetch_calls": state.fetch_calls, "rounds": state.round_index}
        gate = stop_gate(
            state, finish_requested=False, max_rounds=max_rounds,
            max_search_calls=max_search_calls, max_fetch_calls=max_fetch_calls,
            max_seconds=max_seconds, no_progress_limit=no_progress_limit,
        )
        return {"brand": brand, "evidences": state.evidences, "images": state.images,
                "found": len(state.candidates), "status": gate["status"],
                "missing": gate["missing"], "reason": gate["reason"],
                "actions": state.actions, "search_calls": state.search_calls,
                "fetch_calls": state.fetch_calls, "rounds": state.round_index}
    except Exception as exc:
        if fallback is not None:
            try:
                result = await asyncio.to_thread(fallback)
                return {**result, "brand": brand, "status": "fallback",
                        "reason": f"ReAct failed; fixed collector used: {exc}",
                        "actions": state.actions}
            except Exception as fallback_exc:
                return {"brand": brand, "evidences": state.evidences, "images": state.images,
                        "found": len(state.candidates), "status": "failed",
                        "missing": state.missing, "reason": f"{exc}; fallback failed: {fallback_exc}",
                        "actions": state.actions}
        return {"brand": brand, "evidences": state.evidences, "images": state.images,
                "found": len(state.candidates), "status": "failed",
                "missing": state.missing, "reason": str(exc), "actions": state.actions}
