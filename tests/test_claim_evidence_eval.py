import json
import unittest
from pathlib import Path

from app.core.audit import normalize_claim_evidence_reviews


class ClaimEvidenceEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "claim_evidence_review_cases.json"
        cls.cases = json.loads(fixture.read_text(encoding="utf-8"))

    def test_manual_baseline_cases(self):
        for case in self.cases:
            with self.subTest(case=case["name"]):
                reviews, blockers = normalize_claim_evidence_reviews(
                    [case["model_review"]], [case["context"]]
                )
                self.assertEqual(reviews[0]["verdict"], case["expected_verdict"])
                self.assertEqual(bool(blockers), case["expected_blocking"])


if __name__ == "__main__":
    unittest.main()
