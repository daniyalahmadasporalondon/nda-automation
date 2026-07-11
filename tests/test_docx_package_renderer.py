from __future__ import annotations

import re
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import docx_package_renderer
from nda_automation.docx_export import SourceRedlinePackage
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
    # Behavior change (commit 44bf3fef, "Fix reviewed-DOCX coverage gate false-reject on
    # counterparty-redlined NDAs"): when the raw source bytes are available -- which they
    # always are through render_source_redline_package -- verify_export_content_coverage
    # now builds the EXPECTED accepted-paragraph sequence from the source docx's OWN
    # accepted view, not from the expected_source_text string. That string is no longer the
    # sequence authority (it only feeds the length-ratio floor). This test predated that
    # refactor and used to force a mismatch by passing a REVERSED expected_source_text; that
    # trick is now (correctly) ignored, so it stopped detecting anything.
    #
    # The gate's fail-closed contract is intact, so this asserts it against a REAL
    # divergence: the renderer builds a faithful export from the source, so to exercise the
    # content gate we patch the build seam to physically reorder the exported paragraphs
    # (health-valid, same char count so the ratio floor passes). The renderer must surface
    # the accepted-change-sequence content error and mark the package invalid -- proving a
    # reordered/misplaced/dropped redline cannot ship.
    source_docx = _source_docx(["First source paragraph.", "Second source paragraph."])
    paragraphs = extract_docx_paragraphs(source_docx)
    review_result = {"paragraphs": paragraphs, "redline_edits": []}
    expected_source_text = "\n\n".join(paragraph["text"] for paragraph in paragraphs)

    real_builder = docx_package_renderer.build_source_redline_docx

    def reordering_builder(source_docx_arg, rr, **kwargs):
        package = real_builder(source_docx_arg, rr, **kwargs)
        data = package.data if isinstance(package, SourceRedlinePackage) else package
        return SourceRedlinePackage(
            data=_reorder_body_paragraphs(data), anchor_uncertain_redlines=[]
        )

    with patch.object(
        docx_package_renderer,
        "build_source_redline_docx",
        side_effect=reordering_builder,
    ):
        result = docx_package_renderer.render_source_redline_package(
            source_docx,
            review_result,
            expected_source_text=expected_source_text,
            expected_redline_edits=[],
        )

    assert result.health_errors == []
    assert result.content_errors
    assert "accepted-change paragraph sequence" in result.content_errors[0]
    assert result.valid is False


def test_open_health_validated_exactly_once_per_export(monkeypatch):
    """Dedup guard: ``validate_docx_open_health`` must run ONCE per export, not twice.

    It was previously called both inside ``build_source_redline_package`` (which
    RAISES on failure) AND again in ``render_source_redline_package`` on the same
    bytes. Counting across every binding the export path can reach proves the check
    now runs a single time (it was 2 before the dedup).
    """
    from nda_automation import docx_health, source_redline_docx

    calls = {"n": 0}
    real = docx_health.validate_docx_open_health

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    # The surviving caller binds it via ``from .docx_health import ...``; patch that
    # binding. Also patch the renderer's binding IF it re-imports it (it must not
    # after the dedup) so a reintroduced duplicate would push the count back to 2.
    monkeypatch.setattr(source_redline_docx, "validate_docx_open_health", counting)
    if hasattr(docx_package_renderer, "validate_docx_open_health"):
        monkeypatch.setattr(docx_package_renderer, "validate_docx_open_health", counting)

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
    assert calls["n"] == 1


def _reorder_body_paragraphs(docx_bytes: bytes) -> bytes:
    """Reverse the order of the body <w:p> paragraphs while preserving every other
    package part (rels/content-types/sectPr) so the result stays HEALTH-valid but
    CONTENT-diverged. Isolates the content-coverage sequence check from the open-health
    check (health is verified first and short-circuits coverage)."""
    with ZipFile(BytesIO(docx_bytes)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    document = parts["word/document.xml"].decode("utf-8")
    body = re.search(r"(<w:body>)(.*)(</w:body>)", document, re.S)
    assert body is not None, "rendered DOCX has no w:body"
    inner = body.group(2)
    body_paragraphs = re.findall(r"<w:p\b.*?</w:p>", inner, re.S)
    sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", inner, re.S)
    new_inner = "".join(reversed(body_paragraphs)) + (sect.group(0) if sect else "")
    new_document = (
        document[: body.start()]
        + body.group(1)
        + new_inner
        + body.group(3)
        + document[body.end() :]
    )
    parts["word/document.xml"] = new_document.encode("utf-8")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


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
