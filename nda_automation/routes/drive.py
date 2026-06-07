"""Routes for the "Save NDA to Google Drive" outbound flow.

Mirrors the Gmail outbound routes (``routes/gmail.py``) but for Google Drive:

* ``GET /api/drive/status`` — is Drive connected + the configured upload folder.
* ``GET /auth/drive/start`` / ``GET /auth/drive/callback`` — the OAuth consent
  flow, role fixed to ``"drive"`` (least-privilege ``drive.file`` scope). Reuses
  the same ``oauth_state`` + token exchange + ``save_user_gmail_oauth_token`` as
  the Gmail connect flow.
* ``POST /api/drive/upload-matter`` — push a matter's final NDA into Drive.
* ``POST /api/admin/drive-settings`` — admin-only upload-folder + enable config.

Matter ownership is validated exactly like ``send-redline`` via
``request_owner_user_id``; the doc bytes are pulled through the artifact layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .. import (
    app_settings,
    artifact_registry,
    artifact_service,
    drive_integration,
    gmail_integration,
    matter_store,
    matter_view,
    telemetry,
    user_store,
)
from .common import request_owner_user_id, require_admin
from .gmail import gmail_owner_user_id

# Role preference for the document to upload: a reviewed export is the most
# authoritative, then a freshly generated NDA, then the original counterparty
# document. ``upload-matter`` resolves the best available unless the caller pins
# an explicit role.
DRIVE_ROLE_PREFERENCE = (
    artifact_registry.ROLE_REVIEWED,
    artifact_registry.ROLE_GENERATED,
    artifact_registry.ROLE_ORIGINAL,
)
DRIVE_CONNECT_URL = "/auth/drive/start"
MAX_DRIVE_FILENAME_SUBJECT_CHARS = 120


def handle_drive_status(handler, *, send_body: bool = True) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    settings = app_settings.drive_settings()
    connected = bool(owner_user_id) and drive_integration.drive_connected(owner_user_id)
    account = ""
    if connected:
        account = drive_integration.drive_account_email(owner_user_id)
    folder = None
    folder_id = str(settings.get("folder_id") or "")
    if folder_id:
        folder = {"id": folder_id, "name": str(settings.get("folder_name") or "")}
    handler._send_json(
        {
            "connected": connected,
            "account": account,
            "folder": folder,
            "enabled": bool(settings.get("enabled", False)),
        },
        send_body=send_body,
    )


def handle_drive_connect_start(handler, *, send_body: bool = True) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before connecting Drive."}, status=403, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    next_path = query.get("next", ["/"])[0]
    try:
        state = user_store.create_oauth_state(
            purpose="drive",
            user_id=owner_user_id,
            next_path=next_path,
            metadata={"role": "drive"},
        )
        authorization_url = gmail_integration.build_gmail_authorization_url(
            redirect_uri=_drive_redirect_uri(handler),
            role="drive",
            state=state,
        )
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    handler._send_redirect(authorization_url, send_body=send_body)


def handle_drive_connect_callback(handler, *, send_body: bool = True) -> None:
    owner_user_id = gmail_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before connecting Drive."}, status=403, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    if query.get("error"):
        handler._send_json({"error": "Drive connection was not completed."}, status=400, send_body=send_body)
        return
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    state_record = user_store.consume_oauth_state(state, purpose="drive", user_id=owner_user_id)
    if not code or state_record is None:
        handler._send_json({"error": "Drive connection state is invalid or expired."}, status=400, send_body=send_body)
        return
    try:
        token_response = gmail_integration.exchange_gmail_oauth_code(code, redirect_uri=_drive_redirect_uri(handler))
        gmail_integration.save_user_gmail_oauth_token(owner_user_id, token_response, role="drive")
    except gmail_integration.GmailIntegrationError as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return
    next_path = str(state_record.get("next_path") or "/")
    handler._send_redirect(
        next_path,
        headers={"X-Drive-Connected": "1"},
        send_body=send_body,
    )


def handle_drive_upload_matter(handler) -> None:
    telemetry.increment("drive_upload_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    matter_id = payload.get("matter_id")
    if not isinstance(matter_id, str) or not matter_id.strip():
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Matter not found."}, status=400)
        return
    matter_id = matter_id.strip()

    requested_role = _requested_role(payload.get("role"))
    if requested_role is _INVALID_ROLE:
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Unsupported Drive upload role."}, status=400)
        return

    owner_user_id = request_owner_user_id(handler)
    drive_token_owner_user_id = gmail_owner_user_id(handler)

    matter = matter_store.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Matter not found."}, status=400)
        return

    if not drive_integration.drive_connected(drive_token_owner_user_id):
        telemetry.increment("drive_upload_failed")
        handler._send_json(
            {
                "error": "Google Drive is not connected.",
                "needs_connect": True,
                "connect_url": DRIVE_CONNECT_URL,
            },
            status=409,
        )
        return

    resolved = _resolve_matter_document(matter, matter_id, requested_role, owner_user_id=owner_user_id)
    if resolved is None:
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Matter has no document to save to Drive."}, status=400)
        return
    file_bytes, filename = resolved

    settings = app_settings.drive_settings()
    folder_id = str(settings.get("folder_id") or "")

    try:
        uploaded = drive_integration.upload_docx_to_drive(
            file_bytes=file_bytes,
            filename=filename,
            folder_id=folder_id,
            owner_user_id=drive_token_owner_user_id,
        )
    except drive_integration.DriveNotConnectedError:
        telemetry.increment("drive_upload_failed")
        handler._send_json(
            {
                "error": "Google Drive is not connected.",
                "needs_connect": True,
                "connect_url": DRIVE_CONNECT_URL,
            },
            status=409,
        )
        return
    except drive_integration.DriveRateLimitError as error:
        telemetry.increment("drive_upload_failed")
        telemetry.increment("drive_upload_rate_limited")
        handler._send_json({"error": str(error)}, status=429)
        return
    except drive_integration.DriveIntegrationError as error:
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": str(error)}, status=502)
        return

    updated_matter = matter_store.update_matter_fields(
        matter_id,
        {
            "last_drive_file_id": uploaded.get("file_id", ""),
            "last_drive_web_link": uploaded.get("web_link", ""),
            "last_drive_filename": uploaded.get("filename", ""),
            "last_drive_folder_id": uploaded.get("folder_id", ""),
            "last_drive_uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        owner_user_id=owner_user_id,
    )
    if updated_matter is None:
        updated_matter = matter

    telemetry.increment("drive_upload_succeeded")
    handler._send_json(
        {
            "uploaded": uploaded,
            "matter": matter_view.public_matter(updated_matter),
        }
    )


def handle_drive_settings_update(handler) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return

    updates: dict[str, object] = {}
    if "enabled" in payload:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            handler._send_json({"error": "Drive enabled setting must be true or false."}, status=400)
            return
        updates["enabled"] = enabled
    if "folder_id" in payload:
        folder_id = payload.get("folder_id")
        if not isinstance(folder_id, str):
            handler._send_json({"error": "Drive folder id must be a string."}, status=400)
            return
        updates["folder_id"] = folder_id
    if "folder_name" in payload:
        folder_name = payload.get("folder_name")
        if not isinstance(folder_name, str):
            handler._send_json({"error": "Drive folder name must be a string."}, status=400)
            return
        updates["folder_name"] = folder_name
    if not updates:
        handler._send_json({"error": "Provide a Drive setting to update."}, status=400)
        return

    previous = app_settings.drive_settings()
    try:
        settings = app_settings.update_drive_settings(updates)
    except app_settings.AppSettingsError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    _record_drive_settings_audit(previous, settings)
    handler._send_json(
        {
            "drive": {
                "enabled": bool(settings.get("enabled", False)),
                "folder_id": str(settings.get("folder_id") or ""),
                "folder_name": str(settings.get("folder_name") or ""),
            }
        }
    )


# --- helpers ---------------------------------------------------------------
_INVALID_ROLE = object()


def _requested_role(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_ROLE
    role = value.strip().lower()
    if not role:
        return None
    if role in DRIVE_ROLE_PREFERENCE:
        return role
    return _INVALID_ROLE


def _resolve_matter_document(matter, matter_id, requested_role, *, owner_user_id=""):
    """Find the best document for the matter and return ``(bytes, filename)``.

    With an explicit ``requested_role`` only that role is tried; otherwise the
    preference order reviewed > generated > original is walked. Returns ``None``
    when no artifact with usable bytes exists.
    """
    roles = (requested_role,) if requested_role else DRIVE_ROLE_PREFERENCE
    for role in roles:
        artifact = artifact_registry.latest_artifact_for_role(matter, role)
        if artifact is None:
            continue
        file_bytes = artifact_service.get_artifact_bytes(
            matter_id,
            artifact.id,
            owner_user_id=owner_user_id,
        )
        if not file_bytes:
            continue
        filename = str(artifact.name or "").strip() or _fallback_filename(matter)
        return file_bytes, filename
    return None


def _fallback_filename(matter) -> str:
    subject = (
        str(matter.get("counterparty") or "")
        or str(matter.get("subject") or "")
        or str(matter.get("sender") or "")
    ).strip()
    subject = " ".join(subject.split())[:MAX_DRIVE_FILENAME_SUBJECT_CHARS]
    label = subject or "NDA"
    return f"NDA - {label}.docx" if subject else "NDA.docx"


def _record_drive_settings_audit(previous: dict, current: dict) -> None:
    changes = []
    for key in ("enabled", "folder_id", "folder_name"):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            changes.append({"setting": f"drive.{key}", "before": before, "after": after})
    if not changes:
        return
    telemetry.increment("settings_audit_events")
    app_settings.record_settings_audit_event({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": "admin",
        "action": "drive_settings_update",
        "changes": changes,
    })


def _drive_redirect_uri(handler) -> str:
    configured = gmail_integration.configured_gmail_redirect_uri()
    if configured:
        return configured
    return f"{_request_base_url(handler)}/auth/drive/callback"


def _request_base_url(handler) -> str:
    scheme = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() or "http"
    host = handler.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if not host:
        host = handler.headers.get("Host", "").strip()
    if not host:
        server_host, server_port = handler.server.server_address[:2]
        host = f"{server_host}:{server_port}"
    return f"{scheme}://{host}"
