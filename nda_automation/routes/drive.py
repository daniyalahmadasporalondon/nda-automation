"""Routes for the "Save NDA to Google Drive" outbound flow.

Mirrors the Gmail outbound routes (``routes/gmail.py``) but for Google Drive:

* ``GET /api/drive/status`` — is Drive connected + the configured upload folder.
* ``GET /auth/drive/start`` / ``GET /auth/drive/callback`` — the OAuth consent
  flow, role fixed to ``"drive"`` (least-privilege ``drive.file`` scope). Reuses
  the same ``oauth_state`` + token exchange + ``save_user_gmail_oauth_token`` as
  the Gmail connect flow.
* ``POST /api/drive/upload-matter`` — SYNC a matter's whole artifact set into a
  structured per-matter Drive folder (Drive v2).
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
    drive_integration,
    gmail_integration,
    matter_store,
    matter_view,
    telemetry,
    user_store,
)
from .common import request_owner_user_id, require_admin
from .gmail import gmail_owner_user_id

DRIVE_CONNECT_URL = "/auth/drive/start"


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
    """Sync a matter's whole artifact set into a structured Drive folder (v2).

    Mirrors :func:`artifact_registry.matter_artifacts` into a per-matter folder
    tree (``{root}/{counterparty}/{matter}/``) with grammar-named files plus a
    ``metadata/matter_summary.json``. Idempotent: re-running uploads only NEW
    artifacts and creates no duplicate folders/files.
    """
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

    owner_user_id = request_owner_user_id(handler)
    drive_token_owner_user_id = gmail_owner_user_id(handler)

    matter = matter_store.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Matter not found."}, status=400)
        return

    if not drive_integration.drive_connected(drive_token_owner_user_id):
        telemetry.increment("drive_upload_failed")
        handler._send_json(_needs_connect_payload(), status=409)
        return

    if not artifact_registry.matter_artifacts(matter):
        telemetry.increment("drive_upload_failed")
        handler._send_json({"error": "Matter has no document to save to Drive."}, status=400)
        return

    settings = app_settings.drive_settings()
    root_folder_id = str(settings.get("folder_id") or "")
    synced_at = datetime.now(timezone.utc).isoformat()

    try:
        synced = drive_integration.sync_matter_folder(
            matter=matter,
            matter_id=matter_id,
            owner_user_id=drive_token_owner_user_id,
            root_folder_id=root_folder_id,
            synced_at=synced_at,
        )
    except drive_integration.DriveNotConnectedError:
        telemetry.increment("drive_upload_failed")
        handler._send_json(_needs_connect_payload(), status=409)
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

    drive_block = {
        "matter_folder_id": synced["matter_folder_id"],
        "matter_folder_url": synced["matter_folder_url"],
        "synced_at": synced_at,
        "artifacts": synced["artifacts"],
    }
    updated_matter = matter_store.update_matter_fields(
        matter_id,
        {"drive": drive_block},
        owner_user_id=owner_user_id,
    )
    if updated_matter is None:
        updated_matter = matter

    telemetry.increment("drive_upload_succeeded")
    telemetry.increment("drive_files_synced", amount=int(synced.get("synced_count") or 0))
    handler._send_json(
        {
            "drive": synced,
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
def _needs_connect_payload() -> dict:
    return {
        "error": "Google Drive is not connected.",
        "needs_connect": True,
        "connect_url": DRIVE_CONNECT_URL,
    }


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
