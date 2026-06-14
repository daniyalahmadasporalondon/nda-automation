"""Structure-aware redline insertion anchor (roadmap #6, attempt #4).

#6 was DROPPED from main three times because each version could place a newly
inserted MISSING clause INSIDE or AFTER the signature/execution block of a near-signed
NDA -- worse than the legacy regex placement. This suite reproduces EVERY prior
counterexample and pins the two invariants of the robust re-implementation:

  INVARIANT 1 (signature-region guard): the anchor is NEVER at/after the detected
      signature-region start paragraph. It lands strictly before, or returns None so
      the legacy regex tiers run.
  INVARIANT 2 (never-worse-than-legacy): _insertion_anchor_paragraph never returns a
      structure anchor at a LATER paragraph index than the legacy regex tiers would.

The counterexamples (named per the roadmap brief):
  C1   : "1. Term" then Signed:/Per: ___/Duly authorized representative.
  C1b  : the worst -- "1. Confidentiality","2. Term", then per-party
         "Signed for ABC Ltd:"/"Per: ___"/"Duly authorized representative",
         missing governing_law (signatures live in the FINAL source-backed section).
  D1   : clauses then "Signed for and on behalf of Aspora Limited"/"Per: ___"/
         "Authorised signatory"/"Its: Director".
  E1   : single-party -- "1. Confidential Information" then "Accepted and agreed:"/
         "Authorised signatory of the Recipient".
  D3   : a clause then bare "Signature"/"Print Name"/"Date".
  SCHEDULES-AFTER-SIGNATURES: clauses, a one-marker-per-paragraph signature block,
         THEN a source-backed "Schedule A" heading + content (signatures now in a
         NON-final source-backed section -- the premise the prior attempts assumed
         false). The anchor must STILL refuse the signature region.
  DEED + NOTARY: "EXECUTED as a DEED by ... / John Smith / Director" then a
         source-backed "Notarial Acknowledgment" heading.

Plus: the standard "For X"/"By:"/"Title:"/"Date:" layout captured correctly, and a
body-prose stray "Date:" NOT mistaken for a signature region.
"""
from __future__ import annotations

import unittest

from nda_automation import clause_outcomes
from nda_automation import redline_anchor as ra
from nda_automation.contract_structure import build_contract_structure
from nda_automation.review_document import align_document_paragraphs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _numbered(number: str, heading: str) -> dict:
    """A source-backed numbered heading paragraph (real Word numbering metadata)."""
    return {
        "text": f"{number}. {heading}",
        "structure_number": number,
        "source_kind": "docx",
    }


def _body(text: str) -> dict:
    return {"text": text, "source_kind": "docx"}


def _heading(text: str) -> dict:
    """A source-backed style-driven heading (Heading 1) with no number."""
    return {"text": text, "heading_level": 1, "source_kind": "docx"}


def _build(raw: list[dict]) -> tuple[list[dict], dict, dict]:
    """Return (paragraphs, paragraphs_by_id, contract_structure) built the real way."""
    for index, paragraph in enumerate(raw):
        paragraph.setdefault("source_index", index)
    source_text = "\n".join(str(paragraph["text"]) for paragraph in raw)
    paragraphs = align_document_paragraphs(raw, source_text)
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    structure = build_contract_structure(paragraphs)
    return paragraphs, paragraphs_by_id, structure


def _missing_clause(clause_id: str) -> dict:
    """A clause result shaped like a MISSING required clause (drives an insert).

    Carries the minimal Playbook-derived fields each builder needs to actually emit an
    ``insert_after_paragraph`` edit (otherwise it would no-op): governing_law needs
    approved laws to build template options; term_and_survival / mutuality /
    confidential_information need a fallback redline_template wording.
    """
    clause: dict = {
        "id": clause_id,
        "name": clause_id.replace("_", " ").title(),
        "status": "not_present",
        "issue_type": "missing",
        "passes": False,
        "matched_paragraph_ids": [],
    }
    if clause_id == "governing_law":
        clause["approved_laws"] = ["England and Wales"]
        clause["preferred_law"] = "England and Wales"
    if clause_id == "term_and_survival":
        clause["redline_template"] = (
            "This Agreement shall remain in force for {max_term_years_label}."
        )
        clause["max_term_years"] = 3
    if clause_id == "mutuality":
        clause["redline_template"] = "The obligations under this Agreement are mutual."
    if clause_id == "confidential_information":
        clause["redline_template"] = "Confidential Information means non-public information."
    return clause


