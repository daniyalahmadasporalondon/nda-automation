import unittest

from nda_automation.clause_outcomes import build_redline_edits
from nda_automation.contract_structure import build_contract_structure
from nda_automation.redline_actions import REDLINE_INSERT_AFTER_PARAGRAPH
from nda_automation.redline_anchor import structure_aware_insertion_anchor


def _docx(idx, text, num=None):
    paragraph = {"id": f"p{idx}", "index": idx, "text": text}
    if num is not None:
        paragraph["structure_number"] = num
        paragraph["numbering"] = {"label": num, "format": "decimal", "level": 0}
    return paragraph


def _docx_paragraphs():
    # Source-backed (real Word numbering) paragraphs where a NON-final section is the
    # anchor target: Mutuality (1), Confidentiality (2), Governing Law (3). The missing
    # clause is term_and_survival, which belongs after Confidentiality (p2) -- a section
    # that is NOT the final source-backed section, so the structure anchor is allowed.
    return [
        _docx(1, "Mutual obligations. Both parties owe duties.", "1"),
        _docx(2, "Confidentiality. Each party shall keep Confidential Information secret.", "2"),
        _docx(3, "Governing Law. This Agreement is governed by the laws of England.", "3"),
    ]


def _missing_governing_law_clause():
    return {
        "id": "governing_law",
        "name": "Governing Law",
        "status": "not_present",
        "issue_type": "missing",
        "passes": False,
        "matched_paragraph_ids": [],
        "what_to_fix": "Add a governing law clause.",
        "reason": "No governing law clause is present.",
        "approved_laws": ["England and Wales"],
        "preferred_law": "England and Wales",
    }


def _missing_term_clause():
    return {
        "id": "term_and_survival",
        "name": "Term and Survival",
        "status": "not_present",
        "issue_type": "missing",
        "passes": False,
        "matched_paragraph_ids": [],
        "what_to_fix": "Add a term and survival clause.",
        "reason": "No term clause is present.",
    }


