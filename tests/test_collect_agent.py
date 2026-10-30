import unittest
from unittest.mock import patch

from app.core.collect_agent import run_collect_agent, stop_gate, CollectState
from app.core.models import Evidence


def _evidence(brand="Acme", url="https://acme.example/pricing", text="Acme Pricing plan"):
    return Evidence(
        evidence_id="e_1", source_url=url, source_type="official", title="Pricing",
        excerpt=text, captured_at="2026-10-30", credibility=80,
        collected_by="L1-001", brand=brand, domain="acme.example",
    )


class CollectAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_fetch_and_finish_uses_program_built_evidence(self):
        actions = [
            {"action": "search_web", "query": "Acme pricing"},
            {"action": "fetch_webpage", "url": "https://acme.example/pricing"},
            {"action": "finish_collection", "reason": "coverage complete"},
        ]
        search_result = [{
            "url": "https://acme.example/pricing", "title": "Acme pricing",
            "snippet": "Acme Pricing details", "captured_at": "2026-10-30",
        }]
        page = {"ok": True, "text": "Acme pricing plan and price details", "images": []}
        observed_actions = []
        with patch("app.core.collect_agent.chat_json", side_effect=actions), \
                patch("app.core.collect_agent.multi_search", return_value=search_result), \
                patch("app.core.collect_agent.fetch_page", return_value=page), \
                patch("app.core.collect_agent.is_relevant_content", return_value=True):
            result = await run_collect_agent(
                "task_1", "Acme", "software", ["pricing"], ["pricing"], "L1-001",
                4, "oneYear", set(), max_rounds=5, on_action=observed_actions.append,
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["evidences"]), 1)
        self.assertEqual(result["evidences"][0].brand, "Acme")
        self.assertEqual(result["search_calls"], 1)
        self.assertEqual(result["fetch_calls"], 1)
        self.assertEqual([item["action"] for item in observed_actions], [
            "search_web", "fetch_webpage", "finish_collection",
        ])
        self.assertTrue(all("decision_ms" in item for item in observed_actions))

    async def test_finish_with_missing_dimension_continues_until_budget(self):
        with patch("app.core.collect_agent.chat_json", return_value={"action": "finish_collection"}):
            result = await run_collect_agent(
                "task_1", "Acme", "software", ["enterprise pricing"], ["overview"],
                "L1-001", 1, "oneYear", set(), max_rounds=2,
            )

        self.assertEqual(result["status"], "budget_exhausted")
        self.assertIn("Acme 有效 Evidence", result["missing"])

    async def test_no_progress_ends_incomplete(self):
        with patch("app.core.collect_agent.chat_json", return_value={"action": "search_web", "query": "Acme"}), \
                patch("app.core.collect_agent.multi_search", return_value=[]):
            result = await run_collect_agent(
                "task_1", "Acme", "software", [], ["overview"], "L1-001",
                2, "oneYear", set(), max_rounds=6, no_progress_limit=2,
            )

        self.assertEqual(result["status"], "incomplete")
        self.assertIn("连续 2 轮", result["reason"])

    async def test_agent_failure_falls_back_to_fixed_collector(self):
        fallback = {"evidences": [_evidence()], "images": [], "found": 1}
        with patch("app.core.collect_agent.chat_json", side_effect=RuntimeError("model unavailable")):
            result = await run_collect_agent(
                "task_1", "Acme", "software", [], ["overview"], "L1-001",
                2, "oneYear", set(), fallback=lambda: fallback,
            )

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["evidences"], fallback["evidences"])

    def test_stop_gate_passes_only_when_requested_and_minimums_met(self):
        state = CollectState(brand="Acme", focus=["pricing"])
        state.evidences.append(_evidence())
        result = stop_gate(
            state, finish_requested=True, max_rounds=8, max_search_calls=5,
            max_fetch_calls=6, max_seconds=180, no_progress_limit=2,
        )
        self.assertEqual(result["status"], "complete")


if __name__ == "__main__":
    unittest.main()
