"""Zero-redline PDF-source export must serve the ORIGINAL, not a lossy rebuild.

Confirmed defect (sibling of the silent-redline-drop bug): a PDF-source matter with
ZERO accepted redlines still ran the lossy ``reconstruct_pdf_to_docx`` rebuild AND the
post-render coverage gate short-circuits to "pass" when there are no redlines -- so the
user downloaded a pdf2docx reconstruction (which can differ from the original text)
stamped "verified" with NO content check at all.

The fix: with no accepted redlines (and no clean fills) there is nothing to apply, so the
faithful reviewed output IS the original PDF, served unchanged and marked honestly as the
original -- never as a verified reconstruction. The WITH-redlines path is unchanged.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from nda_automation import redline_export_service

from tests.test_docx_export import make_source_docx
from tests.test_pdf_redline_anchor import (
    CONFIDENTIALITY,
    GOVERNING_LAW,
    GOVERNING_LAW_REPLACEMENT,
    _pdf_replace_redline,
    _pdf_review_paragraphs,
)
from tests.test_pdf_redline_coverage import _tracked_replace_docx


_ORIGINAL_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class _LandedRenderResult:
    """A package render result whose bytes DO carry the landed redline -- so the
    with-redlines path passes the coverage gate exactly as in production."""

    def __init__(self, data: bytes):
        self.data = data
        self.health_errors: list[str] = []
        self.content_errors: list[str] = []
        self.anchor_uncertain_redlines: list[dict] = []


def _pdf_review_result_with_redline() -> dict:
    review_paragraphs = _pdf_review_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
    return {
        "paragraphs": review_paragraphs,
        "redline_edits": [
            _pdf_replace_redline(
                review_paragraphs[0],
                original_text=GOVERNING_LAW,
                replacement_text=GOVERNING_LAW_REPLACEMENT,
            )
        ],
    }


def _pdf_review_result_no_redlines() -> dict:
    return {
        "paragraphs": _pdf_review_paragraphs([GOVERNING_LAW, CONFIDENTIALITY]),
        "redline_edits": [],
    }


class _Repo:
    """Placeholder repository -- _review_result_for_export is patched, so the real
    repository is never consulted."""


class PdfZeroRedlineExportTests(unittest.TestCase):
    def _build(self, review_result: dict, *, reconstruct_should_run: bool):
        reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
            data=make_source_docx([GOVERNING_LAW, CONFIDENTIALITY]),
            filename="Signed-NDA.docx",
            headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
        )
        # A reconstruction that LANDS the redline (used only on the with-redlines path).
        landed = _tracked_replace_docx(
            GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT, others=[CONFIDENTIALITY]
        )
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, _ORIGINAL_PDF_BYTES, "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=reconstructed,
        ) as reconstruct, patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            return_value=_LandedRenderResult(landed),
        ):
            export = redline_export_service.build_matter_redline(
                "matter-pdf", {}, persist=False, repository=_Repo()
            )
        if reconstruct_should_run:
            reconstruct.assert_called()
        else:
            reconstruct.assert_not_called()
        return export

    # --- zero-redline: serve the original, no lossy rebuild, not falsely verified --- #
    def test_zero_redline_serves_original_pdf_bytes_unchanged(self):
        export = self._build(
            _pdf_review_result_no_redlines(), reconstruct_should_run=False
        )
        # The faithful output IS the original PDF, byte-for-byte.
        self.assertEqual(export.data, _ORIGINAL_PDF_BYTES)
        self.assertTrue(export.data.startswith(b"%PDF-"))
        self.assertEqual(export.content_type, redline_export_service.PDF_CONTENT_TYPE)
        self.assertEqual(export.filename, "Signed-NDA.pdf")

    def test_zero_redline_does_not_run_lossy_reconstruction(self):
        # reconstruct_pdf_to_docx must NOT be called for the no-change case.
        self._build(_pdf_review_result_no_redlines(), reconstruct_should_run=False)

    def test_zero_redline_marked_original_not_verified_reconstruction(self):
        export = self._build(
            _pdf_review_result_no_redlines(), reconstruct_should_run=False
        )
        headers = export.headers or {}
        # Honest "original" marker, NOT a reconstruction-verified stamp.
        self.assertEqual(
            headers.get(redline_export_service.ORIGINAL_EXPORT_MARKER_HEADER),
            redline_export_service.ORIGINAL_UNCHANGED_EXPORT_HEADER,
        )
        self.assertNotIn("X-PDF-DOCX-Reconstruction", headers)

    def test_zero_redline_original_is_not_persisted_as_redline_artifact(self):
        export = self._build(
            _pdf_review_result_no_redlines(), reconstruct_should_run=False
        )
        self.assertIsNone(export.saved_path)

    # --- with-redlines: reconstruction + coverage gate + verified stamp UNCHANGED --- #
    def test_with_redlines_still_reconstructs_and_is_docx(self):
        export = self._build(
            _pdf_review_result_with_redline(), reconstruct_should_run=True
        )
        # Reconstructed reviewed DOCX, NOT the original PDF.
        self.assertEqual(export.filename, "Signed-NDA-reviewed.docx")
        self.assertNotEqual(export.data, _ORIGINAL_PDF_BYTES)
        self.assertIsNone(export.content_type)
        headers = export.headers or {}
        # The reconstruction header (the route's verified value) is still present.
        self.assertEqual(headers.get("X-PDF-DOCX-Reconstruction"), "pdf2docx")
        self.assertNotIn(
            redline_export_service.ORIGINAL_EXPORT_MARKER_HEADER, headers
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
