"""PDF-source redline anchoring: text-anchor-first + fail-closed.

These tests pin the P0 legal-correctness fix for PDF-source reviewed-DOCX exports.
Before the fix, a PDF matter's redlines (every review paragraph carries
``source_part:"pdf"``) were SILENTLY DROPPED from the reconstructed Word document
with only a ``LOGGER.warning`` -- the file downloaded/sent clean, missing every
accepted change. The fix:

1. Text-anchors each PDF redline into the reconstructed body by CONFIDENT TEXT
   match (the loose PDF paragraph index is never trusted).
2. Fails closed (``strict=True``, the default for send/approve/export) when any
   required redline cannot be confidently placed.
3. Stays lenient (``strict=False``) for preview/draft/diagnostic: still produces
   the file but reports the unplaceable redlines so it can be labelled incomplete.
4. Never lets a PDF redline vanish with only a warning.
"""
from __future__ import annotations

import logging
import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from zipfile import ZipFile

from nda_automation import docx_package_renderer, redline_export_service, source_redline_docx
from nda_automation.docx_export import PdfRedlineAnchorError
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH

from tests.test_docx_export import W_NS, make_source_docx, revision_text_for_state


GOVERNING_LAW = "This Agreement shall be governed by the laws of California."
GOVERNING_LAW_REPLACEMENT = "This Agreement shall be governed by the laws of England and Wales."
CONFIDENTIALITY = "Each party shall keep the other party's Confidential Information confidential."


def _pdf_review_paragraphs(texts: list[str]) -> list[dict]:
    """Review paragraphs as a PDF matter produces them (pdf_text.py): each carries
    ``source_part:"pdf"`` and a 1-based ``source_index``/``index``."""
    return [
        {
            "id": f"p{index}",
            "index": index,
            "source_index": index,
            "source_part": "pdf",
            "page_number": 1,
            "text": text,
        }
        for index, text in enumerate(texts, start=1)
    ]


def _pdf_replace_redline(paragraph: dict, *, original_text: str, replacement_text: str) -> dict:
    return {
        "id": f"redline-{paragraph['id']}",
        "action": REDLINE_REPLACE_PARAGRAPH,
        "clause_id": "governing_law",
        "paragraph_id": paragraph["id"],
        # A PDF redline carries the (loose, unreliable) index AND the source_part
        # marker. Resolution must ignore the index and anchor on text.
        "paragraph_index": paragraph["index"],
        "source_index": paragraph["source_index"],
        "source_part": "pdf",
        "original_text": original_text,
        "replacement_text": replacement_text,
    }


def _document_root(docx_bytes: bytes) -> ET.Element:
    with ZipFile(BytesIO(docx_bytes)) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def _body_paragraph_states(docx_bytes: bytes) -> list[tuple[str, str]]:
    document_root = _document_root(docx_bytes)
    return [
        (
            revision_text_for_state(paragraph, accepted=False),
            revision_text_for_state(paragraph, accepted=True),
        )
        for paragraph in document_root.findall(".//w:body/w:p", W_NS)
    ]


class PdfRedlineConfidentPlacementTests(unittest.TestCase):
    def test_pdf_redline_with_matching_text_is_placed(self):
        # (1) A PDF redline whose original_text confidently matches a reconstructed
        # body paragraph is PLACED correctly as a tracked change.
        reconstructed = make_source_docx([GOVERNING_LAW, CONFIDENTIALITY])
        review_paragraphs = _pdf_review_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=GOVERNING_LAW,
                    replacement_text=GOVERNING_LAW_REPLACEMENT,
                )
            ],
        }

        package = source_redline_docx.build_source_redline_package(reconstructed, review_result)

        self.assertEqual(package.anchor_uncertain_redlines, [])
        states = _body_paragraph_states(package.data)
        # The matched paragraph carries the tracked replacement: rejected view keeps
        # California, accepted view becomes England and Wales. The sibling paragraph
        # is untouched.
        self.assertIn((GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT), states)
        self.assertIn((CONFIDENTIALITY, CONFIDENTIALITY), states)

    def test_pdf_redline_anchors_despite_wrong_index(self):
        # The loose PDF index is unreliable: a redline whose source_index points at
        # the WRONG paragraph still anchors on TEXT, landing on the correct one.
        reconstructed = make_source_docx([CONFIDENTIALITY, GOVERNING_LAW])
        review_paragraphs = _pdf_review_paragraphs([CONFIDENTIALITY, GOVERNING_LAW])
        redline = _pdf_replace_redline(
            review_paragraphs[1],
            original_text=GOVERNING_LAW,
            replacement_text=GOVERNING_LAW_REPLACEMENT,
        )
        # Corrupt the index to point at paragraph 1 (the confidentiality clause).
        redline["source_index"] = 1
        redline["paragraph_index"] = 1
        review_result = {"paragraphs": review_paragraphs, "redline_edits": [redline]}

        package = source_redline_docx.build_source_redline_package(reconstructed, review_result)

        states = _body_paragraph_states(package.data)
        # Text wins: the governing-law paragraph is the one redlined, NOT paragraph 1.
        self.assertIn((GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT), states)
        self.assertIn((CONFIDENTIALITY, CONFIDENTIALITY), states)


