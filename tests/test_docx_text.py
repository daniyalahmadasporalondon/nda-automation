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
                {"source_index": 2, "source_kind": "paragraph", "text": "First real paragraph."},
                {"source_index": 3, "source_kind": "paragraph", "text": "Second real paragraph."},
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
            },
        )

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs,
            [
                {"source_index": 1, "source_kind": "paragraph", "text": "Body paragraph."},
                {
                    "source_index": 2,
                    "source_kind": "table_cell",
                    "table": {"table_index": 1, "row_index": 1, "cell_index": 1},
                    "text": "Signature table text.",
                },
                {"source_kind": "supplemental", "source_part": "endnotes", "text": "Endnote residual clause."},
                {"source_kind": "supplemental", "source_part": "footer1", "text": "Footer governing law note."},
                {"source_kind": "supplemental", "source_part": "footnotes", "text": "Footnote survival language."},
                {"source_kind": "supplemental", "source_part": "header1", "text": "Header confidentiality term."},
            ],
        )

    def test_comments_xml_is_excluded_from_reviewable_text(self):
        # word/comments.xml carries counterparty/reviewer annotations, not body
        # text. It must never reach the verdict engine: a comment that mentions a
        # clause ("check non-circumvention") would otherwise manufacture a hit the
        # agreement itself never makes.
        data = make_docx(
            ["The Receiving Party shall keep the information confidential."],
            extra_parts={
                "word/comments.xml": part_xml("We should add a non-circumvention covenant here."),
            },
        )

        paragraphs = extract_docx_paragraphs(data)
        text = extract_docx_text(data)

        self.assertEqual(
            paragraphs,
            [
                {
                    "source_index": 1,
                    "source_kind": "paragraph",
                    "text": "The Receiving Party shall keep the information confidential.",
                },
            ],
        )
        self.assertNotIn("non-circumvention", text)
        self.assertNotIn("comments", [paragraph.get("source_part") for paragraph in paragraphs])

    def test_extracts_word_numbering_styles_and_table_context(self):
        data = make_structured_docx()

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(paragraphs[0]["heading_level"], 1)
        self.assertEqual(paragraphs[0]["style_id"], "Heading1")
        self.assertEqual(paragraphs[0]["style_name"], "heading 1")
        self.assertEqual(paragraphs[1]["numbering"]["label"], "1.")
        self.assertEqual(paragraphs[1]["structure_number"], "1")
        self.assertEqual(paragraphs[2]["numbering"]["label"], "1.1")
        self.assertEqual(paragraphs[2]["structure_number"], "1.1")
        self.assertEqual(paragraphs[3]["source_kind"], "table_cell")
        self.assertEqual(paragraphs[3]["heading_level"], 2)
        self.assertEqual(paragraphs[3]["table"], {"table_index": 1, "row_index": 1, "cell_index": 1})

    def test_rejects_non_docx_bytes(self):
        with self.assertRaises(DocxExtractionError):
            extract_docx_text(b"not a word document")

    def test_rejects_excessive_uncompressed_docx_size(self):
        data = make_zip({"word/document.xml": "A" * 128}, compression=ZIP_STORED)

        with patch.object(docx_text, "MAX_DOCX_UNCOMPRESSED_BYTES", 64):
            with self.assertRaisesRegex(DocxExtractionError, "too large after decompression"):
                extract_docx_paragraphs(data)

    def test_rejects_excessive_docx_zip_entry_count(self):
        data = make_zip(
            {
                "word/document.xml": part_xml("Safe body text."),
                "word/header1.xml": part_xml("Header text."),
            },
            compression=ZIP_STORED,
        )

        with patch.object(docx_text, "MAX_DOCX_ZIP_ENTRIES", 1):
            with self.assertRaisesRegex(DocxExtractionError, "too many archive entries"):
                extract_docx_paragraphs(data)

    def test_rejects_excessive_docx_zip_entry_count_before_zipfile_open(self):
        data = make_zip(
            {
                "word/document.xml": part_xml("Safe body text."),
                "word/header1.xml": part_xml("Header text."),
            },
            compression=ZIP_STORED,
        )

        with (
            patch.object(docx_text, "MAX_DOCX_ZIP_ENTRIES", 1),
            patch.object(docx_text, "ZipFile", side_effect=AssertionError("ZipFile should not open")),
        ):
            with self.assertRaisesRegex(DocxExtractionError, "too many archive entries"):
                extract_docx_paragraphs(data)

    def test_rejects_suspicious_docx_compression_ratio(self):
        data = make_zip({"word/document.xml": "A" * 4096}, compression=ZIP_DEFLATED)

        with patch.object(docx_text, "MAX_DOCX_ENTRY_COMPRESSION_RATIO", 2):
            with self.assertRaisesRegex(DocxExtractionError, "suspicious compression ratio"):
                extract_docx_paragraphs(data)

    def test_rejects_suspicious_docx_compression_ratio_before_zipfile_open(self):
        data = make_zip({"word/document.xml": "A" * 4096}, compression=ZIP_DEFLATED)

        with (
            patch.object(docx_text, "MAX_DOCX_ENTRY_COMPRESSION_RATIO", 2),
            patch.object(docx_text, "ZipFile", side_effect=AssertionError("ZipFile should not open")),
        ):
            with self.assertRaisesRegex(DocxExtractionError, "suspicious compression ratio"):
                extract_docx_paragraphs(data)

    def test_rejects_deeply_nested_tables_without_recursion_error(self):
        data = make_zip({"word/document.xml": deeply_nested_table_document_xml(1200)}, compression=ZIP_STORED)

        with self.assertRaisesRegex(DocxExtractionError, "tables nested too deeply"):
            extract_docx_paragraphs(data)

    def test_rejects_docx_xml_dtd_entity_declarations_before_parsing(self):
        data = make_docx(["Safe body text."], extra_parts={"word/header1.xml": unsafe_xml_part()})

        with self.assertRaisesRegex(DocxExtractionError, "unsupported XML DTD/entity declarations"):
            extract_docx_paragraphs(data)

    def test_rejects_utf16_docx_xml_dtd_entity_declarations_before_parsing(self):
        data = make_docx(
            ["Safe body text."],
            extra_parts={"word/header1.xml": unsafe_xml_part("UTF-16").encode("utf-16")},
        )

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


def make_structured_docx():
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Definitions</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="42"/></w:numPr></w:pPr><w:r><w:t>Confidentiality Obligations</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="42"/></w:numPr></w:pPr><w:r><w:t>Permitted Disclosures</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Signature Block</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:pPr><w:outlineLvl w:val="1"/></w:pPr>
  </w:style>
</w:styles>"""
    numbering_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="7">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="42"><w:abstractNumId w:val="7"/></w:num>
</w:numbering>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/styles.xml", styles_xml)
            archive.writestr("word/numbering.xml", numbering_xml)
        return output.getvalue()


def part_xml(text):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:part xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>{escape_xml(text)}</w:t></w:r></w:p>
</w:part>"""


def unsafe_xml_part(encoding="UTF-8"):
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""


def deeply_nested_table_document_xml(depth):
    inner = "<w:p><w:r><w:t>Nested table text.</w:t></w:r></w:p>"
    for _ in range(depth):
        inner = f"<w:tbl><w:tr><w:tc>{inner}</w:tc></w:tr></w:tbl>"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{inner}</w:body>
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
