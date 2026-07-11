"""State-interaction guard: a FAILED /reviewed-pdf must return an error STATUS with
a JSON body -- never a .pdf-typed (download) error body.

The reviewed-pdf handler forwards the upstream ``PdfExportError``'s headers onto the
JSON error response. ``_send_json`` sets ``Content-Type: application/json`` up front
and then appends forwarded headers verbatim (no de-dup), so a stray body-describing
header (``Content-Type: application/pdf`` / ``Content-Disposition: attachment;
filename="...pdf"``) would double-emit and mislabel the error -- the client would save
the JSON error blob AS a .pdf. These tests prove the handler strips body-content
headers from the forwarded set while preserving transport hints (Retry-After).
"""
from __future__ import annotations

import pytest

from nda_automation import approval, matter_store, pdf_export_service
from nda_automation.routes import approval as approval_routes

# Reuse the DOCX fixture + paragraphs from the sibling suite.
from tests.test_reviewed_docx_changes_mode import NDA_PARAGRAPHS, _docx


class _HeaderCapturingHandler:
    """Route double that records the status/headers passed to _send_json / downloads."""

    def __init__(self, *, current_user_id="owner-1", path=""):
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id} if current_user_id else None
        self.path = path
        self.headers = {}
        self.status = None
        self.json = None
        self.json_headers = None
        self.download = None
        self.download_headers = None

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload
        self.json_headers = headers or {}

    def _send_download_file(self, path, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = {"path": path, "filename": filename, "content_type": content_type}
        self.download_headers = headers or {}


class _StubExport:
    data = b"stub docx bytes"
    filename = "nda.docx"


class _StubReviewedDocx:
    export = _StubExport()


def _seed_approved_docx_matter():
    matter = matter_store.create_matter(
        source_filename="nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        extracted_text="\n\n".join(NDA_PARAGRAPHS),
        review_result={"clauses": []},
        triage={"triage_status": "ready_to_sign", "issue_count": 0},
        source_type="manual_upload",
        owner_user_id="owner-1",
    )
    matter_store.update_matter_fields(matter["id"], {"status": approval.MATTER_STATUS_APPROVED})
    return matter["id"]


def _drive_reviewed_pdf(matter_id):
    handler = _HeaderCapturingHandler(path=f"/api/matters/{matter_id}/reviewed-pdf")
    approval_routes.handle_matter_reviewed_pdf(handler, f"/api/matters/{matter_id}/reviewed-pdf")
    return handler


def test_json_safe_error_headers_strips_body_content_headers():
    """The helper drops any Content-Type / Content-Disposition (case-insensitive)
    while preserving transport hints like Retry-After."""
    poison = {
        "Content-Type": "application/pdf",
        "content-disposition": 'attachment; filename="evil.pdf"',
        "Retry-After": "3",
        "X-Whatever": "keep",
    }
    safe = approval_routes._json_safe_error_headers(poison)
    lower = {k.lower() for k in safe}
    assert "content-type" not in lower
    assert "content-disposition" not in lower
    assert safe["Retry-After"] == "3"
    assert safe["X-Whatever"] == "keep"


def test_json_safe_error_headers_handles_empty():
    assert approval_routes._json_safe_error_headers(None) == {}
    assert approval_routes._json_safe_error_headers({}) == {}


def test_reviewed_pdf_error_never_forwards_pdf_content_type(monkeypatch):
    """A PdfExportError carrying a body Content-Type/Disposition must NOT reach the
    JSON error response: the client must never be handed a .pdf-typed error blob."""
    matter_id = _seed_approved_docx_matter()

    # build_reviewed_docx is invoked first; return a cheap stub so the handler
    # proceeds to the (patched) PDF conversion step without doing real work.
    monkeypatch.setattr(
        approval_routes.matter_document_artifacts,
        "build_reviewed_docx",
        lambda *a, **k: _StubReviewedDocx(),
    )

    def _raise_with_poison_headers(*_a, **_k):
        raise pdf_export_service.PdfExportError(
            {"error": "DOCX to PDF export failed."},
            status=503,
            headers={
                # A malicious/buggy upstream that tries to type the error as a pdf
                # download. The handler must strip these.
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="nda-redlined.pdf"',
                # A legitimate transport hint that MUST survive.
                "Retry-After": "5",
            },
        )

    monkeypatch.setattr(
        approval_routes.pdf_export_service, "build_docx_pdf_export", _raise_with_poison_headers
    )

    handler = _drive_reviewed_pdf(matter_id)

    # Error STATUS, JSON body -- never a 200 download.
    assert handler.status == 503, handler.json
    assert handler.download is None
    assert "error" in (handler.json or {})

    # The forwarded headers carry NO body-describing header (so _send_json's own
    # application/json is the only Content-Type, and there is no attachment filename).
    forwarded_lower = {k.lower() for k in (handler.json_headers or {})}
    assert "content-type" not in forwarded_lower
    assert "content-disposition" not in forwarded_lower
    # ...but the legitimate transport hint is preserved.
    assert handler.json_headers.get("Retry-After") == "5"
