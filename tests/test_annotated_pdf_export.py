import unittest

from nda_automation import annotated_pdf_export
from nda_automation.annotated_pdf_export import (
    ANNOTATED_PDF_VERIFICATION_HEADER,
    AnnotatedPdfExportError,
    annotated_pdf_download_filename,
    build_annotated_pdf,
)


def _fitz():
    try:
        import fitz
    except ImportError:
        return None
    return fitz


def _sample_pdf_bytes():
    fitz = _fitz()
    if fitz is None:
        raise unittest.SkipTest("PyMuPDF is not installed")
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "This Agreement shall be governed by the laws of Abu Dhabi.\n"
        "Each party must protect Confidential Information.",
    )
    data = document.write()
    document.close()
    return data


class AnnotatedPdfExportTests(unittest.TestCase):
    def test_annotated_pdf_highlights_clause_evidence_and_adds_note(self):
        fitz = _fitz()
        if fitz is None:
            self.skipTest("PyMuPDF is not installed")

        source_pdf = _sample_pdf_bytes()
        result = build_annotated_pdf(
            source_pdf,
            source_filename="sample.pdf",
            review_result={
                "clauses": [{
                    "id": "governing_law",
                    "name": "Governing Law",
                    "decision": "fail",
                    "reason": "Abu Dhabi is outside the approved governing-law list.",
                    "structured_evidence": [{
                        "paragraph_id": "p1",
                        "text": "This Agreement shall be governed by the laws of Abu Dhabi.",
                        "matched_text": "laws of Abu Dhabi",
                    }],
                }],
            },
        )

        self.assertEqual(result.filename, "sample-annotated-review.pdf")
        self.assertGreaterEqual(result.annotation_count, 1)
        self.assertEqual(result.unmatched_evidence_count, 0)
        self.assertTrue(result.data.startswith(source_pdf))
        annotated = fitz.open(stream=result.data, filetype="pdf")
        annotations = list(annotated[0].annots() or [])
        annotated.close()
        self.assertGreaterEqual(len(annotations), 2)

    def test_coordinate_word_boxes_place_evidence_when_search_for_misses(self):
        fitz = _fitz()
        if fitz is None:
            self.skipTest("PyMuPDF is not installed")

        class FakePage:
            rect = fitz.Rect(0, 0, 612, 792)

            def __init__(self):
                self.search_calls = 0

            def get_text(self, mode):
                assert mode == "words"
                return [
                    (72, 60, 94, 72, "laws", 0, 0, 0),
                    (97, 60, 104, 72, "of", 0, 0, 1),
                    (107, 60, 128, 72, "Abu", 0, 0, 2),
                    (131, 60, 165, 72, "Dhabi", 0, 0, 3),
                ]

            def search_for(self, *_args, **_kwargs):
                self.search_calls += 1
                return []

        page = FakePage()
        rects, match_text = annotated_pdf_export._match_evidence_rects(page, ["laws of Abu Dhabi"])

        self.assertEqual(match_text, "laws of Abu Dhabi")
        self.assertEqual(page.search_calls, 0)
        self.assertEqual(len(rects), 4)

    def test_annotated_pdf_renders_visible_proposed_change_markup(self):
        fitz = _fitz()
        if fitz is None:
            self.skipTest("PyMuPDF is not installed")

        source_pdf = _sample_pdf_bytes()
        result = build_annotated_pdf(
            source_pdf,
            source_filename="sample.pdf",
            review_result={
                "clauses": [{
                    "id": "governing_law",
                    "name": "Governing Law",
                    "decision": "fail",
                    "reason": "Abu Dhabi is outside the approved governing-law list.",
                    "structured_evidence": [{
                        "paragraph_id": "p1",
                        "text": "This Agreement shall be governed by the laws of Abu Dhabi.",
                        "matched_text": "laws of Abu Dhabi",
                    }],
                    "proposed_change": {
                        "action": "replace",
                        "source_text": "laws of Abu Dhabi",
                        "proposed_text": "laws of England and Wales",
                    },
                }],
            },
        )

        self.assertEqual(result.unmatched_evidence_count, 0)
        self.assertTrue(result.data.startswith(source_pdf))
        annotated = fitz.open(stream=result.data, filetype="pdf")
        page = annotated[0]
        annotations = list(page.annots() or [])
        annotation_types = {annotation.type[1] for annotation in annotations}
        annotation_text = "\n".join(annotation.info.get("content", "") for annotation in annotations)
        annotated.close()

        self.assertIn("StrikeOut", annotation_types)
        self.assertIn("FreeText", annotation_types)
        self.assertIn("Proposed change: laws of England and Wales", annotation_text)

    def test_scanned_pdf_still_requires_ocr_before_annotation(self):
        fitz = _fitz()
        if fitz is None:
            self.skipTest("PyMuPDF is not installed")

        document = fitz.open()
        document.new_page()
        source_pdf = document.write()
        document.close()

        with self.assertRaisesRegex(AnnotatedPdfExportError, "Scanned PDFs need OCR before review"):
            build_annotated_pdf(source_pdf, source_filename="scan.pdf", review_result={"clauses": []})

    def test_annotated_pdf_filename_is_pdf(self):
        self.assertEqual(
            annotated_pdf_download_filename("Bad Filename (Final).pdf"),
            "Bad-Filename-Final-annotated-review.pdf",
        )

    def test_verification_header_documents_export_contract(self):
        self.assertEqual(
            ANNOTATED_PDF_VERIFICATION_HEADER,
            "pdf-annotations; evidence-highlights; proposed-change-markup",
        )


if __name__ == "__main__":
    unittest.main()
