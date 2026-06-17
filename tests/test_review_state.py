import unittest

from nda_automation.decision_arbiter import SEMANTIC_REVIEW_THRESHOLD, deterministic_decision
from nda_automation.review_state import (
    _normalize_clause_decision,
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

    def test_review_state_normalizer_agrees_with_decision_arbiter(self):
        # The reconciliation: review_state._normalize_clause_decision is no longer
        # an independent re-derivation -- it delegates to the canonical
        # decision_arbiter.deterministic_decision when there is no explicit
        # decision. Pin that the two stay in lockstep across the verdict matrix,
        # including the confidence < threshold rule (the case review_state used to
        # disagree on by ignoring confidence).
        below = round(SEMANTIC_REVIEW_THRESHOLD - 0.15, 2)
        at_or_above = round(SEMANTIC_REVIEW_THRESHOLD + 0.15, 2)
        clauses = [
            {"passes": True, "confidence": below},
            {"passes": True, "confidence": at_or_above},
            {"passes": True, "semantic_confidence": below},
            {"passes": True},
            {"passes": False},
            {"needs_review": True},
            {},
        ]
        for clause in clauses:
            with self.subTest(clause=clause):
                self.assertEqual(
                    _normalize_clause_decision(clause),
                    deterministic_decision(clause),
                )

    def test_passing_clause_below_confidence_threshold_no_longer_false_clears(self):
        # The concrete bug: a clause that "passes" but only at low semantic
        # confidence must surface for human review, not silently clear the gate.
        clause = {"id": "governing_law", "passes": True, "confidence": SEMANTIC_REVIEW_THRESHOLD - 0.15}

        state = clause_review_state(clause)

        self.assertEqual(state["decision"], "review")
        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertFalse(clause_passes(clause))
        self.assertTrue(clause_needs_review(clause))

    def test_truncated_all_pass_result_still_blocks_send(self):
        # Document-level gate: when the AI packet was truncated, the AI never saw
        # part of the text, so an all-pass clause set must still block. Re-deriving
        # state from the (all-pass) clauses must not silently drop that escalation.
        review_result = {
            "clauses": [{"id": "c1", "decision": "pass"}, {"id": "c2", "decision": "pass"}],
            "truncation": {"truncated": True, "message": "3 paragraphs were not reviewed."},
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertTrue(state["requires_human_review"])
        self.assertTrue(state["truncation_forced_review"])
        self.assertEqual(state["truncation_reason"], "3 paragraphs were not reviewed.")
        self.assertTrue(result_requires_human_review(review_result))

    def test_truncation_marker_never_downgrades_a_check(self):
        # A document-level fail already blocks; truncation must not soften it to
        # review, but must still force human review / block send.
        review_result = {
            "clauses": [{"id": "c1", "decision": "fail"}],
            "truncation": {"truncated": True, "message": "clipped"},
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "check")
        self.assertTrue(state["blocks_send"])
        self.assertTrue(state["requires_human_review"])

    def test_review_state_nested_truncation_marker_is_honored(self):
        # The escalation may also arrive as a marker on the nested review_state
        # rather than a top-level truncation block.
        review_result = {
            "clauses": [{"id": "c1", "decision": "pass"}],
            "review_state": {"truncation_forced_review": True, "truncation_reason": "clipped paragraph"},
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertEqual(state["truncation_reason"], "clipped paragraph")

    def test_clean_all_pass_result_is_not_gated(self):
        # Guardrail: the truncation hook must not gate a normal, untruncated
        # all-pass result.
        review_result = {"clauses": [{"id": "c1", "decision": "pass"}]}

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "pass")
        self.assertFalse(state["blocks_send"])
        self.assertFalse(result_requires_human_review(review_result))

    def test_tracked_changes_all_pass_result_still_blocks_send(self):
        # Document-level gate: an all-pass verdict computed from a source carrying
        # unresolved tracked changes must still force human review and block send.
        review_result = {
            "clauses": [{"id": "c1", "decision": "pass"}, {"id": "c2", "decision": "pass"}],
            "tracked_changes": {"has_tracked_changes": True, "tracked_insertions": 1, "tracked_deletions": 1},
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertTrue(state["requires_human_review"])
        self.assertTrue(state["tracked_changes_forced_review"])
        self.assertTrue(result_requires_human_review(review_result))

    def test_tracked_changes_marker_never_downgrades_a_check(self):
        review_result = {
            "clauses": [{"id": "c1", "decision": "fail"}],
            "tracked_changes": {"has_tracked_changes": True},
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "check")
        self.assertTrue(state["blocks_send"])
        self.assertTrue(state["requires_human_review"])

    def test_clean_all_pass_result_has_no_tracked_changes_gate(self):
        review_result = {"clauses": [{"id": "c1", "decision": "pass"}]}

        state = review_state_from_result(review_result)

        self.assertNotIn("tracked_changes_forced_review", state)

    def test_pure_fail_result_requires_human_review(self):
        # The blocker fix: a matter the AI FAILED on a required clause (state
        # 'check', counts.review == 0, no truncation) must require human review so
        # the send/clear gate blocks it -- previously aggregate_review_state set
        # requires_human_review/blocks_send to review>0 only, so a pure-fail matter
        # false-cleared the gate and the failed NDA could be emailed.
        review_result = {
            "clauses": [
                {"id": "governing_law", "decision": "fail"},
                {"id": "mutuality", "decision": "pass"},
            ]
        }

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "check")
        self.assertEqual(state["counts"]["review"], 0)
        self.assertEqual(state["counts"]["check"], 1)
        # The already-computed flags consumed by the gate.
        self.assertTrue(state["blocks_auto_send"])
        self.assertTrue(state["requires_redline"])
        self.assertTrue(result_requires_human_review(review_result))

    def test_missing_required_clause_fail_requires_human_review(self):
        # A missing required clause arrives as a fail (issue_type "missing"); the
        # gate must block it just like the unapproved-governing-law fail.
        review_result = {
            "clauses": [{"id": "confidentiality", "decision": "fail", "issue_type": "missing"}]
        }

        self.assertTrue(result_requires_human_review(review_result))

    def test_pure_fail_summary_counts_require_human_review(self):
        # The integer-summary fallback (no clause list) must gate on a failed count
        # too, mirroring the per-clause path.
        review_result = {
            "requirements_passed": 4,
            "requirements_needs_review": 0,
            "requirements_failed": 1,
        }

        self.assertTrue(result_requires_human_review(review_result))

    def test_explicit_low_confidence_pass_is_forced_to_review(self):
        # #35(a) -- the LIVE AI-first path. The AI writes an EXPLICIT decision on
        # every clause, so a {decision:"pass", confidence:0.1} used to short-circuit
        # the normalizer and skip the confidence<0.75 -> review floor entirely,
        # clearing the send gate. The floor must now fire on the explicit-pass case.
        clause = {"id": "governing_law", "decision": "pass", "confidence": 0.1}

        state = clause_review_state(clause)

        self.assertEqual(_normalize_clause_decision(clause), "review")
        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertFalse(clause_passes(clause))
        self.assertTrue(clause_needs_review(clause))

    def test_explicit_low_confidence_pass_blocks_the_send_gate_at_result_level(self):
        # End-to-end through the result-level gate the send path consults.
        review_result = {"clauses": [{"id": "c1", "decision": "pass", "confidence": 0.1}]}

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "review")
        self.assertTrue(state["blocks_send"])
        self.assertTrue(result_requires_human_review(review_result))

    def test_explicit_confident_pass_still_clears(self):
        # Guardrail: a high-confidence explicit pass must NOT be escalated.
        review_result = {"clauses": [{"id": "c1", "decision": "pass", "confidence": 0.95}]}

        state = review_state_from_result(review_result)

        self.assertEqual(state["state"], "pass")
        self.assertFalse(state["blocks_send"])
        self.assertFalse(result_requires_human_review(review_result))

    def test_explicit_pass_without_confidence_still_clears(self):
        # The floor only escalates when a confidence signal is present and below the
        # threshold; a pass with no confidence at all is left untouched.
        clause = {"id": "c1", "decision": "pass"}

        self.assertEqual(_normalize_clause_decision(clause), "pass")
        self.assertTrue(clause_passes(clause))

    def test_low_confidence_floor_never_softens_a_fail(self):
        # The floor is escalate-only: a fail at low confidence stays a fail (check),
        # never gets softened to a (lower-blocking) review.
        clause = {"id": "c1", "decision": "fail", "confidence": 0.1}

        self.assertEqual(_normalize_clause_decision(clause), "fail")

    def test_empty_zero_clause_review_does_not_clear_send_gate(self):
        # #35(b) -- an AI review that emits NO clauses reads as "nothing to review".
        # aggregate_review_state([]) is PENDING with every block flag False, so the
        # send gate used to clear. An empty/zero-clause review must instead require a
        # human (block) -- "nothing reviewed" is not "reviewed clean".
        for review_result in (
            {"clauses": []},
            {"clauses": []},  # no requirements summary, no status, no nested state
            {},
        ):
            with self.subTest(review_result=review_result):
                state = review_state_from_result(review_result)

                self.assertEqual(state["state"], "review")
                self.assertTrue(state["blocks_send"])
                self.assertTrue(state["requires_human_review"])
                self.assertTrue(result_requires_human_review(review_result))

    def test_review_with_real_clauses_is_not_treated_as_empty(self):
        # Guardrail: a result that actually produced clause verdicts is a real review
        # and follows the normal derivation (here, a clean all-pass clears).
        review_result = {"clauses": [{"id": "c1", "decision": "pass"}]}

        self.assertFalse(result_requires_human_review(review_result))


if __name__ == "__main__":
    unittest.main()
