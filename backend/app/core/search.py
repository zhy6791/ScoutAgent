"""Search providers used by the research pipeline.

Both providers return the same normalized shape:
title, url, snippet, source, captured_at.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional

import httpx

from app.core.config import get_settings

_BOCHA_ERR = {
    400: "Bocha request parameters are invalid.",
    401: "Bocha API key is invalid or missing.",
    403: "Bocha account balance is insufficient.",
    429: "Bocha request rate limit exceeded.",
    500: "Bocha search service error.",
}

_SERPAPI_ERR = {
    400: "SerpAPI request parameters are invalid.",
    401: "SerpAPI API key is invalid or missing.",
    403: "SerpAPI account access is forbidden or exhausted.",
    429: "SerpAPI request rate limit exceeded.",
}

_FRESHNESS_TBS = {
    "oneDay": "qdr:d",
    "oneWeek": "qdr:w",
    "oneMonth": "qdr:m",
    "oneYear": "qdr:y",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_relevant(query: str, title: str, snippet: str) -> bool:
    """Light relevance filter to drop obvious off-topic search results."""
    if not query:
        return True
    text = f"{title} {snippet}".lower()
    q = query.lower()

    english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", q)
    chinese_words = re.findall(r"[\u4e00-\u9fa5]{2,}", q)

    must_match = []
    if english_words:
        must_match.append(english_words[0])
    if chinese_words:
        must_match.append(chinese_words[0])

    if not must_match:
        return True

    for kw in must_match:
        if kw.lower() in text:
            return True

    all_kw = english_words + chinese_words
    hits = sum(1 for kw in all_kw if kw.lower() in text)
    return hits >= 2


def _site_query(query: str, site: Optional[str]) -> str:
    if not site:
        return query
    domains = [s.strip() for s in site.split("|") if s.strip()]
    if not domains:
        return query
    site_expr = " OR ".join(f"site:{d}" for d in domains)
    return f"({site_expr}) {query}"


def search_bocha(
    query: str,
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
) -> list[dict]:
    """Search web pages with Bocha Web Search."""
    settings = get_settings()
    if not settings.bocha_api_key:
        raise RuntimeError("BOCHA_API_KEY is not configured.")

    count = max(1, min(int(num), 50))
    fetch_count = min(count * 2, 50)
    payload = {
        "query": query,
        "summary": True,
        "freshness": freshness or "noLimit",
        "count": fetch_count,
    }
    if site:
        payload["include"] = site

    endpoint = f"{settings.bocha_base_url.rstrip('/')}/web-search"
    headers = {
        "Authorization": f"Bearer {settings.bocha_api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(
        connect=8, read=settings.search_timeout, write=5, pool=5
    )
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.post(endpoint, headers=headers, json=payload)
        if r.status_code != 200:
            msg = _BOCHA_ERR.get(
                r.status_code, f"Bocha returned HTTP {r.status_code}."
            )
            raise RuntimeError(msg)
        body = r.json()

    code = body.get("code")
    if code is not None and int(code) != 200:
        msg = _BOCHA_ERR.get(
            int(code), body.get("msg") or f"Bocha returned business code {code}."
        )
        raise RuntimeError(msg)

    data = body.get("data") or {}
    web_pages = (data.get("webPages") or {}).get("value") or []

    results: list[dict] = []
    for item in web_pages:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not url:
            continue
        snippet = (item.get("summary") or item.get("snippet") or "").strip()
        title = (item.get("name") or "").strip()
        if not _is_relevant(query, title, snippet):
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": item.get("siteName") or item.get("displayUrl", ""),
                "captured_at": item.get("datePublished") or _now(),
            }
        )
        if len(results) >= count:
            break
    return results


def search_serpapi(
    query: str,
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
) -> list[dict]:
    """Search Google results through SerpAPI."""
    settings = get_settings()
    if not settings.serpapi_key:
        raise RuntimeError("SERPAPI_KEY is not configured.")

    count = max(1, min(int(num), 100))
    fetch_count = min(count * 2, 100)
    params = {
        "engine": "google",
        "q": _site_query(query, site),
        "api_key": settings.serpapi_key,
        "num": fetch_count,
        "hl": "zh-cn",
    }
    tbs = _FRESHNESS_TBS.get(freshness or "")
    if tbs:
        params["tbs"] = tbs

    timeout = httpx.Timeout(
        connect=8, read=settings.search_timeout, write=5, pool=5
    )
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(settings.serpapi_base_url, params=params)
        if r.status_code != 200:
            msg = _SERPAPI_ERR.get(
                r.status_code, f"SerpAPI returned HTTP {r.status_code}."
            )
            raise RuntimeError(msg)
        body = r.json()

    if body.get("error"):
        raise RuntimeError(f"SerpAPI error: {body['error']}")

    results: list[dict] = []
    for item in body.get("organic_results") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("link", "")
        if not url:
            continue
        title = (item.get("title") or "").strip()
        snippet = (
            item.get("snippet")
            or item.get("snippet_highlighted_words")
            or item.get("rich_snippet", {})
            or ""
        )
        if isinstance(snippet, list):
            snippet = " ".join(str(x) for x in snippet)
        elif isinstance(snippet, dict):
            snippet = " ".join(str(x) for x in snippet.values())
        snippet = str(snippet).strip()
        if not _is_relevant(query, title, snippet):
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": item.get("source") or item.get("displayed_link", ""),
                "captured_at": item.get("date") or _now(),
            }
        )
        if len(results) >= count:
            break
    return results


def search(
    query: str,
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
) -> list[dict]:
    """Search entry point selected by SEARCH_PROVIDER."""
    provider = (get_settings().search_provider or "bocha").strip().lower()
    if provider in {"serpapi", "serp", "google"}:
        return search_serpapi(query, num=num, site=site, freshness=freshness)
    if provider == "bocha":
        return search_bocha(query, num=num, site=site, freshness=freshness)
    raise RuntimeError(f"Unsupported SEARCH_PROVIDER: {provider}")


def multi_search(
    queries: List[str],
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
) -> list[dict]:
    """Run several queries and deduplicate by URL. Failed queries are skipped."""
    seen: set[str] = set()
    out: list[dict] = []
    for q in queries:
        try:
            for r in search(q, num=num, site=site, freshness=freshness):
                url = r.get("url", "")
                key = url or r.get("title", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                r["query"] = q
                out.append(r)
        except Exception:
            continue
    return out
