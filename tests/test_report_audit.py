import unittest

from app.core.report_audit import report_completeness_audit


class ReportAuditTests(unittest.TestCase):
    def test_passes_with_required_sections_and_valid_references(self):
        result = report_completeness_audit(
            {"summary": {"paragraphs": ["Acme overview [e_1]"]}},
            [{"claim_id": "c1", "text": "Acme overview", "confidence": "low"}],
            [{"evidence_id": "e_1"}], ["Acme"], ["summary"],
        )
        self.assertTrue(result["passed"])

    def test_reports_invalid_reference_and_empty_section(self):
        result = report_completeness_audit(
            {"summary": {"paragraphs": ["Acme [e_missing]"]}, "pricing": {"paragraphs": []}},
            [], [{"evidence_id": "e_1"}], ["Acme"], ["summary", "pricing"],
        )
        self.assertFalse(result["passed"])
        self.assertIn("e_missing", result["invalid_evidence_ids"])
        self.assertIn("pricing", result["empty_sections"])


if __name__ == "__main__":
    unittest.main()
