import unittest
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation.docx_text import extract_docx_paragraphs
from nda_automation.review_document import align_document_paragraphs
from nda_automation.routes import matters as matters_routes


class _SourceRepository:
    def __init__(self, document_bytes: bytes | None) -> None:
        self.document_bytes = document_bytes

    def get_source_document_bytes(self, matter: dict) -> bytes | None:
        return self.document_bytes


def _titled_numbered_docx() -> bytes:
    """A .docx with a Title paragraph and a decimal-numbered clause."""
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Non-Disclosure Agreement</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="42"/></w:numPr></w:pPr><w:r><w:t>Definitions clause text.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
</w:styles>"""
    numbering_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="7">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="42"><w:abstractNumId w:val="7"/></w:num>
</w:numbering>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/styles.xml", styles_xml)
            archive.writestr("word/numbering.xml", numbering_xml)
        return output.getvalue()


def _flat_paragraphs(docx_bytes: bytes) -> list[dict]:
    """The flat (structure-stripped) paragraphs an old matter would have stored."""
    rich = extract_docx_paragraphs(docx_bytes)
    text = "\n\n".join(str(paragraph.get("text", "")) for paragraph in rich)
    aligned = align_document_paragraphs(rich, text)
    return [
        {key: paragraph[key] for key in ("id", "index", "text", "start", "end") if key in paragraph}
        for paragraph in aligned
    ]


class RestoreParagraphStructureTests(unittest.TestCase):
    def _matter(self, docx_bytes: bytes, paragraphs: list[dict], *, source_filename: str = "nda.docx") -> dict:
        return {
            "id": "matter_x",
            "source_filename": source_filename,
            "review_result": {"paragraphs": paragraphs},
        }

    def test_restores_numbering_and_title_style_onto_flat_paragraphs(self):
        docx = _titled_numbered_docx()
        flat = _flat_paragraphs(docx)
        # Sanity: the stored paragraphs carry no structure.
        self.assertTrue(all(not p.get("structure_label") and not p.get("numbering") for p in flat))
        matter = self._matter(docx, flat)
        merged = matters_routes._restored_review_result_paragraphs(
            matter,
            repository=_SourceRepository(docx),
        )
        self.assertIsNotNone(merged)
        title, clause = merged
        self.assertEqual(title["text"], "Non-Disclosure Agreement")
        self.assertEqual(str(title.get("style_name") or title.get("style_id")), "Title")
        self.assertEqual(clause.get("structure_label"), "1.")
        self.assertTrue(clause.get("numbering"))

    def test_returns_none_when_already_structured(self):
        docx = _titled_numbered_docx()
        flat = _flat_paragraphs(docx)
        flat[1]["structure_label"] = "1."  # already structured
        matter = self._matter(docx, flat)
        self.assertIsNone(matters_routes._restored_review_result_paragraphs(
            matter,
            repository=_SourceRepository(docx),
        ))

    def test_bails_when_stored_text_diverges_from_reextraction(self):
        docx = _titled_numbered_docx()
        flat = _flat_paragraphs(docx)
        flat[1]["text"] = "A completely different clause that won't align."
        matter = self._matter(docx, flat)
        self.assertIsNone(matters_routes._restored_review_result_paragraphs(
            matter,
            repository=_SourceRepository(docx),
        ))

    def test_returns_none_for_non_docx_source(self):
        docx = _titled_numbered_docx()
        matter = self._matter(docx, _flat_paragraphs(docx), source_filename="nda.pdf")
        self.assertIsNone(matters_routes._restored_review_result_paragraphs(
            matter,
            repository=_SourceRepository(docx),
        ))

    def test_with_restored_structure_leaves_structured_matter_untouched(self):
        docx = _titled_numbered_docx()
        flat = _flat_paragraphs(docx)
        flat[1]["numbering"] = {"label": "1."}
        matter = self._matter(docx, flat)
        result = matters_routes._with_restored_paragraph_structure(
            matter,
            repository=_SourceRepository(docx),
        )
        self.assertIs(result, matter)


if __name__ == "__main__":
    unittest.main()
