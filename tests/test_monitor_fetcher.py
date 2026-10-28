import socket
import unittest

import httpx

from app.core.monitor_fetcher import fetch_monitor_page


def public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class MonitorFetcherTests(unittest.TestCase):
    def test_fetches_normalizes_and_sends_conditional_headers(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "etag": '"v2"',
                    "last-modified": "Sun, 25 Oct 2026 08:00:00 GMT",
                },
                content=(
                    "<html><title>Pricing</title><body><nav>Menu</nav><main>"
                    "<h1>Pro Plan</h1><p>Price is USD 20 per month for each user.</p>"
                    "</main></body></html>"
                ).encode(),
            )

        result = fetch_monitor_page(
            "https://example.com/pricing",
            etag='"v1"',
            last_modified="Sat, 24 Oct 2026 08:00:00 GMT",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.not_modified)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.etag, '"v2"')
        self.assertIn("USD 20", result.text)
        self.assertEqual(requests[0].headers["if-none-match"], '"v1"')
        self.assertTrue(result.content_hash)

    def test_304_is_success_without_new_content(self):
        result = fetch_monitor_page(
            "https://example.com/pricing",
            resolver=public_resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(304, headers={"etag": '"v1"'})),
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.not_modified)
        self.assertEqual(result.text, "")

    def test_redirect_to_private_address_is_blocked_before_second_request(self):
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        result = fetch_monitor_page(
            "https://example.com/start",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "blocked_address")
        self.assertEqual(call_count, 1)

    def test_private_source_and_oversized_response_are_rejected(self):
        private = fetch_monitor_page(
            "http://localhost:8000/admin",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret")),
        )
        oversized = fetch_monitor_page(
            "https://example.com/large",
            resolver=public_resolver,
            max_response_bytes=10,
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "100"},
                content=b"0123456789" * 10,
            )),
        )

        self.assertEqual(private.error_code, "blocked_address")
        self.assertEqual(oversized.error_code, "response_too_large")

    def test_unsupported_binary_response_is_not_treated_as_unchanged(self):
        result = fetch_monitor_page(
            "https://example.com/logo.png",
            resolver=public_resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"png"
            )),
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.not_modified)
        self.assertEqual(result.error_code, "unsupported_content_type")


if __name__ == "__main__":
    unittest.main()
