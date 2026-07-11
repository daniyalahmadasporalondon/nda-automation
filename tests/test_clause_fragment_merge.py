"""DOCX clause-fragment merge for the AI reviewer's view.

A DOCX clause authored across multiple <w:p> paragraphs reaches the reviewer as
separate fragments; the model then grades half-sentences and the middle limbs get
no verdict. These tests pin the CONSERVATIVE continuation-merge (trap fixtures must
stay separate, positive fixtures must merge), the identity mapping that un-orphans
the limbs, and the packet-only invariant (stored paragraphs + redline byte-unchanged).
"""

import unittest
from copy import deepcopy

from nda_automation.ai_assessment_contract import AI_REDLINE_NO_CHANGE
from nda_automation.ai_assessment_prompt import build_ai_assessment_packet
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import load_playbook
from nda_automation.clause_fragment_merge import (
    expand_group_members,
    merge_continuation_fragments,
)
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import validate_clause_evidence_trust


def _p(index, text, **metadata):
    paragraph = {"id": f"p{index}", "index": index, "text": text}
    paragraph.update(metadata)
    return paragraph


def _groups(paragraphs):
    _model, groups = merge_continuation_fragments(paragraphs)
    return groups


def _is_all_singletons(paragraphs):
    _model, groups = merge_continuation_fragments(paragraphs)
    return all(members == [str(p["id"])] for p, members in zip(paragraphs, [groups[str(p["id"])] for p in paragraphs]))


class TrapFixtureTests(unittest.TestCase):
    """Fixtures that must NOT merge (a false merge is catastrophic)."""

    def _assert_separate(self, paragraphs):
        model, groups = merge_continuation_fragments(paragraphs)
        # One model record per original paragraph, each its own singleton group.
        self.assertEqual(len(model), len(paragraphs))
        for paragraph in paragraphs:
            self.assertEqual(groups[str(paragraph["id"])], [str(paragraph["id"])])
        # No record was reconstructed.
        self.assertFalse(any("merged_fragment_ids" in record for record in model))

    def test_recital_boundary_stays_separate(self):
        self._assert_separate([
            _p(1, "(A) The Disclosing Party wishes to disclose certain confidential information to the other Party; and"),
            _p(2, "The Disclosing Party undertakes to keep the terms of this Agreement confidential."),
        ])

    def test_party_address_block_stays_separate(self):
        self._assert_separate([
            _p(1, "Moorwand Limited (Registered No.08491211)"),
            _p(2, "and: Vance Inc"),
        ])

    def test_signature_block_stays_separate(self):
        self._assert_separate([
            _p(1, "Authorised Signatory"),
            _p(2, "Position/Title"),
        ])

    def test_heading_then_body_stays_separate(self):
        self._assert_separate([
            _p(1, "Governing Law"),
            _p(2, "This Agreement shall be governed by the laws of England and Wales."),
        ])

    def test_autonumbered_next_clause_stays_separate(self):
        # No literal digit in the run; the next clause is a Word auto-number carried
        # only as metadata. The head is a genuinely-incomplete clause body.
        self._assert_separate([
            _p(1, "The Receiving Party shall keep all Confidential Information secret and shall not disclose it to any third party save as permitted under this Agreement or by law"),
            _p(2, "Return of Materials on written demand by the Disclosing Party.",
               numbering={"label": "6", "format": "decimal", "level": 0},
               style_name="Heading 1", heading_level=1),
        ])

    def test_bulleted_list_without_terminal_punctuation_stays_separate(self):
        self._assert_separate([
            _p(1, "the Receiving Party shall not use the Confidential Information",
               numbering={"label": "•", "format": "bullet", "level": 0}),
            _p(2, "the Receiving Party shall not copy the Confidential Information",
               numbering={"label": "•", "format": "bullet", "level": 0}),
            _p(3, "the Receiving Party shall not disclose the Confidential Information",
               numbering={"label": "•", "format": "bullet", "level": 0}),
        ])

    def test_document_title_then_defined_term_stays_separate(self):
        self._assert_separate([
            _p(1, "NON-DISCLOSURE AGREEMENT", style_name="Title"),
            _p(2, '(the "Agreement")'),
        ])

    def test_table_cells_never_merge(self):
        self._assert_separate([
            _p(1, "Party A", source_kind="table_cell", table={"row": 0, "col": 0}),
            _p(2, "Party B", source_kind="table_cell", table={"row": 0, "col": 1}),
        ])

    def test_supplemental_header_footer_never_merges(self):
        self._assert_separate([
            _p(1, "Strictly Private and Confidential", source_kind="supplemental"),
            _p(2, "Page 1 of 4", source_kind="supplemental"),
        ])


