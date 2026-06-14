"""Send Document outbound flow.

A basic, practical way to email a document to a counterparty from the dashboard
without going through the NDA review pipeline. It REUSES the existing Gmail
outbound plumbing (``gmail_integration.send_redline_email``) and the matter board
'Sent' column — it does not introduce new send infrastructure. The uploaded
document is sent as-is; no review or redline is run.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from .. import gmail_integration, google_connection, matter_view, telemetry
from ..app_settings import gmail_role_enabled
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..matter_lifecycle import (
    MatterNotFoundError,
    RepositoryMatterLifecycle,
)
from ..matter_repository import DiskMatterRepository
from .common import request_owner_user_id
from .gmail import (
    clean_outbound_body,
    clean_outbound_recipient,
    clean_outbound_subject,
    gmail_send_error_status,
)

# send_redline_email attaches the document as a Word document, so the outbound
# Send Document flow accepts .docx only for now (kept deliberately simple).
SEND_DOCUMENT_EXTENSION = ".docx"


def handle_send_document(handler) -> None:
    telemetry.increment("send_document_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    if not is_supported_send_filename(filename):
        handler._send_json({"error": "Attach a .docx Word document to send."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Attach a document to send."}, status=400)
        return

    recipient = clean_outbound_recipient(payload.get("to"))
    if not recipient:
        handler._send_json({"error": "Enter a valid recipient email address."}, status=400)
        return

    if not gmail_role_enabled("outbound"):
        handler._send_json({"error": "Gmail outbound is disabled in Admin."}, status=409)
        return

    try:
        document_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": "The attached document could not be decoded."}, status=400)
        return
    if not document_bytes:
        handler._send_json({"error": "Attach a document to send."}, status=400)
        return

    try:
        ensure_document_size(document_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    subject = clean_outbound_subject(payload.get("subject")) or _default_subject(filename)
    body = clean_outbound_body(payload.get("body"))
    owner_user_id = request_owner_user_id(handler)
    gmail_token_owner_user_id = google_connection.connected_owner_user_id(
        getattr(handler, "current_user", None),
        owner_user_id=owner_user_id,
    )

    try:
        sent_document = RepositoryMatterLifecycle(DiskMatterRepository()).send_document(
            filename=filename,
            document_bytes=document_bytes,
            recipient=recipient,
            subject=subject,
            body=body,
            owner_user_id=owner_user_id,
            token_owner_user_id=gmail_token_owner_user_id,
        )
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=gmail_send_error_status(error))
        return
    except MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404)
        return

    telemetry.increment("send_document_sent")
    handler._send_json(
        {
            "filename": filename,
            "matter": matter_view.public_matter(sent_document.matter),
            "sent": sent_document.sent,
        },
        status=201,
    )


def is_supported_send_filename(filename: object) -> bool:
    return isinstance(filename, str) and filename.lower().endswith(SEND_DOCUMENT_EXTENSION)


def _default_subject(filename: str) -> str:
    return Path(str(filename or "")).stem or "Document"
