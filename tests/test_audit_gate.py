import unittest
from unittest.mock import patch

from app.core.audit import (
    QualityReport,
    accept_write_constraints,
    build_rework_envelopes,
    classify_review_issue,
    llm_quality_review,
    normalize_issue_routes,
    partition_rework_envelopes,
    quality_gate_passed,
)
from app.core.orchestrator import MODE_CONFIG


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

    def test_advisories_do_not_block_or_schedule_rework(self):
        review = {
            "verdict": "pass",
            "issues": [],
            "advisories": ["可以继续补充更多背景材料"],
        }

        self.assertTrue(quality_gate_passed(QualityReport(), review))
        self.assertEqual(build_rework_envelopes(QualityReport(), review), [])

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

    def test_collect_only_rework_forces_analysis_after_collection(self):
        review = {
            "verdict": "rework",
            "issues": ["核心量化结论缺少可核验来源"],
            "issue_routes": [{
                "category": "collect",
                "issue": "核心量化结论缺少可核验来源",
                "suggestion": "补充原始来源后重新计算结论",
            }],
            "suggestions": ["补充原始来源后重新计算结论"],
        }
        envelopes = build_rework_envelopes(QualityReport(), review, ["Trae"])
        self.assertEqual([env.receiver for env in envelopes], ["collect", "analyze"])
        self.assertEqual(envelopes[1].payload["source"], "collect_rework")
        self.assertIn("补充原始来源后重新计算结论", envelopes[1].payload["feedback"])

    def test_existing_analyze_route_is_not_duplicated(self):
        review = {
            "verdict": "rework",
            "issues": ["缺少来源", "论点逻辑不完整"],
            "issue_routes": [
                {"category": "collect", "issue": "缺少来源"},
                {"category": "analyze", "issue": "论点逻辑不完整"},
            ],
        }
        envelopes = build_rework_envelopes(QualityReport(), review, ["Trae"])
        self.assertEqual([env.receiver for env in envelopes], ["collect", "analyze"])

    def test_write_only_feedback_passes_analysis_gate_without_rework(self):
        review = {
            "verdict": "rework",
            "issues": ["正文段落重复，表达需要压缩"],
            "issue_routes": [{
                "category": "write",
                "issue": "正文段落重复，表达需要压缩",
                "suggestion": "写作时合并重复段落",
            }],
        }
        envelopes = build_rework_envelopes(QualityReport(), review, ["Trae"])
        analysis_envelopes, write_envelopes = partition_rework_envelopes(envelopes)

        self.assertEqual(analysis_envelopes, [])
        self.assertEqual([env.receiver for env in write_envelopes], ["write"])
        accepted = accept_write_constraints(review)
        self.assertTrue(quality_gate_passed(QualityReport(), accepted))
        self.assertEqual(accepted["original_verdict"], "rework")
        self.assertEqual(accepted["writing_constraints"], [
            "正文段落重复，表达需要压缩", "写作时合并重复段落",
        ])

    def test_mixed_feedback_keeps_analysis_rework_and_write_constraints(self):
        review = {
            "verdict": "rework",
            "issues": ["缺少来源", "正文表达重复"],
            "issue_routes": [
                {"category": "collect", "issue": "缺少来源"},
                {"category": "write", "issue": "正文表达重复"},
            ],
        }
        envelopes = build_rework_envelopes(QualityReport(), review, ["Trae"])
        analysis_envelopes, write_envelopes = partition_rework_envelopes(envelopes)

        self.assertEqual([env.receiver for env in analysis_envelopes], ["collect", "analyze"])
        self.assertEqual([env.receiver for env in write_envelopes], ["write"])

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

    @patch("app.core.llm.chat_json")
    def test_llm_review_passes_when_only_advisories_exist(self, mock_chat_json):
        mock_chat_json.return_value = {
            "verdict": "rework",  # The normalized verdict follows blockers, not this raw value.
            "scores": {"证据充分性": 82, "维度完整性": 90},
            "review": "核心结论可用，仍有一般改进空间。",
            "blocking_issues": [],
            "advisories": ["可增加更多背景材料"],
        }

        review = llm_quality_review("主题", ["A"], ["功能"], [], {}, QualityReport())

        self.assertEqual(review["verdict"], "pass")
        self.assertEqual(review["issues"], [])
        self.assertEqual(review["advisories"], ["可增加更多背景材料"])
        self.assertEqual(mock_chat_json.call_args.kwargs["max_tokens"], 6000)

    @patch("app.core.llm.chat_json")
    def test_llm_review_routes_only_blocking_issues_to_rework(self, mock_chat_json):
        mock_chat_json.return_value = {
            "verdict": "pass",
            "scores": {"证据充分性": 55},
            "review": "存在一个阻断问题。",
            "blocking_issues": [{
                "category": "collect",
                "issue": "核心量化结论没有可核验来源",
                "suggestion": "补充原始来源或降低结论强度",
            }],
            "advisories": ["标题可以更简洁"],
        }

        review = llm_quality_review("主题", ["A"], ["功能"], [], {}, QualityReport())
        envelopes = build_rework_envelopes(QualityReport(), review, ["A"])

        self.assertEqual(review["verdict"], "rework")
        self.assertEqual(review["issues"], ["核心量化结论没有可核验来源"])
        self.assertEqual([env.receiver for env in envelopes], ["collect", "analyze"])

    @patch("app.core.llm.chat_json", return_value=None)
    def test_invalid_llm_review_fails_closed_after_one_retry(self, mock_chat_json):
        review = llm_quality_review("主题", ["A"], ["功能"], [], {}, QualityReport())

        self.assertEqual(mock_chat_json.call_count, 2)
        self.assertEqual(review["verdict"], "rework")
        self.assertTrue(review["review_failed"])
        self.assertFalse(quality_gate_passed(QualityReport(), review))

    def test_all_modes_allow_three_rework_rounds(self):
        self.assertEqual(
            {mode: config["rework_rounds"] for mode, config in MODE_CONFIG.items()},
            {"quick": 3, "deep": 3, "expert": 3},
        )


if __name__ == "__main__":
    unittest.main()
