import unittest

from nda_automation.review_state import (
    aggregate_review_state,
    clause_needs_review,
    clause_passes,
    clause_review_state,
    result_requires_human_review,
    review_state_from_result,
)


class ReviewStateTests(unittest.TestCase):
    def test_unknown_empty_or_missing_clause_decision_fails_safe_to_review(self):
        for clause in (
            {},
            {"id": "mutuality", "decision": ""},
            {"id": "mutuality", "decision": "unknown", "passes": True},
        ):
            with self.subTest(clause=clause):
                state = clause_review_state(clause)

                self.assertEqual(state["decision"], "review")
                self.assertEqual(state["state"], "review")
                self.assertTrue(state["blocks_send"])
                self.assertTrue(clause_needs_review(clause))
                self.assertFalse(clause_passes(clause))

    def test_aggregate_review_state_does_not_count_unknown_decision_as_pass(self):
        state = aggregate_review_state([{"id": "mutuality", "decision": "unknown", "passes": True}])

        self.assertEqual(state["state"], "review")
        self.assertEqual(state["counts"]["pass"], 0)
        self.assertEqual(state["counts"]["review"], 1)
        self.assertEqual(state["clause_ids"]["review"], ["mutuality"])

    def test_stale_nested_review_state_cannot_override_unknown_decision(self):
        clause = {
            "id": "mutuality",
            "decision": "unknown",
            "passes": True,
            "review_state": {"state": "pass", "decision": "pass"},
        }

        state = aggregate_review_state([clause])

        self.assertEqual(state["state"], "review")
        self.assertEqual(state["counts"]["review"], 1)
        self.assertFalse(clause_passes(clause))
        self.assertTrue(clause_needs_review(clause))

    def test_review_result_derives_state_from_clauses_before_stale_summary(self):
        review_result = {
            "review_state": {"state": "pass", "requires_human_review": False, "blocks_send": False},
            "clauses": [{
                "id": "mutuality",
                "decision": "unknown",
                "passes": True,
                "review_state": {"state": "pass", "decision": "pass"},
            }],
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "review")
        self.assertTrue(result_requires_human_review(review_result))

    def test_legacy_passes_signal_can_pass_only_without_decision_field(self):
        state = clause_review_state({"id": "mutuality", "passes": True})

        self.assertEqual(state["decision"], "pass")
        self.assertEqual(state["state"], "pass")


if __name__ == "__main__":
    unittest.main()
