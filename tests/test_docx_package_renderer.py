from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import docx_package_renderer
from nda_automation.docx_text import extract_docx_paragraphs
from nda_automation.docx_xml import _escape_xml
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


def test_source_redline_renderer_returns_validated_package_result():
    source_docx = _source_docx(["This Agreement shall be governed by the laws of California."])
    paragraphs = extract_docx_paragraphs(source_docx)
    review_result = _review_result(paragraphs)
    review_result["extracted_text"] = "\n\n".join(paragraph["text"] for paragraph in paragraphs)

    result = docx_package_renderer.render_source_redline_package(
        source_docx,
        review_result,
        expected_source_text=review_result["extracted_text"],
        expected_redline_edits=review_result["redline_edits"],
    )

    assert result.valid is True
    assert result.health_errors == []
    assert result.content_errors == []
    with ZipFile(BytesIO(result.data)) as archive:
        assert archive.testzip() is None
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "California" in document_xml
    assert "England and Wales" in document_xml


def test_source_redline_renderer_reports_content_validation_errors():
    source_docx = _source_docx(["First source paragraph.", "Second source paragraph."])
    paragraphs = extract_docx_paragraphs(source_docx)
    review_result = {"paragraphs": paragraphs, "redline_edits": []}

    result = docx_package_renderer.render_source_redline_package(
        source_docx,
        review_result,
        expected_source_text="\n\n".join(paragraph["text"] for paragraph in reversed(paragraphs)),
        expected_redline_edits=[],
    )

    assert result.health_errors == []
    assert result.content_errors
    assert "accepted-change paragraph sequence" in result.content_errors[0]
    assert result.valid is False


def _review_result(paragraphs: list[dict]) -> dict:
    review_paragraphs = _review_paragraphs(paragraphs)
    paragraph = review_paragraphs[0]
    replacement = "This Agreement shall be governed by the laws of England and Wales."
    return {
        "paragraphs": review_paragraphs,
        "redline_edits": [
            {
                "id": "governing-law-replace",
                "action": REDLINE_REPLACE_PARAGRAPH,
                "clause_id": "governing_law",
                "paragraph_id": paragraph["id"],
                "paragraph_index": paragraph["index"],
                "source_index": paragraph["source_index"],
                "original_text": paragraph["text"],
                "replacement_text": replacement,
            }
        ],
    }


def _review_paragraphs(paragraphs: list[dict]) -> list[dict]:
    return [
        {
            "id": f"p{index}",
            "index": index,
            "source_index": paragraph.get("source_index", index),
            "text": paragraph["text"],
        }
        for index, paragraph in enumerate(paragraphs, start=1)
    ]


def _source_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{_escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}</w:body>
</w:document>"""
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    package_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("_rels/.rels", package_rels_xml)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", document_rels_xml)
        return output.getvalue()
