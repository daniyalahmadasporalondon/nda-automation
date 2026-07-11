"""D5 route guard: a matter's reviewed PDF must stay downloadable once approved AND
after it is EXECUTED / fully_signed.

Before the fix ``handle_matter_reviewed_pdf`` gated strictly on ``status ==
"approved"`` and returned 409 for an executed/fully_signed matter, so the reviewer
could no longer download the reviewed PDF of a signed NDA (and the download menu
falsely read "available after approved"). Executing is a strictly later,
approval-presupposing lifecycle state, so the reviewed artifact must remain
downloadable. The gate must NOT widen to an unreviewed, un-approved matter.
"""
from __future__ import annotations

from nda_automation import approval, matter_store, pdf_export_service
from nda_automation.routes import approval as approval_routes

# Reuse the DOCX fixture + paragraphs + route double from the sibling suites.
from tests.test_reviewed_docx_changes_mode import NDA_PARAGRAPHS, _docx
from tests.test_reviewed_pdf_error_content_type import (
    _HeaderCapturingHandler,
    _drive_reviewed_pdf,
)


class _StubExport:
    data = b"stub reviewed docx bytes"
    filename = "nda-redlined.docx"


class _StubReviewedDocx:
    export = _StubExport()
    payload = {"export_redline_edits": []}
    artifact = None


def _seed_docx_matter_with_review():
    matter = matter_store.create_matter(
        source_filename="nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        extracted_text="\n\n".join(NDA_PARAGRAPHS),
        review_result={"clauses": []},
        triage={"triage_status": "ready_to_sign", "issue_count": 0},
        source_type="manual_upload",
        owner_user_id="owner-1",
    )
    return matter["id"]


def _stub_reviewed_pdf_pipeline(monkeypatch, tmp_path):
    """Patch the two heavy builders so the route reaches its download branch cheaply."""
    monkeypatch.setattr(
        approval_routes.matter_document_artifacts,
        "build_reviewed_docx",
        lambda *a, **k: _StubReviewedDocx(),
    )
    pdf_path = tmp_path / "reviewed.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nreviewed\n%%EOF\n")

    def _fake_build_docx_pdf_export(_data, _filename, *, owner_user_id=""):
        return pdf_export_service.MatterPdfExport(
            path=pdf_path,
            filename="nda-redlined.pdf",
            content_type=pdf_export_service.PDF_EXPORT_MIME,
            headers={"X-PDF-Export-Verified": pdf_export_service.PDF_EXPORT_VERIFICATION_HEADER},
        )

    monkeypatch.setattr(
        approval_routes.pdf_export_service, "build_docx_pdf_export", _fake_build_docx_pdf_export
    )


def test_reviewed_pdf_available_for_executed_matter(monkeypatch, tmp_path):
    matter_id = _seed_docx_matter_with_review()
    # EXECUTED / fully_signed (NOT status="approved"): the DocuSign-completion triad.
    matter_store.update_matter_fields(
        matter_id,
        {"status": "fully_signed", "executed": True, "executed_at": "2026-07-11T00:00:00+00:00"},
    )
    _stub_reviewed_pdf_pipeline(monkeypatch, tmp_path)

    handler = _drive_reviewed_pdf(matter_id)

    # Downloadable (200), NOT the old 409 -- the signed matter keeps its reviewed PDF.
    assert handler.status == 200, handler.json
    assert handler.download is not None
    assert handler.download["filename"] == "nda-redlined.pdf"
    assert handler.download["content_type"] == pdf_export_service.PDF_EXPORT_MIME


def test_reviewed_pdf_available_for_approved_matter(monkeypatch, tmp_path):
    # Regression: the canonical approved state must remain downloadable after the fix.
    matter_id = _seed_docx_matter_with_review()
    matter_store.update_matter_fields(matter_id, {"status": approval.MATTER_STATUS_APPROVED})
    _stub_reviewed_pdf_pipeline(monkeypatch, tmp_path)

    handler = _drive_reviewed_pdf(matter_id)

    assert handler.status == 200, handler.json
    assert handler.download is not None


def test_reviewed_pdf_blocked_for_unapproved_matter(monkeypatch, tmp_path):
    # A reviewed-but-unapproved matter (has a review_result but never approved/executed)
    # must still 409 -- the gate is extended PAST approval, never before it.
    matter_id = _seed_docx_matter_with_review()
    matter_store.update_matter_fields(matter_id, {"status": "awaiting_human"})

    def _boom(*_a, **_k):
        raise AssertionError("build must not run for an unapproved matter")

    monkeypatch.setattr(
        approval_routes.matter_document_artifacts, "build_reviewed_docx", _boom
    )

    handler = _drive_reviewed_pdf(matter_id)

    assert handler.status == 409, handler.json
    assert handler.download is None
    assert "approved" in (handler.json or {}).get("error", "")