class PositiveFixtureTests(unittest.TestCase):
    """Fixtures that MUST merge into one clause for the model."""

    def _assert_merged(self, paragraphs):
        model, groups = merge_continuation_fragments(paragraphs)
        expected_ids = [str(p["id"]) for p in paragraphs]
        self.assertEqual(len(model), 1, "the whole clause should collapse to one record")
        self.assertEqual(model[0]["id"], expected_ids[0])
        self.assertEqual(model[0]["merged_fragment_ids"], expected_ids)
        for paragraph in paragraphs:
            self.assertEqual(groups[str(paragraph["id"])], expected_ids)
        # The merged text carries every fragment's words in order.
        for paragraph in paragraphs:
            self.assertIn(paragraph["text"].split()[-1], model[0]["text"])
        return model[0]

    def test_moorwand_exclusions_split_merges(self):
        merged = self._assert_merged([
            _p(1, "The obligations of confidentiality shall not apply to information that was already known to the Receiving Party before receipt of the Confidential Information from the Disclosing"),
            _p(2, "Party; or (ii) is or later becomes publicly available other than through a breach of this Agreement."),
        ])
        self.assertIn("Disclosing Party; or (ii)", merged["text"])

    def test_burden_of_proving_split_merges(self):
        self._assert_merged([
            _p(1, "The party asserting that information was independently developed carries the burden of proving"),
            _p(2, "independent development shall be on the party claiming the benefit of that exclusion."),
        ])

    def test_promptly_advise_split_merges(self):
        self._assert_merged([
            _p(1, "The Receiving Party shall promptly advise the Disclosing Party of any required disclosure of Confidential Information (pursuant to provisions 4 and 5) or"),
            _p(2, "unauthorised disclosure of Confidential Information by the Receiving Party or its Representatives of which it becomes aware."),
        ])

    def test_three_fragment_run_merges_transitively(self):
        self._assert_merged([
            _p(1, "The Receiving Party shall hold the Confidential Information in strict confidence and shall not, without the prior written consent of the Disclosing"),
            _p(2, "Party, disclose the Confidential Information to any third party except to those of its Representatives who need to know it for the"),
            _p(3, "Purpose and who are bound by obligations of confidentiality no less onerous than these."),
        ])

    def test_terminal_punctuation_closes_the_run(self):
        # p1+p2 merge (p1 incomplete), but p2 ends the sentence, so p3 (a new
        # sentence) is NOT swept in.
        paragraphs = [
            _p(1, "The Receiving Party shall keep confidential all Confidential Information disclosed by the Disclosing"),
            _p(2, "Party under this Agreement or any related agreement between the parties from time to time."),
            _p(3, "This obligation survives termination of this Agreement for five years."),
        ]
        model, groups = merge_continuation_fragments(paragraphs)
        self.assertEqual(len(model), 2)
        self.assertEqual(groups["p1"], ["p1", "p2"])
        self.assertEqual(groups["p2"], ["p1", "p2"])
        self.assertEqual(groups["p3"], ["p3"])


class ExpandGroupMembersTests(unittest.TestCase):
    def test_first_cited_member_expands_whole_group_in_document_order(self):
        groups = {"p4": ["p4", "p5"], "p5": ["p4", "p5"], "p7": ["p7"]}
        self.assertEqual(expand_group_members(["p4", "p7"], groups), ["p4", "p5", "p7"])
        # Even when the model cites only the tail limb, the head is recovered in order.
        self.assertEqual(expand_group_members(["p5"], groups), ["p4", "p5"])

    def test_no_groups_is_identity(self):
        self.assertEqual(expand_group_members(["p1", "p2"], None), ["p1", "p2"])


# --- Integration: a fragmented document run through the real pipeline -------------

TITLE = "Mutual Non-Disclosure Agreement"