def _index_of(paragraphs_by_id: dict, predicate) -> int | None:
    for paragraph in paragraphs_by_id.values():
        if predicate(str(paragraph["text"])):
            return int(paragraph["index"])
    return None


# ---------------------------------------------------------------------------
# Signature-region detector (Invariant 1, unit level)
# ---------------------------------------------------------------------------


class SignatureRegionDetectorTests(unittest.TestCase):
    def _region(self, raw: list[dict]) -> int | None:
        _paragraphs, paragraphs_by_id, _structure = _build(raw)
        return ra.signature_region_start_index(paragraphs_by_id)

    def test_c1_signed_per_role_run(self):
        # C1: "1. Term" then Signed:/Per: ___/Duly authorized representative.
        raw = [
            _numbered("1", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        # Region starts at the FIRST signature-ish line ("Signed:").
        self.assertEqual(self._region(raw), 3)

    def test_c1b_per_party_block_in_final_section(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        self.assertEqual(self._region(raw), 5)

    def test_d1_on_behalf_authorised_signatory_director(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("Signed for and on behalf of Aspora Limited"),
            _body("Per: ___"),
            _body("Authorised signatory"),
            _body("Its: Director"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_e1_single_party_accepted_and_agreed(self):
        raw = [
            _numbered("1", "Confidential Information"),
            _body("The Recipient shall keep Confidential Information secret."),
            _body("Accepted and agreed:"),
            _body("Authorised signatory of the Recipient"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_d3_bare_signature_print_name_date(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("Signature"),
            _body("Print Name"),
            _body("Date"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_deed_execution_preamble_is_region_opener(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("EXECUTED as a DEED by the parties"),
            _body("John Smith"),
            _body("Director"),
        ]
        # The execution preamble opens the region on its own.
        self.assertEqual(self._region(raw), 3)

    def test_in_witness_whereof_is_region_opener(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("IN WITNESS WHEREOF the parties have executed this Agreement."),
            _body("John Smith"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_notary_heading_is_region_opener(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("Notarial Acknowledgment"),
            _body("Sworn before me this day."),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_standard_for_by_title_date_layout(self):
        # The classic "For X" + per-marker lines layout is captured correctly.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("For Acme Corporation"),
            _body("By: ___"),
            _body("Title: Director"),
            _body("Date: 1 January 2026"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_merged_single_paragraph_block(self):
        # The legacy merged shape: one paragraph carrying multiple markers.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("By: ___\nTitle: Director\nDate: 1 Jan 2026"),
        ]
        self.assertEqual(self._region(raw), 3)

    def test_body_prose_stray_date_is_not_a_region(self):
        # A single "Date:" appearing in a notices clause must NOT be read as a region.
        raw = [
            _numbered("1", "Notices"),
            _body("Notices shall be given in writing."),
            _body("Date: the date of receipt shall be the date of delivery."),
            _body("All notices take effect on receipt."),
        ]
        self.assertIsNone(self._region(raw))

    def test_no_signatures_at_all(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
        ]
        self.assertIsNone(self._region(raw))

    def test_body_prose_for_lines_are_not_a_region(self):
        # "For the purposes of" / "For the avoidance of doubt" are body prose, not a
        # "For <party>" signature line (the SIGNATURE_FOR_LINE_PATTERN excludes them).
        raw = [
            _numbered("1", "Confidentiality"),
            _body("For the purposes of this Agreement, Confidential Information means non-public data."),
            _body("For the avoidance of doubt, the obligations are mutual."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
        ]
        self.assertIsNone(self._region(raw))

    def test_director_mid_sentence_is_not_a_region(self):
        # "Director" inside a sentence must not single-handedly open a region.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("The Director of each party shall enforce these obligations."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
        ]
        self.assertIsNone(self._region(raw))


# ---------------------------------------------------------------------------
# Structure anchor end-to-end (Invariant 1, via build_redline_edits)
# ---------------------------------------------------------------------------


class StructureAnchorPlacementTests(unittest.TestCase):
    """For each counterexample: insert a MISSING clause via the real redline builder
    with the real contract structure, and assert the insertion lands STRICTLY before
    the detected signature region (never at/after any signature paragraph)."""

    def _insert_anchor_index(self, raw: list[dict], clause_id: str) -> tuple[int, int | None]:
        """Return (anchor_paragraph_index, signature_region_index)."""
        paragraphs, paragraphs_by_id, structure = _build(raw)
        edits = clause_outcomes.build_redline_edits(
            [_missing_clause(clause_id)], paragraphs, contract_structure=structure
        )
        self.assertEqual(len(edits), 1, "expected exactly one insertion edit")
        edit = edits[0]
        self.assertEqual(edit["action"], "insert_after_paragraph")
        region = ra.signature_region_start_index(paragraphs_by_id)
        return int(edit["paragraph_index"]), region

    def _assert_before_region(self, raw: list[dict], clause_id: str):
        anchor_index, region = self._insert_anchor_index(raw, clause_id)
        self.assertIsNotNone(region, "the layout must have a detected signature region")
        self.assertLess(
            anchor_index,
            region,
            f"insertion anchor p{anchor_index} must precede the signature region "
            f"starting at p{region}",
        )

    def test_c1(self):
        raw = [
            _numbered("1", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        self._assert_before_region(raw, "governing_law")

    def test_c1b_worst_case(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
            _body("Signed for XYZ Inc:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        anchor_index, region = self._insert_anchor_index(raw, "governing_law")
        self.assertEqual(region, 5)
        # The anchor lands strictly before the signature region (the core safety
        # invariant). Invariant 2 (never-worse-than-legacy) keeps it at the legacy
        # pick p3 ("2. Term") here, since the structure pick p4 is not strictly
        # earlier than legacy -- either way it is well clear of the signatures.
        self.assertLess(anchor_index, region)
        self.assertIn(anchor_index, (3, 4))

    def test_d1(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for and on behalf of Aspora Limited"),
            _body("Per: ___"),
            _body("Authorised signatory"),
            _body("Its: Director"),
        ]
        self._assert_before_region(raw, "governing_law")

    def test_e1_single_party(self):
        raw = [
            _numbered("1", "Confidential Information"),
            _body("The Recipient shall keep Confidential Information secret."),
            _body("Accepted and agreed:"),
            _body("Authorised signatory of the Recipient"),
        ]
        self._assert_before_region(raw, "term_and_survival")

    def test_d3_bare_labels(self):
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _body("Signature"),
            _body("Print Name"),
            _body("Date"),
        ]
        self._assert_before_region(raw, "term_and_survival")

    def test_schedules_after_signatures(self):
        # The premise the prior attempts assumed false: a source-backed Schedule
        # FOLLOWS the signatures, so the signatures are NOT in the final source-backed
        # section. The anchor must STILL refuse the signature region.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
            _heading("Schedule A"),
            _body("The Confidential Information comprises the following materials."),
        ]
        paragraphs, paragraphs_by_id, structure = _build(raw)
        # Sanity: the signatures are NOT in the final source-backed section.
        source_sections = [s for s in structure["sections"] if "source" in s]
        final_section = max(source_sections, key=lambda s: s.get("end_index", -1))
        self.assertNotIn(
            "p5", final_section["paragraph_ids"],
            "the signature block must sit in a NON-final section for this test",
        )
        region = ra.signature_region_start_index(paragraphs_by_id)
        self.assertEqual(region, 5)
        edits = clause_outcomes.build_redline_edits(
            [_missing_clause("governing_law")], paragraphs, contract_structure=structure
        )
        self.assertEqual(len(edits), 1)
        anchor_index = int(edits[0]["paragraph_index"])
        # The anchor must be before the signature region -- NOT in the schedule after it.
        self.assertLess(anchor_index, region)
        self.assertIn(anchor_index, (3, 4))

    def test_deed_then_notary_heading(self):
        # A deed execution preamble, then a source-backed notarial acknowledgment
        # heading following the signatures. The anchor must refuse the whole region.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("EXECUTED as a DEED by Aspora Limited"),
            _body("John Smith"),
            _body("Director"),
            _heading("Notarial Acknowledgment"),
            _body("Sworn before me this first day of January."),
        ]
        paragraphs, paragraphs_by_id, structure = _build(raw)
        region = ra.signature_region_start_index(paragraphs_by_id)
        self.assertEqual(region, 5)
        edits = clause_outcomes.build_redline_edits(
            [_missing_clause("governing_law")], paragraphs, contract_structure=structure
        )
        self.assertEqual(len(edits), 1)
        anchor_index = int(edits[0]["paragraph_index"])
        self.assertLess(anchor_index, region)
        self.assertIn(anchor_index, (3, 4))


# ---------------------------------------------------------------------------
# Invariant 2: never worse than legacy
# ---------------------------------------------------------------------------


class NeverWorseThanLegacyTests(unittest.TestCase):
    def _legacy_anchor_index(self, paragraphs_by_id: dict, clause_id: str) -> int:
        """The anchor index the legacy regex tiers alone would pick (no structure)."""
        anchor = clause_outcomes._insertion_anchor_paragraph(
            _missing_clause(clause_id), paragraphs_by_id
        )
        self.assertIsNotNone(anchor)
        return int(anchor["index"])

    def _structure_anchor_index(self, paragraphs, structure, clause_id: str) -> int:
        edits = clause_outcomes.build_redline_edits(
            [_missing_clause(clause_id)], paragraphs, contract_structure=structure
        )
        self.assertEqual(len(edits), 1)
        return int(edits[0]["paragraph_index"])

    def test_structure_never_anchors_later_than_legacy(self):
        # Across every counterexample layout, the structure-aware anchor index must be
        # <= the legacy anchor index (never later -> never closer to signatures).
        layouts = {
            "c1b": [
                _numbered("1", "Confidentiality"),
                _body("Each party shall keep Confidential Information secret."),
                _numbered("2", "Term"),
                _body("This Agreement remains in force for three years."),
                _body("Signed for ABC Ltd:"),
                _body("Per: ___"),
                _body("Duly authorized representative"),
            ],
            "schedules_after_signatures": [
                _numbered("1", "Confidentiality"),
                _body("Each party shall keep Confidential Information secret."),
                _numbered("2", "Term"),
                _body("This Agreement remains in force for three years."),
                _body("Signed for ABC Ltd:"),
                _body("Per: ___"),
                _body("Duly authorized representative"),
                _heading("Schedule A"),
                _body("The Confidential Information comprises the following materials."),
            ],
            "deed_notary": [
                _numbered("1", "Confidentiality"),
                _body("Each party shall keep Confidential Information secret."),
                _numbered("2", "Term"),
                _body("This Agreement remains in force for three years."),
                _body("EXECUTED as a DEED by Aspora Limited"),
                _body("John Smith"),
                _body("Director"),
                _heading("Notarial Acknowledgment"),
                _body("Sworn before me this first day of January."),
            ],
        }
        for name, raw in layouts.items():
            with self.subTest(layout=name):
                paragraphs, paragraphs_by_id, structure = _build(raw)
                legacy_index = self._legacy_anchor_index(paragraphs_by_id, "governing_law")
                structure_index = self._structure_anchor_index(paragraphs, structure, "governing_law")
                self.assertLessEqual(
                    structure_index,
                    legacy_index,
                    f"[{name}] structure anchor p{structure_index} must not be later "
                    f"than legacy anchor p{legacy_index}",
                )

    def test_invariant2_alone_is_sufficient_when_region_guard_disabled(self):
        # Defense-in-depth proof: disable BOTH Invariant-1 layers (the region scan AND
        # the per-paragraph signature-line check) so the RAW structure anchor wanders
        # INTO the signature block (p7, "Duly authorized representative"). Invariant 2
        # (never-worse-than-legacy) must STILL clamp the final anchor back to the safe
        # legacy pick. This is the airtight guarantee: even if region detection ever
        # under-detects a novel signature wording, the anchor can never land later than
        # the battle-tested legacy tiers, so it can never enter the signatures.
        from unittest.mock import patch

        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        paragraphs, paragraphs_by_id, structure = _build(raw)
        region = ra.signature_region_start_index(paragraphs_by_id)
        self.assertEqual(region, 5)

        with patch.object(ra, "_signature_region_start_index", return_value=None), patch.object(
            ra, "_is_signature_line_paragraph", return_value=False
        ):
            # Confirm the raw structure anchor really does wander into the signatures.
            wandering = ra.structure_aware_insertion_anchor(
                "governing_law", paragraphs_by_id, structure
            )
            self.assertIsNotNone(wandering)
            self.assertGreaterEqual(int(wandering["index"]), region)
            # ...but the full builder, via Invariant 2, still lands safely before them.
            anchor_index = self._structure_anchor_index(paragraphs, structure, "governing_law")
        self.assertLess(anchor_index, region)

    def test_no_structure_reproduces_legacy_exactly(self):
        # With no contract_structure supplied, the builder must behave identically to
        # the legacy regex tiers (zero behaviour change for non-structure callers).
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
        ]
        paragraphs, paragraphs_by_id, _structure = _build(raw)
        legacy_index = self._legacy_anchor_index(paragraphs_by_id, "governing_law")
        edits = clause_outcomes.build_redline_edits(
            [_missing_clause("governing_law")], paragraphs, contract_structure=None
        )
        self.assertEqual(len(edits), 1)
        self.assertEqual(int(edits[0]["paragraph_index"]), legacy_index)


# ---------------------------------------------------------------------------
# Regression demonstration: the OLD final-source-backed-section premise FAILS the
# schedules-after-signatures case. This documents WHY the prior attempts were dropped
# and proves the new design is not equivalent to them.
# ---------------------------------------------------------------------------


class PriorPremiseWouldFailTests(unittest.TestCase):
    def test_signatures_live_outside_final_section_when_schedule_follows(self):
        # The prior attempts guarded ONLY the "final source-backed section". Here a
        # source-backed Schedule follows the signatures, so the final source-backed
        # section is the SCHEDULE -- and the signature block sits in section-2 (Term),
        # which is NOT final. A final-section-only guard would have left the signatures
        # unprotected and could anchor among them. The current region guard does not.
        raw = [
            _numbered("1", "Confidentiality"),
            _body("Each party shall keep Confidential Information secret."),
            _numbered("2", "Term"),
            _body("This Agreement remains in force for three years."),
            _body("Signed for ABC Ltd:"),
            _body("Per: ___"),
            _body("Duly authorized representative"),
            _heading("Schedule A"),
            _body("The Confidential Information comprises the following materials."),
        ]
        _paragraphs, paragraphs_by_id, structure = _build(raw)
        source_sections = [s for s in structure["sections"] if "source" in s]
        final_section = max(source_sections, key=lambda s: s.get("end_index", -1))
        # Final source-backed section is the Schedule (p8..), NOT where signatures live.
        self.assertEqual(final_section["heading"], "Schedule A")
        self.assertNotIn("p5", final_section["paragraph_ids"])
        # But the region detector still finds the signature block at p5.
        self.assertEqual(ra.signature_region_start_index(paragraphs_by_id), 5)


if __name__ == "__main__":
    unittest.main()
