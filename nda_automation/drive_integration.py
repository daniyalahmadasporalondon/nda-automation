"""Google Drive outbound integration — "Save NDA to Google Drive".

A thin client that uploads a matter's final NDA (.docx) into the signed-in
user's Google Drive, reusing the role-parameterized Google OAuth machinery in
``gmail_integration``. A dedicated ``"drive"`` OAuth role (added to
``gmail_integration.GMAIL_OAUTH_SCOPES_BY_ROLE``) carries the least-privilege
``drive.file`` scope, so this app can only ever see or modify files it creates,
never the user's whole Drive.

The OAuth token storage, refresh, locking and per-user token paths are all
reused from ``gmail_integration`` via ``_credentials_for_role("drive", ...)`` —
this module only adds the Drive API calls (file upload + account lookup) and the
Drive-specific error taxonomy that mirrors the Gmail one.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from . import gmail_integration

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DriveIntegrationError(RuntimeError):
    """A Drive upload could not be completed."""


class DriveNotConnectedError(DriveIntegrationError):
    """The signed-in user has not connected Google Drive.

    The route maps this to a 409 with ``needs_connect`` + ``connect_url`` so the
    frontend can prompt the user through the ``/auth/drive/start`` consent flow.
    """


class DriveRateLimitError(DriveIntegrationError):
    def __init__(self, message: str, *, retry_after_epoch: float = 0.0):
        super().__init__(message)
        self.retry_after_epoch = retry_after_epoch


def _drive_service(owner_user_id: str = "") -> Any:
    """Build an authenticated Drive v3 service for the signed-in user.

    Reuses the Gmail OAuth credentials machinery under the ``"drive"`` role:
    the token lives at ``data/users/gmail/{user_id}/drive-token.json`` and is
    refreshed/locked exactly like the Gmail tokens. A missing/invalid token
    surfaces as :class:`DriveNotConnectedError` so the route can prompt connect.
    """
    try:
        creds = gmail_integration._credentials_for_role("drive", owner_user_id=owner_user_id)
    except gmail_integration.GmailIntegrationError as error:
        raise DriveNotConnectedError(str(error)) from error
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise DriveIntegrationError("Google API packages are not installed.") from exc
    try:
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise DriveIntegrationError("Drive service could not start.") from exc


def upload_docx_to_drive(
    *,
    file_bytes: bytes,
    filename: str,
    folder_id: str = "",
    owner_user_id: str = "",
    service: Any | None = None,
) -> dict[str, str]:
    """Upload ``file_bytes`` as a Word document to the user's Drive.

    ``folder_id`` is the raw Drive folder id (stored verbatim in admin settings);
    when empty the file lands in the Drive root. ``service`` is injectable so
    tests can supply a fake Drive client without making live Google calls.

    Returns ``{"file_id", "web_link", "filename", "folder_id"}``.
    """
    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise DriveIntegrationError("No document bytes to upload to Drive.")
    upload_name = str(filename or "").strip() or "NDA.docx"
    parents = [folder_id] if folder_id else []

    drive_service = service or _drive_service(owner_user_id)
    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError as exc:
        raise DriveIntegrationError("Google API packages are not installed.") from exc

    media = MediaIoBaseUpload(
        BytesIO(bytes(file_bytes)),
        mimetype=DOCX_MIME,
        resumable=False,
    )
    try:
        created = drive_service.files().create(
            body={"name": upload_name, "parents": parents},
            media_body=media,
            fields="id,webViewLink",
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive upload failed.")

    if not isinstance(created, dict):
        created = {}
    return {
        "file_id": str(created.get("id") or ""),
        "web_link": str(created.get("webViewLink") or ""),
        "filename": upload_name,
        "folder_id": folder_id,
    }


def drive_account_email(owner_user_id: str = "", *, service: Any | None = None) -> str:
    """Best-effort Drive account email for the status panel.

    Never raises: a failure (not connected, rate-limited, API hiccup) just yields
    an empty string so ``/api/drive/status`` can still report connectivity.
    """
    try:
        drive_service = service or _drive_service(owner_user_id)
        about = drive_service.about().get(fields="user/emailAddress").execute()
    except Exception:
        return ""
    if not isinstance(about, dict):
        return ""
    user = about.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("emailAddress") or "")


def drive_connected(owner_user_id: str = "") -> bool:
    """Whether the signed-in user has a usable Drive credential."""
    try:
        gmail_integration._credentials_for_role("drive", owner_user_id=owner_user_id)
    except gmail_integration.GmailIntegrationError:
        return False
    return True


def _raise_drive_api_error(error: Exception, fallback_message: str) -> None:
    retry_after_epoch = gmail_integration._gmail_retry_after_epoch(error)
    if retry_after_epoch:
        raise DriveRateLimitError(
            gmail_integration._gmail_rate_limit_message(retry_after_epoch).replace("Gmail", "Drive"),
            retry_after_epoch=retry_after_epoch,
        ) from error
    raise DriveIntegrationError(fallback_message) from error