class PdfRedlineFailClosedTests(unittest.TestCase):
    def _unplaceable_review_result(self) -> tuple[bytes, dict]:
        # The reconstructed body does NOT contain the redline's original_text, so it
        # cannot be confidently anchored.
        reconstructed = make_source_docx(["Some entirely unrelated reconstructed paragraph."])
        review_paragraphs = _pdf_review_paragraphs([GOVERNING_LAW])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=GOVERNING_LAW,
                    replacement_text=GOVERNING_LAW_REPLACEMENT,
                )
            ],
        }
        return reconstructed, review_result

    def test_strict_raises_pdf_redline_anchor_error(self):
        # (2a) Under strict=True the build RAISES rather than silently dropping the
        # unplaceable redline. No partial file is returned.
        reconstructed, review_result = self._unplaceable_review_result()

        with self.assertRaises(PdfRedlineAnchorError) as caught:
            source_redline_docx.build_source_redline_package(
                reconstructed, review_result, strict=True
            )
        self.assertEqual(caught.exception.count, 1)

    def test_strict_renderer_translates_to_unavailable_with_exact_message(self):
        # (2b) The service-facing renderer + error carries the EXACT user message and
        # the correct count, with the annotated-PDF recovery path in the payload.
        reconstructed, review_result = self._unplaceable_review_result()

        with self.assertRaises(PdfRedlineAnchorError) as caught:
            docx_package_renderer.render_source_redline_package(
                reconstructed,
                review_result,
                expected_source_text="",
                expected_redline_edits=[],
                strict=True,
            )

        # Translate exactly as redline_export_service does on the PDF path.
        error = redline_export_service.PdfSourceRedlineUnavailableError.for_unplaceable_anchors(
            caught.exception.count, source_filename="Signed NDA.pdf"
        )
        self.assertEqual(
            str(error),
            "Couldn't confidently place 1 proposed changes in the reconstructed Word document. "
            "Export blocked to avoid sending an incomplete redline.",
        )
        self.assertEqual(error.status, 503)
        self.assertEqual(error.payload["reason"], "redline_anchor_uncertain")
        self.assertEqual(error.payload["unplaceable_redline_count"], 1)
        self.assertEqual(error.payload["recovery"]["path"], "annotated_pdf")

    def test_exact_message_count_parameterizes(self):
        self.assertEqual(
            redline_export_service.pdf_redline_anchor_blocked_message(3),
            "Couldn't confidently place 3 proposed changes in the reconstructed Word document. "
            "Export blocked to avoid sending an incomplete redline.",
        )


class PdfRedlineLenientTests(unittest.TestCase):
    def test_lenient_produces_doc_flagged_incomplete(self):
        # (3) Under strict=False the same unplaceable redline does NOT raise: the file
        # is produced and the unplaceable redline is reported so it can be labelled
        # an incomplete redline.
        reconstructed = make_source_docx(["Some entirely unrelated reconstructed paragraph."])
        review_paragraphs = _pdf_review_paragraphs([GOVERNING_LAW])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=GOVERNING_LAW,
                    replacement_text=GOVERNING_LAW_REPLACEMENT,
                )
            ],
        }

        result = docx_package_renderer.render_source_redline_package(
            reconstructed,
            review_result,
            expected_source_text="",
            expected_redline_edits=[],
            strict=False,
        )

        self.assertTrue(result.data)
        self.assertTrue(result.anchor_incomplete)
        self.assertEqual(len(result.anchor_uncertain_redlines), 1)
        self.assertEqual(
            result.anchor_uncertain_redlines[0]["paragraph_id"],
            review_paragraphs[0]["id"],
        )


