import unittest
from unittest.mock import patch

from app.main import SemanticReviewReplayBody, replay_report_semantic_review


class SemanticReviewReplayTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "id": "r_1", "query": "topic", "brands": ["A"], "focus": ["feature"],
            "claims": [], "evidence": [], "structured": {}, "quality_after": {},
        }

    @patch("app.main.db.save_report")
    @patch("app.main.llm_quality_review_with_evidence", return_value={"verdict": "pass"})
    @patch("app.main.db.get_report")
    def test_replay_is_read_only_by_default(self, get_report, review, save_report):
        get_report.return_value = dict(self.report)

        result = replay_report_semantic_review("r_1", SemanticReviewReplayBody())

        self.assertTrue(result["ok"])
        self.assertFalse(result["persisted"])
        save_report.assert_not_called()
        review.assert_called_once()

    @patch("app.main.db.save_report")
    @patch("app.main.llm_quality_review_with_evidence", return_value={"verdict": "rework"})
    @patch("app.main.db.get_report")
    def test_replay_persists_only_when_requested(self, get_report, review, save_report):
        report = dict(self.report)
        get_report.return_value = report

        result = replay_report_semantic_review("r_1", SemanticReviewReplayBody(persist=True))

        self.assertTrue(result["persisted"])
        self.assertEqual(report["audit_review"]["historical_semantic_review"]["verdict"], "rework")
        save_report.assert_called_once_with(report, task_id="")


if __name__ == "__main__":
    unittest.main()
