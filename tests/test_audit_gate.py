import unittest

from app.core.audit import QualityReport, build_rework_envelopes, quality_gate_passed


class QualityGateTests(unittest.TestCase):
    def test_gate_requires_rules_and_llm_to_pass(self):
        clean = QualityReport()
        passing_review = {"verdict": "pass", "issues": []}

        self.assertTrue(quality_gate_passed(clean, passing_review))
        self.assertFalse(quality_gate_passed(clean, {"verdict": "rework", "issues": []}))
        self.assertFalse(quality_gate_passed(clean, {"verdict": "pass", "issues": ["缺少证据"]}))
        self.assertFalse(quality_gate_passed(
            QualityReport(issues=[{"target": "schema", "reason": "结构不完整"}]), passing_review
        ))

    def test_clean_gate_does_not_schedule_rework(self):
        envelopes = build_rework_envelopes(
            QualityReport(), {"verdict": "pass", "issues": [], "suggestions": []}
        )
        self.assertEqual(envelopes, [])

    def test_llm_only_failure_schedules_analysis_with_latest_feedback(self):
        envelopes = build_rework_envelopes(
            QualityReport(),
            {"verdict": "rework", "issues": ["缺少竞品间的功能差异"],
             "suggestions": ["补充可核验的功能对比"]},
        )

        self.assertEqual([env.receiver for env in envelopes], ["analyze"])
        self.assertIn("缺少竞品间的功能差异", [item["reason"] for item in envelopes[0].issues])
        self.assertEqual(envelopes[0].payload["suggestions"], ["补充可核验的功能对比"])

    def test_rule_collection_failure_also_schedules_analysis(self):
        quality = QualityReport(issues=[{
            "target": "brand:竞品A", "reason": "该竞品证据不足"
        }])
        envelopes = build_rework_envelopes(quality, {"verdict": "pass", "issues": []})

        self.assertEqual({env.receiver for env in envelopes}, {"collect", "analyze"})

    def test_rule_dimension_failure_schedules_analysis(self):
        quality = QualityReport(issues=[{
            "target": "dimension:定价", "reason": "缺少定价分析"
        }])
        envelopes = build_rework_envelopes(quality, {"verdict": "pass", "issues": []})

        self.assertEqual([env.receiver for env in envelopes], ["analyze"])


if __name__ == "__main__":
    unittest.main()
