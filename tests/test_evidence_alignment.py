import unittest
from collections import Counter
from unittest.mock import patch

from app.core.models import Evidence
from app.core.orchestrator import (
    ANALYSIS_EVIDENCE_LIMIT,
    STRUCTURED_EVIDENCE_LIMIT,
    _analyze,
    _claim_evidence_matches_brands,
    _evidence_digest,
    _fallback_claims,
    _filter_structured_evidence_by_brand,
    _select_digest_evidences,
)


def evidence(eid: str, brand: str, index: int = 0) -> Evidence:
    return Evidence(
        evidence_id=eid,
        source_url=f"https://{brand.lower().replace(' ', '-')}.example/{index}",
        source_type="official",
        title=f"{brand} evidence {index}",
        excerpt=f"Evidence about {brand}",
        captured_at="2026-10-18",
        credibility=80,
        collected_by="L1-001",
        brand=brand,
    )


class EvidenceAlignmentTests(unittest.TestCase):
    def test_analysis_limits_are_increased(self):
        self.assertEqual(ANALYSIS_EVIDENCE_LIMIT, 72)
        self.assertEqual(STRUCTURED_EVIDENCE_LIMIT, 60)

    def test_digest_balances_brands_and_labels_each_evidence(self):
        brands = ["GitHub Copilot", "Cursor", "JetBrains AI Assistant", "Trae"]
        evidences = [
            evidence(f"e_{brand}_{index}", brand, index)
            for brand in brands
            for index in range(30)
        ]

        selected = _select_digest_evidences(evidences, ANALYSIS_EVIDENCE_LIMIT)
        counts = Counter(item.brand for item in selected)
        digest = _evidence_digest(evidences)

        self.assertEqual(counts, Counter({brand: 18 for brand in brands}))
        for brand in brands:
            self.assertIn(f"品牌:{brand}", digest)
            self.assertIn(f"e_{brand}_29", {item.evidence_id for item in selected})

    def test_claim_requires_evidence_for_every_mentioned_brand(self):
        brands = ["GitHub Copilot", "Cursor", "Trae"]
        brand_by_id = {"e_cursor": "Cursor", "e_trae": "Trae"}

        self.assertFalse(_claim_evidence_matches_brands(
            "Trae 应采用低价定位", ["Trae"], ["e_cursor"], brands, brand_by_id
        ))
        self.assertFalse(_claim_evidence_matches_brands(
            "Trae 相比 Cursor 更便宜", ["Trae", "Cursor"], ["e_trae"], brands, brand_by_id
        ))
        self.assertTrue(_claim_evidence_matches_brands(
            "Trae 相比 Cursor 更便宜", ["Trae", "Cursor"],
            ["e_trae", "e_cursor"], brands, brand_by_id
        ))
        self.assertFalse(_claim_evidence_matches_brands(
            "该产品更便宜", [], ["e_trae"], brands, brand_by_id
        ))

    @patch("app.core.orchestrator.chat_json")
    def test_analyzer_drops_claim_with_competitor_only_evidence(self, mock_chat_json):
        evidences = [evidence("e_cursor", "Cursor"), evidence("e_trae", "Trae")]
        mock_chat_json.return_value = {
            "claims": [
                {
                    "text": "Trae 已形成明确的低价优势",
                    "brands": ["Trae"],
                    "field": "pricing_model",
                    "evidence_ids": ["e_cursor"],
                    "author": "L2-001",
                },
                {
                    "text": "Trae 的官方定价体现低价策略",
                    "brands": ["Trae"],
                    "field": "pricing_model",
                    "evidence_ids": ["e_trae"],
                    "author": "L2-001",
                },
            ]
        }

        result = _analyze(
            "分析 Trae", ["Cursor", "Trae"], ["定价"], evidences, ["L2-001"]
        )

        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["claims"][0]["evidence_ids"], ["e_trae"])

    def test_fallback_selects_a_representative_from_each_brand(self):
        ids = ["e_cursor_1", "e_cursor_2", "e_trae_1"]
        brand_by_id = {
            "e_cursor_1": "Cursor",
            "e_cursor_2": "Cursor",
            "e_trae_1": "Trae",
        }
        claims = _fallback_claims(
            ["Cursor", "Trae"], ids,
            {eid: f"{eid}.example" for eid in ids}, ["L2-001"], brand_by_id
        )

        self.assertIn("e_cursor_1", claims[0]["evidence_ids"])
        self.assertIn("e_trae_1", claims[0]["evidence_ids"])

    def test_structured_evidence_from_other_brand_is_removed(self):
        structured = {
            "feature_tree": [{
                "brand": "Trae",
                "modules": [{
                    "sub_features": [{"evidence_ids": ["e_trae", "e_cursor"]}]
                }],
            }],
            "pricing_model": [],
            "user_persona": [],
        }

        _filter_structured_evidence_by_brand(
            structured, {"e_trae": "Trae", "e_cursor": "Cursor"}
        )

        self.assertEqual(
            structured["feature_tree"][0]["modules"][0]["sub_features"][0]["evidence_ids"],
            ["e_trae"],
        )


if __name__ == "__main__":
    unittest.main()
