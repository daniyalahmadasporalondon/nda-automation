import unittest
from unittest.mock import Mock

from nda_automation import telemetry
from nda_automation.ai_assessor import AIAssessorError
from nda_automation.review_comparison import (
    COMPARISON_SOURCE_AGREEMENT,
    COMPARISON_SOURCE_MOST_CONSERVATIVE,
    REVIEW_COMPARISON_MODE,
    ReviewComparisonError,
    build_review_comparison,
    compare_nda_reviews,
)


def _result(*clauses, review_mode="deterministic"):
    return {
        "review_mode": review_mode,
        "overall_status": "needs_review",
        "requirements_passed": 1,
        "requirements_needs_review": 0,
        "requirements_failed": 1,
        "review_engine_version": 3,
        "clauses": list(clauses),
    }


def _clause(clause_id, decision, *, paragraph_ids=None, issue_type="none", reason_code=""):
    return {
        "id": clause_id,
        "decision": decision,
        "passes": decision == "pass",
        "needs_review": decision == "review",
        "issue_type": issue_type,
        "reason_code": reason_code,
        "matched_paragraph_ids": paragraph_ids or [],
        "finding": f"{clause_id} {decision}",
    }


class ReviewComparisonTests(unittest.TestCase):
    def setUp(self):
        telemetry.reset()

    def test_build_review_comparison_detects_decision_and_evidence_deltas(self):
        deterministic = _result(
            _clause("mutuality", "pass", paragraph_ids=["p1"], reason_code="det_pass"),
            _clause("governing_law", "fail", paragraph_ids=["p2"], issue_type="present_but_wrong", reason_code="det_fail"),
        )
        ai_first = _result(
            _clause("mutuality", "pass", paragraph_ids=["p1"], reason_code="ai_pass"),
            _clause("governing_law", "pass", paragraph_ids=["p3"], issue_type="none", reason_code="ai_pass"),
            review_mode="ai_first_compat",
        )

        comparison = build_review_comparison(deterministic, ai_first, checked_at="2026-06-04T00:00:00+00:00")

        self.assertEqual(comparison["mode"], REVIEW_COMPARISON_MODE)
        self.assertEqual(comparison["summary"]["compared_clause_count"], 2)
        self.assertEqual(comparison["summary"]["disagreement_count"], 2)
        self.assertEqual(comparison["summary"]["decision_disagreement_clause_ids"], ["governing_law"])
        mutuality = next(item for item in comparison["clauses"] if item["clause_id"] == "mutuality")
        governing_law = next(item for item in comparison["clauses"] if item["clause_id"] == "governing_law")
        self.assertFalse(mutuality["decision_changed"])
        self.assertTrue(mutuality["reason_code_changed"])
        self.assertEqual(mutuality["final_verdict"]["source"], COMPARISON_SOURCE_AGREEMENT)
        self.assertTrue(governing_law["decision_changed"])
        self.assertTrue(governing_law["evidence_changed"])
        self.assertEqual(governing_law["evidence_delta"]["only_deterministic_paragraph_ids"], ["p2"])
        self.assertEqual(governing_law["evidence_delta"]["only_ai_first_paragraph_ids"], ["p3"])
        self.assertEqual(governing_law["final_verdict"]["decision"], "fail")
        self.assertEqual(governing_law["final_verdict"]["source"], COMPARISON_SOURCE_MOST_CONSERVATIVE)
        self.assertEqual(comparison["final_verdict"]["source"], COMPARISON_SOURCE_MOST_CONSERVATIVE)

    def test_compare_nda_reviews_runs_both_engines_and_records_telemetry(self):
        deterministic = Mock(return_value=_result(_clause("mutuality", "pass", paragraph_ids=["p1"])))
        ai_first = Mock(return_value=_result(_clause("mutuality", "pass", paragraph_ids=["p1"]), review_mode="ai_first_compat"))
        paragraphs = [{"id": "p1", "text": "Each party."}]

        comparison = compare_nda_reviews(
            "Each party.",
            paragraphs=paragraphs,
            deterministic_review_func=deterministic,
            ai_first_review_func=ai_first,
        )

        deterministic.assert_called_once_with("Each party.", paragraphs=paragraphs)
        ai_first.assert_called_once_with("Each party.", paragraphs=paragraphs)
        self.assertEqual(comparison["summary"]["disagreement_count"], 0)
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["review_comparison_requests"], 1)
        self.assertEqual(counters["review_comparison_completed"], 1)

    def test_compare_nda_reviews_wraps_ai_first_failure(self):
        deterministic = Mock(return_value=_result(_clause("mutuality", "pass")))
        ai_first = Mock(side_effect=AIAssessorError("no key"))

        with self.assertRaisesRegex(ReviewComparisonError, "AI-first comparison review failed"):
            compare_nda_reviews("NDA text", deterministic_review_func=deterministic, ai_first_review_func=ai_first)

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["review_comparison_requests"], 1)
        self.assertEqual(counters["review_comparison_ai_first_failures"], 1)


if __name__ == "__main__":
    unittest.main()
