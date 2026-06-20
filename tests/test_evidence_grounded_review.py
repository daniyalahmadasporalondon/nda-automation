"""Integration + eval coverage for evidence-grounded findings.

These exercise the whole AI-first normalization path so the grounding status,
citation surface, and ungrounded-finding downgrade are verified against the
real review_result contract (including its evidence-trust validation).
"""

import unittest

from nda_automation.ai_assessment_contract import AI_REDLINE_NO_CHANGE
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.evidence_grounding import (
    GROUNDING_ABSENCE,
    GROUNDING_GROUNDED,
    UNGROUNDED_REASON_CODE,
)
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, supplier, '
    "pricing, market, product, proprietary and trade secret information disclosed by either party.",
    "This Agreement shall be governed by the laws of California.",
    "The confidentiality obligations survive for a fixed period of five years.",
    "Each party remains free to deal with third parties outside the Purpose.",
    "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
])

QUOTES_BY_PARAGRAPH_ID = {
    "p1": "Each party may disclose Confidential Information",
    "p2": '"Confidential Information" means non-public business',
    "p3": "laws of California",
    "p4": "fixed period of five years",
    "p5": "free to deal with third parties",
    "p6": "For Aspora Limited",
}


def _grounded(clause_id, decision, *, paragraph_id, issue_type=None, **overrides):
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "present_but_wrong"
    payload = {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed against the playbook with a cited quote from the document.",
        "evidence": [{
            "paragraph_id": paragraph_id,
            "quote": QUOTES_BY_PARAGRAPH_ID[paragraph_id],
            "relevance": "Supports the AI verdict.",
        }],
        "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
        "confidence": 0.91,
        "blocks_send": decision == "review",
    }
    payload.update(overrides)
    return payload


def _clause(result, clause_id):
    return next(clause for clause in result["clauses"] if clause["id"] == clause_id)


