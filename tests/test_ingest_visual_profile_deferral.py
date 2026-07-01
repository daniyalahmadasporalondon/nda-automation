"""Wire the fix-5 lazy visual-profile skip into the Gmail-poll ingest path.

``pdf_text.extract_pdf_document`` gained (in fix-5) a keyword-only
``include_visual_profile`` that, when ``False``, skips the visual profile's SECOND full
PyMuPDF parse and records a byte-compatible ``{"status": "unavailable", "reason":
"deferred", "requires_source_preview": True}`` marker (recomputed on demand for the
source preview). fix-5 left that flag inert: NO production caller passed ``False``.

fix-5b wires it. ``ingestion_service.create_matter_from_document`` forwards
``include_visual_profile=not defer_pdf_conversion`` through ``extract_document`` into
``extract_pdf_document``. ``defer_pdf_conversion`` is set True ONLY on the Gmail-poll
ingest path (``gmail_matter_inbox``, mirroring ``defer_ai_review`` /
``defer_pdf_conversion``), so:

  * a POLL/deferred-path PDF ingest does NOT compute the visual profile
    (``_pdf_visual_profile`` is never called; the deferred marker is recorded);
  * a MANUAL-upload PDF ingest DOES compute it (defer flag defaults False -- unchanged);
  * paragraphs / extracted_text are byte-identical either way;
  * the source preview still works: an interactive fidelity request recomputes the
    profile on demand.

The visual profile is NOT consumed by AI review or redline -- the reviewer sees
text + paragraphs only and redlines anchor on paragraph indices -- so deferring it does
not change review or redline output. It feeds only source-fidelity / the preview, which
already fail-open to "unavailable". These tests pin the wiring end-to-end through the
REAL ``extract_pdf_document`` (they do NOT stub extraction), spying on the real
``pdf_text._pdf_visual_profile`` that fix-5 gates.
"""
from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

from nda_automation import ingestion_service, pdf_text
from nda_automation.matter_repository import InMemoryMatterRepository

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
PYMUPDF_AVAILABLE = importlib.util.find_spec("fitz") is not None
requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")
requires_pymupdf = unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF is not installed")

# A single-clause, text-based PDF built the same minimal way as tests/test_pdf_text.py's
# helpers: a real Type1-font content stream so the REAL pypdf extractor mints real
# paragraphs and the REAL PyMuPDF visual profiler would run if not skipped.
_CLAUSE = "This Agreement shall be governed by the laws of California."


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_package(objects: list[str]) -> bytes:
    header = b"%PDF-1.7\n"
    body = bytearray(header)
    offsets = []
    for obj in objects:
        offsets.append(len(body))
        body += obj.encode("latin-1")
    xref_position = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode("latin-1")
    body += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(body)


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({_escape_pdf_text(text)}) Tj ET\n"
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >> endobj\n",
        f"4 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n",
        "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _pdf_package(objects)


PDF_BYTES = make_pdf(_CLAUSE)