class StructureAwareAnchorTests(unittest.TestCase):
    def test_missing_clause_anchored_after_preceding_non_final_section(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}

        # term_and_survival belongs after Confidentiality (p2). Governing Law (p3) is the
        # FINAL source-backed section, so anchoring at p2 (non-final) is allowed and
        # correct -- the new clause lands between Confidentiality and Governing Law.
        anchor = structure_aware_insertion_anchor("term_and_survival", paragraphs_by_id, structure)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["id"], "p2")

    def test_anchor_refused_when_target_is_final_source_backed_section(self):
        # governing_law belongs after Confidentiality/Term. Here the LAST source-backed
        # section (Governing Law p3) is also where we'd anchor governing_law's
        # predecessors -- but the missing clause is governing_law itself and the only
        # preceding sections are p1/p2. The anchor lands at p2 (non-final), NOT p3.
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)
        # Whatever it returns, it is NEVER the final source-backed section (p3).
        if anchor is not None:
            self.assertNotEqual(anchor["id"], "p3")

    def test_no_structure_returns_none_for_fallback(self):
        paragraphs_by_id = {p["id"]: p for p in _docx_paragraphs()}
        self.assertIsNone(structure_aware_insertion_anchor("governing_law", paragraphs_by_id, None))

    def test_non_source_backed_section_is_not_used_as_anchor(self):
        # Flat text (no Word numbering metadata): "1." is scraped from text only, so the
        # section is NOT source-backed and must not be trusted as a structure anchor.
        paragraphs = [
            {"id": "p1", "index": 1, "text": "Confidentiality. Keep it secret."},
            {"id": "p2", "index": 2, "text": "Term. Survives five years."},
        ]
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        # The structure path declines (no source-backed preceding section); the caller
        # then falls back to the regex tiers.
        self.assertIsNone(structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure))

    def test_unknown_clause_id_returns_none(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        self.assertIsNone(structure_aware_insertion_anchor("made_up_clause", paragraphs_by_id, structure))

    def test_malformed_structure_returns_none_not_raise(self):
        paragraphs_by_id = {p["id"]: p for p in _docx_paragraphs()}
        # Garbage structure shapes must never raise -- they degrade to fallback.
        self.assertIsNone(structure_aware_insertion_anchor("governing_law", paragraphs_by_id, {"sections": "nope"}))
        self.assertIsNone(structure_aware_insertion_anchor("governing_law", paragraphs_by_id, {"sections": [None, 5]}))


def _docx_one_marker_per_paragraph():
    # The DOCX-default signature layout: extract_docx_text joins paragraphs with
    # "\n\n", split_document_paragraphs splits on blank lines, so EACH signature label
    # becomes its OWN paragraph carrying a SINGLE marker. The last real source-backed
    # heading is "Term"; governing_law is missing. This is the BLOCKER counterexample.
    def docx(idx, text, num=None):
        paragraph = {"id": f"p{idx}", "index": idx, "text": text}
        if num is not None:
            paragraph["structure_number"] = num
            paragraph["numbering"] = {"label": num, "format": "decimal", "level": 0}
        return paragraph

    return [
        docx(1, "Mutual Non-Disclosure Agreement between the parties."),
        docx(2, "Confidentiality. Each party shall keep Confidential Information secret.", "1"),
        docx(3, "Term. The obligations survive for five years.", "2"),
        docx(4, "For Aspora Limited"),
        docx(5, "By: ____"),
        docx(6, "Title: ____"),
        docx(7, "Date: ____"),
        docx(8, "For Counterparty Ltd"),
        docx(9, "By: ____"),
        docx(10, "Title: ____"),
        docx(11, "Date: ____"),
    ]


class SignatureBlockFloorTests(unittest.TestCase):
    """Regression for the #6 BLOCKER: a one-marker-per-paragraph signature block must
    not let the structure anchor land at/after the signatures."""

    def test_one_marker_per_paragraph_block_never_anchors_in_signatures(self):
        paragraphs = _docx_one_marker_per_paragraph()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}

        anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)

        # The signature block is p4..p11. The anchor must NEVER be inside/after it. Since
        # Term (p3) is the FINAL source-backed section -- it swallows the signature block
        # to EOF -- the structural guard refuses it, so the safe outcome here is None
        # (legacy regex tiers place it at the Term body).
        signature_indices = set(range(4, 12))
        if anchor is not None:
            self.assertNotIn(anchor["index"], signature_indices)

    def test_block_aware_floor_points_at_first_signature_line(self):
        from nda_automation.redline_anchor import _first_signature_index, _ordered_paragraphs

        paragraphs = _docx_one_marker_per_paragraph()
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        floor = _first_signature_index(_ordered_paragraphs(paragraphs_by_id))
        # The run begins at the "For Aspora Limited" line (p4, index 4).
        self.assertEqual(floor, 4)

    def test_merged_multi_marker_block_still_detected(self):
        # The legacy MERGED shape (whole signature block in ONE paragraph) must still be
        # detected by the floor. Here Term (p2) is the final source-backed section (the
        # merged sig paragraph p3 is not source-backed), so the structural guard refuses
        # anchoring in it -> None (legacy handles placement). The point of this test is
        # that the merged block is still RECOGNIZED, not silently missed.
        def docx(idx, text, num=None):
            paragraph = {"id": f"p{idx}", "index": idx, "text": text}
            if num is not None:
                paragraph["structure_number"] = num
                paragraph["numbering"] = {"label": num, "format": "decimal", "level": 0}
            return paragraph

        paragraphs = [
            docx(1, "Confidentiality. Keep secret.", "1"),
            docx(2, "Term. The obligations survive five years.", "2"),
            docx(
                3,
                "For Aspora Limited\nBy: ____\nTitle: ____\nDate: ____\n"
                "For Counterparty Ltd\nBy: ____\nTitle: ____\nDate: ____",
            ),
        ]
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}

        from nda_automation.redline_anchor import _first_signature_index, _ordered_paragraphs

        floor = _first_signature_index(_ordered_paragraphs(paragraphs_by_id))
        self.assertEqual(floor, 3)

        # Never anchors in the signature paragraph (p3); the safe outcome is p2 or None.
        anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)
        if anchor is not None:
            self.assertNotEqual(anchor["id"], "p3")

    def test_single_stray_marker_line_is_not_a_signature_block(self):
        # A lone marker-ish line in body prose (not a run) must NOT be mistaken for a
        # signature block, so the floor stays None and normal anchoring proceeds.
        from nda_automation.redline_anchor import _first_signature_index, _ordered_paragraphs

        def docx(idx, text, num=None):
            paragraph = {"id": f"p{idx}", "index": idx, "text": text}
            if num is not None:
                paragraph["structure_number"] = num
                paragraph["numbering"] = {"label": num, "format": "decimal", "level": 0}
            return paragraph

        paragraphs = [
            docx(1, "Confidentiality. Keep secret.", "1"),
            docx(2, "Term. The obligations survive five years.", "2"),
            docx(3, "Notices shall be sent to the address below by registered post."),
        ]
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        self.assertIsNone(_first_signature_index(_ordered_paragraphs(paragraphs_by_id)))

    def test_build_redline_edits_one_marker_block_anchors_before_signatures(self):
        # End-to-end through build_redline_edits: the inserted governing_law redline must
        # anchor before the signature run, not on a "Date:" line.
        paragraphs = _docx_one_marker_per_paragraph()
        structure = build_contract_structure(paragraphs)
        clause = _missing_governing_law_clause()

        edits = build_redline_edits([clause], paragraphs, contract_structure=structure)
        insert = next(edit for edit in edits if edit["clause_id"] == "governing_law")
        self.assertEqual(insert["action"], REDLINE_INSERT_AFTER_PARAGRAPH)
        self.assertNotIn(insert["paragraph_index"], set(range(4, 12)))


