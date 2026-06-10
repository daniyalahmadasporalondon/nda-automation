import unittest

from nda_automation.review_document import align_document_paragraphs, split_document_paragraphs


class ReviewDocumentTests(unittest.TestCase):
    def test_align_splits_extracted_paragraphs_on_internal_blank_lines_like_source_text(self):
        source_text = "First block.\n\nSecond block.\n\nThird block."
        extracted_paragraphs = [
            {"source_index": 1, "source_kind": "paragraph", "text": "First block.\n\nSecond block."},
            {"source_index": 2, "source_kind": "paragraph", "text": "Third block."},
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)
        split = split_document_paragraphs(source_text)

        self.assertEqual([paragraph["text"] for paragraph in aligned], [paragraph["text"] for paragraph in split])
        self.assertEqual([paragraph["id"] for paragraph in aligned], ["p1", "p2", "p3"])
        self.assertEqual([paragraph["index"] for paragraph in aligned], [1, 2, 3])
        self.assertEqual([paragraph["source_index"] for paragraph in aligned], [1, 1, 2])
        self.assertEqual([paragraph["start"] for paragraph in aligned], [0, 14, 29])
        self.assertEqual([paragraph["end"] for paragraph in aligned], [12, 27, 41])

    def test_align_preserves_run_formatting_for_unsplit_paragraph(self):
        source_text = "Plain bold text."
        runs = [
            {"text": "Plain ", "bold": False, "italic": False, "underline": False},
            {"text": "bold", "bold": True, "italic": False, "underline": False},
            {"text": " text.", "bold": False, "italic": False, "underline": False},
        ]
        extracted_paragraphs = [
            {"source_index": 1, "source_kind": "paragraph", "text": "Plain bold text.", "runs": runs},
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual(aligned[0]["runs"], runs)

    def test_align_drops_run_formatting_when_paragraph_is_resplit(self):
        source_text = "First block.\n\nSecond block."
        runs = [{"text": "First block.\n\nSecond block.", "bold": True, "italic": False, "underline": False}]
        extracted_paragraphs = [
            {"source_index": 1, "source_kind": "paragraph", "text": "First block.\n\nSecond block.", "runs": runs},
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual([paragraph["text"] for paragraph in aligned], ["First block.", "Second block."])
        self.assertNotIn("runs", aligned[0])
        self.assertNotIn("runs", aligned[1])

    def test_align_preserves_run_and_paragraph_font_sizes(self):
        source_text = "Plain bold text."
        runs = [
            {"text": "Plain ", "bold": False, "italic": False, "underline": False, "size": 12},
            {"text": "bold", "bold": True, "italic": False, "underline": False, "size": 18},
            {"text": " text.", "bold": False, "italic": False, "underline": False, "size": 12},
        ]
        extracted_paragraphs = [
            {
                "source_index": 1,
                "source_kind": "paragraph",
                "text": "Plain bold text.",
                "runs": runs,
                "fontSize": 12,
            },
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        # Per-run ``size`` rides through inside the carried ``runs`` records.
        self.assertEqual(aligned[0]["runs"], runs)
        self.assertEqual([run["size"] for run in aligned[0]["runs"]], [12, 18, 12])
        # Paragraph-level ``fontSize`` is carried as structural metadata.
        self.assertEqual(aligned[0]["fontSize"], 12)

    def test_align_preserves_pdf_page_number_metadata(self):
        source_text = "First PDF block.\n\nSecond PDF block."
        extracted_paragraphs = [
            {"source_index": 1, "source_part": "pdf", "page_number": 3, "text": "First PDF block."},
            {"source_index": 2, "source_part": "pdf", "page_number": 4, "text": "Second PDF block."},
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual([paragraph["page_number"] for paragraph in aligned], [3, 4])
        self.assertEqual([paragraph["source_part"] for paragraph in aligned], ["pdf", "pdf"])


if __name__ == "__main__":
    unittest.main()
