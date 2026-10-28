import unittest

from app.core.change_detector import detect_change


class ChangeDetectorTests(unittest.TestCase):
    def test_identical_and_volatile_timestamp_changes_are_ignored(self):
        identical = detect_change("Same body", "Same body")
        timestamp = detect_change(
            "Product body\nLast updated: 2026-10-24",
            "Product body\nLast updated: 2026-10-25",
        )

        self.assertFalse(identical.changed)
        self.assertFalse(timestamp.changed)

    def test_small_non_business_change_is_not_material(self):
        result = detect_change(
            "Welcome to our website.",
            "Welcome to our website!",
            min_changed_chars=40,
            min_change_ratio=0.01,
        )

        self.assertTrue(result.changed)
        self.assertFalse(result.material)
        self.assertEqual(result.keyword_hits, [])

    def test_price_change_is_material_even_when_short(self):
        result = detect_change(
            "Pro plan price: USD 20 /month",
            "Pro plan price: USD 25 /month",
            min_changed_chars=100,
            min_change_ratio=0.5,
        )

        self.assertTrue(result.material)
        self.assertIn("pricing", result.keyword_hits)
        self.assertIn("USD 20", result.before_excerpt)
        self.assertIn("USD 25", result.after_excerpt)

    def test_large_change_is_material_and_diff_is_bounded(self):
        result = detect_change(
            "Old paragraph. " * 100,
            "Completely new market expansion paragraph. " * 100,
            min_changed_chars=40,
            min_change_ratio=0.01,
            max_diff_chars=500,
        )

        self.assertTrue(result.material)
        self.assertLessEqual(len(result.diff_summary), 530)


if __name__ == "__main__":
    unittest.main()
