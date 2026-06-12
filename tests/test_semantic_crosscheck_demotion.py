"""Demotion contract for the deterministic semantic cross-check.

The cross-check (semantic_crosscheck.py) is a paraphrase-fragile, polarity-blind
regex pass. It used to mint a HARD FAIL the AI could not overturn AND auto-generate
a redline that strips the "prohibited" language. This locks in the demotion:

  * The cross-check may at most ESCALATE TO REVIEW -- never a hard FAIL, never a
    present_but_wrong "check" status (which is what auto-generates a redline edit).
  * The escalation carries a marker (SEMANTIC_CROSSCHECK_ESCALATION_KEY) so the
    arbiter treats it as NON-TERMINAL: a confident AI PASS clears it back to PASS,
    while a genuine checker review/fail (no marker) stays terminal.
  * It never auto-generates a redline edit.

These are unit tests over the owned modules (semantic_crosscheck + decision_arbiter)
and a redline-builder check; they do not touch the shared review_eval fixtures.
"""
from __future__ import annotations

import unittest

from nda_automation import clause_outcomes
from nda_automation.decision_arbiter import (
    SEMANTIC_CROSSCHECK_ESCALATION_KEY,
    arbitrate,
    deterministic_decision,
)
from nda_automation.review_document import split_document_paragraphs
from nda_automation.semantic_crosscheck import apply_semantic_crosscheck

# A genuinely-prohibited non-circ pattern (introduced-contact non-solicit + bypass).
PROHIBITED_TEXT = (
    "Each Party shall not, directly or indirectly, contact, solicit, or transact with any "
    "customers or business relationships introduced or made known to it by the other Party, "
    "nor shall it bypass the introducing Party."
)
# Polarity-clean freedom-preserving carve-out (the exact task clause).
CLEAN_FREEDOM_TEXT = (
    "Nothing herein shall restrict either Party from dealing directly with any third party "
    "introduced by the other Party."
)
# An unqualified independent-development exclusion (CI explicit-exclusion branch).
CI_EXCLUSION_TEXT = (
    "Confidential Information does not include information that the Receiving Party "
    "independently develops."
)


def _clean_non_circ_result() -> dict:
    return {
        "id": "non_circumvention",
        "name": "Non-Circumvention",
        "requirement": "The NDA must not impose non-circumvention restrictions.",
        "type": "prohibited",
        "status": "match",
        "passes": True,
        "needs_review": False,
        "decision": "pass",
        "issue_type": "none",
        "fallback": {"redline_action": "delete_paragraph"},
    }


def _clean_ci_result() -> dict:
    return {
        "id": "confidential_information",
        "name": "Confidential Information",
        "requirement": "The NDA must define Confidential Information adequately.",
        "type": "required",
        "status": "match",
        "passes": True,
        "needs_review": False,
        "decision": "pass",
        "issue_type": "none",
    }


def _run_crosscheck(clause_result: dict, text: str):
    clause_id = clause_result["id"]
    clauses_by_id = {
        clause_id: {
            "id": clause_id,
            "name": clause_result["name"],
            "requirement": clause_result["requirement"],
            "type": clause_result["type"],
        }
    }
    paragraphs = split_document_paragraphs(text)
    results, summary = apply_semantic_crosscheck(
        clause_results=[dict(clause_result)],
        clauses_by_id=clauses_by_id,
        paragraphs=paragraphs,
    )
    clause = results[0]
    clause["deterministic_decision"] = deterministic_decision(clause)
    return clause, summary, paragraphs


