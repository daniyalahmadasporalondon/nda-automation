"""Routes for the "Save NDA to Google Drive" outbound flow.

Mirrors the Gmail outbound routes (``routes/gmail.py``) but for Google Drive:

* ``GET /api/drive/status`` — is Drive connected + the configured upload folder.
* ``GET /auth/drive/start`` / ``GET /auth/drive/callback`` — the OAuth consent
  flow, role fixed to ``"drive"`` (least-privilege ``drive.file`` scope). Reuses
  the same Google connection state as the Gmail connect flow.
* ``POST /api/drive/upload-matter`` — SYNC a matter's whole artifact set into a
  structured per-matter Drive folder (Drive v2).
* ``POST /api/admin/drive-settings`` — admin-only upload-folder + enable config.

Matter ownership is validated exactly like ``send-redline`` via
``request_owner_user_id``; the doc bytes are pulled through the artifact layer.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .. import (
    app_settings,
    artifact_registry,
    drive_integration,
    google_connection,
    matter_view,
    telemetry,
    user_store,
)
from ..matter_repository import DiskMatterRepository, MatterRepository
from .common import request_owner_user_id, require_admin

DRIVE_CONNECT_URL = "/auth/drive/start"
# Drive has its OWN OAuth callback path. It must not reuse the Gmail redirect
# (NDA_GMAIL_OAUTH_REDIRECT_URI), which points at /auth/gmail/callback.
DRIVE_OAUTH_REDIRECT_URI_ENV = "NDA_DRIVE_OAUTH_REDIRECT_URI"


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def handle_drive_status(handler, *, send_body: bool = True) -> None:
    owner_user_id = _google_owner_user_id(handler)
    settings = app_settings.drive_settings()
    connected = bool(owner_user_id) and drive_integration.drive_connected(owner_user_id)
    token_status = _drive_token_status(owner_user_id)
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
            "signed_in": bool(owner_user_id),
            "user_scoped": bool(owner_user_id),
            "needs_connect": bool(owner_user_id) and not connected,
            "connect_url": _drive_connect_url(owner_user_id),
            "token": token_status,
        },
        send_body=send_body,
    )


def handle_drive_connect_start(handler, *, send_body: bool = True) -> None:
    owner_user_id = _google_owner_user_id(handler)
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
        authorization_url = google_connection.build_authorization_url(
            redirect_uri=_drive_redirect_uri(handler),
            role="drive",
            state=state,
            login_hint=google_connection.login_hint(getattr(handler, "current_user", None)),
        )
    except google_connection.GoogleConnectionError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    handler._send_redirect(authorization_url, send_body=send_body)


def handle_drive_connect_callback(handler, *, send_body: bool = True) -> None:
    owner_user_id = _google_owner_user_id(handler)
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
        token_response = google_connection.exchange_oauth_code(code, redirect_uri=_drive_redirect_uri(handler))
        google_connection.save_user_oauth_token(owner_user_id, token_response, role="drive")
    except google_connection.GoogleConnectionError as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return
    # A fresh connection lands active: the single Drive toggle treats "connected"
    # as "on", so enable the Drive feature (best-effort) instead of leaving it off.
    try:
        app_settings.update_drive_settings({"enabled": True})
    except Exception:  # pragma: no cover - enabling is best-effort, never blocks connect
        pass
    next_path = str(state_record.get("next_path") or "/")
    handler._send_redirect(
        next_path,
        headers={"X-Drive-Connected": "1"},
        send_body=send_body,
    )


def handle_drive_disconnect(handler) -> None:
    """Remove the signed-in user's Drive OAuth token (the toggle's Off action).

    The Drive token is stored under the shared ``"drive"`` Google connection role.
    """
    owner_user_id = _google_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in with Google before disconnecting Drive."}, status=403)
        return
    try:
        removed = google_connection.disconnect_user_oauth(owner_user_id, role="drive")
    except google_connection.GoogleConnectionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    # Off also turns the Drive feature off so a later reconnect starts clean.
    try:
        app_settings.update_drive_settings({"enabled": False})
    except Exception:  # pragma: no cover - best-effort
        pass
    handler._send_json({"disconnected": removed})


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
    drive_token_owner_user_id = _google_owner_user_id(handler)
    repository = _repository(handler)

    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
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
    updated_matter = repository.update_matter_fields(
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
    if "auto_intake" in payload:
        auto_intake = payload.get("auto_intake")
        if not isinstance(auto_intake, bool):
            handler._send_json({"error": "Drive auto-intake setting must be true or false."}, status=400)
            return
        updates["auto_intake"] = auto_intake
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
                "auto_intake": bool(settings.get("auto_intake", True)),
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


def _drive_connect_url(owner_user_id: str) -> str:
    if owner_user_id:
        return DRIVE_CONNECT_URL
    return "/auth/google/start"


def _drive_token_status(owner_user_id: str) -> dict[str, object]:
    if not owner_user_id:
        return {
            "configured": False,
            "label": "Sign in with Google",
            "source": "missing",
        }
    return google_connection.role_token_status("drive", owner_user_id=owner_user_id)


def _google_owner_user_id(handler) -> str:
    return google_connection.connected_owner_user_id(
        getattr(handler, "current_user", None),
        owner_user_id=request_owner_user_id(handler),
    )


def _record_drive_settings_audit(previous: dict, current: dict) -> None:
    changes = []
    for key in ("enabled", "folder_id", "folder_name", "auto_intake"):
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
    # Use a Drive-specific configured redirect if provided, otherwise build the
    # Drive callback from the request. NEVER fall back to the Gmail-configured
    # redirect: that points at /auth/gmail/callback, which would route the Drive
    # consent to the Gmail handler and reject it (the OAuth state purpose is
    # "drive", but the Gmail callback only accepts "gmail"), so Drive could never
    # connect on a deployment that sets NDA_GMAIL_OAUTH_REDIRECT_URI.
    configured = os.environ.get(DRIVE_OAUTH_REDIRECT_URI_ENV, "").strip()
    if configured:
        return configured
    return f"{google_connection.request_base_url(handler)}/auth/drive/callback"
