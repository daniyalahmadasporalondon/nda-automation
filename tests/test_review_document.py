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

    def test_align_carries_footnote_and_comment_display_metadata(self):
        # The DOCX source's footnotes/embedded comments must survive alignment so
        # the reviewer surface can show them; they are additive display metadata.
        source_text = "The term survives for five years."
        footnotes = [{"id": "2", "kind": "footnote", "offset": 17, "text": "Trade secrets only."}]
        comments = [{"id": "1", "author": "Counsel", "text": "Add a carve-out.", "offset": 0}]
        extracted_paragraphs = [
            {
                "source_index": 1,
                "source_kind": "paragraph",
                "text": "The term survives for five years.",
                "footnotes": footnotes,
                "comments": comments,
            },
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual(aligned[0]["footnotes"], footnotes)
        self.assertEqual(aligned[0]["comments"], comments)

    def test_align_drops_footnote_and_comment_metadata_when_paragraph_is_resplit(self):
        # Their offsets address the WHOLE paragraph; a soft-return split invalidates
        # them, so -- like runs -- they are dropped rather than duplicated wrongly.
        source_text = "First block.\n\nSecond block."
        extracted_paragraphs = [
            {
                "source_index": 1,
                "source_kind": "paragraph",
                "text": "First block.\n\nSecond block.",
                "footnotes": [{"id": "1", "kind": "footnote", "offset": 5, "text": "Note."}],
                "comments": [{"id": "1", "text": "A comment.", "offset": 0}],
            },
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual([paragraph["text"] for paragraph in aligned], ["First block.", "Second block."])
        for paragraph in aligned:
            self.assertNotIn("footnotes", paragraph)
            self.assertNotIn("comments", paragraph)

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

    def test_align_preserves_paragraph_indent_and_extended_run_formatting(self):
        source_text = "Plain red struck text."
        runs = [
            {"text": "Plain ", "bold": False, "italic": False, "underline": False},
            {"text": "red", "bold": False, "italic": False, "underline": False, "color": "#ff0000"},
            {"text": " struck", "bold": False, "italic": False, "underline": False, "strike": True},
            {"text": " text.", "bold": False, "italic": False, "underline": False, "highlight": "yellow"},
        ]
        extracted_paragraphs = [
            {
                "source_index": 1,
                "source_kind": "paragraph",
                "text": "Plain red struck text.",
                "runs": runs,
                "indent_left": 54,
            },
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        # Paragraph-level ``indent_left`` is carried as structural metadata.
        self.assertEqual(aligned[0]["indent_left"], 54)
        # The new run-level fields ride through inside the carried ``runs`` records.
        self.assertEqual(aligned[0]["runs"], runs)
        self.assertEqual(aligned[0]["runs"][1]["color"], "#ff0000")
        self.assertEqual(aligned[0]["runs"][2]["strike"], True)
        self.assertEqual(aligned[0]["runs"][3]["highlight"], "yellow")

    def test_align_preserves_paragraph_alignment_and_font(self):
        # The extractor stamps a paragraph's from-state alignment ("both"->justify)
        # and base font name. These are additive STYLE metadata: they must survive
        # align_document_paragraphs so the reconstruction/source-fidelity renderers
        # can honour them, and they must NEVER alter the paragraph's ``text`` (the
        # outbound-redline surface reads ``text``/innerText, not these keys).
        source_text = "Confidentiality Agreement"
        extracted_paragraphs = [
            {
                "source_index": 1,
                "source_kind": "paragraph",
                "text": "Confidentiality Agreement",
                "alignment": "center",
                "font": "Times New Roman",
            },
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertEqual(aligned[0]["alignment"], "center")
        self.assertEqual(aligned[0]["font"], "Times New Roman")
        # The style keys are metadata only -- the text the redline targets is
        # byte-identical to the source, with no alignment/font token injected.
        self.assertEqual(aligned[0]["text"], "Confidentiality Agreement")

    def test_align_leaves_alignment_and_font_absent_when_source_omits_them(self):
        # Additive: a paragraph the extractor did not tag stays untouched, so the
        # renderer falls back to the source default (left) and the app font.
        source_text = "Body paragraph."
        extracted_paragraphs = [
            {"source_index": 1, "source_kind": "paragraph", "text": "Body paragraph."},
        ]

        aligned = align_document_paragraphs(extracted_paragraphs, source_text)

        self.assertNotIn("alignment", aligned[0])
        self.assertNotIn("font", aligned[0])

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