class EvidenceGroundedReviewTests(unittest.TestCase):
    def _baseline_assessments(self):
        # A fully-grounded packet: every present clause cites a real quote, and
        # the prohibited clause passes on absence.
        return [
            _grounded("mutuality", "pass", paragraph_id="p1"),
            _grounded("confidential_information", "pass", paragraph_id="p2"),
            _grounded(
                "governing_law",
                "fail",
                paragraph_id="p3",
                rationale="Governing law selects California, outside the approved options.",
                proposed_redline={
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p3",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                    "jurisdiction": "England and Wales",
                },
            ),
            _grounded("term_and_survival", "pass", paragraph_id="p4"),
            # non_circumvention is prohibited and absent: passes on absence.
            {
                "clause_id": "non_circumvention",
                "decision": "pass",
                "issue_type": "none",
                "rationale": "No non-circumvention restriction appears in the supplied text.",
                "evidence": [],
                "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
                "confidence": 0.82,
                "blocks_send": False,
            },
            _grounded("signatures", "pass", paragraph_id="p6"),
        ]

    def test_grounded_finding_surfaces_citation_and_grounding_status(self):
        result = build_ai_first_review_result(SOURCE_TEXT, self._baseline_assessments())

        governing_law = _clause(result, "governing_law")
        self.assertEqual(governing_law["grounding"]["status"], GROUNDING_GROUNDED)
        self.assertTrue(governing_law["grounding"]["grounded"])
        self.assertEqual(governing_law["grounding"]["evidence_count"], 1)

        citation = governing_law["citation"]
        self.assertEqual(citation["quote"], "laws of California")
        self.assertEqual(citation["paragraph_id"], "p3")
        # Offsets must resolve back to the exact span in the source document.
        self.assertEqual(SOURCE_TEXT[citation["start"]:citation["end"]], "laws of California")

    def test_absent_prohibited_clause_is_grounded_as_absence(self):
        result = build_ai_first_review_result(SOURCE_TEXT, self._baseline_assessments())

        non_circumvention = _clause(result, "non_circumvention")
        self.assertEqual(non_circumvention["decision"], "pass")
        self.assertEqual(non_circumvention["grounding"]["status"], GROUNDING_ABSENCE)
        self.assertFalse(non_circumvention["grounding"]["requires_quote"])
        self.assertNotIn("citation", non_circumvention)

    def test_missing_required_clause_is_grounded_as_absence(self):
        assessments = self._baseline_assessments()
        # Replace the confidential_information pass with a missing-required fail.
        assessments[1] = {
            "clause_id": "confidential_information",
            "decision": "fail",
            "issue_type": "missing",
            "rationale": "No confidentiality definition appears in the supplied text.",
            "evidence": [],
            "proposed_redline": {
                "action": REDLINE_REPLACE_PARAGRAPH,
                "paragraph_id": "p2",
                "text": '"Confidential Information" means all non-public information disclosed by either party.',
            },
            "confidence": 0.7,
            "blocks_send": False,
        }
        result = build_ai_first_review_result(SOURCE_TEXT, assessments)

        confidential = _clause(result, "confidential_information")
        self.assertEqual(confidential["decision"], "fail")
        self.assertEqual(confidential["grounding"]["status"], GROUNDING_ABSENCE)

    def test_ungrounded_pass_is_downgraded_to_review_and_blocks_send(self):
        assessments = self._baseline_assessments()
        # mutuality claims a pass but cites a quote that is NOT in p1, so after
        # contract resolution it lands with no groundable quote.
        assessments[0] = {
            "clause_id": "mutuality",
            "decision": "pass",
            "issue_type": "none",
            "rationale": "Asserts reciprocity without quoting any supporting text.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.95,
            "blocks_send": False,
        }
        result = build_ai_first_review_result(SOURCE_TEXT, assessments)

        mutuality = _clause(result, "mutuality")
        self.assertEqual(mutuality["decision"], "review")
        self.assertEqual(mutuality["grounding"]["status"], "ungrounded")
        self.assertTrue(mutuality["needs_review"])
        self.assertTrue(mutuality["blocks_send"])
        self.assertEqual(mutuality["reason_code"], UNGROUNDED_REASON_CODE)
        self.assertIn(UNGROUNDED_REASON_CODE, mutuality["reason_codes"])
        # The downgrade must propagate so the document cannot be auto-sent.
        self.assertTrue(result["review_state"]["blocks_send"])

    def test_ungrounded_downgrade_preserves_evidence_trust(self):
        # The whole result must still pass evidence-trust validation after the
        # decision/status rewrite (it raises EvidenceProvenanceError otherwise).
        # NB: the contract already rejects an ungrounded non-missing *fail*, so
        # the grounding layer's remaining job is to catch an ungrounded *pass*.
        assessments = self._baseline_assessments()
        assessments[0] = {
            "clause_id": "mutuality",
            "decision": "pass",
            "issue_type": "none",
            "rationale": "Asserts the clause is fine without quoting any supporting text.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.6,
            "blocks_send": False,
        }
        result = build_ai_first_review_result(SOURCE_TEXT, assessments)

        self.assertEqual(result["evidence_trust"]["status"], "verified")
        mutuality = _clause(result, "mutuality")
        self.assertEqual(mutuality["decision"], "review")
        self.assertEqual(mutuality["reason_code"], UNGROUNDED_REASON_CODE)
        # An ungrounded verdict carries no fabricated citation.
        self.assertNotIn("citation", mutuality)

    def test_contract_degrades_ungrounded_fail_without_discarding_the_batch(self):
        # Documents the division of labour: the contract enforces evidence on
        # non-missing fails. An ungrounded fail is a per-clause defect -- it is
        # quarantined into a SAFE blocking review (never a silent pass) while every
        # other valid clause in the batch survives, instead of rejecting the whole
        # document's review.
        assessments = self._baseline_assessments()
        assessments[0] = {
            "clause_id": "mutuality",
            "decision": "fail",
            "issue_type": "present_but_wrong",
            "rationale": "Claims a defect with no quoted text to back it.",
            "evidence": [],
            "proposed_redline": {
                "action": REDLINE_REPLACE_PARAGRAPH,
                "paragraph_id": "p1",
                "text": "Each party may disclose Confidential Information to the other party.",
            },
            "confidence": 0.6,
            "blocks_send": False,
        }

        result = build_ai_first_review_result(SOURCE_TEXT, assessments)

        clauses_by_id = {clause["id"]: clause for clause in result["clauses"]}
        mutuality = clauses_by_id["mutuality"]
        # The ungrounded fail is NOT honoured as a fail-with-no-evidence; it degrades
        # to a send-blocking review (never a silent pass).
        self.assertEqual(mutuality["decision"], "review")
        self.assertTrue(mutuality["blocks_send"])
        self.assertEqual(
            mutuality["ai_first_assessment"]["status"], "contract_invalid"
        )
        # The other grounded clauses survive (governing_law keeps its real fail).
        self.assertEqual(clauses_by_id["governing_law"]["decision"], "fail")
        self.assertEqual(clauses_by_id["confidential_information"]["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
