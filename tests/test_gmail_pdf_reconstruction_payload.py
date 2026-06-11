from __future__ import annotations

from unittest.mock import patch

from nda_automation import pdf_docx_reconstruction, redline_export_service
from nda_automation.routes import gmail as gmail_routes


class FakeHandler:
    def __init__(self, body: dict):
        self._body = body
        self.current_user = {"id": "user-1", "email": "user@example.com"}
        self.current_user_id = "user-1"
        self.status = None
        self.json = None

    def _read_json_payload(self):
        return self._body

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload


class RaisingLifecycle:
    def __init__(self, error: Exception):
        self.error = error

    def send_redline(self, *args, **kwargs):
        raise self.error


def test_gmail_send_redline_returns_structured_pdf_reconstruction_unavailable_payload():
    error = redline_export_service.PdfSourceRedlineUnavailableError(
        pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE,
        source_filename="Signed NDA.pdf",
    )
    handler = FakeHandler(
        {
            "matter_id": "matter-pdf",
            "confirm_send": True,
            "to": "counterparty@example.com",
            "confirm_recipient": "counterparty@example.com",
            "subject": "Reviewed NDA",
            "body": "Please see attached.",
        }
    )

    with patch.object(gmail_routes, "gmail_owner_user_id", return_value="user-1"):
        with patch.object(gmail_routes, "RepositoryMatterLifecycle", return_value=RaisingLifecycle(error)):
            gmail_routes.handle_gmail_send_redline(handler)

    assert handler.status == 503
    assert handler.json == error.payload
    reconstruction = handler.json["pdf_docx_reconstruction"]
    assert reconstruction["status"] == "unavailable"
    assert reconstruction["filename"] == "Signed-NDA.docx"
    assert reconstruction["converter"]["mode"] == "pdf_to_docx_reconstruction"
    assert reconstruction["fidelity"]["output"] == "reviewed_docx"
