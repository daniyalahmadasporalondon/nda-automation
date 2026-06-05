import unittest

from nda_automation.annotated_pdf_export import (
    ANNOTATED_PDF_VERIFICATION_HEADER,
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
        annotated = fitz.open(stream=result.data, filetype="pdf")
        annotations = list(annotated[0].annots() or [])
        annotated.close()
        self.assertGreaterEqual(len(annotations), 2)

    def test_annotated_pdf_filename_is_pdf(self):
        self.assertEqual(
            annotated_pdf_download_filename("Bad Filename (Final).pdf"),
            "Bad-Filename-Final-annotated-review.pdf",
        )

    def test_verification_header_documents_export_contract(self):
        self.assertEqual(ANNOTATED_PDF_VERIFICATION_HEADER, "pdf-annotations; evidence-highlights")


if __name__ == "__main__":
    unittest.main()