class PdfRedlineNoSilentDropTests(unittest.TestCase):
    def test_unplaceable_pdf_redline_never_skipped_with_only_a_warning(self):
        # (5) The silent drop is gone: an unplaceable PDF redline must NOT vanish with
        # only a log line. Under strict it raises; even when forced lenient it surfaces
        # in anchor_uncertain_redlines. Assert no WARNING-and-continue path swallows it.
        reconstructed = make_source_docx(["Wholly different reconstructed text."])
        review_paragraphs = _pdf_review_paragraphs([GOVERNING_LAW])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=GOVERNING_LAW,
                    replacement_text=GOVERNING_LAW_REPLACEMENT,
                )
            ],
        }

        with self.assertLogs("nda_automation.docx_export", level="WARNING") as logs:
            logging.getLogger("nda_automation.docx_export").warning("sentinel")
            result = docx_package_renderer.render_source_redline_package(
                reconstructed,
                review_result,
                expected_source_text="",
                expected_redline_edits=[],
                strict=False,
            )

        # The ONLY warning is our sentinel: the PDF skip-with-warning path is gone.
        warnings = [line for line in logs.output if "sentinel" not in line]
        self.assertEqual(warnings, [])
        # And the redline is accounted for, not dropped.
        self.assertEqual(len(result.anchor_uncertain_redlines), 1)


class DocxSourceRegressionTests(unittest.TestCase):
    def test_docx_source_matter_exports_unchanged(self):
        # (4) Regression: a DOCX-source matter (NO source_part marker) anchors by the
        # unchanged source_index/text path and exports its tracked change exactly as
        # before -- the PDF branch must not perturb it.
        source_docx = make_source_docx([GOVERNING_LAW, CONFIDENTIALITY])
        review_paragraphs = [
            {"id": "p1", "index": 1, "source_index": 1, "text": GOVERNING_LAW},
            {"id": "p2", "index": 2, "source_index": 2, "text": CONFIDENTIALITY},
        ]
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                {
                    "id": "redline-docx",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "clause_id": "governing_law",
                    "paragraph_id": "p1",
                    "index": 1,
                    "source_index": 1,
                    "original_text": GOVERNING_LAW,
                    "replacement_text": GOVERNING_LAW_REPLACEMENT,
                }
            ],
        }

        result = docx_package_renderer.render_source_redline_package(
            source_docx,
            review_result,
            expected_source_text="\n\n".join([GOVERNING_LAW, CONFIDENTIALITY]),
            expected_redline_edits=review_result["redline_edits"],
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.anchor_uncertain_redlines, [])
        states = _body_paragraph_states(result.data)
        self.assertIn((GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT), states)
        self.assertIn((CONFIDENTIALITY, CONFIDENTIALITY), states)


class SupplementalPartSkipPreservedTests(unittest.TestCase):
    def test_header_part_redline_still_skipped_with_warning(self):
        # Guardrail: the fix must NOT change the genuine supplemental-part behavior. A
        # redline targeting a header/footer (source_part != "pdf") still cannot anchor
        # in the body and remains a logged skip -- it is NOT treated as a PDF redline
        # and does NOT raise or count as incomplete.
        source_docx = make_source_docx([GOVERNING_LAW])
        review_result = {
            "paragraphs": [
                {"id": "p1", "index": 1, "source_part": "header1", "text": GOVERNING_LAW},
            ],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "source_part": "header1",
                    "original_text": GOVERNING_LAW,
                    "replacement_text": GOVERNING_LAW_REPLACEMENT,
                }
            ],
        }

        with self.assertLogs("nda_automation.docx_export", level="WARNING") as logs:
            package = source_redline_docx.build_source_redline_package(
                source_docx, review_result, strict=True
            )

        self.assertIn("unresolved or ambiguous anchor", "\n".join(logs.output))
        self.assertEqual(package.anchor_uncertain_redlines, [])
        # Body paragraph is untouched (header redline did not land in the body).
        states = _body_paragraph_states(package.data)
        self.assertEqual(states, [(GOVERNING_LAW, GOVERNING_LAW)])


if __name__ == "__main__":
    unittest.main()
