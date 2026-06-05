"""Tests for the Send Document outbound flow.

Drives nda_automation.routes.send_document.handle_send_document directly with a
fake handler, a temp-dir matter store, and a mocked Gmail send — so the test
exercises validation, the Sent-column matter creation, and the reuse of the
existing send plumbing without a live Gmail connection.
"""

from __future__ import annotations

import base64
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from nda_automation import gmail_integration, matter_store
from nda_automation.routes import send_document as send_document_routes


def _make_docx(text: str = "Hello document.") -> bytes:
    # Minimal valid .docx (zip with the document part) so the size guard and the
    # base64 round-trip behave like a real upload.
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


_SENT_STUB = {
    "message_id": "msg_123",
    "outbound_account": "legal@aspora.com",
    "sent_at": "2026-06-05T12:00:00+00:00",
    "subject": "Engagement Letter",
    "thread_id": "thread_123",
    "to": "counterparty@example.com",
}


class _FakeHandler:
    current_user_id = ""
    current_user = None

    def __init__(self, payload):
        self._payload = payload
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload


class SendDocumentTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        data_path = Path(self._dir.name)
        for path_patch in (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        ):
            path_patch.start()
            self.addCleanup(path_patch.stop)

    def _payload(self, **overrides):
        payload = {
            "filename": "Engagement Letter.docx",
            "content_base64": base64.b64encode(_make_docx()).decode("ascii"),
            "to": "counterparty@example.com",
            "subject": "Engagement Letter",
            "body": "Please find the attached document.",
        }
        payload.update(overrides)
        return payload

    def _send(self, payload):
        handler = _FakeHandler(payload)
        with (
            patch.object(send_document_routes, "gmail_role_enabled", return_value=True),
            patch.object(gmail_integration, "send_redline_email", return_value=dict(_SENT_STUB)) as send_email,
        ):
            send_document_routes.handle_send_document(handler)
        return handler, send_email

    # --- happy path -------------------------------------------------------

    def test_send_document_creates_sent_matter_and_sends_email(self):
        handler, send_email = self._send(self._payload())

        self.assertEqual(handler.status, 201, handler.response)
        send_email.assert_called_once()
        # The uploaded bytes + filename + recipient flow into the Gmail send.
        _args, kwargs = send_email.call_args
        self.assertEqual(kwargs.get("to"), "counterparty@example.com")
        self.assertEqual(kwargs.get("subject"), "Engagement Letter")
        self.assertEqual(kwargs.get("body"), "Please find the attached document.")

        matter = handler.response["matter"]
        self.assertEqual(matter["board_column"], "sent")
        self.assertEqual(matter["source_type"], "send_document")
        self.assertEqual(handler.response["sent"]["message_id"], "msg_123")
        self.assertEqual(handler.response["filename"], "Engagement Letter.docx")

        # The matter is persisted in the Sent column.
        stored = matter_store.list_matters("")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["board_column"], "sent")
        self.assertEqual(stored[0]["last_outbound_message_id"], "msg_123")
        self.assertEqual(stored[0]["last_outbound_to"], "counterparty@example.com")

    def test_send_document_defaults_subject_to_filename_stem(self):
        handler, send_email = self._send(self._payload(subject=""))

        self.assertEqual(handler.status, 201, handler.response)
        _args, kwargs = send_email.call_args
        self.assertEqual(kwargs.get("subject"), "Engagement Letter")

    # --- validation -------------------------------------------------------

    def test_send_document_rejects_non_docx_filename(self):
        handler, send_email = self._send(self._payload(filename="contract.pdf"))

        self.assertEqual(handler.status, 400)
        self.assertIn(".docx", handler.response["error"])
        send_email.assert_not_called()
        self.assertEqual(matter_store.list_matters(""), [])

    def test_send_document_rejects_missing_content(self):
        handler, send_email = self._send(self._payload(content_base64=""))

        self.assertEqual(handler.status, 400)
        send_email.assert_not_called()
        self.assertEqual(matter_store.list_matters(""), [])

    def test_send_document_rejects_invalid_recipient(self):
        handler, send_email = self._send(self._payload(to="not-an-email"))

        self.assertEqual(handler.status, 400)
        self.assertIn("recipient", handler.response["error"].lower())
        send_email.assert_not_called()
        self.assertEqual(matter_store.list_matters(""), [])

    def test_send_document_blocks_when_outbound_disabled(self):
        handler = _FakeHandler(self._payload())
        with (
            patch.object(send_document_routes, "gmail_role_enabled", return_value=False),
            patch.object(gmail_integration, "send_redline_email") as send_email,
        ):
            send_document_routes.handle_send_document(handler)

        self.assertEqual(handler.status, 409)
        self.assertIn("disabled", handler.response["error"].lower())
        send_email.assert_not_called()
        self.assertEqual(matter_store.list_matters(""), [])

    # --- failed send leaves no phantom card -------------------------------

    def test_failed_send_does_not_persist_a_matter(self):
        handler = _FakeHandler(self._payload())
        with (
            patch.object(send_document_routes, "gmail_role_enabled", return_value=True),
            patch.object(
                gmail_integration,
                "send_redline_email",
                side_effect=gmail_integration.GmailIntegrationError("Gmail outbound send failed."),
            ),
        ):
            send_document_routes.handle_send_document(handler)

        self.assertGreaterEqual(handler.status, 400)
        self.assertIn("error", handler.response)
        # No phantom card in the Sent column when the send fails.
        self.assertEqual(matter_store.list_matters(""), [])


if __name__ == "__main__":
    unittest.main()
