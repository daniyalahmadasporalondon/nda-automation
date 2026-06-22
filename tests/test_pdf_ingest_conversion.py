"""Approach C: PDF→DOCX-at-ingest conversion + the index re-keying it produces.

Covers the module unit (convert_pdf_matter_to_docx) and the ingest wiring in
create_matter_from_document (happy path persists a working artifact + re-keyed
paragraphs; fail-open keeps the legacy PDF matter when conversion is unavailable).
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from nda_automation import (
    artifact_registry,
    ingestion_service,
    pdf_docx_reconstruction,
    pdf_ingest_conversion,
)
from nda_automation.matter_repository import InMemoryMatterRepository

PDF_BYTES = b"%PDF-1.7\nfake pdf\n%%EOF\n"

# The clause text the reconstructed DOCX body carries, in document order.
RECON_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by the parties.",
    "Confidential Information shall be kept strictly confidential by the receiving party.",
    "This Agreement shall be governed by the laws of England and Wales.",
]


def make_docx(paragraphs) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _StubConverter:
    """A pdf2docx stand-in that writes a fixed reconstructed DOCX body."""

    name = "stub-pdf2docx"

    def __init__(self, paragraphs):
        self._paragraphs = paragraphs

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path):
        output_path.write_bytes(make_docx(self._paragraphs))


def _pypdf_paragraphs():
    """pypdf review paragraphs as the PDF extractor mints them: source_part='pdf'
    and a source_index in the pypdf index space (NOT the reconstructed space)."""
    return [
        {"id": "p1", "text": RECON_PARAGRAPHS[0], "source_index": 1, "source_part": "pdf"},
        {"id": "p2", "text": RECON_PARAGRAPHS[1], "source_index": 2, "source_part": "pdf"},
        {"id": "p3", "text": RECON_PARAGRAPHS[2], "source_index": 3, "source_part": "pdf"},
    ]


# --------------------------------------------------------------------------- #
# Module unit: convert_pdf_matter_to_docx
# --------------------------------------------------------------------------- #
def test_convert_rekeys_paragraphs_to_reconstructed_index_and_drops_pdf_marker():
    converter = _StubConverter(RECON_PARAGRAPHS)
    working = pdf_ingest_conversion.convert_pdf_matter_to_docx(
        PDF_BYTES, "inbound.pdf", _pypdf_paragraphs(), converter=converter
    )
    assert working.mapped_count == 3
    assert working.unmapped_count == 0
    # Every mapped paragraph dropped the PDF marker and now anchors by index into the
    # reconstructed body (1-based canonical body-paragraph numbering).
    for index, paragraph in enumerate(working.paragraphs, start=1):
        assert "source_part" not in paragraph
        assert paragraph["source_index"] == index
    # The reconstructed bytes are a real DOCX whose body matches the reconstruction.
    body = pdf_ingest_conversion.reconstructed_body_index(working.docx_bytes)
    assert [text for (_idx, text, _norm) in body] == RECON_PARAGRAPHS


def test_convert_keeps_marker_when_paragraph_unplaceable():
    converter = _StubConverter(RECON_PARAGRAPHS)
    paragraphs = _pypdf_paragraphs()
    paragraphs.append(
        {"id": "p4", "text": "A clause that the reconstruction never contains at all.",
         "source_index": 4, "source_part": "pdf"}
    )
    working = pdf_ingest_conversion.convert_pdf_matter_to_docx(
        PDF_BYTES, "inbound.pdf", paragraphs, converter=converter
    )
    assert working.mapped_count == 3
    assert working.unmapped_count == 1
    # The unplaceable paragraph KEEPS its PDF marker so the fail-closed text anchor
    # path still guards it (never a silent drop).
    assert working.paragraphs[-1].get("source_part") == "pdf"


class _UnavailableConverter:
    name = "stub-unavailable"

    def is_available(self):
        return False

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path):
        raise AssertionError("unavailable converter should not be invoked")


def test_convert_propagates_unavailable_engine_error():
    with pytest.raises(pdf_docx_reconstruction.PdfDocxReconstructionError):
        pdf_ingest_conversion.convert_pdf_matter_to_docx(
            PDF_BYTES, "inbound.pdf", _pypdf_paragraphs(),
            converter=_UnavailableConverter(),
        )


# --------------------------------------------------------------------------- #
# Ingest wiring: create_matter_from_document
# --------------------------------------------------------------------------- #
def test_ingest_pdf_persists_working_docx_and_rekeyed_paragraphs(monkeypatch):
    repo = InMemoryMatterRepository()
    converter = _StubConverter(RECON_PARAGRAPHS)

    monkeypatch.setattr(
        ingestion_service, "extract_document",
        lambda filename, document_bytes: ("pdf", _pypdf_paragraphs(), None),
    )
    # Route the conversion through the stub converter.
    real_convert = pdf_ingest_conversion.convert_pdf_matter_to_docx
    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion, "convert_pdf_matter_to_docx",
        lambda pdf_bytes, source_filename, paragraphs, **_: real_convert(
            pdf_bytes, source_filename, paragraphs, converter=converter
        ),
    )

    matter = ingestion_service.create_matter_from_document(
        filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
        repository=repo,
        drive_sync_runner=lambda func: None,
    )

    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    artifact = artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING)
    assert artifact is not None
    working_bytes = repo.get_artifact_document(artifact.stored_filename)
    assert working_bytes and working_bytes[:2] == b"PK"  # a real (zip) DOCX
    rekeyed = stored.get(ingestion_service.WORKING_DOCX_PARAGRAPHS_FIELD)
    assert isinstance(rekeyed, list) and len(rekeyed) == 3
    assert all("source_part" not in paragraph for paragraph in rekeyed)
    # The matter stays "Not Reviewed": the conversion seeds NO review_result, so the
    # intentional deferred-review state is preserved.
    assert stored.get("review_result") in (None, {})


def test_ingest_pdf_fails_open_when_conversion_unavailable(monkeypatch):
    repo = InMemoryMatterRepository()
    monkeypatch.setattr(
        ingestion_service, "extract_document",
        lambda filename, document_bytes: ("pdf", _pypdf_paragraphs(), None),
    )

    def _boom(*_args, **_kwargs):
        raise pdf_docx_reconstruction.PdfDocxReconstructionUnavailableError("no engine")

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion, "convert_pdf_matter_to_docx", _boom
    )

    matter = ingestion_service.create_matter_from_document(
        filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
        repository=repo,
        drive_sync_runner=lambda func: None,
    )

    # Ingest still succeeded; the matter is the legacy un-converted PDF with NO
    # working artifact and NO re-keyed paragraphs.
    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    assert artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING) is None
    assert stored.get(ingestion_service.WORKING_DOCX_PARAGRAPHS_FIELD) is None


def test_ingest_docx_does_not_convert(monkeypatch):
    repo = InMemoryMatterRepository()
    docx_bytes = make_docx(RECON_PARAGRAPHS)
    calls = {"n": 0}

    def _should_not_run(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("DOCX matters must not be reconstructed")

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion, "convert_pdf_matter_to_docx", _should_not_run
    )
    matter = ingestion_service.create_matter_from_document(
        filename="native.docx",
        document_bytes=docx_bytes,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
        repository=repo,
        drive_sync_runner=lambda func: None,
    )
    assert calls["n"] == 0
    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    assert artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
