"""Service-level tests proving the MatterRepository seam is load-bearing.

ingestion_service and redline_export_service accept an injected repository and
can run end-to-end against InMemoryMatterRepository with no disk involvement.
"""
from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import matter_store
from nda_automation.ingestion_service import create_matter_from_document
from nda_automation.matter_repository import DiskMatterRepository
from nda_automation.redline_export_service import (
    MatterNotFoundError,
    _review_result_for_export,
    build_matter_redline,
)

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def test_ingestion_service_uses_injected_repository(in_memory_matters):
    docx = _docx(NDA_PARAGRAPHS)
    matter = create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=docx,
        source_type="manual_upload",
        board_column="intake",
        repository=in_memory_matters,
    )
    # Landed in the injected in-memory repo...
    assert in_memory_matters.get_matter(matter["id"]) is not None
    assert in_memory_matters.get_source_document_bytes(matter) == docx
    assert isinstance(matter["review_result"], dict)
    # ...and never touched disk.
    assert matter_store.get_matter(matter["id"]) is None


def test_redline_service_reads_from_injected_repository(in_memory_matters):
    docx = _docx(NDA_PARAGRAPHS)
    matter = create_matter_from_document(
        filename="mutual-nda.docx", document_bytes=docx, repository=in_memory_matters
    )
    review_result, source_bytes, source_filename = _review_result_for_export(
        {"matter_id": matter["id"]}, "", repository=in_memory_matters
    )
    assert source_bytes == docx
    assert source_filename == "mutual-nda.docx"
    assert isinstance(review_result, dict)


def test_redline_service_full_export_against_in_memory(in_memory_matters):
    docx = _docx(NDA_PARAGRAPHS)
    matter = create_matter_from_document(
        filename="mutual-nda.docx", document_bytes=docx, repository=in_memory_matters
    )
    export = build_matter_redline(matter["id"], repository=in_memory_matters)
    assert export.data
    assert export.filename.endswith(".docx")
    assert export.saved_path is None  # persist defaults to False


def test_redline_service_queries_injected_repo_not_disk(in_memory_matters, tmp_path, monkeypatch):
    # Seed a matter on DISK; the injected in-memory repo does not have it.
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    disk_matter = create_matter_from_document(
        filename="disk-nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        repository=DiskMatterRepository(),
    )
    # The matter exists on disk, but redline queries the injected in-memory repo.
    assert matter_store.get_matter(disk_matter["id"]) is not None
    with pytest.raises(MatterNotFoundError):
        build_matter_redline(disk_matter["id"], repository=in_memory_matters)
