"""Policy regression for the Confidential Information required inclusions.

The CI clause prose has always required the definition to cover (a) the
existence and terms of the Agreement and (b) the right of publicity, but the
structured ``rules`` -- the decision criteria the AI reviewer actually checks --
did not enforce those two inclusions, so a broad definition that omitted them
still passed.

These tests pin the policy at two levels:

* the structured rules now encode the two required inclusions (the tightened
  ``broad_definition_with_standard_exclusions`` pass condition plus the new
  ``missing_required_inclusions`` review trigger); and
* the review pipeline honors that policy -- a CI definition flagged for missing
  inclusions lands on ``review`` (never a silent pass) and surfaces the
  corrected wording from the existing ``redline_template`` (which already
  carries the right of publicity and the existence and terms of the Agreement),
  while a complete broad definition that DOES cover them still passes.
"""
import unittest

from nda_automation.checker import load_playbook
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.redline_defaults import playbook_redline_text
from nda_automation.ai_assessor import InMemoryAssessmentReviewer, assess_nda_with_ai


def _confidential_information_clause(playbook):
    return next(
        clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
    )


# A document whose CI definition is broad and covers BOTH required inclusions
# (right of publicity + existence and terms of the Agreement). Used to prove a
# complete broad definition is not falsely flagged.
COMPLETE_SOURCE_TEXT = "\n\n".join(
    [
        "Each party may disclose Confidential Information to the other party under this Agreement.",
        '"Confidential Information" means any and all non-public business, financial, technical, '
        "customer, supplier, pricing, market, product, proprietary and trade secret information "
        "disclosed by either party, including the right of publicity and the existence and terms "
        "of this Agreement.",
        "This Agreement shall be governed by the laws of England and Wales.",
        "The confidentiality obligations survive for a fixed period of five years.",
        "Each party remains free to deal with third parties outside the Purpose.",
        "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
    ]
)

# Same document but the CI definition omits the right of publicity and the
# existence/terms of the Agreement -- otherwise a broad, standard definition.
OMITS_INCLUSIONS_SOURCE_TEXT = "\n\n".join(
    [
        "Each party may disclose Confidential Information to the other party under this Agreement.",
        '"Confidential Information" means any and all non-public business, financial, technical, '
        "customer, supplier, pricing, market, product, proprietary and trade secret information "
        "disclosed by either party.",
        "This Agreement shall be governed by the laws of England and Wales.",
        "The confidentiality obligations survive for a fixed period of five years.",
        "Each party remains free to deal with third parties outside the Purpose.",
        "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
    ]
)


def _assessment(clause_id, decision, *, paragraph_id="", quote="", issue_type=None, proposed_redline=None):
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "unclear"
    evidence = []
    if paragraph_id and quote:
        evidence.append(
            {
                "paragraph_id": paragraph_id,
                "quote": quote,
                "relevance": "Supports the AI-first verdict.",
            }
        )
    return {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed against the playbook required inclusions.",
        "evidence": evidence,
        "proposed_redline": proposed_redline or {"action": "no_change"},
        "confidence": 0.91,
        "blocks_send": decision == "review",
    }


def _response_with_ci(ci_assessment, *, ci_quote_text):
    """A complete six-clause response that swaps in a custom CI assessment."""
    return {
        "assessments": [
            _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            ci_assessment,
            _assessment("governing_law", "pass", paragraph_id="p3", quote="laws of England and Wales"),
            _assessment("term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"),
            _assessment("non_circumvention", "pass"),
            _assessment("signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"),
        ]
    }


