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

    def test_extracts_table_cell_visual_style(self):
        data = make_docx(
            [],
            body_xml="""
            <w:tbl>
              <w:tr>
                <w:tc>
                  <w:tcPr>
                    <w:tcW w:w="2400" w:type="dxa"/>
                    <w:shd w:fill="D9EAD3"/>
                  </w:tcPr>
                  <w:p><w:r><w:t>Shaded cell.</w:t></w:r></w:p>
                </w:tc>
                <w:tc>
                  <w:tcPr>
                    <w:tcW w:w="1200" w:type="pct"/>
                    <w:shd w:fill="auto"/>
                  </w:tcPr>
                  <w:p><w:r><w:t>Plain cell.</w:t></w:r></w:p>
                </w:tc>
              </w:tr>
            </w:tbl>
            """,
        )

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(paragraphs[0]["text"], "Shaded cell.")
        self.assertEqual(
            paragraphs[0]["table"],
            {
                "table_index": 1,
                "row_index": 1,
                "cell_index": 1,
                "cell_style": {
                    "background_color": "#d9ead3",
                    "width": {"value": 2400, "type": "dxa"},
                },
            },
        )
        self.assertEqual(
            paragraphs[1]["table"],
            {
                "table_index": 1,
                "row_index": 1,
                "cell_index": 2,
                "cell_style": {"width": {"value": 1200, "type": "pct"}},
            },
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

    def test_extracts_run_level_bold_italic_underline(self):
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">Plain start </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>bold word</w:t></w:r>
      <w:r><w:t xml:space="preserve"> and </w:t></w:r>
      <w:r><w:rPr><w:i/></w:rPr><w:t>italic word</w:t></w:r>
      <w:r><w:t xml:space="preserve"> and </w:t></w:r>
      <w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>underlined</w:t></w:r>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Entirely plain paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
        data = make_zip({"word/document.xml": document_xml}, compression=ZIP_DEFLATED)

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs[0]["text"],
            "Plain start bold word and italic word and underlined.",
        )
        self.assertEqual(
            paragraphs[0]["runs"],
            [
                {"text": "Plain start ", "bold": False, "italic": False, "underline": False},
                {"text": "bold word", "bold": True, "italic": False, "underline": False},
                {"text": " and ", "bold": False, "italic": False, "underline": False},
                {"text": "italic word", "bold": False, "italic": True, "underline": False},
                {"text": " and ", "bold": False, "italic": False, "underline": False},
                {"text": "underlined", "bold": False, "italic": False, "underline": True},
                {"text": ".", "bold": False, "italic": False, "underline": False},
            ],
        )
        # A run-reconstruction must equal the flat text exactly.
        self.assertEqual(
            "".join(run["text"] for run in paragraphs[0]["runs"]),
            paragraphs[0]["text"],
        )
        # Plain paragraphs stay lean: no runs key, so the additive contract holds.
        self.assertNotIn("runs", paragraphs[1])

    def test_extracts_run_and_paragraph_font_sizes(self):
        # ``<w:sz>`` is in half-points: 24 -> 12pt, 36 -> 18pt. The third run has
        # no explicit size, so it stays size-free (additive contract). The fourth
        # paragraph takes its size from the paragraph-mark run default.
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:rPr><w:sz w:val="24"/></w:rPr><w:t xml:space="preserve">twelve point </w:t></w:r>
      <w:r><w:rPr><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr><w:t>eighteen bold</w:t></w:r>
      <w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve"> sizeless italic</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:rPr><w:sz w:val="20"/></w:rPr></w:pPr>
      <w:r><w:rPr><w:b/></w:rPr><w:t>mark default ten point</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
        data = make_zip({"word/document.xml": document_xml}, compression=ZIP_DEFLATED)

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs[0]["runs"],
            [
                {"text": "twelve point ", "bold": False, "italic": False, "underline": False, "size": 12},
                {"text": "eighteen bold", "bold": True, "italic": False, "underline": False, "size": 18},
                {"text": " sizeless italic", "bold": False, "italic": True, "underline": False},
            ],
        )
        # Runs must still tile the flat paragraph text byte-exactly.
        self.assertEqual(
            "".join(run["text"] for run in paragraphs[0]["runs"]),
            paragraphs[0]["text"],
        )
        # Paragraph fontSize comes from the dominant (first sized) run when there
        # is no paragraph-mark default.
        self.assertEqual(paragraphs[0]["fontSize"], 12)
        # The second paragraph prefers its paragraph-mark run-default size (20
        # half-points -> 10pt) over the unsized body run.
        self.assertEqual(paragraphs[1]["fontSize"], 10)

    def test_resolves_paragraph_indent_from_numbering_level(self):
        # Sub-clauses a)/b)/c) often number at ilvl 0 and take their indent from
        # the numbering level's ``<w:ind w:left>`` (twips), not the level depth.
        # 1080 twips -> 54pt; the direct ``<w:ind>`` on the second paragraph wins.
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr></w:pPr><w:r><w:t>a) Sub-clause from numbering indent.</w:t></w:r></w:p>
    <w:p><w:pPr><w:ind w:left="720"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr></w:pPr><w:r><w:t>b) Sub-clause with direct indent.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Flush paragraph with no indent at all.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
        numbering_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="3">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1)"/><w:pPr><w:ind w:left="1080" w:hanging="360"/></w:pPr></w:lvl>
  </w:abstractNum>
  <w:num w:numId="5"><w:abstractNumId w:val="3"/></w:num>
