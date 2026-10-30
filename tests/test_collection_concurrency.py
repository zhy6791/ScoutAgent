import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from app.core.orchestrator import (
    _collect_brand,
    _collect_brand_with_limit,
    _collection_url_key,
    _search_sentiment_platform_with_limit,
)


class CollectionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def test_url_key_ignores_fragments_and_common_tracking_parameters(self):
        self.assertEqual(
            _collection_url_key(" A ", "HTTPS://Example.com/page?utm_source=x&b=2&a=1#section"),
            _collection_url_key("a", "https://example.com/page?a=1&b=2"),
        )

    def test_same_url_can_be_attributed_to_each_relevant_brand(self):
        shared_urls = set()
        search_result = [{
            "url": "https://example.com/compare",
            "title": "Product comparison",
            "snippet": "Comparison page",
            "captured_at": "2026-10-28T00:00:00Z",
        }]
        page = {"ok": True, "text": "Comparison page covering products A and B."}

        with patch("app.core.orchestrator.multi_search", return_value=search_result), \
                patch("app.core.orchestrator.fetch_page", return_value=page), \
                patch("app.core.orchestrator.is_relevant_content", return_value=True):
            result_a = _collect_brand("A", ["overview"], "L1-001", 1, "oneYear", shared_urls)
            result_b = _collect_brand("B", ["overview"], "L1-001", 1, "oneYear", shared_urls)

        self.assertEqual(result_a["evidences"][0].brand, "A")
        self.assertEqual(result_b["evidences"][0].brand, "B")
        self.assertEqual(shared_urls, set())

    async def test_brand_collections_are_bounded_and_failures_are_isolated(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def collect(brand, *_args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                if brand == "broken":
                    raise RuntimeError("provider unavailable")
                return {"brand": brand}
            finally:
                with lock:
                    active -= 1

        semaphore = asyncio.Semaphore(2)
        with patch("app.core.orchestrator._collect_brand", side_effect=collect):
            results = await asyncio.gather(*[
                _collect_brand_with_limit(
                    "task_1", brand, ["pricing"], "L1-001", 4, "oneYear", set(), semaphore,
                )
                for brand in ("A", "B", "broken", "C")
            ])

        self.assertEqual(peak, 2)
        by_brand = {brand: (result, error) for brand, result, error in results}
        self.assertEqual(by_brand["A"], ({"brand": "A"}, ""))
        self.assertEqual(by_brand["B"], ({"brand": "B"}, ""))
        self.assertIsNone(by_brand["broken"][0])
        self.assertEqual(by_brand["broken"][1], "provider unavailable")
        self.assertEqual(by_brand["C"], ({"brand": "C"}, ""))

    async def test_sentiment_platform_searches_are_bounded_and_failures_are_isolated(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def search(*_args, **kwargs):
            nonlocal active, peak
            site = kwargs.get("site")
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                if site == "broken.example":
                    raise RuntimeError("search unavailable")
                return [{"url": f"https://{site}/review", "snippet": "review"}]
            finally:
                with lock:
                    active -= 1

        semaphore = asyncio.Semaphore(2)
        with patch("app.core.orchestrator.multi_search", side_effect=search):
            results = await asyncio.gather(*[
                _search_sentiment_platform_with_limit(
                    "task_1", "Brand", platform, site, " category", "oneYear", semaphore,
                )
                for platform, site in (
                    ("douyin", "douyin.example"),
                    ("weibo", "weibo.example"),
                    ("zhihu", "broken.example"),
                    ("bilibili", "bilibili.example"),
                )
            ])

        self.assertEqual(peak, 2)
        by_platform = {platform: (items, error) for _, platform, items, error in results}
        self.assertEqual(len(by_platform["douyin"][0]), 1)
        self.assertEqual(by_platform["douyin"][1], "")
        self.assertEqual(by_platform["zhihu"], ([], "search unavailable"))
        self.assertEqual(len(by_platform["bilibili"][0]), 1)


if __name__ == "__main__":
    unittest.main()