def _fragmented_document_paragraphs():
    """A source-backed DOCX-like document whose Confidential Information clause is
    authored across two <w:p> fragments (p4 body, p5 continuation)."""
    return [
        _p(1, TITLE, style_name="Title"),
        _p(2, "Each party may disclose Confidential Information to the other party under this Agreement."),
        _p(3, "1. Confidential Information",
           structure_number="1", numbering={"label": "1", "format": "decimal", "level": 0}, style_name="Heading 1"),
        _p(4, '"Confidential Information" means all non-public business, financial and technical information disclosed by either party and expressly includes information that is or later becomes'),
        _p(5, "publicly available other than through a breach of this Agreement by the Receiving Party."),
        _p(6, "2. Governing Law",
           structure_number="2", numbering={"label": "2", "format": "decimal", "level": 0}, style_name="Heading 1"),
        _p(7, "This Agreement shall be governed by the laws of California."),
        _p(8, "3. Term",
           structure_number="3", numbering={"label": "3", "format": "decimal", "level": 0}, style_name="Heading 1"),
        _p(9, "The confidentiality obligations survive for a fixed period of five years."),
        _p(10, "4. Non-Circumvention",
           structure_number="4", numbering={"label": "4", "format": "decimal", "level": 0}, style_name="Heading 1"),
        _p(11, "Each party remains free to deal with third parties outside the Purpose."),
        _p(12, "5. Signatures",
           structure_number="5", numbering={"label": "5", "format": "decimal", "level": 0}, style_name="Heading 1"),
        _p(13, "For Aspora Limited"),
    ]


def _fragmented_source_text(paragraphs):
    return "\n\n".join(str(p["text"]) for p in paragraphs)


_INTEGRATION_QUOTES = {
    "mutuality": ("p2", "Each party may disclose Confidential Information"),
    "confidential_information": ("p4", '"Confidential Information" means all non-public business'),
    "governing_law": ("p7", "laws of California"),
    "term_and_survival": ("p9", "fixed period of five years"),
    "non_circumvention": ("p11", "free to deal with third parties"),
    "signatures": ("p13", "For Aspora Limited"),
}


def _integration_assessments(overrides=None):
    overrides = overrides or {}
    assessments = []
    for clause_id, (paragraph_id, quote) in _INTEGRATION_QUOTES.items():
        payload = {
            "clause_id": clause_id,
            "decision": "pass",
            "issue_type": "none",
            "rationale": f"{clause_id} satisfies the playbook based on the cited clause text.",
            "evidence": [{"paragraph_id": paragraph_id, "quote": quote, "relevance": "Supports the verdict."}],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.92,
            "blocks_send": False,
        }
        payload.update(overrides.get(clause_id, {}))
        assessments.append(payload)
    return assessments