class IngestVisualProfileDeferralTests(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # 1. POLL/deferred path: the visual profile is NOT computed at ingest.
    # ------------------------------------------------------------------ #
    @requires_pypdf
    @requires_pymupdf
    def test_poll_ingest_does_not_compute_visual_profile(self):
        repo = InMemoryMatterRepository()
        with patch.object(pdf_text, "_pdf_visual_profile") as profiler:
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

        # The second full PyMuPDF parse never ran on the poll thread.
        profiler.assert_not_called()

        # The matter is a real 'Not Reviewed' PDF matter (defer_ai_review default True).
        stored = repo.get_matter(matter["id"], owner_user_id="owner-1")
        assert stored.get("review_result") is None
        assert _CLAUSE in stored.get("extracted_text", "")

    @requires_pypdf
    def test_poll_extraction_records_deferred_marker(self):
        # The wiring's EFFECT: extract_document forwards include_visual_profile=False on
        # the poll context (defer_pdf_conversion=True), so the recorded visual_profile is
        # the deferred marker -- the same 'unavailable' contract shape the no-PyMuPDF case
        # emits, which downstream treats as 'not-yet-computed' (fail-open).
        _document_type, poll_paragraphs, poll_quality = ingestion_service.extract_document(
            "inbound.pdf", PDF_BYTES, include_visual_profile=False
        )

        visual_profile = poll_quality["visual_profile"]
        assert visual_profile["status"] == "unavailable"
        assert visual_profile["reason"] == "deferred"
        assert visual_profile["requires_source_preview"] is True
        # Paragraphs are still fully extracted -- only the profile is deferred.
        assert [p["text"] for p in poll_paragraphs] == [_CLAUSE]

    # ------------------------------------------------------------------ #
    # 2. MANUAL upload: the visual profile IS computed at ingest (unchanged).
    # ------------------------------------------------------------------ #
    @requires_pypdf
    @requires_pymupdf
    def test_manual_upload_computes_visual_profile(self):
        repo = InMemoryMatterRepository()
        # Neutralize the ingest-time PDF->working-DOCX reconstruction (irrelevant here,
        # pulls in pdf2docx) so the test isolates the extraction-time profile wiring, and
        # spy on the REAL visual profiler.
        with patch.object(
            ingestion_service, "_convert_pdf_matter_at_ingest", lambda matter, **_: matter
        ), patch.object(
            pdf_text, "_pdf_visual_profile", wraps=pdf_text._pdf_visual_profile
        ) as profiler:
            # The manual-upload path (routes.matters.handle_matter_upload) never passes
            # defer_pdf_conversion, so it stays at its False default -> profile computed.
            ingestion_service.create_matter_from_document(
                filename="upload.pdf",
                document_bytes=PDF_BYTES,
                source_type="manual_upload",
                board_column="in_review",
                owner_user_id="owner-1",
                repository=repo,
                drive_sync_runner=lambda func: None,
            )

        # The visual profile WAS computed at ingest -- exactly once, on the source bytes.
        profiler.assert_called_once_with(PDF_BYTES)

    @requires_pypdf
    @requires_pymupdf
    def test_manual_extraction_computes_ready_profile(self):
        # The manual path (include_visual_profile default True) computes a real,
        # populated profile (status 'ready'), unchanged from before fix-5b.
        _document_type, _paragraphs, manual_quality = ingestion_service.extract_document(
            "upload.pdf", PDF_BYTES
        )
        assert manual_quality["visual_profile"]["status"] == "ready"

    # ------------------------------------------------------------------ #
    # 3. Paragraphs / extracted text are byte-identical either way.
    # ------------------------------------------------------------------ #
    @requires_pypdf
    def test_paragraphs_and_text_identical_poll_vs_manual(self):
        _pt, poll_paragraphs, _pq = ingestion_service.extract_document(
            "inbound.pdf", PDF_BYTES, include_visual_profile=False
        )
        _mt, manual_paragraphs, _mq = ingestion_service.extract_document(
            "upload.pdf", PDF_BYTES, include_visual_profile=True
        )

        # Review paragraphs (the ONLY thing AI review + redline consume) are identical:
        # deferring the profile changes nothing the reviewer or the redline anchor sees.
        assert poll_paragraphs == manual_paragraphs
        poll_text = ingestion_service.extracted_text_from_paragraphs(poll_paragraphs)
        manual_text = ingestion_service.extracted_text_from_paragraphs(manual_paragraphs)
        assert poll_text == manual_text
        assert poll_text == _CLAUSE

    # ------------------------------------------------------------------ #
    # 4. The source preview still works: the profile recomputes on demand.
    # ------------------------------------------------------------------ #
    @requires_pypdf
    @requires_pymupdf
    def test_source_preview_recomputes_profile_on_demand(self):
        # Poll ingest deferred the profile (unavailable/deferred). The interactive source
        # preview recomputes it later on demand: re-extracting WITH the profile (the
        # default) yields a ready profile that source_fidelity surfaces as available.
        from nda_automation.source_fidelity import source_fidelity_payload

        # Deferred at poll time.
        _pt, _pp, deferred_quality = ingestion_service.extract_document(
            "inbound.pdf", PDF_BYTES, include_visual_profile=False
        )
        assert deferred_quality["visual_profile"]["reason"] == "deferred"

        # On-demand recompute for the preview (default include_visual_profile=True).
        _rt, recomputed_paragraphs, recomputed_quality = ingestion_service.extract_document(
            "inbound.pdf", PDF_BYTES
        )
        assert recomputed_quality["visual_profile"]["status"] == "ready"

        review_result = {"paragraphs": [dict(p) for p in recomputed_paragraphs]}
        source = {"kind": "pdf", "extraction_quality": recomputed_quality}
        payload = source_fidelity_payload(review_result, source=source)
        assert payload["source_type"] == "pdf"
        assert payload["capabilities"]["pdf_visual_profile"] is True

    @requires_pypdf
    def test_deferred_profile_source_fidelity_fails_open(self):
        # Even BEFORE the on-demand recompute, source_fidelity tolerates the deferred
        # profile: it treats it as 'not-yet-computed' (a limitation to surface), never as
        # an error and never as 'no visual signals present' -- the fail-open posture.
        from nda_automation.source_fidelity import source_fidelity_payload

        _pt, poll_paragraphs, poll_quality = ingestion_service.extract_document(
            "inbound.pdf", PDF_BYTES, include_visual_profile=False
        )
        review_result = {"paragraphs": [dict(p) for p in poll_paragraphs]}
        source = {"kind": "pdf", "extraction_quality": poll_quality}

        payload = source_fidelity_payload(review_result, source=source)

        assert payload["source_type"] == "pdf"
        limitation_codes = {item["code"] for item in payload["limitations"]}
        assert "pdf_visual_profile_unavailable" in limitation_codes


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