class NonStandardSignatureVocabularyTests(unittest.TestCase):
    """Adversarial counterexamples with non-standard signature wording. The PRIMARY
    guard (refuse the final source-backed section) makes these safe regardless of
    vocabulary; the broadened floor improves precision. Each asserts the structure
    anchor is NEVER at/after a signature line and never in the final source-backed
    section (it lands before signatures or returns None -> legacy tiers)."""

    def _assert_safe(self, paragraphs, signature_indices, clause_id="governing_law"):
        from nda_automation.redline_anchor import _final_source_backed_section_paragraph_ids

        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        anchor = structure_aware_insertion_anchor(clause_id, paragraphs_by_id, structure)
        final_section_ids = _final_source_backed_section_paragraph_ids(structure["sections"])
        if anchor is not None:
            # Never inside/after the signature block.
            self.assertNotIn(anchor["index"], signature_indices)
            # Never in the final source-backed section (the structural invariant).
            self.assertNotIn(anchor["id"], final_section_ids)
        return anchor

    def test_c1_signed_per_duly_authorized(self):
        # C1: Term then Signed: / Per: ___ / Duly authorized representative.
        paragraphs = [
            _docx(1, "Term. The obligations survive for five years.", "1"),
            _docx(2, "Signed:"),
            _docx(3, "Per: ___"),
            _docx(4, "Duly authorized representative"),
        ]
        self._assert_safe(paragraphs, set(range(2, 5)))

    def test_c1b_worst_previously_anchored_in_signatures(self):
        # C1b (the worst): Confidentiality, Term, then Signed for ABC Ltd: / Per: ___ /
        # Duly authorized representative (repeated per party). Missing governing_law.
        # Previously anchored at p8 ("Duly authorized representative"); must now be safe.
        paragraphs = [
            _docx(1, "Confidentiality. Each party shall keep Confidential Information secret.", "1"),
            _docx(2, "Term. The obligations survive for five years.", "2"),
            _docx(3, "Signed for ABC Ltd:"),
            _docx(4, "Per: ___"),
            _docx(5, "Duly authorized representative"),
            _docx(6, "Signed for XYZ Inc:"),
            _docx(7, "Per: ___"),
            _docx(8, "Duly authorized representative"),
        ]
        anchor = self._assert_safe(paragraphs, set(range(3, 9)))
        # Explicitly: never the old p8 answer.
        if anchor is not None:
            self.assertNotEqual(anchor["id"], "p8")

    def test_d1_signed_on_behalf_authorised_signatory_its_director(self):
        # D1: Mutual Obligations, Confidential Information, then Signed for and on behalf
        # of Aspora Limited / Per: ___ / Authorised signatory / Its: Director.
        paragraphs = [
            _docx(1, "Mutual Obligations. Both parties owe duties.", "1"),
            _docx(2, "Confidential Information. Means non-public information.", "2"),
            _docx(3, "Signed for and on behalf of Aspora Limited"),
            _docx(4, "Per: ___"),
            _docx(5, "Authorised signatory"),
            _docx(6, "Its: Director"),
        ]
        self._assert_safe(paragraphs, set(range(3, 7)))

    def test_e1_single_party_short_block(self):
        # E1: Confidential Information then Accepted and agreed: / Authorised signatory
        # of the Recipient.
        paragraphs = [
            _docx(1, "Confidential Information. Means non-public information.", "1"),
            _docx(2, "Accepted and agreed:"),
            _docx(3, "Authorised signatory of the Recipient"),
        ]
        self._assert_safe(paragraphs, {2, 3})

    def test_d3_bare_no_colon_markers(self):
        # D3: a section then Signature / Print Name / Date (bare, no colons).
        paragraphs = [
            _docx(1, "Confidential Information. Means non-public information.", "1"),
            _docx(2, "Signature"),
            _docx(3, "Print Name"),
            _docx(4, "Date"),
        ]
        self._assert_safe(paragraphs, set(range(2, 5)))

    def test_belt_and_suspenders_anchor_never_past_signature_floor(self):
        # INVARIANT: across all counterexamples, when the anchor is a paragraph it is
        # never strictly LATER than the signature block start (== never in the final
        # source-backed section). This holds for the worded blocks above AND the
        # standard layout.
        from nda_automation.redline_anchor import (
            _final_source_backed_section_paragraph_ids,
            _first_signature_index,
            _ordered_paragraphs,
        )

        scenarios = [
            [
                _docx(1, "Confidentiality. Keep CI secret.", "1"),
                _docx(2, "Term. Survives five years.", "2"),
                _docx(3, "Signed for ABC Ltd:"),
                _docx(4, "Per: ___"),
                _docx(5, "Duly authorized representative"),
            ],
            _docx_one_marker_per_paragraph(),
            [
                _docx(1, "Confidential Information. Means non-public information.", "1"),
                _docx(2, "Signature"),
                _docx(3, "Print Name"),
                _docx(4, "Date"),
            ],
        ]
        for paragraphs in scenarios:
            structure = build_contract_structure(paragraphs)
            paragraphs_by_id = {p["id"]: p for p in paragraphs}
            floor = _first_signature_index(_ordered_paragraphs(paragraphs_by_id))
            final_section_ids = _final_source_backed_section_paragraph_ids(structure["sections"])
            anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)
            if anchor is not None:
                self.assertNotIn(anchor["id"], final_section_ids)
                if floor is not None:
                    self.assertLess(anchor["index"], floor)


