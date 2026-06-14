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


# A source with REAL printed numbering where the printed clause number diverges from the
# physical block index: e.g. the section printed as clause 4 ("Governing Law") starts at
# the 5th physical block (p5), and clause 7 ("Signatures") starts at p11. Used to prove
# the prose-anchor fallback grounds a "Clause N" reference to the PRINTED section, not the
# bare Nth block.
NUMBERED_SOURCE_TEXT = "\n\n".join([
    "This Mutual Non-Disclosure Agreement is entered into by the parties.",
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    "3. Confidential Information",
    '"Confidential Information" means non-public business information disclosed by either party.',
    "4. Governing Law",
    "This Agreement shall be governed by the laws of California.",
    "5. Term",
    "The confidentiality obligations survive for a fixed period of five years.",
    "6. Non-Circumvention",
    "Each party remains free to deal with third parties outside the Purpose.",
    "7. Signatures",
    "For Aspora Limited",
])

NUMBERED_QUOTES_BY_PARAGRAPH_ID = {
    "p2": "Each party may disclose Confidential Information",
    "p4": '"Confidential Information" means non-public business',
    "p6": "laws of California",
    "p8": "fixed period of five years",
    "p10": "free to deal with third parties",
    "p12": "For Aspora Limited",
}


def _numbered_source_backed_paragraphs():
    """NUMBERED_SOURCE_TEXT as explicit paragraphs whose numbered headings carry real Word
    numbering metadata, so contract_structure builds SOURCE-BACKED sections from them.

    The structural prose-reference fallback only seeds a paragraph from a source-backed
    section, so this is the fixture used to prove the "grounds" behaviour end-to-end.
    """
    lines = NUMBERED_SOURCE_TEXT.split("\n\n")
    heading_numbers = {2: "3", 4: "4", 6: "5", 8: "6", 10: "7"}  # zero-based line index -> printed number
    paragraphs = []
    for line_index, text in enumerate(lines):
        paragraph = {"id": f"p{line_index + 1}", "index": line_index, "text": text}
        number = heading_numbers.get(line_index)
        if number is not None:
            paragraph["structure_number"] = number
            paragraph["numbering"] = {"label": number, "format": "decimal", "level": 0}
            paragraph["style_name"] = "Heading 1"
        paragraphs.append(paragraph)
    return paragraphs


