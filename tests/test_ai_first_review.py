import unittest
from copy import deepcopy

from nda_automation.ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_REDLINE_NO_CHANGE,
    AIAssessmentContractError,
)
from nda_automation.ai_first_review import AI_FIRST_REVIEW_MODE, build_ai_first_review_result
from nda_automation.checker import load_playbook
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import validate_clause_evidence_trust


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, supplier, pricing, market, product, proprietary and trade secret information disclosed by either party.',
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


def _assessment(clause_id, decision, *, paragraph_id="p1", issue_type=None, **overrides):
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "present_but_wrong"
    payload = {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed by AI against the playbook and cited paragraph text.",
        "evidence": [{"paragraph_id": paragraph_id, "quote": QUOTES_BY_PARAGRAPH_ID[paragraph_id], "relevance": "Supports the AI verdict."}],
        "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
        "confidence": 0.91,
        "blocks_send": decision == "review",
    }
    payload.update(overrides)
    return payload


class AIFirstReviewTests(unittest.TestCase):
    def test_ai_first_review_result_matches_current_contract_shape(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [
                _assessment("mutuality", "pass"),
                _assessment("confidential_information", "pass", paragraph_id="p2"),
                _assessment(
                    "governing_law",
                    "fail",
                    paragraph_id="p3",
                    issue_type="present_but_wrong",
                    rationale="Governing law is present but not an approved jurisdiction.",
                    proposed_redline={
                        "action": REDLINE_REPLACE_PARAGRAPH,
                        "paragraph_id": "p3",
                        "text": "This Agreement shall be governed by the laws of England and Wales.",
                        "jurisdiction": "England and Wales",
                    },
                    evidence=[{"quote": "laws of california", "relevance": "Shows the governing-law jurisdiction."}],
                ),
                _assessment("term_and_survival", "pass", paragraph_id="p4"),
                _assessment("non_circumvention", "pass", paragraph_id="p5"),
                _assessment("signatures", "pass", paragraph_id="p6"),
            ],
            checked_at="2026-06-04T00:00:00+00:00",
        )

        self.assertEqual(result["review_mode"], AI_FIRST_REVIEW_MODE)
        self.assertEqual(result["checked_at"], "2026-06-04T00:00:00+00:00")
        self.assertEqual(result["evidence_trust"], {"status": "verified", "errors": []})
        self.assertEqual(validate_clause_evidence_trust(result, SOURCE_TEXT), [])
        self.assertEqual(result["requirements_failed"], 1)
        self.assertEqual(result["requirements_needs_review"], 0)
        self.assertEqual(result["requirements_passed"], 5)
        self.assertEqual(result["review_state"]["state"], "check")
        self.assertEqual(result["review_state"]["counts"]["check"], 1)
        self.assertEqual(result["review_state"]["clause_ids"]["check"], ["governing_law"])
        self.assertEqual(
            [clause["id"] for clause in result["clauses"]],
            [clause["id"] for clause in load_playbook()["clauses"]],
        )
        mutuality = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        self.assertIn("rules", mutuality)
        self.assertIn("pass_conditions", mutuality["rules"])

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["decision"], "fail")
        self.assertEqual(governing_law["decision_source"], "ai")
        self.assertEqual(governing_law["issue_type"], "present_but_wrong")
        self.assertEqual(governing_law["issue_label"], "Present but wrong")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p3"])
        self.assertEqual(governing_law["structured_evidence"][0]["match_spans"][0]["text"], "laws of California")
        self.assertEqual(governing_law["ai_first_assessment"]["schema_version"], AI_ASSESSMENT_CONTRACT_VERSION)
        self.assertEqual(governing_law["ai_first_assessment"]["proposed_redline_action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertNotIn("why_it_may_be_fine", governing_law)
        self.assertNotIn("why_it_might_be_a_problem", governing_law)
        self.assertIn("sections", governing_law["structure_context"])
        self.assertEqual(governing_law["review_state"]["state"], "check")
        self.assertEqual(governing_law["proposed_change"]["action"], "replace")
        self.assertEqual(governing_law["proposed_change"]["source_text"], "This Agreement shall be governed by the laws of California.")
        self.assertEqual(
            governing_law["proposed_change"]["proposed_text"],
            "This Agreement shall be governed by the laws of England and Wales.",
        )
        self.assertEqual(governing_law["proposed_change"]["evidence"]["paragraph_id"], "p3")
        self.assertEqual(governing_law["proposed_change"]["safety"]["status"], "proposed_redline_available")
        self.assertEqual(result["proposed_changes"], [governing_law["proposed_change"]])

        redline = next(edit for edit in result["redline_edits"] if edit["clause_id"] == "governing_law")
        self.assertEqual(redline["action"], "replace_paragraph")
        self.assertEqual(redline["paragraph_id"], "p3")
        self.assertIn("inline_diff_operations", redline)
        self.assertEqual(
            [option["label"] for option in redline["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC", "Ontario, Canada"],
        )
        self.assertEqual(
            [option["label"] for option in redline["template_options"] if option.get("selected")],
            ["England and Wales"],
        )

    def test_review_resolution_fields_are_carried_to_clause_and_proposed_change(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [
                _assessment("mutuality", "pass"),
                _assessment("confidential_information", "pass", paragraph_id="p2"),
                _assessment("governing_law", "pass", paragraph_id="p3"),
                _assessment(
                    "term_and_survival",
                    "review",
                    paragraph_id="p4",
                    issue_type="unclear",
                    rationale="The survival period is longer than the usual playbook cap but may be acceptable with confirmation.",
                    resolution_question="Should the survival period be reduced to the approved cap?",
                    suggested_redline="The confidentiality obligations survive for three years.",
                    recommended_option={
                        "option": "Three-year survival",
                        "reason": "It matches the normalized playbook cap.",
                    },
                    proposed_redline={"action": AI_REDLINE_NO_CHANGE},
                ),
                _assessment("non_circumvention", "pass", paragraph_id="p5"),
                _assessment("signatures", "pass", paragraph_id="p6"),
            ],
            verify=False,
        )

        term = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term["decision"], "review")
        self.assertEqual(term["resolution_question"], "Should the survival period be reduced to the approved cap?")
        self.assertEqual(term["suggested_redline"], "The confidentiality obligations survive for three years.")
        self.assertEqual(term["recommended_option"]["option"], "Three-year survival")
        self.assertEqual(term["proposed_change"]["resolution_question"], term["resolution_question"])
        self.assertEqual(term["proposed_change"]["suggested_redline"], term["suggested_redline"])
        self.assertEqual(term["proposed_change"]["recommended_option"], term["recommended_option"])

    def test_missing_ai_assessment_fails_safe_to_human_review(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [_assessment("mutuality", "pass")],
            checked_at="2026-06-04T00:00:00+00:00",
        )

        # The overall review state blocks send: either a failed clause (governing_law
        # backstop fires on California) or a review clause blocks the document.
        self.assertTrue(result["review_state"]["blocks_send"])
        self.assertEqual(result["ai_review"]["missing_clause_ids"], [
            "confidential_information",
            "governing_law",
            "term_and_survival",
            "non_circumvention",
            "signatures",
        ])
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        # SOURCE_TEXT contains "laws of California" — the deterministic backstop
        # fires even when no AI assessment is provided, because the jurisdiction is
        # clearly unapproved.  The backstop overrides the missing-assessment default.
        self.assertEqual(governing_law["decision"], "fail")
        self.assertEqual(governing_law["reason_code"], "unapproved_governing_law")
        self.assertTrue(governing_law["blocks_send"])
        # review_state.blocks_send is True only for review-state clauses; a failed
        # clause blocks via blocks_send on the clause itself (checked above).
        self.assertEqual(governing_law["review_state"]["state"], "check")

    def test_ai_first_review_result_uses_normalized_playbook_policy_text(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["max_term_years"] = 3
        term["requirement"] = "The NDA term and ordinary confidentiality survival must be fixed at up to five years."
        term["preferred_position"] = "Old five year preferred position."
        term["check_trigger"] = "Old five year trigger."

        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [
                _assessment("mutuality", "pass"),
                _assessment("confidential_information", "pass", paragraph_id="p2"),
                _assessment("governing_law", "pass", paragraph_id="p3"),
                _assessment("term_and_survival", "pass", paragraph_id="p4"),
                _assessment("non_circumvention", "pass", paragraph_id="p5"),
                _assessment("signatures", "pass", paragraph_id="p6"),
            ],
            playbook=playbook,
        )

        term_result = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertIn("three years", term_result["requirement"])
        self.assertIn("three years", term_result["preferred_position"])
        self.assertIn("longer than three years", term_result["check_trigger"])

    def test_evidence_quote_without_paragraph_id_resolves_to_source_paragraph(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [
                _assessment("mutuality", "pass"),
                _assessment("confidential_information", "pass", paragraph_id="p2"),
                _assessment("governing_law", "pass", paragraph_id="p3", evidence=[{"quote": "laws of california", "relevance": "Supports the AI verdict."}]),
                _assessment("term_and_survival", "pass", paragraph_id="p4"),
                _assessment("non_circumvention", "pass", paragraph_id="p5"),
                _assessment("signatures", "pass", paragraph_id="p6"),
            ],
        )

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p3"])
        self.assertEqual(governing_law["structured_evidence"][0]["matched_text"], "laws of california")
        self.assertEqual(governing_law["structured_evidence"][0]["match_spans"][0]["text"], "laws of California")

    def test_ambiguous_quote_without_paragraph_id_is_rejected_before_redline_anchor(self):
        with self.assertRaises(AIAssessmentContractError) as error:
            build_ai_first_review_result(
                SOURCE_TEXT,
                [
                    _assessment("mutuality", "pass"),
                    _assessment("confidential_information", "pass", paragraph_id="p2"),
                    _assessment(
                        "governing_law",
                        "fail",
                        paragraph_id="p3",
                        issue_type="present_but_wrong",
                        rationale="Governing law is present but not an approved jurisdiction.",
                        proposed_redline={
                            "action": REDLINE_REPLACE_PARAGRAPH,
                            "paragraph_id": "p3",
                            "text": "This Agreement shall be governed by the laws of England and Wales.",
                            "jurisdiction": "England and Wales",
                        },
                        evidence=[{
                            "quote": "Confidential Information",
                            "relevance": "This short phrase appears in more than one paragraph.",
                        }],
                    ),
                    _assessment("term_and_survival", "pass", paragraph_id="p4"),
                    _assessment("non_circumvention", "pass", paragraph_id="p5"),
                    _assessment("signatures", "pass", paragraph_id="p6"),
                ],
            )

        self.assertIn("quote matches multiple reviewed paragraphs; provide paragraph_id", str(error.exception))


class QuoteOffsetRobustnessTests(unittest.TestCase):
    """BUGFIX: downstream quote location must use the SAME normalization the
    contract grounds with (glyph-fold + whitespace-collapse), so a quote the
    contract accepted on a curly-quoted, double-spaced paragraph still resolves to
    its paragraph AND keeps its highlight offsets instead of silently dropping them.
    """

    def test_quote_spans_tolerate_curly_quotes_and_collapsed_whitespace(self):
        from nda_automation.ai_first_review import _quote_spans

        paragraph = {
            "id": "p1",
            "text": 'The Recipient shall  not  disclose the “Confidential Information”.',
            "start": 100,
        }
        spans = _quote_spans(paragraph, 'shall not disclose the "Confidential Information"')
        self.assertEqual(len(spans), 1)
        # Offsets map back to the ORIGINAL text (double spaces + curly quotes), not
        # the normalized form.
        self.assertEqual(spans[0]["start"], 114)
        self.assertTrue(spans[0]["end"] > spans[0]["start"])
        self.assertIn("Confidential Information", spans[0]["text"])

    def test_quote_spans_fast_path_for_clean_ascii(self):
        from nda_automation.ai_first_review import _quote_spans

        paragraph = {"id": "p1", "text": "governed by the laws of Delaware", "start": 0}
        spans = _quote_spans(paragraph, "laws of Delaware")
        self.assertEqual(spans, [{"start": 16, "end": 32, "text": "laws of Delaware", "term": "laws of Delaware"}])

    def test_paragraph_id_resolves_through_glyph_and_whitespace_variants(self):
        from nda_automation.ai_first_review import _paragraph_id_for_quote

        paragraphs = [{"id": "p1", "text": 'It is a “mutual”  agreement between the parties.'}]
        self.assertEqual(_paragraph_id_for_quote(paragraphs, 'a "mutual" agreement'), "p1")

    def test_ambiguous_quote_prefers_existing_matched_paragraph_id(self):
        from nda_automation.ai_first_review import _paragraph_id_for_quote

        paragraphs = [
            {"id": "p1", "text": "The parties agree to keep boilerplate notices confidential."},
            {"id": "p2", "text": "The parties agree to keep boilerplate notices confidential."},
        ]

        self.assertEqual(
            _paragraph_id_for_quote(
                paragraphs,
                "boilerplate notices confidential",
                preferred_ids=["p2"],
            ),
            "p2",
        )

    def test_is_document_title_paragraph_detects_title_style_only(self):
        from nda_automation.ai_first_review import _is_document_title_paragraph

        self.assertTrue(_is_document_title_paragraph({"style_name": "Title"}))
        self.assertTrue(_is_document_title_paragraph({"style_id": "Title"}))
        self.assertTrue(_is_document_title_paragraph({"style_name": "title"}))
        # Real clause headings use Heading styles, not Title -- they stay eligible.
        self.assertFalse(_is_document_title_paragraph({"style_name": "Heading 1"}))
        self.assertFalse(_is_document_title_paragraph({"style_name": "Body Text"}))
        self.assertFalse(_is_document_title_paragraph({}))

    def test_matched_paragraphs_drops_document_title_from_clause_evidence(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs = [
            {"id": "p1", "text": "Non-Disclosure Agreement", "style_name": "Title"},
            {"id": "p2", "text": "Each party may disclose Confidential Information."},
        ]
        # The AI cited the title (p1) and a real paragraph (p2) as evidence.
        assessment = {"matched_paragraph_ids": ["p1", "p2"]}
        matched = _matched_paragraphs(paragraphs, assessment)
        # The title is dropped; the substantive paragraph is kept.
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p2"])

    def test_matched_paragraphs_keeps_real_paragraphs_when_no_title_cited(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs = [
            {"id": "p1", "text": "Heading", "style_name": "Heading 1"},
            {"id": "p2", "text": "Body."},
        ]
        matched = _matched_paragraphs(paragraphs, {"matched_paragraph_ids": ["p1", "p2"]})
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p1", "p2"])


if __name__ == "__main__":
    unittest.main()