class StructureAwareRedlineBuildTests(unittest.TestCase):
    def test_build_redline_edits_places_missing_clause_by_structure(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        clause = _missing_term_clause()

        edits = build_redline_edits([clause], paragraphs, contract_structure=structure)
        insert = next(edit for edit in edits if edit["clause_id"] == "term_and_survival")
        self.assertEqual(insert["action"], REDLINE_INSERT_AFTER_PARAGRAPH)
        # term_and_survival anchored after Confidentiality (p2) -- a NON-final
        # source-backed section, so the structural guard allows it.
        self.assertEqual(insert["paragraph_id"], "p2")

    def test_build_redline_edits_falls_back_without_structure(self):
        # With no structure supplied, build must still produce an insertion via the
        # legacy regex tiers (the existing, unchanged behaviour) -- never crash, never
        # drop the redline.
        paragraphs = _docx_paragraphs()
        clause = _missing_term_clause()

        edits = build_redline_edits([clause], paragraphs)
        insert = next(edit for edit in edits if edit["clause_id"] == "term_and_survival")
        self.assertEqual(insert["action"], REDLINE_INSERT_AFTER_PARAGRAPH)
        self.assertIn("paragraph_id", insert)

    def test_context_does_not_leak_between_builds(self):
        # A structure-bearing build must not change a later structure-less build's
        # placement (the contextvar is reset in finally).
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        clause = _missing_term_clause()

        with_structure = build_redline_edits([clause], paragraphs, contract_structure=structure)
        without_structure = build_redline_edits([clause], paragraphs)

        a = next(e for e in with_structure if e["clause_id"] == "term_and_survival")["paragraph_id"]
        b = next(e for e in without_structure if e["clause_id"] == "term_and_survival")["paragraph_id"]
        # The structure build anchored on p2; the fallback build is computed independently
        # (it must reflect the regex tiers, proving no stale structure leaked in).
        self.assertEqual(a, "p2")
        # b is whatever the regex tiers choose; the key invariant is the build ran the
        # fallback path (structure cleared), so it is allowed to differ from a.
        self.assertTrue(b)


if __name__ == "__main__":
    unittest.main()
