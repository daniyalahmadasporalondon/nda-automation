from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from nda_automation import docx_text
from nda_automation.docx_text import DocxExtractionError, extract_docx_paragraphs, extract_docx_text


class DocxTextTests(unittest.TestCase):
    def test_extracts_paragraph_text_from_docx(self):
        data = make_docx(
            [
                "Mutual Non-Disclosure Agreement",
                "Each party is a Disclosing Party and Receiving Party.",
                "This Agreement continues for five (5) years.",
            ]
        )

        text = extract_docx_text(data)

        self.assertIn("Mutual Non-Disclosure Agreement", text)
        self.assertIn("Each party is a Disclosing Party and Receiving Party.", text)
        self.assertIn("five (5) years", text)

    def test_extracts_structured_docx_paragraphs(self):
        data = make_docx(["", "First real paragraph.", "Second real paragraph."])

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs,
            [
                {"source_index": 2, "text": "First real paragraph."},
                {"source_index": 3, "text": "Second real paragraph."},
            ],
        )

    def test_extracts_tables_and_supplemental_docx_parts(self):
        data = make_docx(
            ["Body paragraph."],
            body_xml='<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Signature table text.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
            extra_parts={
                "word/header1.xml": part_xml("Header confidentiality term."),
                "word/footer1.xml": part_xml("Footer governing law note."),
                "word/footnotes.xml": part_xml("Footnote survival language."),
                "word/endnotes.xml": part_xml("Endnote residual clause."),
                "word/comments.xml": part_xml("Comment says check non-circumvention."),
            },
        )

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs,
            [
                {"source_index": 1, "text": "Body paragraph."},
                {"source_index": 2, "text": "Signature table text."},
                {"source_part": "comments", "text": "Comment says check non-circumvention."},
                {"source_part": "endnotes", "text": "Endnote residual clause."},
                {"source_part": "footer1", "text": "Footer governing law note."},
                {"source_part": "footnotes", "text": "Footnote survival language."},
                {"source_part": "header1", "text": "Header confidentiality term."},
            ],
        )

    def test_rejects_non_docx_bytes(self):
        with self.assertRaises(DocxExtractionError):
            extract_docx_text(b"not a word document")

    def test_rejects_excessive_uncompressed_docx_size(self):
        data = make_zip({"word/document.xml": "A" * 128}, compression=ZIP_STORED)

        with patch.object(docx_text, "MAX_DOCX_UNCOMPRESSED_BYTES", 64):
            with self.assertRaisesRegex(DocxExtractionError, "too large after decompression"):
                extract_docx_paragraphs(data)

    def test_rejects_suspicious_docx_compression_ratio(self):
        data = make_zip({"word/document.xml": "A" * 4096}, compression=ZIP_DEFLATED)

        with patch.object(docx_text, "MAX_DOCX_ENTRY_COMPRESSION_RATIO", 2):
            with self.assertRaisesRegex(DocxExtractionError, "suspicious compression ratio"):
                extract_docx_paragraphs(data)

    def test_rejects_docx_xml_dtd_entity_declarations_before_parsing(self):
        data = make_docx(["Safe body text."], extra_parts={"word/header1.xml": unsafe_xml_part()})

        with self.assertRaisesRegex(DocxExtractionError, "unsupported XML DTD/entity declarations"):
            extract_docx_paragraphs(data)


def make_docx(paragraphs, *, body_xml="", extra_parts=None):
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}{body_xml}</w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
            for name, content in (extra_parts or {}).items():
                archive.writestr(name, content)
        return output.getvalue()


def part_xml(text):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:part xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>{escape_xml(text)}</w:t></w:r></w:p>
</w:part>"""


def unsafe_xml_part():
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""


def escape_xml(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def make_zip(parts, *, compression):
    with BytesIO() as output:
        with ZipFile(output, "w", compression) as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
