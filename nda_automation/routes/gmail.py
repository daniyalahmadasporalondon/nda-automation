from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .. import app_settings, gmail_integration, matter_store, matter_view, redline_export_service, telemetry
from ..docx_export import DocxExportError
from ..docx_text import DocxExtractionError
from .common import request_owner_user_id
from .. import user_store

MAX_OUTBOUND_SUBJECT_CHARS = 240
MAX_OUTBOUND_BODY_CHARS = 10_000


def handle_gmail_status(handler, *, send_body: bool = True) -> None:
    try:
        handler._send_json(
            {"gmail": gmail_integration.gmail_status(owner_user_id=gmail_owner_user_id(handler))},
            send_body=send_body,
        )
    except app_settings.AppSettingsError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)


def handle_gmail_connect_start(handler, *, send_body: bool = True) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before connecting Gmail."}, status=403, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    role = str(query.get("role", ["all"])[0] or "all").strip().lower()
    next_path = query.get("next", ["/"])[0]
    try:
        state = user_store.create_oauth_state(
            purpose="gmail",
            user_id=owner_user_id,
            next_path=next_path,
            metadata={"role": role},
        )
        authorization_url = gmail_integration.build_gmail_authorization_url(
            redirect_uri=_gmail_redirect_uri(handler),
            role=role,
            state=state,
        )
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    handler._send_redirect(authorization_url, send_body=send_body)


def handle_gmail_connect_callback(handler, *, send_body: bool = True) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before connecting Gmail."}, status=403, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    if query.get("error"):
        handler._send_json({"error": "Gmail connection was not completed."}, status=400, send_body=send_body)
        return
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    state_record = user_store.consume_oauth_state(state, purpose="gmail", user_id=owner_user_id)
    if not code or state_record is None:
        handler._send_json({"error": "Gmail connection state is invalid or expired."}, status=400, send_body=send_body)
        return
    role = str((state_record.get("metadata") or {}).get("role") or "all")
    try:
        token_response = gmail_integration.exchange_gmail_oauth_code(code, redirect_uri=_gmail_redirect_uri(handler))
        connected_roles = gmail_integration.save_user_gmail_oauth_token(
            owner_user_id,
            token_response,
            role=role,
        )
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return
    next_path = str(state_record.get("next_path") or "/")
    handler._send_redirect(
        next_path,
        headers={"X-Gmail-Connected-Roles": ",".join(connected_roles)},
        send_body=send_body,
    )


def handle_gmail_disconnect(handler) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before disconnecting Gmail."}, status=403)
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    role = str(payload.get("role") or "all").strip().lower()
    try:
        removed = gmail_integration.disconnect_user_gmail(owner_user_id, role=role)
        status = gmail_integration.gmail_status(owner_user_id=owner_user_id)
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    handler._send_json({"disconnected": removed, "gmail": status})


def handle_gmail_import(handler) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Manual Gmail sync is disabled. Use Admin sync frequency."}, status=410)
        return
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        result = gmail_integration.import_inbound_matters(
            limit=_manual_import_limit(payload.get("limit")),
            query=payload.get("query") if isinstance(payload.get("query"), str) else None,
            owner_user_id=owner_user_id,
        )
        result = {
            **result,
            "deduplicated_count": matter_store.deduplicate_gmail_matters(owner_user_id=owner_user_id),
        }
    except gmail_integration.GmailRateLimitError as error:
        handler._send_json({"error": str(error)}, status=429)
        return
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=502)
        return

    finished_at = datetime.now(timezone.utc).isoformat()
    app_settings.record_gmail_sync(result, synced_at=finished_at, started_at=finished_at, finished_at=finished_at)
    handler._send_json({
        "gmail": gmail_integration.gmail_status(owner_user_id=owner_user_id),
        "result": result,
    })