def _numbered_assessment(clause_id, decision, *, paragraph_id, issue_type=None, **overrides):
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "present_but_wrong"
    payload = {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed by AI against the playbook and cited paragraph text.",
        "evidence": [
            {
                "paragraph_id": paragraph_id,
                "quote": NUMBERED_QUOTES_BY_PARAGRAPH_ID[paragraph_id],
                "relevance": "Supports the AI verdict.",
            }
        ],
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

        # A missing AI assessment fails safe to human review: every un-assessed
        # clause defaults to review, which blocks the document send.
        self.assertTrue(result["review_state"]["blocks_send"])
        self.assertEqual(result["ai_review"]["missing_clause_ids"], [
            "confidential_information",
            "governing_law",
            "term_and_survival",
            "non_circumvention",
            "signatures",
        ])
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        # The deterministic governing-law backstop was removed once the primary AI
        # proved it reliably fails an unapproved jurisdiction on its own. With no AI
        # assessment supplied here, governing_law fails safe to review (the
        # missing-assessment default) rather than being force-failed by a backstop.
        self.assertEqual(governing_law["decision"], "review")
        self.assertEqual(governing_law["review_state"]["state"], "review")
        self.assertTrue(governing_law["review_state"]["blocks_send"])

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


class ProseAnchorFallbackTests(unittest.TestCase):
    """When a finding has no structured evidence but its prose narrative names a place
    in the document ("Paragraph 11 defines Confidential Information...", "see Schedule
    3"), resolve that reference against the document's REAL printed structure (the
    contract_structure reference_index) and seed the referenced section's first
    paragraph. This is accuracy-or-nothing: "Paragraph 11" lands on whatever block the
    document PRINTS as clause 11 -- never the bare 11th physical block -- and a
    reference that does not resolve to a real section seeds nothing. The fallback only
    fires when the structured match set is empty, never overrides a real structured
    match, and never changes the clause's verdict.
    """

    @staticmethod
    def _structure_with_divergent_numbering(*, source_backed=True):
        """A document where the PRINTED clause number 11 is NOT the 11th physical block.

        The numbered headings 1/11/12 and a "Schedule 3" attachment occupy physical
        blocks p1.. p8, so the section the document prints as "11" starts at p3 -- never
        p11 (which does not even exist). This is the exact case the naive ``p{N}``
        fallback got wrong.

        By default the detected sections are marked SOURCE-BACKED -- i.e. each carries a
        ``source`` mapping, as it would when contract_structure builds the section from
        real Word numbering/heading metadata. The structural prose-reference fallback only
        seeds a paragraph from a SOURCE-BACKED section (a section scraped out of a flat /
        PDF document -- e.g. a street-address digit mistaken for a clause number -- carries
        no ``source`` and must seed nothing). Pass ``source_backed=False`` to model that
        scraped-from-text case.
        """
        from nda_automation.contract_structure import build_contract_structure
        from nda_automation.review_document import split_document_paragraphs

        source = "\n\n".join([
            "1. Confidentiality",
            "Each party may disclose Confidential Information to the other party.",
            "11. Governing Law",
            "This Agreement shall be governed by the laws of California.",
            "12. Term",
            "The confidentiality obligations survive for a fixed period of five years.",
            "Schedule 3: Permitted Recipients",
            "The named recipients are listed in this schedule.",
        ])
        paragraphs = split_document_paragraphs(source)
        reference_index = build_contract_structure(paragraphs)["reference_index"]
        if source_backed:
            # Stamp the Word-metadata ``source`` these headings would carry when the doc is
            # a real .docx (vs scraped from flat text). The aliases / paragraph ranges are
            # the resolver's own output and are left untouched.
            for record in reference_index["sections_by_id"].values():
                record["source"] = {"source_kind": "docx_numbering", "style_name": "Heading 1"}
        return paragraphs, reference_index

    def test_paragraph_reference_grounds_to_printed_section_not_block_index(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # Sanity: the section the document prints as "11" starts at p3, and there is no
        # p11 paragraph -- so a correct resolver MUST return p3, and the old p{N} mapping
        # would have returned nothing (no p11) / a wrong block.
        self.assertEqual(reference_index["alias_to_section_id"]["number:11"], "section-2")
        self.assertNotIn("p11", {p["id"] for p in paragraphs})

        assessment = {
            # No structured evidence -- the AI named the printed clause only in prose.
            "reason": "Paragraph 11 sets the governing law and needs review.",
        }
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p3"])

    def test_schedule_reference_grounds_to_schedule_section_start(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        self.assertEqual(reference_index["alias_to_section_id"]["schedule:3"], "section-4")

        assessment = {"finding": "The permitted recipients are listed in Schedule 3."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        # Schedule 3's section starts at p7 (its heading block), not the bogus "p3".
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p7"])

    def test_unresolved_reference_stays_ungrounded(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # Neither a Paragraph 99 nor a Schedule 99 exists in the printed structure, so
        # both must seed NOTHING -- no block-index guess.
        assessment = {
            "reason": "Paragraph 99 should define this, and Schedule 99 would list it.",
        }
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual(matched, [])

    def test_structural_keyword_uses_printed_number_fallback(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # "Clause 12" -> the bare numbered heading printed as 12 (via the number:N body
        # fallback the shared resolver applies), which starts at p5.
        assessment = {"reason": "Clause 12 governs the term of confidentiality."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p5"])

    def test_structural_reference_to_non_source_backed_section_seeds_nothing(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        # The SAME printed structure, but the sections were SCRAPED out of flat text (no
        # Word numbering/heading metadata) -- exactly how the parser invents a FALSE
        # section from a street-address digit ("145 Curtain Road" -> number:145) or a
        # cover-table cell ("2 year" -> number:2). Those sections carry no ``source``.
        # Grounding to one would anchor a finding to an ADDRESS, so the structural
        # prose-reference fallback must seed NOTHING (accuracy-or-nothing).
        paragraphs, reference_index = self._structure_with_divergent_numbering(source_backed=False)
        # The reference still RESOLVES (the alias exists) -- the guard rejects it purely
        # because the resolved section is not source-backed.
        self.assertEqual(reference_index["alias_to_section_id"]["number:11"], "section-2")
        self.assertNotIn("source", reference_index["sections_by_id"]["section-2"])

        for assessment in (
            {"reason": "Paragraph 11 sets the governing law and needs review."},
            {"finding": "The permitted recipients are listed in Schedule 3."},
            {"reason": "Clause 12 governs the term of confidentiality."},
        ):
            matched = _matched_paragraphs(paragraphs, assessment, reference_index)
            self.assertEqual(matched, [], assessment)

    def test_structural_reference_to_address_derived_section_does_not_anchor(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        # A hand-built reference_index reproducing the real calibration finding: a
        # ``number:145`` section the parser scraped from the street address
        # "145 Curtain Road" -- emitted with source=null (and even confidence=high). A
        # finding that says "see paragraph 145" must NOT anchor onto the address block.
        paragraphs = [
            {"id": "p0", "index": 0, "text": "Confidentiality obligations apply to both parties."},
            {"id": "p1", "index": 1, "text": "Registered office at 145 Curtain Road, London EC2A 3QQ."},
        ]
        reference_index = {
            "alias_to_section_id": {"number:145": "section-addr"},
            "ambiguous_alias_keys": [],
            "sections_by_id": {
                "section-addr": {
                    "id": "section-addr",
                    "kind": "numbered",
                    "number": "145",
                    "label": "145",
                    "heading": "",
                    "level": 0,
                    "paragraph_ids": ["p1"],
                    "start_index": 1,
                    "end_index": 1,
                    "parent_id": None,
                    "source": None,  # scraped from text -- NOT source-backed
                    "confidence": "high",
                },
            },
        }
        assessment = {"reason": "The notice address in paragraph 145 should be confirmed."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual(matched, [])

    def test_section_reference_does_not_borrow_schedule_namespace(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # "Section 3" must NOT resolve onto "Schedule 3" -- attachments and in-body
        # sections are different namespaces. There is no in-body section 3, so nothing
        # is seeded.
        assessment = {"reason": "Section 3 would need to address this point."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual(matched, [])

    def test_exhibit_reference_is_matched_by_the_prose_regex(self):
        # FIX 2: the prose regex did not include "exhibit", so the BE never even saw an
        # "Exhibit N" reference while the FE linked it. The regex now matches it (FE/BE
        # parity on the reference WORD); the namespace guard governs how it RESOLVES.
        from nda_automation.ai_first_review import _PROSE_REFERENCE_RE, _PROSE_REFERENCE_KINDS

        match = _PROSE_REFERENCE_RE.search("the recipients are listed in Exhibit 3.")
        self.assertIsNotNone(match, "the prose regex must now match 'Exhibit N'")
        self.assertEqual(match.group("keyword").lower(), "exhibit")
        self.assertEqual(match.group("number"), "3")
        # "Exhibit" is treated as an ATTACHMENT kind so it obeys the same namespace guard
        # as Schedule/Annex/Appendix (no kind-agnostic number:N body fallback).
        self.assertIn(_PROSE_REFERENCE_KINDS["exhibit"], {"schedule", "annex", "annexure", "appendix"})

    def test_exhibit_reference_does_not_borrow_body_or_schedule_namespace(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # "Exhibit 11"/"Exhibit 12" must NOT borrow the in-body numbered headings (printed
        # 11/12) via number:N -- an attachment-kind reference never appends that fallback.
        # "Exhibit 3" must NOT borrow the Schedule 3 either (no exhibit:3/appendix:3 alias).
        # There is no real Exhibit section, so every form seeds NOTHING -- the same outcome
        # the FE reaches under the identical attachment guard.
        for assessment in (
            {"reason": "Exhibit 11 defines the governing law and needs review."},
            {"reason": "Exhibit 12 governs the term of confidentiality."},
            {"finding": "The permitted recipients are listed in Exhibit 3."},
        ):
            matched = _matched_paragraphs(paragraphs, assessment, reference_index)
            self.assertEqual(matched, [], assessment)

    def test_bare_paragraph_token_is_a_direct_id(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # A bare "pN" token is a DIRECT document paragraph id, validated against the
        # real ids -- not routed through the printed-number index.
        assessment = {"finding": "cf. p3 for the carve-out wording."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p3"])

    def test_bare_paragraph_token_grounds_regardless_of_source_backing(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        # The source-backed GUARD applies ONLY to the structural Paragraph/Clause/... N
        # path (which goes through section resolution). A bare "pN" token is a DIRECT,
        # already-validated document paragraph id and NEVER touches section resolution, so
        # it still grounds even when every detected section is NOT source-backed.
        paragraphs, reference_index = self._structure_with_divergent_numbering(source_backed=False)
        self.assertNotIn("source", reference_index["sections_by_id"]["section-2"])
        assessment = {"finding": "cf. p3 for the carve-out wording."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p3"])

    def test_bare_token_to_nonexistent_paragraph_is_not_seeded(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # The document has no p99, so the direct token seeds nothing.
        assessment = {"finding": "cf. p99 for the carve-out wording."}
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual(matched, [])

    def test_prose_fallback_never_overrides_structured_match(self):
        from nda_automation.ai_first_review import _matched_paragraphs

        paragraphs, reference_index = self._structure_with_divergent_numbering()
        # Structured evidence cites p5; prose mentions Paragraph 11. The structured
        # match wins and the prose fallback is never consulted.
        assessment = {
            "matched_paragraph_ids": ["p5"],
            "reason": "Paragraph 11 also touches the governing law.",
        }
        matched = _matched_paragraphs(paragraphs, assessment, reference_index)
        self.assertEqual([paragraph["id"] for paragraph in matched], ["p5"])

    def test_review_clause_with_prose_only_anchor_becomes_grounded_evidence(self):
        # End-to-end: a "review" clause whose AI assessment names "Clause 4" in PROSE
        # ONLY (no structured evidence array) now carries the PRINTED-4 section's start
        # paragraph in its structured evidence so the Review tab can cite + jump to it,
        # while the verdict stays a review. In NUMBERED_SOURCE_TEXT the section printed
        # as clause 4 starts at the 5th physical block (p5), so a correct resolver
        # grounds to p5 -- never the bare block "p4".
        review_assessment = {
            "clause_id": "governing_law",
            "decision": "review",
            "issue_type": "unclear",
            "rationale": "Clause 4 names the governing law, but the choice needs confirmation.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.6,
            "blocks_send": True,
        }
        result = build_ai_first_review_result(
            NUMBERED_SOURCE_TEXT,
            [
                _numbered_assessment("mutuality", "pass", paragraph_id="p2"),
                _numbered_assessment("confidential_information", "pass", paragraph_id="p4"),
                review_assessment,
                _numbered_assessment("term_and_survival", "pass", paragraph_id="p8"),
                _numbered_assessment("non_circumvention", "pass", paragraph_id="p10"),
                _numbered_assessment("signatures", "pass", paragraph_id="p12"),
            ],
            # Numbered headings carry real Word numbering metadata, so clause-4's section is
            # SOURCE-BACKED and the structural prose-reference fallback may seed from it.
            paragraphs=_numbered_source_backed_paragraphs(),
        )

        clause = next(c for c in result["clauses"] if c["id"] == "governing_law")
        # The prose anchor resolved printed-clause-4 to its start paragraph p5 (NOT p4).
        self.assertEqual(clause["matched_paragraph_ids"], ["p5"])
        self.assertTrue(clause["structured_evidence"])
        self.assertEqual(clause["structured_evidence"][0]["paragraph_id"], "p5")
        # Purely an evidence fallback: the verdict stays a review.
        self.assertEqual(clause["decision"], "review")

    def test_review_clause_with_bogus_prose_anchor_stays_ungrounded(self):
        # A "review" clause whose prose names a NON-existent printed section must not get
        # a bogus seed: no structured evidence, and the finding remains ungrounded.
        review_assessment = {
            "clause_id": "confidential_information",
            "decision": "review",
            "issue_type": "unclear",
            "rationale": "Paragraph 99 would need to define Confidential Information, but it is unclear.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.6,
            "blocks_send": True,
        }
        result = build_ai_first_review_result(
            NUMBERED_SOURCE_TEXT,
            [
                _numbered_assessment("mutuality", "pass", paragraph_id="p2"),
                review_assessment,
                _numbered_assessment("governing_law", "pass", paragraph_id="p6"),
                _numbered_assessment("term_and_survival", "pass", paragraph_id="p8"),
                _numbered_assessment("non_circumvention", "pass", paragraph_id="p10"),
                _numbered_assessment("signatures", "pass", paragraph_id="p12"),
            ],
        )

        clause = next(c for c in result["clauses"] if c["id"] == "confidential_information")
        self.assertEqual(clause["matched_paragraph_ids"], [])
        self.assertEqual(clause["structured_evidence"], [])
        self.assertEqual(clause["grounding"]["status"], "ungrounded")

    def test_review_clause_prose_anchor_to_non_source_backed_section_stays_ungrounded(self):
        # End-to-end source-backed GUARD: a "review" clause names "Clause 4" in PROSE, the
        # reference RESOLVES to the printed-4 section, but the document is flat text (no
        # Word numbering metadata) so that section is NOT source-backed -- the same shape
        # as a parser-invented address/table-cell section. The fallback must seed NOTHING
        # rather than anchor onto a possibly-bogus block, and the verdict must be unchanged.
        review_assessment = {
            "clause_id": "governing_law",
            "decision": "review",
            "issue_type": "unclear",
            "rationale": "Clause 4 names the governing law, but the choice needs confirmation.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.6,
            "blocks_send": True,
        }
        result = build_ai_first_review_result(
            # No explicit metadata-bearing paragraphs: contract_structure scrapes the
            # headings from flat text, so the printed-4 section carries no ``source``.
            NUMBERED_SOURCE_TEXT,
            [
                _numbered_assessment("mutuality", "pass", paragraph_id="p2"),
                _numbered_assessment("confidential_information", "pass", paragraph_id="p4"),
                review_assessment,
                _numbered_assessment("term_and_survival", "pass", paragraph_id="p8"),
                _numbered_assessment("non_circumvention", "pass", paragraph_id="p10"),
                _numbered_assessment("signatures", "pass", paragraph_id="p12"),
            ],
        )

        clause = next(c for c in result["clauses"] if c["id"] == "governing_law")
        # The non-source-backed section seeds nothing -- accuracy-or-nothing.
        self.assertEqual(clause["matched_paragraph_ids"], [])
        self.assertEqual(clause["structured_evidence"], [])
        self.assertEqual(clause["grounding"]["status"], "ungrounded")
        # The guard is purely an evidence gate: the verdict is unchanged.
        self.assertEqual(clause["decision"], "review")


if __name__ == "__main__":
    unittest.main()
