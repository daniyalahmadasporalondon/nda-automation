"""Tests for the document-level reference-integrity signal (structure-awareness #1).

The reference resolver already computes per-reference resolution status and the
structure already flags ambiguous alias collisions; this signal rolls them up into
one additive, document-level record. Guards keep it from crying wolf on a PDF /
collapsed parse, and treat ambiguous collisions as unknown rather than dangling.
"""
import unittest

from nda_automation.contract_structure import build_contract_structure
from nda_automation.reference_resolver import (
    REFERENCE_INTEGRITY_VERSION,
    build_reference_integrity_signal,
    resolve_document_references,
)
from nda_automation.review_document import split_document_paragraphs


def _numbered(paragraph_id, index, text, label):
    return {
        "id": paragraph_id,
        "index": index,
        "text": text,
        "numbering": {"label": label, "format": "decimal", "level": 0},
        "source_kind": "paragraph",
    }


def _body(paragraph_id, index, text):
    return {"id": paragraph_id, "index": index, "text": text, "source_kind": "paragraph"}


def _docx_numbered_paragraphs():
    """A clean DOCX-style parse with real numbered sections and a dangling reference.

    "Schedule 3" is referenced but no Schedule 3 (nor a Section 3 in the attachment
    namespace) exists -- the dangling cross-reference the signal must surface.
    """
    return [
        _numbered("p1", 0, "Section 1 Definitions", "1."),
        _body("p2", 1, "Confidential Information means non-public information."),
        _numbered("p3", 2, "Section 2 Confidentiality", "2."),
        _body("p4", 3, "Each party shall protect Confidential Information as set out in Schedule 3."),
        _numbered("p5", 4, "Section 3 Term", "3."),
        _body("p6", 5, "The obligations in Section 2 survive termination."),
    ]