def _manual_import_limit(value: object) -> int:
    try:
        return int(value or gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
    except (TypeError, ValueError):
        return gmail_integration.MAX_GMAIL_IMPORT_LIMIT


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
    if "inbound_search_terms" in payload:
        inbound_search_terms = app_settings.gmail_search_terms_from_payload(
            payload.get("inbound_search_terms"),
            fallback=[],
        )
        if not inbound_search_terms:
            handler._send_json({"error": "Provide at least one Gmail inbound search term."}, status=400)
            return
        updates["inbound_search_terms"] = inbound_search_terms
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

    owner_user_id = request_owner_user_id(handler)
    gmail_token_owner_user_id = gmail_owner_user_id(handler)
    matter = matter_store.get_matter(matter_id.strip(), owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    outbound_to = clean_outbound_recipient(payload.get("to"))
    if not outbound_to and not gmail_integration.matter_reply_recipient(matter):
        handler._send_json({"error": "Matter does not have a valid reply recipient email address."}, status=400)
        return
    if matter_blocks_redline_send(matter):
        handler._send_json({"error": "Matter needs human review before a redline can be sent."}, status=409)
        return
    if not app_settings.gmail_role_enabled("outbound"):
        handler._send_json({"error": "Gmail outbound is disabled in Admin."}, status=409)
        return
    outbound_subject = clean_outbound_subject(payload.get("subject"))
    outbound_body = clean_outbound_body(payload.get("body"))

    try:
        if gmail_token_owner_user_id:
            gmail_integration.validate_outbound_send_ready(
                matter,
                to=outbound_to,
                owner_user_id=gmail_token_owner_user_id,
            )
        else:
            gmail_integration.validate_outbound_send_ready(matter, to=outbound_to)
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=gmail_send_error_status(error))
        return

    try:
        redline_export = redline_export_service.build_matter_redline(
            matter_id.strip(),
            payload,
            owner_user_id=owner_user_id,
        )
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

    send_matter = matter_store.get_matter(matter_id.strip(), owner_user_id=owner_user_id)
    if send_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if matter_blocks_redline_send(send_matter):
        handler._send_json({"error": "Matter needs human review before a redline can be sent."}, status=409)
        return

    try:
        if gmail_token_owner_user_id:
            sent = gmail_integration.send_redline_email(
                send_matter,
                redline_export.data,
                redline_export.filename,
                body=outbound_body,
                owner_user_id=gmail_token_owner_user_id,
                subject=outbound_subject,
                to=outbound_to,
            )
        else:
            sent = gmail_integration.send_redline_email(
                send_matter,
                redline_export.data,
                redline_export.filename,
                body=outbound_body,
                subject=outbound_subject,
                to=outbound_to,
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
        owner_user_id=owner_user_id,
    )
    if updated_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({
        "filename": redline_export.filename,
        "matter": matter_view.public_matter(updated_matter),
        "sent": sent,
    })


def matter_blocks_redline_send(matter: dict) -> bool:
    return matter_view.matter_needs_human_review(matter) and not matter.get("human_reviewed")


def gmail_owner_user_id(handler) -> str:
    current_user = getattr(handler, "current_user", None)
    if isinstance(current_user, dict) and current_user.get("provider") == "google":
        return request_owner_user_id(handler)
    return ""


def _gmail_redirect_uri(handler) -> str:
    configured = gmail_integration.configured_gmail_redirect_uri()
    if configured:
        return configured
    return f"{_request_base_url(handler)}/auth/gmail/callback"


def _request_base_url(handler) -> str:
    scheme = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() or "http"
    host = handler.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if not host:
        host = handler.headers.get("Host", "").strip()
    if not host:
        server_host, server_port = handler.server.server_address[:2]
        host = f"{server_host}:{server_port}"
    return f"{scheme}://{host}"


def clean_outbound_subject(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_OUTBOUND_SUBJECT_CHARS]


def clean_outbound_recipient(value: object) -> str | None:
    recipient = gmail_integration.recipient_email(value)
    return recipient or None


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