class CIRequiredInclusionsPolicyTests(unittest.TestCase):
    """The structured rules encode the two required inclusions."""

    def test_pass_condition_requires_publicity_and_existence_and_terms(self):
        clause = _confidential_information_clause(load_playbook())
        pass_condition = clause["rules"]["pass_conditions"][0]
        self.assertEqual(pass_condition["id"], "broad_definition_with_standard_exclusions")
        description = pass_condition["description"]
        # The definition must now expressly cover both required inclusions to pass.
        self.assertIn("right of publicity", description)
        self.assertIn("existence and terms of the Agreement", description)
        # A broad-but-incomplete definition is explicitly excluded from the pass.
        self.assertIn("does not satisfy this condition", description)
        # The existing independent-development qualification phrasing is preserved
        # (the general broad-categories behavior is unchanged).
        self.assertIn("no use of, access to, or reference to Confidential Information", description)

    def test_missing_required_inclusions_review_trigger_exists(self):
        clause = _confidential_information_clause(load_playbook())
        trigger = next(
            t for t in clause["rules"]["review_triggers"] if t["id"] == "missing_required_inclusions"
        )
        self.assertEqual(trigger["decision"], "review")
        self.assertEqual(trigger["issue_type"], "unclear")
        # The trigger names BOTH required inclusions.
        self.assertIn("existence and terms of the Agreement", trigger["description"])
        self.assertIn("right of publicity", trigger["description"])
        # The redline_action surfaces the corrected wording (the existing
        # redline_template), consistent with the fail_conditions' replace action.
        self.assertEqual(trigger["redline_action"], REDLINE_REPLACE_PARAGRAPH)

    def test_redline_template_carries_both_required_inclusions(self):
        # The proposed fix surfaced for the new trigger derives from the existing
        # redline_template, which already includes both inclusions.
        clause = _confidential_information_clause(load_playbook())
        redline_text = playbook_redline_text(clause)
        self.assertTrue(redline_text)
        self.assertIn("right of publicity", redline_text)
        self.assertIn("existence and terms of this Agreement", redline_text)


class CIRequiredInclusionsReviewPipelineTests(unittest.TestCase):
    """The review pipeline honors the policy and surfaces the fix."""

    def test_definition_missing_inclusions_yields_review_not_pass(self):
        # The reviewer flags the CI definition for the missing inclusions with the
        # template-backed replace action (blank text); the pipeline keeps it on
        # review and defaults the corrected wording from the playbook template.
        ci = _assessment(
            "confidential_information",
            "review",
            paragraph_id="p2",
            quote='"Confidential Information" means any and all non-public business',
            issue_type="unclear",
            proposed_redline={"action": REDLINE_REPLACE_PARAGRAPH, "paragraph_id": "p2"},
        )
        reviewer = InMemoryAssessmentReviewer(
            response=_response_with_ci(ci, ci_quote_text="non-public business")
        )

        result = assess_nda_with_ai(OMITS_INCLUSIONS_SOURCE_TEXT, reviewer=reviewer)

        clause = next(c for c in result["clauses"] if c["id"] == "confidential_information")
        self.assertEqual(clause["decision"], "review")
        self.assertNotEqual(clause["decision"], "pass")

        # For a review verdict the proposed fix surfaces on the clause's own
        # proposed_redline (the deterministic redline_edits builder fires only for
        # fail/present-but-wrong; review verdicts carry the fix on the assessment).
        # The blank AI text was defaulted from the existing redline_template, so the
        # surfaced wording carries BOTH required inclusions for the reviewer.
        proposed = clause["proposed_redline"]
        self.assertEqual(proposed["action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertEqual(proposed["paragraph_id"], "p2")
        self.assertIn("right of publicity", proposed["text"])
        self.assertIn("existence and terms of this Agreement", proposed["text"])
        # The same corrected wording is mirrored to suggested_redline.
        self.assertIn("right of publicity", clause["suggested_redline"])

    def test_definition_with_both_inclusions_passes(self):
        # A definition that DOES cover the right of publicity and the existence and
        # terms of the Agreement still passes -- the policy adds only the two
        # specific inclusion checks; it does not make broad definitions noisier.
        ci = _assessment(
            "confidential_information",
            "pass",
            paragraph_id="p2",
            quote="including the right of publicity and the existence and terms of this Agreement",
        )
        reviewer = InMemoryAssessmentReviewer(
            response=_response_with_ci(ci, ci_quote_text="right of publicity")
        )

        result = assess_nda_with_ai(COMPLETE_SOURCE_TEXT, reviewer=reviewer)

        clause = next(c for c in result["clauses"] if c["id"] == "confidential_information")
        self.assertEqual(clause["decision"], "pass")
        # A complete broad definition is not falsely flagged: no redline edit and
        # nothing routed to review for this clause.
        self.assertFalse(
            [edit for edit in result["redline_edits"] if edit["clause_id"] == "confidential_information"]
        )


if __name__ == "__main__":
    unittest.main()
