from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .. import (
    app_settings,
    gmail_integration,
    google_connection,
    matter_view,
    redline_export_service,
    telemetry,
    user_store,
)
from ..docx_export import DocxExportError
from ..docx_text import DocxExtractionError
from ..matter_lifecycle import (
    MatterDeliveryError,
    MatterNotFoundError,
    MatterSendBlockedError,
    RepositoryMatterLifecycle,
)
from ..matter_repository import DiskMatterRepository
from .common import request_owner_user_id

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
        authorization_url = google_connection.build_authorization_url(
            redirect_uri=_gmail_redirect_uri(handler),
            role=role,
            state=state,
            login_hint=google_connection.login_hint(getattr(handler, "current_user", None)),
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
        token_response = google_connection.exchange_oauth_code(code, redirect_uri=_gmail_redirect_uri(handler))
        connected_roles = google_connection.save_user_oauth_token(
            owner_user_id,
            token_response,
            role=role,
        )
        gmail_integration._clear_profile_cache_for_owner(owner_user_id)
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return
    # A fresh connection should land active: the single Gmail toggle treats
    # "connected" as "on", so enable both roles (best-effort) rather than leaving
    # the integration paused from a previous disable.
    try:
        app_settings.update_gmail_settings({"inbound_enabled": True, "outbound_enabled": True})
    except Exception:  # pragma: no cover - enabling is best-effort, never blocks connect
        pass
    # The unified "all" connect also grants Drive (drive.file); enable Drive too so
    # one click lands Gmail AND Drive active.
    if "drive" in connected_roles:
        try:
            app_settings.update_drive_settings({"enabled": True})
        except Exception:  # pragma: no cover - best-effort
            pass
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
        removed = google_connection.disconnect_user_oauth(owner_user_id, role=role)
        gmail_integration._clear_profile_cache_for_owner(owner_user_id)
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

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = gmail_integration.import_inbound_matters(
            limit=_manual_import_limit(payload.get("limit")),
            query=payload.get("query") if isinstance(payload.get("query"), str) else None,
            owner_user_id=owner_user_id,
        )
        result = {
            **result,
            "deduplicated_count": DiskMatterRepository().deduplicate_gmail_matters(owner_user_id=owner_user_id),
        }
    except gmail_integration.GmailRateLimitError as error:
        finished_at = datetime.now(timezone.utc).isoformat()
        user_store.record_user_gmail_sync_error(
            owner_user_id,
            str(error),
            started_at=started_at,
            finished_at=finished_at,
            query=payload.get("query") if isinstance(payload.get("query"), str) else gmail_integration._default_inbound_query(),
        )
        handler._send_json({"error": str(error)}, status=429)
        return
    except gmail_integration.GmailIntegrationError as error:
        finished_at = datetime.now(timezone.utc).isoformat()
        user_store.record_user_gmail_sync_error(
            owner_user_id,
            str(error),
            started_at=started_at,
            finished_at=finished_at,
            query=payload.get("query") if isinstance(payload.get("query"), str) else gmail_integration._default_inbound_query(),
        )
        handler._send_json({"error": str(error)}, status=502)
        return

    finished_at = datetime.now(timezone.utc).isoformat()
    user_store.record_user_gmail_sync(
        owner_user_id,
        result,
        synced_at=finished_at,
        started_at=started_at,
        finished_at=finished_at,
    )
    app_settings.record_gmail_sync(result, synced_at=finished_at, started_at=started_at, finished_at=finished_at)
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
    outbound_to = clean_outbound_recipient(payload.get("to"))
    # The recipient can originate from an attacker-controlled inbound header
    # (Reply-To/From), so require the operator to confirm the exact destination
    # address; the integration layer rejects the send if it does not match the
    # address the redline is actually going to.
    confirmed_recipient = clean_outbound_recipient(payload.get("confirm_recipient"))
    outbound_subject = clean_outbound_subject(payload.get("subject"))
    outbound_body = clean_outbound_body(payload.get("body"))

    try:
        sent_redline = RepositoryMatterLifecycle(DiskMatterRepository()).send_redline(
            matter_id.strip(),
            payload,
            owner_user_id=owner_user_id,
            token_owner_user_id=gmail_token_owner_user_id,
            to=outbound_to,
            confirmed_recipient=confirmed_recipient,
            subject=outbound_subject,
            body=outbound_body,
        )
    except MatterNotFoundError:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    except MatterDeliveryError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except MatterSendBlockedError as error:
        handler._send_json({"error": str(error)}, status=409)
        return
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=gmail_send_error_status(error))
        return
    except redline_export_service.MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404)
        return
    except redline_export_service.DocxOpenHealthError as error:
        handler._send_json({"error": str(error), "details": error.details}, status=500)
        return
    except redline_export_service.MatterSourceTextChangedError as error:
        handler._send_json({"error": str(error)}, status=409)
        return
    except redline_export_service.StaleMatterReviewError as error:
        handler._send_json({
            "error": str(error),
            "stale_reasons": error.reasons,
            "review_refresh": error.summary,
        }, status=409)
        return
    except redline_export_service.PdfSourceRedlineUnavailableError as error:
        handler._send_json(error.payload, status=error.status)
        return
    except DocxExtractionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except DocxExportError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    handler._send_json({
        "filename": sent_redline.filename,
        "matter": matter_view.public_matter(sent_redline.matter),
        "sent": sent_redline.sent,
    })


def gmail_owner_user_id(handler) -> str:
    return google_connection.connected_owner_user_id(
        getattr(handler, "current_user", None),
        owner_user_id=request_owner_user_id(handler),
    )


def _gmail_redirect_uri(handler) -> str:
    configured = gmail_integration.configured_gmail_redirect_uri()
    if configured:
        return configured
    return f"{google_connection.request_base_url(handler)}/auth/gmail/callback"


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
    if isinstance(error, gmail_integration.RecipientConfirmationError):
        return 400
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
