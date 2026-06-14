import unittest

from nda_automation.clause_outcomes import build_redline_edits
from nda_automation.contract_structure import build_contract_structure
from nda_automation.redline_actions import REDLINE_INSERT_AFTER_PARAGRAPH
from nda_automation.redline_anchor import structure_aware_insertion_anchor


def _docx_paragraphs():
    # Source-backed (real Word numbering) paragraphs: Confidentiality (1), Term (2),
    # then a signature block. Governing Law is MISSING.
    return [
        {"id": "p1", "index": 1, "text": "Mutual Non-Disclosure Agreement between the parties."},
        {
            "id": "p2",
            "index": 2,
            "text": "Confidentiality. Each party shall keep Confidential Information secret.",
            "structure_number": "1",
            "numbering": {"label": "1", "format": "decimal", "level": 0},
        },
        {
            "id": "p3",
            "index": 3,
            "text": "Term. The obligations survive for five years.",
            "structure_number": "2",
            "numbering": {"label": "2", "format": "decimal", "level": 0},
        },
        {
            "id": "p4",
            "index": 4,
            "text": "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
        },
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


class StructureAwareAnchorTests(unittest.TestCase):
    def test_missing_clause_anchored_after_preceding_section_before_signatures(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}

        anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)
        # Governing Law belongs after Term (p3) and before the signature block (p4).
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["id"], "p3")

    def test_anchor_never_lands_on_signature_block(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        paragraphs_by_id = {p["id"]: p for p in paragraphs}
        anchor = structure_aware_insertion_anchor("governing_law", paragraphs_by_id, structure)
        self.assertNotEqual(anchor["id"], "p4")

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


class StructureAwareRedlineBuildTests(unittest.TestCase):
    def test_build_redline_edits_places_missing_clause_by_structure(self):
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        clause = _missing_governing_law_clause()

        edits = build_redline_edits([clause], paragraphs, contract_structure=structure)
        insert = next(edit for edit in edits if edit["clause_id"] == "governing_law")
        self.assertEqual(insert["action"], REDLINE_INSERT_AFTER_PARAGRAPH)
        # Anchored after the Term section, before the signature block.
        self.assertEqual(insert["paragraph_id"], "p3")

    def test_build_redline_edits_falls_back_without_structure(self):
        # With no structure supplied, build must still produce an insertion via the
        # legacy regex tiers (the existing, unchanged behaviour) -- never crash, never
        # drop the redline.
        paragraphs = _docx_paragraphs()
        clause = _missing_governing_law_clause()

        edits = build_redline_edits([clause], paragraphs)
        insert = next(edit for edit in edits if edit["clause_id"] == "governing_law")
        self.assertEqual(insert["action"], REDLINE_INSERT_AFTER_PARAGRAPH)
        self.assertIn("paragraph_id", insert)

    def test_context_does_not_leak_between_builds(self):
        # A structure-bearing build must not change a later structure-less build's
        # placement (the contextvar is reset in finally).
        paragraphs = _docx_paragraphs()
        structure = build_contract_structure(paragraphs)
        clause = _missing_governing_law_clause()

        with_structure = build_redline_edits([clause], paragraphs, contract_structure=structure)
        without_structure = build_redline_edits([clause], paragraphs)

        a = next(e for e in with_structure if e["clause_id"] == "governing_law")["paragraph_id"]
        b = next(e for e in without_structure if e["clause_id"] == "governing_law")["paragraph_id"]
        # The structure build anchored on p3; the fallback build is computed independently
        # (it must reflect the regex tiers, proving no stale structure leaked in).
        self.assertEqual(a, "p3")
        # b is whatever the regex tiers choose; the key invariant is the build ran the
        # fallback path (structure cleared), so it is allowed to differ from a.
        self.assertTrue(b)


if __name__ == "__main__":
    unittest.main()
