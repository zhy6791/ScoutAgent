import unittest
from unittest.mock import patch

from app.core.change_classifier import classify_change
from app.core.change_detector import ChangeDetection


def detection(material=True):
    return ChangeDetection(
        changed=True,
        material=material,
        change_ratio=0.2,
        added_chars=20,
        deleted_chars=20,
        changed_lines=2,
        keyword_hits=["pricing"] if material else [],
        diff_summary="-USD 20\n+USD 25",
        before_excerpt="USD 20",
        after_excerpt="USD 25",
    )


class ChangeClassifierTests(unittest.TestCase):
    @patch("app.core.change_classifier.chat_json")
    def test_valid_result_is_cleaned_and_usage_is_returned(self, mock_chat_json):
        def response(*args, **kwargs):
            kwargs["usage_out"].update({"total_tokens": 321})
            return {
                "material": True,
                "change_type": "PRICING",
                "severity": "HIGH",
                "summary": "企业版月费由 20 美元调整为 25 美元。",
                "facts": ["月费从 20 美元变为 25 美元"] * 8,
            }

        mock_chat_json.side_effect = response
        result = classify_change("Trae", "pricing", "https://example.com", detection())

        self.assertEqual(result.change_type, "pricing")
        self.assertEqual(result.severity, "high")
        self.assertEqual(result.llm_tokens, 321)
        self.assertEqual(len(result.facts), 5)
        self.assertEqual(mock_chat_json.call_args.kwargs["max_tokens"], 1200)

    @patch("app.core.change_classifier.chat_json")
    def test_minor_change_skips_llm(self, mock_chat_json):
        result = classify_change("Trae", "official", "https://example.com", detection(False))

        mock_chat_json.assert_not_called()
        self.assertEqual(result.llm_status, "skipped")
        self.assertFalse(result.material)
        self.assertEqual(result.severity, "info")

    @patch("app.core.change_classifier.chat_json", return_value=None)
    def test_invalid_llm_result_preserves_deterministic_change(self, mock_chat_json):
        result = classify_change("Trae", "pricing", "https://example.com", detection())

        self.assertTrue(result.material)
        self.assertEqual(result.change_type, "pricing")
        self.assertEqual(result.severity, "medium")
        self.assertEqual(result.llm_status, "failed")

    @patch("app.core.change_classifier.chat_json")
    def test_string_false_is_not_coerced_to_true_and_failed_json_keeps_usage(self, mock_chat_json):
        def false_response(*args, **kwargs):
            kwargs["usage_out"].update({"total_tokens": 111})
            return {
                "material": "false",
                "change_type": "pricing",
                "severity": "high",
                "summary": "变化不影响实际定价。",
                "facts": [],
            }

        mock_chat_json.side_effect = false_response
        false_result = classify_change("Trae", "pricing", "https://example.com", detection())
        self.assertFalse(false_result.material)
        self.assertEqual(false_result.severity, "info")
        self.assertEqual(false_result.llm_tokens, 111)

        def invalid_response(*args, **kwargs):
            kwargs["usage_out"].update({"total_tokens": 222})
            return None

        mock_chat_json.side_effect = invalid_response
        invalid_result = classify_change("Trae", "pricing", "https://example.com", detection())
        self.assertEqual(invalid_result.llm_status, "failed")
        self.assertEqual(invalid_result.llm_tokens, 222)


if __name__ == "__main__":
    unittest.main()