class SemanticCrosscheckDemotionTests(unittest.TestCase):
    def test_prohibited_non_circ_escalates_to_review_never_fail(self):
        clause, summary, _ = _run_crosscheck(_clean_non_circ_result(), PROHIBITED_TEXT)
        self.assertEqual(summary["record_count"], 1)
        # Escalation, not a fail: never a present_but_wrong "check".
        self.assertEqual(clause["status"], "match")
        self.assertEqual(clause["issue_type"], "none")
        self.assertEqual(clause["decision"], "review")
        self.assertEqual(clause["deterministic_decision"], "review")
        self.assertTrue(clause.get(SEMANTIC_CROSSCHECK_ESCALATION_KEY))

    def test_prohibited_non_circ_generates_no_redline_edit(self):
        clause, _, paragraphs = _run_crosscheck(_clean_non_circ_result(), PROHIBITED_TEXT)
        paragraphs_by_id = {str(p["id"]): p for p in paragraphs}
        edits = clause_outcomes.redline_edits_for_clause(clause, paragraphs_by_id, 1)
        self.assertEqual(edits, [])
        self.assertFalse(clause_outcomes._is_present_but_wrong_check(clause))

    def test_clean_freedom_clause_is_not_escalated(self):
        clause, summary, _ = _run_crosscheck(_clean_non_circ_result(), CLEAN_FREEDOM_TEXT)
        # Freedom-preserving guard keeps the clean clause clean -- no false review.
        self.assertEqual(summary["record_count"], 0)
        self.assertEqual(clause["decision"], "pass")
        self.assertFalse(clause.get(SEMANTIC_CROSSCHECK_ESCALATION_KEY))

    def test_ci_unqualified_exclusion_escalates_to_review_never_fail(self):
        clause, summary, paragraphs = _run_crosscheck(_clean_ci_result(), CI_EXCLUSION_TEXT)
        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(clause["status"], "match")
        self.assertEqual(clause["issue_type"], "none")
        self.assertEqual(clause["decision"], "review")
        self.assertTrue(clause.get(SEMANTIC_CROSSCHECK_ESCALATION_KEY))
        # The CI clause has a registered redline builder; confirm it stays silent.
        paragraphs_by_id = {str(p["id"]): p for p in paragraphs}
        edits = clause_outcomes.redline_edits_for_clause(clause, paragraphs_by_id, 1)
        self.assertEqual(edits, [])


class ArbiterCrosscheckHandoffTests(unittest.TestCase):
    def _escalated_clause(self, ai_status=None, ai_decision=None, **extra):
        clause = {
            "deterministic_decision": "review",
            SEMANTIC_CROSSCHECK_ESCALATION_KEY: True,
        }
        if ai_status is not None or ai_decision is not None:
            clause["ai_review_analysis"] = {
                "status": ai_status or "",
                "ai_decision": ai_decision or "",
                "ai_reason": "ai reasoning",
            }
        clause.update(extra)
        return clause

    def test_escalation_without_ai_holds_at_review(self):
        verdict = arbitrate(self._escalated_clause())
        self.assertEqual(verdict["decision"], "review")
        self.assertEqual(verdict["source"], "semantic_crosscheck")

    def test_confident_ai_pass_clears_escalation_to_pass(self):
        verdict = arbitrate(self._escalated_clause(ai_status="disagreement", ai_decision="pass"))
        self.assertEqual(verdict["decision"], "pass")
        self.assertEqual(verdict["source"], "ai")
        self.assertEqual(verdict["reason_code"], "ai_cleared_semantic_crosscheck")

    def test_ai_fail_never_becomes_a_hard_fail(self):
        verdict = arbitrate(self._escalated_clause(ai_status="disagreement", ai_decision="fail"))
        self.assertEqual(verdict["decision"], "review")
        self.assertEqual(verdict["source"], "semantic_crosscheck")

    def test_untrustworthy_ai_pass_holds_at_review(self):
        for status in ("low_confidence", "invalid", "error", "disabled"):
            verdict = arbitrate(self._escalated_clause(ai_status=status, ai_decision="pass"))
            self.assertEqual(verdict["decision"], "review", status)
            self.assertEqual(verdict["source"], "semantic_crosscheck", status)

    def test_genuine_checker_review_without_marker_stays_terminal(self):
        # A real deterministic review (no cross-check marker) is NOT softened by the
        # AI -- the demotion is scoped strictly to cross-check escalations.
        clause = {
            "deterministic_decision": "review",
            "ai_review_analysis": {"status": "disagreement", "ai_decision": "pass"},
        }
        verdict = arbitrate(clause)
        self.assertEqual(verdict["decision"], "review")
        self.assertEqual(verdict["source"], "deterministic")

    def test_genuine_checker_fail_without_marker_stays_terminal(self):
        clause = {
            "deterministic_decision": "fail",
            "ai_review_analysis": {"status": "disagreement", "ai_decision": "pass"},
        }
        verdict = arbitrate(clause)
        self.assertEqual(verdict["decision"], "fail")
        self.assertEqual(verdict["source"], "deterministic")


if __name__ == "__main__":
    unittest.main()