</w:numbering>"""
        data = make_zip(
            {"word/document.xml": document_xml, "word/numbering.xml": numbering_xml},
            compression=ZIP_DEFLATED,
        )

        paragraphs = extract_docx_paragraphs(data)

        # ilvl 0 paragraph resolves its indent from the numbering level (1080 twips).
        self.assertEqual(paragraphs[0]["numbering"]["level"], 0)
        self.assertEqual(paragraphs[0]["indent_left"], 54)
        # The paragraph's own ``<w:ind>`` (720 twips -> 36pt) takes precedence.
        self.assertEqual(paragraphs[1]["indent_left"], 36)
        # A flush paragraph keeps the additive contract: no indent_left key.
        self.assertNotIn("indent_left", paragraphs[2])

    def test_extracts_run_color_highlight_strike_and_vert_align(self):
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">Plain </w:t></w:r>
      <w:r><w:rPr><w:color w:val="FF0000"/></w:rPr><w:t>red</w:t></w:r>
      <w:r><w:t xml:space="preserve"> </w:t></w:r>
      <w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>highlighted</w:t></w:r>
      <w:r><w:t xml:space="preserve"> </w:t></w:r>
      <w:r><w:rPr><w:strike/></w:rPr><w:t>struck</w:t></w:r>
      <w:r><w:t xml:space="preserve"> x</w:t></w:r>
      <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>2</w:t></w:r>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:rPr><w:color w:val="auto"/><w:strike w:val="false"/></w:rPr><w:t>Auto color, strike off.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
        data = make_zip({"word/document.xml": document_xml}, compression=ZIP_DEFLATED)

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(
            paragraphs[0]["runs"],
            [
                {"text": "Plain ", "bold": False, "italic": False, "underline": False},
                {"text": "red", "bold": False, "italic": False, "underline": False, "color": "#ff0000"},
                {"text": " ", "bold": False, "italic": False, "underline": False},
                {"text": "highlighted", "bold": False, "italic": False, "underline": False, "highlight": "yellow"},
                {"text": " ", "bold": False, "italic": False, "underline": False},
                {"text": "struck", "bold": False, "italic": False, "underline": False, "strike": True},
                {"text": " x", "bold": False, "italic": False, "underline": False},
                {"text": "2", "bold": False, "italic": False, "underline": False, "vertAlign": "superscript"},
                {"text": ".", "bold": False, "italic": False, "underline": False},
            ],
        )
        # The run-text tiling invariant must hold: runs concatenate to flat text.
        self.assertEqual(
            "".join(run["text"] for run in paragraphs[0]["runs"]),
            paragraphs[0]["text"],
        )
        # ``auto`` color and an explicitly-disabled strike are not formatting, so
        # the second paragraph stays lean with no runs key.
        self.assertNotIn("runs", paragraphs[1])

    def test_run_toggle_disabled_is_not_treated_as_formatting(self):
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>Not actually bold.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
        data = make_zip({"word/document.xml": document_xml}, compression=ZIP_DEFLATED)

        paragraphs = extract_docx_paragraphs(data)

        self.assertEqual(paragraphs[0]["text"], "Not actually bold.")
        self.assertNotIn("runs", paragraphs[0])

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
