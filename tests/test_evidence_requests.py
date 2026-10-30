import unittest

from app.core.orchestrator import _normalize_evidence_requests


class EvidenceRequestTests(unittest.TestCase):
    def test_requests_are_limited_to_known_brands_and_bounded_fields(self):
        result = _normalize_evidence_requests([
            {
                "brand": " acme ",
                "dimension": "Enterprise pricing",
                "queries": [" Acme enterprise price ", "", "Acme team plan", "ignored query"],
                "reason": "Not covered by current sources" * 20,
            },
            {"brand": "Unknown", "dimension": "pricing", "queries": ["unknown"]},
            {"brand": "Acme", "dimension": "", "queries": ["empty dimension"]},
        ], ["Acme"], ["pricing"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brand"], "Acme")
        self.assertEqual(result[0]["dimension"], "Enterprise pricing")
        self.assertEqual(result[0]["queries"], ["Acme enterprise price", "Acme team plan", "ignored query"])
        self.assertLessEqual(len(result[0]["reason"]), 300)


if __name__ == "__main__":
    unittest.main()