class ReferenceIntegritySignalTests(unittest.TestCase):
    def test_dangling_reference_fires_on_clean_docx(self):
        paragraphs = _docx_numbered_paragraphs()
        structure = build_contract_structure(paragraphs)
        # Sanity: this fixture is a real multi-section DOCX-numbered parse.
        self.assertEqual(structure["stats"]["section_count"], 3)
        self.assertEqual(structure["stats"]["docx_numbered_paragraph_count"], 3)
        resolver = resolve_document_references(paragraphs, structure)

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertEqual(signal["version"], REFERENCE_INTEGRITY_VERSION)
        self.assertTrue(signal["applicable"])
        self.assertEqual(signal["skipped_reason"], "")
        self.assertEqual(signal["status"], "issues_found")
        self.assertEqual(signal["dangling_reference_count"], 1)
        self.assertEqual(signal["ambiguous_reference_count"], 0)
        issue = signal["issues"][0]
        self.assertEqual(issue["reference_text"], "Schedule 3")
        self.assertEqual(issue["kind"], "schedule")
        self.assertEqual(issue["missing_numbers"], ["3"])
        self.assertEqual(issue["source_section_id"], "section-2")
        self.assertIn("Schedule 3 is referenced but no matching section", issue["summary"])

    def test_resolved_only_document_reports_no_issues(self):
        # Same fixture but the only cross-reference (Section 2) resolves cleanly.
        paragraphs = [
            _numbered("p1", 0, "Section 1 Definitions", "1."),
            _body("p2", 1, "Confidential Information means non-public information."),
            _numbered("p3", 2, "Section 2 Confidentiality", "2."),
            _body("p4", 3, "Each party shall protect Confidential Information."),
            _numbered("p5", 4, "Section 3 Term", "3."),
            _body("p6", 5, "The obligations in Section 2 survive termination."),
        ]
        structure = build_contract_structure(paragraphs)
        resolver = resolve_document_references(paragraphs, structure)

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertTrue(signal["applicable"])
        self.assertEqual(signal["status"], "ok")
        self.assertEqual(signal["dangling_reference_count"], 0)
        self.assertEqual(signal["issues"], [])

    def test_suppressed_on_pdf_or_plain_text_parse(self):
        # A PDF / plain-text parse carries no numbering metadata, so cross-reference
        # targets cannot be trusted -- guard #1 disables the signal.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Confidential Information means non-public information.",
            "Section 2 Confidentiality",
            "Each party shall protect Confidential Information as set out in Schedule 3.",
            "Section 3 Term",
            "The obligations in Section 2 survive termination.",
        ]))
        structure = build_contract_structure(paragraphs)
        self.assertEqual(structure["stats"]["docx_numbered_paragraph_count"], 0)
        resolver = resolve_document_references(paragraphs, structure)

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertFalse(signal["applicable"])
        self.assertEqual(signal["skipped_reason"], "not_docx_numbered")
        self.assertEqual(signal["status"], "ok")
        self.assertEqual(signal["dangling_reference_count"], 0)
        self.assertEqual(signal["issues"], [])

    def test_suppressed_on_collapsed_single_section_parse(self):
        # A parse that collapsed to a single section cannot validate references --
        # every target would look dangling. Guard #2 disables the signal.
        paragraphs = [
            _numbered("p1", 0, "All terms in one block referencing Schedule 9 and Section 4.", "1."),
        ]
        structure = build_contract_structure(paragraphs)
        self.assertEqual(structure["stats"]["section_count"], 1)
        resolver = resolve_document_references(paragraphs, structure)

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertFalse(signal["applicable"])
        self.assertEqual(signal["skipped_reason"], "collapsed_single_section")
        self.assertEqual(signal["issues"], [])

    def test_suppressed_when_one_section_owns_more_than_seventy_percent(self):
        # Multiple sections, but one swallowed >70% of paragraphs -> the parse
        # collapsed even though section_count > 1. Guard #2 (dominant-section).
        structure = {
            "stats": {"docx_numbered_paragraph_count": 5, "section_count": 2},
            "sections": [
                {"id": "section-1", "paragraph_ids": ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"]},
                {"id": "section-2", "paragraph_ids": ["p9"]},
            ],
            "reference_index": {"paragraph_to_section_id": {}, "sections_by_id": {}},
        }
        resolver = {
            "references": [
                {
                    "status": "unresolved",
                    "reference_text": "Schedule 9",
                    "kind": "schedule",
                    "items": [{"number": "9", "section_id": None, "ambiguous": False}],
                    "unresolved_numbers": ["9"],
                }
            ]
        }

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertFalse(signal["applicable"])
        self.assertEqual(signal["skipped_reason"], "collapsed_dominant_section")
        self.assertEqual(signal["dangling_reference_count"], 0)

    def test_ambiguous_collision_is_unknown_never_a_dangling_violation(self):
        # A "Section 2" claimed by two restarted-numbering sections is an ambiguous
        # alias collision: the target is UNKNOWN (guard #3). It must be reported as
        # ambiguous, never as a dangling-reference violation, and must not flip status.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Section 2 Governing Law",
            "Governed by the laws of England and Wales.",
            "ORDER FORM",
            "Section 1 Pricing",
            "Pricing terms.",
            "Section 2 Local Terms",
            "Texas terms.",
            "The parties agree this Agreement is governed by Section 2.",
        ]))
        structure = build_contract_structure(paragraphs)
        # Simulate a DOCX-numbered parse so guard #1 passes and we exercise guard #3.
        structure["stats"]["docx_numbered_paragraph_count"] = 4
        resolver = resolve_document_references(paragraphs, structure)

        signal = build_reference_integrity_signal(resolver, structure)

        self.assertTrue(signal["applicable"])
        # The collision is surfaced as ambiguous, not dangling.
        self.assertEqual(signal["dangling_reference_count"], 0)
        self.assertGreaterEqual(signal["ambiguous_reference_count"], 1)
        self.assertEqual(signal["status"], "ok")
        self.assertTrue(any(
            issue["reference_text"] == "Section 2" for issue in signal["ambiguous_issues"]
        ))

    def test_signal_is_present_on_full_review_result(self):
        # End-to-end: the additive document-level key lands on the review result.
        from nda_automation.ai_first_review import build_ai_first_review_result

        result = build_ai_first_review_result(
            "MUTUAL NON-DISCLOSURE AGREEMENT\n\nThe parties agree.",
            [],
        )
        self.assertIn("reference_integrity", result)
        self.assertEqual(result["reference_integrity"]["version"], REFERENCE_INTEGRITY_VERSION)


if __name__ == "__main__":
    unittest.main()
