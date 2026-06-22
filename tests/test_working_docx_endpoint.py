"""The /working-docx endpoint + the working_docx_ready render-status flag (Approach C).

SEAM CONTRACT (frozen): GET /api/matters/<id>/working-docx -> 200 canonical DOCX
bytes (wordprocessingml.document), owner-scoped, 404 until ready; render-status gains
a boolean working_docx_ready.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document

from nda_automation import artifact_service, matter_render_job
from nda_automation.routes import matters as matter_routes
from nda_automation.matter_repository import InMemoryMatterRepository

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_BYTES = b"%PDF-1.7\nfake\n%%EOF\n"


def make_docx(text="Working DOCX body") -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _FakeHandler:
    def __init__(self, repo, *, current_user_id, path=""):
        self.matter_repository = repo
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


def _seed_pdf_matter(repo, *, owner_user_id, with_working=True):
    matter = repo.create_matter(
        source_filename="inbound.pdf",
        document_bytes=PDF_BYTES,
        extracted_text="Some text",
        review_result=None,
        triage={},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner_user_id,
    )
    if with_working:
        artifact_service.register_working_docx(
            matter["id"], make_docx(), repository=repo, owner_user_id=owner_user_id
        )
    return matter["id"]


# --------------------------------------------------------------------------- #
# Endpoint: GET /working-docx
# --------------------------------------------------------------------------- #
def test_working_docx_serves_canonical_docx_bytes():
    repo = InMemoryMatterRepository()
    matter_id = _seed_pdf_matter(repo, owner_user_id="owner-1")
    handler = _FakeHandler(repo, current_user_id="owner-1")
    matter_routes.handle_matter_working_docx(handler, f"/api/matters/{matter_id}/working-docx")
    assert handler.status == 200
    assert handler.download is not None
    assert handler.download["content_type"] == DOCX_MIME
    assert handler.download["data"][:2] == b"PK"  # real zip/DOCX
    assert handler.download["filename"].endswith(".docx")


def test_working_docx_404_until_ready():
    repo = InMemoryMatterRepository()
    matter_id = _seed_pdf_matter(repo, owner_user_id="owner-1", with_working=False)
    handler = _FakeHandler(repo, current_user_id="owner-1")
    matter_routes.handle_matter_working_docx(handler, f"/api/matters/{matter_id}/working-docx")
    assert handler.status == 404
    assert handler.download is None


def test_working_docx_owner_scoped_404_on_mismatch():
    repo = InMemoryMatterRepository()
    matter_id = _seed_pdf_matter(repo, owner_user_id="owner-1")
    # A different authenticated user must NOT receive the document (fail-closed).
    handler = _FakeHandler(repo, current_user_id="attacker@example.com")
    matter_routes.handle_matter_working_docx(handler, f"/api/matters/{matter_id}/working-docx")
    assert handler.status == 404
    assert handler.download is None


def test_working_docx_unknown_matter_404():
    repo = InMemoryMatterRepository()
    handler = _FakeHandler(repo, current_user_id="owner-1")
    matter_routes.handle_matter_working_docx(handler, "/api/matters/does-not-exist/working-docx")
    assert handler.status == 404
    assert handler.download is None


# --------------------------------------------------------------------------- #
# render-status: working_docx_ready
# --------------------------------------------------------------------------- #
def test_matter_has_working_docx_reflects_artifact_presence():
    repo = InMemoryMatterRepository()
    matter_id = _seed_pdf_matter(repo, owner_user_id="owner-1", with_working=True)
    stored = repo.get_matter(matter_id, owner_user_id="owner-1")
    assert matter_render_job.matter_has_working_docx(stored) is True

    repo2 = InMemoryMatterRepository()
    matter_id2 = _seed_pdf_matter(repo2, owner_user_id="owner-1", with_working=False)
    stored2 = repo2.get_matter(matter_id2, owner_user_id="owner-1")
    assert matter_render_job.matter_has_working_docx(stored2) is False
    assert matter_render_job.matter_has_working_docx(None) is False


def test_render_status_payload_includes_working_docx_ready():
    repo = InMemoryMatterRepository()
    matter_id = _seed_pdf_matter(repo, owner_user_id="owner-1", with_working=True)
    payload = matter_render_job.render_status_payload(
        matter_id, owner_user_id="owner-1", poll_grace_seconds=0.0, repository=repo
    )
    assert "document_render" in payload
    assert payload["document_render"]["working_docx_ready"] is True

    repo2 = InMemoryMatterRepository()
    matter_id2 = _seed_pdf_matter(repo2, owner_user_id="owner-1", with_working=False)
    payload2 = matter_render_job.render_status_payload(
        matter_id2, owner_user_id="owner-1", poll_grace_seconds=0.0, repository=repo2
    )
    assert payload2["document_render"]["working_docx_ready"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