class IntegrationMappingTests(unittest.TestCase):
    def test_packet_merges_the_fragmented_clause_for_the_model(self):
        paragraphs = _fragmented_document_paragraphs()
        source_text = _fragmented_source_text(paragraphs)
        packet = build_ai_assessment_packet(source_text, playbook=load_playbook(), paragraphs=paragraphs)
        packet_ids = [record["id"] for record in packet["paragraphs"]]
        # p5 was folded into p4: the model sees p4 (merged) but no standalone p5.
        self.assertIn("p4", packet_ids)
        self.assertNotIn("p5", packet_ids)
        merged_record = next(record for record in packet["paragraphs"] if record["id"] == "p4")
        self.assertIn('"Confidential Information" means all non-public business', merged_record["text"])
        self.assertIn("publicly available other than through a breach", merged_record["text"])
        # The headings and singleton clauses are untouched.
        self.assertIn("p3", packet_ids)
        self.assertIn("p7", packet_ids)

    def test_middle_limb_is_not_orphaned(self):
        paragraphs = _fragmented_document_paragraphs()
        source_text = _fragmented_source_text(paragraphs)
        result = build_ai_first_review_result(
            source_text,
            _integration_assessments(),
            paragraphs=paragraphs,
            verify=False,
        )
        clause = next(c for c in result["clauses"] if c["id"] == "confidential_information")
        # The model cited only the head fragment p4; both limbs now carry the verdict.
        self.assertEqual(clause["matched_paragraph_ids"], ["p4", "p5"])
        self.assertEqual(
            clause["matched_text"],
            f'{paragraphs[3]["text"]}\n\n{paragraphs[4]["text"]}',
        )
        # The continuation limb p5 is present in the structured evidence too.
        structured_ids = [record["paragraph_id"] for record in clause["structured_evidence"]]
        self.assertEqual(structured_ids, ["p4", "p5"])
        # A singleton clause is unaffected by the merge.
        governing = next(c for c in result["clauses"] if c["id"] == "governing_law")
        self.assertEqual(governing["matched_paragraph_ids"], ["p7"])
        # The evidence-provenance contract still holds end to end.
        self.assertEqual(validate_clause_evidence_trust(result, source_text), [])
        self.assertEqual(result["evidence_trust"], {"status": "verified", "errors": []})

    def test_boundary_spanning_quote_grounds_to_both_limbs(self):
        # The model, seeing the merged clause, quotes a span that crosses the original
        # p4/p5 boundary and cites the head id p4. It must still ground (to p4) and
        # highlight both limbs -- not mis-anchor to the top of the document or degrade
        # a sound clause to review.
        paragraphs = _fragmented_document_paragraphs()
        source_text = _fragmented_source_text(paragraphs)
        overrides = {
            "confidential_information": {
                "evidence": [{
                    "paragraph_id": "p4",
                    "quote": "information that is or later becomes publicly available",
                    "relevance": "The exclusion carve-out spans the split sentence.",
                }],
            },
        }
        result = build_ai_first_review_result(
            source_text,
            _integration_assessments(overrides),
            paragraphs=paragraphs,
            verify=False,
        )
        clause = next(c for c in result["clauses"] if c["id"] == "confidential_information")
        self.assertEqual(clause["matched_paragraph_ids"], ["p4", "p5"])
        self.assertEqual(clause["decision"], "pass")
        self.assertEqual(clause["grounding"]["status"], "grounded")
        self.assertEqual(validate_clause_evidence_trust(result, source_text), [])

    def test_stored_paragraphs_are_byte_unchanged_by_the_merge(self):
        paragraphs = _fragmented_document_paragraphs()
        source_text = _fragmented_source_text(paragraphs)
        result = build_ai_first_review_result(
            source_text,
            _integration_assessments(),
            paragraphs=paragraphs,
            verify=False,
        )
        stored_texts = [p["text"] for p in result["paragraphs"]]
        self.assertEqual(stored_texts, [p["text"] for p in paragraphs])
        # No merged text leaked into storage: p4 and p5 remain distinct records.
        stored_ids = [p["id"] for p in result["paragraphs"]]
        self.assertIn("p4", stored_ids)
        self.assertIn("p5", stored_ids)
        self.assertEqual(result["paragraphs"][3]["text"], paragraphs[3]["text"])
        self.assertEqual(result["paragraphs"][4]["text"], paragraphs[4]["text"])

    def test_redline_targets_original_fragment_ids(self):
        paragraphs = _fragmented_document_paragraphs()
        source_text = _fragmented_source_text(paragraphs)
        overrides = {
            "governing_law": {
                "decision": "fail",
                "issue_type": "present_but_wrong",
                "rationale": "Governing law is present but names a non-approved jurisdiction.",
                "blocks_send": False,
                "proposed_redline": {
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p7",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                    "jurisdiction": "England and Wales",
                },
                "evidence": [{"paragraph_id": "p7", "quote": "laws of California", "relevance": "Shows the jurisdiction."}],
            },
        }
        result = build_ai_first_review_result(
            source_text,
            _integration_assessments(overrides),
            paragraphs=paragraphs,
            verify=False,
        )
        redline = next(edit for edit in result["redline_edits"] if edit["clause_id"] == "governing_law")
        self.assertEqual(redline["action"], "replace_paragraph")
        self.assertEqual(redline["paragraph_id"], "p7")
        # The redline still reads and rewrites the ORIGINAL fragment text verbatim.
        self.assertEqual(
            redline["source_text"] if "source_text" in redline else redline.get("original_text"),
            "This Agreement shall be governed by the laws of California.",
        )
        self.assertEqual(validate_clause_evidence_trust(result, source_text), [])


if __name__ == "__main__":
    unittest.main()
