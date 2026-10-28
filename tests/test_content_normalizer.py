import unittest

from app.core.content_normalizer import normalize_html, normalize_text


class ContentNormalizerTests(unittest.TestCase):
    def test_layout_noise_does_not_change_content_hash(self):
        first = normalize_html("""
            <html><head><title>Trae Pricing</title><script>window.now=1</script></head>
            <body><nav>Home Products</nav><main>
              <h1>Trae Pro</h1><p>团队版价格为每人每月 20 美元。</p>
            </main><div class="cookie-consent">Accept all cookies</div></body></html>
        """)
        second = normalize_html("""
            <html><head><title>Trae Pricing</title><script>window.now=2</script></head>
            <body><nav>New navigation</nav><main>
              <h1>Trae Pro</h1><p>团队版价格为每人每月 20 美元。</p>
            </main><div id="cookie-banner">Reject cookies</div></body></html>
        """)

        self.assertEqual(first.text, second.text)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertIn("20 美元", first.text)
        self.assertNotIn("cookies", first.text.lower())

    def test_text_normalization_is_stable_and_deduplicates_adjacent_lines(self):
        self.assertEqual(
            normalize_text("ＡＩ  助手\r\nＡＩ 助手\r\n\u200b新增功能"),
            "AI 助手\n新增功能",
        )

    def test_title_is_part_of_the_stable_content(self):
        old = normalize_html("<html><title>Starter Plan</title><body><main>Same body</main></body></html>")
        new = normalize_html("<html><title>Pro Plan</title><body><main>Same body</main></body></html>")

        self.assertIn("Starter Plan", old.text)
        self.assertNotEqual(old.content_hash, new.content_hash)

    def test_nested_noise_container_can_be_removed_safely(self):
        content = normalize_html(
            "<html><body><div class='cookie-banner'><span>Accept cookies</span></div>"
            "<main><p>Product content remains available.</p></main></body></html>"
        )

        self.assertEqual(content.text, "Product content remains available.")


if __name__ == "__main__":
    unittest.main()
