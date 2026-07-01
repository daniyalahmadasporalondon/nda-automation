"""Defer the PDF->working-DOCX reconstruction OFF the Gmail-poll ingest path.

The inline per-PDF reconstruction (pdf2docx + in-process PyMuPDF page open + DOCX
unzip) that ``_convert_pdf_matter_at_ingest`` runs monopolizes the single worker while a
poll churns imports, making the app unresponsive for users. ``defer_pdf_conversion``
(set True ONLY on the Gmail-poll ingest path, exactly like ``defer_ai_review``) SKIPS
that reconstruction: the matter is left as a legacy PDF and the reconstruction is
materialized LATER, off the request thread, by the on-demand/retro conversion that fires
when a human clicks Review.

These tests pin the three-part contract:
  * a POLL/deferred-path PDF matter does NOT reconstruct at ingest (working DOCX
    deferred; reconstruction not called);
  * a MANUAL-upload PDF matter STILL reconstructs at ingest (defer flag defaults False --
    unchanged);
  * a deferred matter DOES get converted via the on-demand/retro path when reviewed
    (retro_convert_pdf_matter reconstructs the working DOCX the deferred ingest skipped).
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document

from nda_automation import (
    artifact_registry,
    ingestion_service,
    pdf_ingest_conversion,
)
from nda_automation.matter_repository import InMemoryMatterRepository

PDF_BYTES = b"%PDF-1.7\nfake pdf\n%%EOF\n"

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


def _route_conversion_through_stub(monkeypatch, spy):
    """Route ingestion_service's conversion through the stub converter, counting calls.

    ``spy`` is a mutable dict whose ``n`` key counts every reconstruction invocation, so a
    test can assert reconstruction was (or was NOT) attempted.
    """
    real_convert = pdf_ingest_conversion.convert_pdf_matter_to_docx

    def _counting_convert(pdf_bytes, source_filename, paragraphs, **_):
        spy["n"] += 1
        return real_convert(
            pdf_bytes, source_filename, paragraphs, converter=_StubConverter(RECON_PARAGRAPHS)
        )

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion,
        "convert_pdf_matter_to_docx",
        _counting_convert,
    )


def _stub_pdf_extraction(monkeypatch):
    monkeypatch.setattr(
        ingestion_service,
        "extract_document",
        lambda filename, document_bytes: ("pdf", _pypdf_paragraphs(), None),
    )


# --------------------------------------------------------------------------- #
# 1. POLL/deferred path: reconstruction is SKIPPED at ingest.
# --------------------------------------------------------------------------- #
def test_deferred_pdf_ingest_does_not_reconstruct(monkeypatch):
    repo = InMemoryMatterRepository()
    spy = {"n": 0}
    _stub_pdf_extraction(monkeypatch)
    _route_conversion_through_stub(monkeypatch, spy)

    matter = ingestion_service.create_matter_from_document(
        filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        source_type="gmail_inbound",
        board_column="gmail_demo",
        owner_user_id="owner-1",
        repository=repo,
        defer_pdf_conversion=True,  # the Gmail-poll signal
        drive_sync_runner=lambda func: None,
    )

    # Reconstruction was NOT attempted -- the pdf2docx child / PyMuPDF open / DOCX unzip
    # never ran on the poll thread.
    assert spy["n"] == 0

    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    # No working artifact + no re-keyed paragraphs: this is a legacy PDF with the working
    # DOCX deferred.
    assert artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING) is None
    assert stored.get(ingestion_service.WORKING_DOCX_PARAGRAPHS_FIELD) is None
    # The deferred state is OBSERVABLE: the status field records the intentional defer
    # (a benign, expected outcome -- NOT a failure).
    assert stored.get(ingestion_service.WORKING_DOCX_STATUS_FIELD) == (
        ingestion_service.WORKING_DOCX_STATUS_DEFERRED
    )
    # The matter is still a normal PDF matter, viewable as a legacy page-image PDF: its
    # source bytes are retained and no working DOCX exists yet.
    assert repo.get_source_document_bytes(stored) == PDF_BYTES


# --------------------------------------------------------------------------- #
# 2. MANUAL upload: reconstruction STILL happens at ingest (unchanged).
# --------------------------------------------------------------------------- #
def test_manual_upload_pdf_still_reconstructs_at_ingest(monkeypatch):
    repo = InMemoryMatterRepository()
    spy = {"n": 0}
    _stub_pdf_extraction(monkeypatch)
    _route_conversion_through_stub(monkeypatch, spy)

    # The manual-upload path (routes.matters.handle_matter_upload) never passes
    # defer_pdf_conversion, so it stays at its False default -> converts at ingest.
    matter = ingestion_service.create_matter_from_document(
        filename="upload.pdf",
        document_bytes=PDF_BYTES,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
        repository=repo,
        drive_sync_runner=lambda func: None,
    )

    # Reconstruction DID run at ingest -- exactly once.
    assert spy["n"] == 1

    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    artifact = artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING)
    assert artifact is not None
    working_bytes = repo.get_artifact_document(artifact.stored_filename)
    assert working_bytes and working_bytes[:2] == b"PK"  # a real (zip) DOCX
    rekeyed = stored.get(ingestion_service.WORKING_DOCX_PARAGRAPHS_FIELD)
    assert isinstance(rekeyed, list) and len(rekeyed) == 3
    assert stored.get(ingestion_service.WORKING_DOCX_STATUS_FIELD) == (
        ingestion_service.WORKING_DOCX_STATUS_CONVERTED
    )


def test_defer_flag_defaults_false(monkeypatch):
    """A caller that omits defer_pdf_conversion gets the eager-convert behavior. This is
    the guarantee the manual-upload path relies on -- it never sets the flag."""
    repo = InMemoryMatterRepository()
    spy = {"n": 0}
    _stub_pdf_extraction(monkeypatch)
    _route_conversion_through_stub(monkeypatch, spy)

    ingestion_service.create_matter_from_document(
        filename="default.pdf",
        document_bytes=PDF_BYTES,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
        repository=repo,
        drive_sync_runner=lambda func: None,
    )
    assert spy["n"] == 1


# --------------------------------------------------------------------------- #
# 3. On-demand/retro path materializes the deferred working DOCX.
# --------------------------------------------------------------------------- #
def test_deferred_matter_is_converted_by_retro_path(monkeypatch):
    """A PDF matter that was DEFERRED at ingest (poll path) gets its working DOCX built by
    the on-demand/retro conversion -- the exact call the review worker fires with
    is_on_demand=True (ingestion_service ~line 1088:
    ``retro_convert_pdf_matter_guarded(...)``)."""
    repo = InMemoryMatterRepository()
    spy = {"n": 0}
    _stub_pdf_extraction(monkeypatch)
    _route_conversion_through_stub(monkeypatch, spy)

    # Ingest via the deferred (poll) path: no working DOCX built.
    matter = ingestion_service.create_matter_from_document(
        filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        source_type="gmail_inbound",
        board_column="gmail_demo",
        owner_user_id="owner-1",
        repository=repo,
        defer_pdf_conversion=True,
        drive_sync_runner=lambda func: None,
    )
    assert spy["n"] == 0
    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
    assert artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING) is None

    # Now a human clicks Review: the review worker (is_on_demand=True) runs
    # retro_convert_pdf_matter_guarded, which calls retro_convert_pdf_matter. That is the
    # trigger under test -- run it directly over the stored deferred matter.
    converted = ingestion_service.retro_convert_pdf_matter_guarded(
        stored, repository=repo, owner_user_id="owner-1"
    )

    # The retro path DID reconstruct (the deferred ingest did not).
    assert spy["n"] == 1
    stored_after = repo.get_matter(converted["id"], owner_user_id="owner-1")
    artifact = artifact_registry.latest_artifact_for_role(
        stored_after, artifact_registry.ROLE_WORKING
    )
    assert artifact is not None
    working_bytes = repo.get_artifact_document(artifact.stored_filename)
    assert working_bytes and working_bytes[:2] == b"PK"  # a real (zip) DOCX now exists
    rekeyed = stored_after.get(ingestion_service.WORKING_DOCX_PARAGRAPHS_FIELD)
    assert isinstance(rekeyed, list) and len(rekeyed) == 3
    assert all("source_part" not in paragraph for paragraph in rekeyed)
    assert stored_after.get(ingestion_service.WORKING_DOCX_STATUS_FIELD) == (
        ingestion_service.WORKING_DOCX_STATUS_CONVERTED
    )


def test_retro_is_noop_once_working_docx_present(monkeypatch):
    """After the retro conversion materializes the working DOCX, a second on-demand Review
    is idempotent: no re-reconstruction (the guarded retro is a no-op for a matter that
    already has a working DOCX)."""
    repo = InMemoryMatterRepository()
    spy = {"n": 0}
    _stub_pdf_extraction(monkeypatch)
    _route_conversion_through_stub(monkeypatch, spy)

    matter = ingestion_service.create_matter_from_document(
        filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        source_type="gmail_inbound",
        board_column="gmail_demo",
        owner_user_id="owner-1",
        repository=repo,
        defer_pdf_conversion=True,
        drive_sync_runner=lambda func: None,
    )
    stored = repo.get_matter(matter["id"], owner_user_id="owner-1")

    # First Review -> builds the working DOCX.
    ingestion_service.retro_convert_pdf_matter_guarded(
        stored, repository=repo, owner_user_id="owner-1"
    )
    assert spy["n"] == 1

    # Second Review -> idempotent no-op (already_present), NO second reconstruction.
    stored_after = repo.get_matter(matter["id"], owner_user_id="owner-1")
    ingestion_service.retro_convert_pdf_matter_guarded(
        stored_after, repository=repo, owner_user_id="owner-1"
    )
    assert spy["n"] == 1
    assert repo.get_matter(matter["id"], owner_user_id="owner-1").get(
        ingestion_service.WORKING_DOCX_STATUS_FIELD
    ) == ingestion_service.WORKING_DOCX_STATUS_ALREADY_PRESENT
