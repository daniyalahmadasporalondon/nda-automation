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


if __name__ == "__main__":
    unittest.main()
