from __future__ import annotations

from .. import app_settings, gmail_integration, matter_store, matter_view, redline_export_service, telemetry
from ..docx_export import DocxExportError
from ..docx_text import DocxExtractionError

MAX_OUTBOUND_SUBJECT_CHARS = 240
MAX_OUTBOUND_BODY_CHARS = 10_000


def handle_gmail_status(handler, *, send_body: bool = True) -> None:
    try:
        handler._send_json({"gmail": gmail_integration.gmail_status()}, send_body=send_body)
    except app_settings.AppSettingsError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)


def handle_gmail_import(handler) -> None:
    handler._send_json({"error": "Manual Gmail sync is disabled. Use Admin sync frequency."}, status=410)


def handle_gmail_settings_update(handler) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    updates: dict[str, object] = {}
    for key in ("inbound_enabled", "outbound_enabled"):
        if key not in payload:
            continue
        value = payload.get(key)
        if not isinstance(value, bool):
            handler._send_json({"error": "Gmail enabled settings must be true or false."}, status=400)
            return
        updates[key] = value
    if "sync_cadence" in payload:
        handler._send_json({"error": "Use sync_frequency for Gmail sync frequency."}, status=400)
        return
    if "sync_frequency" in payload:
        sync_frequency = payload.get("sync_frequency")
        if not isinstance(sync_frequency, str) or sync_frequency not in app_settings.GMAIL_SYNC_FREQUENCIES:
            handler._send_json({"error": "Unsupported Gmail sync frequency."}, status=400)
            return
        updates["sync_frequency"] = sync_frequency
    if not updates:
        handler._send_json({"error": "Provide a Gmail setting to update."}, status=400)
        return

    settings = app_settings.update_gmail_settings(updates)
    handler._send_json({"gmail_settings": settings, "gmail": gmail_integration.gmail_status()})


def handle_gmail_send_redline(handler) -> None:
    telemetry.increment("gmail_send_redline_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    matter_id = payload.get("matter_id")
    if not isinstance(matter_id, str) or not matter_id.strip():
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if payload.get("confirm_send") is not True:
        handler._send_json({"error": "Confirm send is required before emailing a redline."}, status=400)
        return

    matter = matter_store.get_matter(matter_id.strip())
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if not gmail_integration.matter_reply_recipient(matter):
        handler._send_json({"error": "Matter does not have a valid reply recipient email address."}, status=400)
        return
    if matter_view.matter_needs_human_review(matter):
        handler._send_json({"error": "Matter needs human review before a redline can be sent."}, status=409)
        return
    if not app_settings.gmail_role_enabled("outbound"):
        handler._send_json({"error": "Gmail outbound is disabled in Admin."}, status=409)
        return
    outbound_subject = clean_outbound_subject(payload.get("subject"))
    outbound_body = clean_outbound_body(payload.get("body"))

    try:
        gmail_integration.validate_outbound_send_ready(matter)
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=gmail_send_error_status(error))
        return

    try:
        redline_export = redline_export_service.build_matter_redline(matter_id.strip(), payload)
    except redline_export_service.DocxOpenHealthError as error:
        handler._send_json({"error": str(error), "details": error.details}, status=500)
        return
    except redline_export_service.MatterSourceTextChangedError as error:
        handler._send_json({"error": str(error)}, status=409)
        return
    except DocxExtractionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except DocxExportError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    try:
        sent = gmail_integration.send_redline_email(
            matter,
            redline_export.data,
            redline_export.filename,
            body=outbound_body,
            subject=outbound_subject,
        )
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=gmail_send_error_status(error))
        return

    updated_matter = matter_store.update_matter_fields(
        matter_id.strip(),
        {
            "board_column": "redline_ready",
            "last_outbound_account": sent.get("outbound_account", ""),
            "last_outbound_at": sent.get("sent_at", ""),
            "last_outbound_filename": redline_export.filename,
            "last_outbound_message_id": sent.get("message_id", ""),
            "last_outbound_subject": sent.get("subject", ""),
            "last_outbound_thread_id": sent.get("thread_id", ""),
            "last_outbound_to": sent.get("to", ""),
            "status": "active",
        },
    )
    if updated_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({
        "filename": redline_export.filename,
        "matter": matter_view.public_matter(updated_matter),
        "sent": sent,
    })


def clean_outbound_subject(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_OUTBOUND_SUBJECT_CHARS]


def clean_outbound_body(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return None
    return cleaned[:MAX_OUTBOUND_BODY_CHARS]


def gmail_send_error_status(error: Exception) -> int:
    message = str(error).lower()
    if "valid reply recipient" in message:
        return 400
    conflict_markers = (
        "disabled in admin",
        "does not match inbound gmail account",
        "outbound gmail account mismatch",
        "self-sent gmail message",
        "gmail outbound profile",
        "set nda_gmail_outbound_token_path",
        "gmail outbound token",
    )
    if any(marker in message for marker in conflict_markers):
        return 409
    return 503
