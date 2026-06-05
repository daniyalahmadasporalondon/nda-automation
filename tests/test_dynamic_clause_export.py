"""Dynamic redline/export/comments (Dynamic Clause Types, task #11).

A dynamic clause's finding flows through the redline -> docx export path
generically, using the clause's own fallback wording — the export and comment
paths consume the generic redline_edits[] / comments and never key off a clause
id, so a clause type the code has never seen exports like any other.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from zipfile import ZipFile

from nda_automation.docx_export import build_source_redline_docx

from test_docx_export import assert_docx_package_healthy, make_source_docx

DYNAMIC_FALLBACK_WORDING = (
    "Each party shall comply with applicable data protection laws in processing personal data."
)


def _document_xml(redlined_docx: bytes) -> str:
    with ZipFile(BytesIO(redlined_docx)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _review_result_with_dynamic_edit(action: str, *, original_text: str) -> dict:
    source_paragraphs = [
        "This Agreement is mutual.",
        original_text,
    ]
    review_result = {
        "paragraphs": [
            {"id": "p1", "index": 1, "source_index": 1, "text": source_paragraphs[0]},
            {"id": "p2", "index": 2, "source_index": 2, "text": source_paragraphs[1]},
        ],
        # A redline edit for a clause id the export code has never seen.
        "redline_edits": [
            {
                "id": "r1",
                "clause_id": "data_protection",
                "clause_name": "Data Protection",
                "paragraph_id": "p2",
                "paragraph_index": 2,
                "source_index": 2,
                "action": action,
                "original_text": "" if action == "insert_after_paragraph" else source_paragraphs[1],
                "replacement_text": DYNAMIC_FALLBACK_WORDING,
            }
        ],
    }
    if action == "insert_after_paragraph":
        review_result["redline_edits"][0]["anchor_text"] = source_paragraphs[1]
        review_result["redline_edits"][0]["insert_text"] = DYNAMIC_FALLBACK_WORDING
    return review_result, source_paragraphs


class DynamicClauseExportTests(unittest.TestCase):
    def test_dynamic_clause_replace_redline_exports_fallback_wording(self):
        review_result, source_paragraphs = _review_result_with_dynamic_edit(
            "replace_paragraph",
            original_text="The receiving party may use personal data freely.",
        )
        source_docx = make_source_docx(source_paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        document_xml = _document_xml(redlined_docx)
        ET.fromstring(document_xml)
        # The dynamic clause's fallback wording is exported even though the export
        # path has no knowledge of the "data_protection" clause id.
        self.assertIn("comply with applicable data protection laws", document_xml)

    def test_dynamic_clause_insert_redline_exports_fallback_wording(self):
        review_result, source_paragraphs = _review_result_with_dynamic_edit(
            "insert_after_paragraph",
            original_text="The parties exchange confidential information.",
        )
        source_docx = make_source_docx(source_paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        document_xml = _document_xml(redlined_docx)
        ET.fromstring(document_xml)
        self.assertIn("comply with applicable data protection laws", document_xml)


if __name__ == "__main__":
    unittest.main()
