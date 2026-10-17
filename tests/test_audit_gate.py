import unittest

from app.core.audit import (
    QualityReport,
    build_rework_envelopes,
    classify_review_issue,
    normalize_issue_routes,
    quality_gate_passed,
)


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

    def test_feedback_is_classified_to_the_expected_owner(self):
        self.assertEqual(classify_review_issue("Trae 证据不足，缺少第二独立来源"), "collect")
        self.assertEqual(classify_review_issue("论点逻辑没有建立功能与战略的映射"), "analyze")
        self.assertEqual(classify_review_issue("正文表达空泛、段落重复"), "write")

    def test_llm_feedback_can_dispatch_multiple_rework_owners(self):
        review = {
            "verdict": "rework",
            "issues": [
                "Trae 缺少第三方来源",
                "SWOT 论点逻辑未形成矩阵",
                "章节表达重复且空泛",
            ],
            "suggestions": [],
        }
        envelopes = build_rework_envelopes(
            QualityReport(), review, ["Trae", "Cursor"]
        )

        self.assertEqual({env.receiver for env in envelopes}, {"collect", "analyze", "write"})
        collect = next(env for env in envelopes if env.receiver == "collect")
        self.assertEqual(collect.payload["brands"], ["Trae"])

    def test_model_routes_are_normalized_and_invalid_categories_fall_back(self):
        routes = normalize_issue_routes([
            {"category": "collect", "issue": "缺少来源"},
            {"category": "unknown", "issue": "章节表达空泛"},
        ], [])

        self.assertEqual([route["category"] for route in routes], ["collect", "write"])


if __name__ == "__main__":
    unittest.main()
