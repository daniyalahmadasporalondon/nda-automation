"""Ticket 1 + the G2 regression for the reviewed-docx pipeline.

* ``GET /api/matters/<id>/reviewed-docx?changes=tracked|accepted`` -- tracked keeps
  the w:ins/w:del revision markup; accepted flattens it (accept-all-revisions);
  owner-scoped 404; bad changes value -> 400.
* G2 (Approach C): a PDF-source matter + a redline serves a NON-EMPTY reviewed/working
  DOCX whose redline markup is present (anchored by index into the working DOCX),
  rather than the old 0/29 silently-dropped-redline class.
"""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest
from docx import Document

from nda_automation import (
    approval,
    artifact_registry,
    artifact_service,
    matter_store,
)
from nda_automation.review_engine import review_nda_with_active_engine
from nda_automation.routes import approval as approval_routes
from nda_automation.triage import triage_review_result

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of the State of Mars.",
    "The Receiving Party shall not solicit any employees of the Disclosing Party.",
]


def _docx(paragraphs) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _has_tracked_markup(docx_bytes: bytes) -> bool:
    with ZipFile(BytesIO(docx_bytes)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    return "w:ins" in xml or "w:del" in xml


class _FakeHandler:
    def __init__(self, *, current_user_id, path=""):
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id} if current_user_id else None
        self.path = path
        self.status = None
        self.json = None
        self.download = None
        self.download_headers = None

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload

    def _send_download(self, data, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = {"data": data, "filename": filename, "content_type": content_type}
        self.download_headers = headers or {}


def _seed_approved_matter_with_redline(*, source_docx=True, working_docx=False):
    """Create an APPROVED matter whose flagged clause has an accepted redline.

    DOCX source: the source DOCX itself carries the redline's anchor paragraph.
    PDF source (+working_docx): the matter is a PDF whose reconstructed working DOCX
    carries the anchor paragraph and whose review paragraph is re-keyed by index.
    """
    review_result = review_nda_with_active_engine("\n\n".join(NDA_PARAGRAPHS))
    flagged = [
        str(clause.get("id"))
        for clause in review_result.get("clauses", [])
        if clause.get("decision") in ("fail", "review")
    ]
    target_clause = flagged[0]
    review_result["redline_edits"] = [
        {
            "id": "seeded-redline",
            "clause_id": target_clause,
            "paragraph_id": "p3",
            "source_index": 3,
            "action": "replace_paragraph",
            "original_text": NDA_PARAGRAPHS[2],
            "replacement_text": NDA_PARAGRAPHS[2] + " (amended)",
        }
    ]
    document_bytes = _docx(NDA_PARAGRAPHS) if source_docx else b"%PDF-1.7\nsource pdf\n%%EOF\n"
    matter = matter_store.create_matter(
        source_filename="mutual-nda.docx" if source_docx else "mutual-nda.pdf",
        document_bytes=document_bytes,
        extracted_text="\n\n".join(NDA_PARAGRAPHS),
        review_result=review_result,
        triage=triage_review_result(review_result),
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
    )
    matter_id = matter["id"]
    if working_docx:
        # Approach C: the reconstructed working DOCX carries the body the redline
        # anchors into (source_index 3 == NDA_PARAGRAPHS[2]).
        artifact_service.register_working_docx(
            matter_id, _docx(NDA_PARAGRAPHS), owner_user_id="owner-1"
        )
    # Accept the flagged clause so its redline is exported, then approve.
    matter_store.set_clause_reviewer_decision(
        matter_id,
        target_clause,
        approval.normalize_reviewer_decision({"action": "accept"}, actor="reviewer"),
    )
    matter_store.update_matter_fields(matter_id, {"status": approval.MATTER_STATUS_APPROVED})
    return matter_id


# --------------------------------------------------------------------------- #
# Ticket 1: changes=tracked|accepted
# --------------------------------------------------------------------------- #
def test_reviewed_docx_tracked_keeps_revision_markup():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    handler = _FakeHandler(
        current_user_id="owner-1",
        path=f"/api/matters/{matter_id}/reviewed-docx?changes=tracked",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 200
    assert handler.download["content_type"].startswith("application/vnd.openxmlformats")
    assert _has_tracked_markup(handler.download["data"]) is True
    assert handler.download_headers.get("X-Reviewed-Changes") == "tracked"


def test_reviewed_docx_accepted_flattens_revision_markup():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    handler = _FakeHandler(
        current_user_id="owner-1",
        path=f"/api/matters/{matter_id}/reviewed-docx?changes=accepted",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 200
    # Accepted: the tracked w:ins/w:del are flattened away.
    assert _has_tracked_markup(handler.download["data"]) is False
    assert handler.download_headers.get("X-Reviewed-Changes") == "accepted"
    assert handler.download["filename"].endswith("-accepted.docx")


def test_reviewed_docx_defaults_to_tracked_when_no_query():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    handler = _FakeHandler(current_user_id="owner-1", path=f"/api/matters/{matter_id}/reviewed-docx")
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 200
    assert _has_tracked_markup(handler.download["data"]) is True
    assert handler.download_headers.get("X-Reviewed-Changes") == "tracked"


def test_reviewed_docx_rejects_unknown_changes_value():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    handler = _FakeHandler(
        current_user_id="owner-1",
        path=f"/api/matters/{matter_id}/reviewed-docx?changes=bogus",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 400
    assert handler.download is None


def test_reviewed_docx_owner_scoped_404_on_mismatch():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    handler = _FakeHandler(
        current_user_id="attacker@example.com",
        path=f"/api/matters/{matter_id}/reviewed-docx?changes=tracked",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 404
    assert handler.download is None


def test_reviewed_docx_409_when_not_approved():
    review_result = review_nda_with_active_engine("\n\n".join(NDA_PARAGRAPHS))
    matter = matter_store.create_matter(
        source_filename="mutual-nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        extracted_text="\n\n".join(NDA_PARAGRAPHS),
        review_result=review_result,
        triage=triage_review_result(review_result),
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="owner-1",
    )
    matter_store.update_matter_fields(matter["id"], {"status": "in_review"})
    handler = _FakeHandler(
        current_user_id="owner-1",
        path=f"/api/matters/{matter['id']}/reviewed-docx",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter['id']}/reviewed-docx")
    assert handler.status == 409
    assert handler.download is None


# --------------------------------------------------------------------------- #
# G2 regression: PDF-source matter + redline -> anchored, non-empty reviewed DOCX
# --------------------------------------------------------------------------- #
def test_g2_pdf_source_with_working_docx_serves_anchored_reviewed_docx():
    matter_id = _seed_approved_matter_with_redline(source_docx=False, working_docx=True)
    # Sanity: the matter IS a PDF source that carries a working DOCX artifact.
    stored = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    assert stored["source_filename"].endswith(".pdf")
    assert artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING) is not None

    handler = _FakeHandler(
        current_user_id="owner-1",
        path=f"/api/matters/{matter_id}/reviewed-docx?changes=tracked",
    )
    approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
    assert handler.status == 200, handler.json
    data = handler.download["data"]
    # NON-EMPTY and a real DOCX (the redline did not silently vanish: the 0/29 class).
    assert data and data[:2] == b"PK"
    # The redline markup is PRESENT -- the accepted change anchored into the working
    # DOCX body by index rather than being dropped.
    assert _has_tracked_markup(data) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
