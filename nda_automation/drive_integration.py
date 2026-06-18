"""Google Drive outbound integration — "Save NDA to Google Drive".

A thin client that files a matter's NDA documents into the signed-in user's
Google Drive, reusing the role-parameterized Google OAuth machinery in
``google_connection``. A dedicated ``"drive"`` OAuth role carries the least-privilege
``drive.file`` scope, so this app can only ever see or modify files it creates,
never the user's whole Drive.

The OAuth token storage, refresh, locking and per-user token paths are owned by
``google_connection``; this module only adds the Drive API calls and the
Drive-specific error taxonomy that mirrors the Gmail one.

Drive v2 — structured per-matter filing
========================================
Beyond the original flat single-file upload (``upload_docx_to_drive``), this
module mirrors the matter's :mod:`artifact_registry` into a per-matter folder
tree::

    {root}/{counterparty}/{YYYY-MM-DD · document title · ref}/
        01_received.docx
        02_ai_redline_v1.docx
        03_legal_review_v1.docx
        04_sent_v1.docx
        05_counter_v1.docx
        08_signed.pdf
        ...
        metadata/matter_summary.json

Every artifact in :func:`artifact_registry.matter_artifacts` order becomes one
file, named by the lifecycle grammar ``{NN}_{stage}[_v{N}].{ext}`` (see
:func:`artifact_registry.stage_for` for the role/actor -> stage map and
:func:`artifact_registry.stage_filename`). ``NN`` is the chronological capture
order at sync time. :func:`sync_matter_folder` is the orchestrator; it is
idempotent — re-running it creates no duplicate folders or files.

drive.file scope constraint
---------------------------
The ``drive.file`` scope only lets the app see and modify the files (and
folders) it itself created. That is *why* the app must OWN the tree it manages:
``find_or_create_folder`` can only ever find folders this app created, so the
``NDAs`` root (and everything below it) is app-created and therefore app-owned.
A user-pasted pre-existing ``folder_id`` (admin setting) may NOT be writable
under ``drive.file`` unless this app created it — so the admin ``folder_id`` is
treated as an *optional* parent for the app-created ``NDAs`` root, never as a
required, app-visible folder.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
import logging
from typing import Any

from . import artifact_registry, artifact_service, gmail_integration, google_connection

logger = logging.getLogger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JSON_MIME = "application/json"
FOLDER_MIME = "application/vnd.google-apps.folder"

# The app-created root folder under My Drive that holds the whole NDA filing tree.
# Because drive.file only exposes app-created files, this root must be app-created.
DEFAULT_ROOT_FOLDER_NAME = "NDAs"
METADATA_FOLDER_NAME = "metadata"
MATTER_SUMMARY_FILENAME = "matter_summary.json"

# Drive filenames use the chronological lifecycle grammar ``{NN}_{stage}[_v{N}]``.
# The ``stage`` is derived from each artifact's ``(role, actor)`` pair by
# :func:`artifact_registry.stage_for` — the registry stays the single source of
# truth for the role/actor vocabulary and the stage map; this module only renders
# the filename via :func:`_drive_filename_for_artifact`.


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

    Reuses the shared Google OAuth credentials machinery under the ``"drive"``
    role. A missing/invalid token surfaces as :class:`DriveNotConnectedError`
    so the route can prompt connect.
    """
    try:
        creds = google_connection.credentials_for_role("drive", owner_user_id=owner_user_id, integration_label="Drive")
    except google_connection.GoogleConnectionError as error:
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


# --- Drive v2: structured per-matter filing --------------------------------
def _escape_drive_query_value(value: str) -> str:
    """Escape a value for inclusion in a Drive ``q=`` string literal.

    Drive query string literals are single-quoted; a literal single quote (or
    backslash) inside the value must be backslash-escaped or it terminates the
    literal early and lets attacker-controlled folder/file names inject query
    clauses. We escape backslash first, then the single quote.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(
    *,
    name: str,
    parent_id: str = "",
    owner_user_id: str = "",
    service: Any | None = None,
) -> str:
    """Return the id of the app-created folder ``name`` under ``parent_id``.

    IDEMPOTENT: first queries the user's *app-created* folders (drive.file only
    ever exposes folders this app created) for a non-trashed folder of that exact
    name (scoped to ``parent_id`` when given); returns the first match. Only when
    no match exists does it ``files().create`` a new folder. Re-running therefore
    never creates a duplicate folder.

    The name is escaped into the ``q=`` query so a folder name containing a single
    quote cannot inject extra query clauses.
    """
    folder_name = str(name or "").strip()
    if not folder_name:
        raise DriveIntegrationError("A Drive folder name is required.")
    drive_service = service or _drive_service(owner_user_id)

    query = (
        f"mimeType='{FOLDER_MIME}' "
        f"and name='{_escape_drive_query_value(folder_name)}' "
        "and trashed=false"
    )
    if parent_id:
        query += f" and '{_escape_drive_query_value(parent_id)}' in parents"
    try:
        listing = drive_service.files().list(
            q=query,
            fields="files(id,name)",
            pageSize=1,
            spaces="drive",
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive folder lookup failed.")
    files = listing.get("files") if isinstance(listing, dict) else None
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])

    body: dict[str, Any] = {"name": folder_name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    try:
        created = drive_service.files().create(body=body, fields="id").execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive folder creation failed.")
    if not isinstance(created, dict) or not created.get("id"):
        raise DriveIntegrationError("Drive folder creation returned no id.")
    return str(created["id"])


def upload_or_replace_file(
    *,
    file_bytes: bytes,
    filename: str,
    parent_id: str,
    mimetype: str = DOCX_MIME,
    owner_user_id: str = "",
    service: Any | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Upload ``file_bytes`` as ``filename`` into ``parent_id`` (idempotent by name).

    Returns ``{"file_id", "web_link", "filename", "created": bool, "replaced":
    bool}``. IDEMPOTENT by name within the parent. Default behaviour: the grammar
    makes per-artifact filenames unique per matter, so a pre-existing file of that
    exact name means "already synced" — we SKIP the upload and return the existing
    id with ``created=False`` / ``replaced=False``.

    When ``replace_existing`` is True a pre-existing file of that name is TRULY
    overwritten in place via ``files().update`` (a media update keyed by file id)
    rather than skipped. This is for fixed-name files whose CONTENT changes between
    syncs — chiefly ``matter_summary.json`` — so a re-sync after the matter became
    executed/signed actually lands the fresh facets (``created=False`` but
    ``replaced=True``). Only genuinely new filenames are created (``created=True``).
    """
    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise DriveIntegrationError("No document bytes to upload to Drive.")
    upload_name = str(filename or "").strip()
    if not upload_name:
        raise DriveIntegrationError("A Drive filename is required.")
    if not parent_id:
        raise DriveIntegrationError("A Drive parent folder id is required.")
    drive_service = service or _drive_service(owner_user_id)

    query = (
        f"name='{_escape_drive_query_value(upload_name)}' "
        f"and '{_escape_drive_query_value(parent_id)}' in parents "
        "and trashed=false"
    )
    try:
        listing = drive_service.files().list(
            q=query,
            fields="files(id,name,webViewLink)",
            pageSize=1,
            spaces="drive",
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive file lookup failed.")
    files = listing.get("files") if isinstance(listing, dict) else None
    existing_id = ""
    existing_link = ""
    if isinstance(files, list) and files:
        existing = files[0]
        if isinstance(existing, dict) and existing.get("id"):
            existing_id = str(existing["id"])
            existing_link = str(existing.get("webViewLink") or "")
    if existing_id and not replace_existing:
        return {
            "file_id": existing_id,
            "web_link": existing_link,
            "filename": upload_name,
            "created": False,
            "replaced": False,
        }

    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError as exc:
        raise DriveIntegrationError("Google API packages are not installed.") from exc

    media = MediaIoBaseUpload(
        BytesIO(bytes(file_bytes)),
        mimetype=str(mimetype or DOCX_MIME),
        resumable=False,
    )
    if existing_id and replace_existing:
        # TRUE replace: overwrite the existing file's media in place (keeps the id
        # + URL stable so a previously-stored pointer stays valid).
        try:
            updated = drive_service.files().update(
                fileId=existing_id,
                media_body=media,
                fields="id,webViewLink",
            ).execute()
        except Exception as exc:
            _raise_drive_api_error(exc, "Drive replace failed.")
        if not isinstance(updated, dict):
            updated = {}
        return {
            "file_id": str(updated.get("id") or existing_id),
            "web_link": str(updated.get("webViewLink") or existing_link),
            "filename": upload_name,
            "created": False,
            "replaced": True,
        }

    try:
        created = drive_service.files().create(
            body={"name": upload_name, "parents": [parent_id]},
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
        "created": True,
        "replaced": False,
    }


def folder_web_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


# --- Read-only / rename primitives (used by the folder-name migration) -------
def find_folder(*, name: str, parent_id: str = "", owner_user_id: str = "", service: Any | None = None) -> str:
    """Return the id of a non-trashed folder of this exact ``name``, or ``""``.

    The read-only counterpart to :func:`find_or_create_folder`: it never creates.
    Scoped to ``parent_id`` when given. Used to locate the existing NDAs tree for
    the folder-name migration without writing anything.
    """
    folder_name = str(name or "").strip()
    if not folder_name:
        return ""
    drive_service = service or _drive_service(owner_user_id)
    query = (
        f"mimeType='{FOLDER_MIME}' "
        f"and name='{_escape_drive_query_value(folder_name)}' "
        "and trashed=false"
    )
    if parent_id:
        query += f" and '{_escape_drive_query_value(parent_id)}' in parents"
    try:
        listing = drive_service.files().list(
            q=query, fields="files(id,name)", pageSize=1, spaces="drive"
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive folder lookup failed.")
    files = listing.get("files") if isinstance(listing, dict) else None
    if isinstance(files, list) and files and isinstance(files[0], dict):
        return str(files[0].get("id") or "")
    return ""


def list_child_folders(*, parent_id: str, owner_user_id: str = "", service: Any | None = None) -> list[dict[str, str]]:
    """List the non-trashed sub*folders* of ``parent_id`` as ``[{"id","name"}]``.

    Pages through all results so a large counterparty/matter tree is fully
    enumerated. Returns ``[]`` when ``parent_id`` is empty.
    """
    if not parent_id:
        return []
    drive_service = service or _drive_service(owner_user_id)
    query = (
        f"mimeType='{FOLDER_MIME}' "
        f"and '{_escape_drive_query_value(parent_id)}' in parents "
        "and trashed=false"
    )
    folders: list[dict[str, str]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {
            "q": query,
            "fields": "nextPageToken, files(id,name)",
            "pageSize": 100,
            "spaces": "drive",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            listing = drive_service.files().list(**params).execute()
        except Exception as exc:
            _raise_drive_api_error(exc, "Drive folder listing failed.")
        if not isinstance(listing, dict):
            break
        for rec in listing.get("files") or []:
            if isinstance(rec, dict) and rec.get("id"):
                folders.append({"id": str(rec["id"]), "name": str(rec.get("name") or "")})
        page_token = str(listing.get("nextPageToken") or "")
        if not page_token:
            break
    return folders


def find_child_file(*, name: str, parent_id: str, owner_user_id: str = "", service: Any | None = None) -> str:
    """Return the id of a non-trashed, non-folder file named ``name`` under ``parent_id``."""
    file_name = str(name or "").strip()
    if not file_name or not parent_id:
        return ""
    drive_service = service or _drive_service(owner_user_id)
    query = (
        f"name='{_escape_drive_query_value(file_name)}' "
        f"and '{_escape_drive_query_value(parent_id)}' in parents "
        f"and mimeType!='{FOLDER_MIME}' "
        "and trashed=false"
    )
    try:
        listing = drive_service.files().list(
            q=query, fields="files(id,name)", pageSize=1, spaces="drive"
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive file lookup failed.")
    files = listing.get("files") if isinstance(listing, dict) else None
    if isinstance(files, list) and files and isinstance(files[0], dict):
        return str(files[0].get("id") or "")
    return ""


def download_file_bytes(*, file_id: str, owner_user_id: str = "", service: Any | None = None) -> bytes:
    """Download a Drive file's raw content. Returns ``b""`` when unavailable."""
    if not file_id:
        return b""
    drive_service = service or _drive_service(owner_user_id)
    try:
        content = drive_service.files().get_media(fileId=file_id).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive file download failed.")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return b""


def rename_file(*, file_id: str, new_name: str, owner_user_id: str = "", service: Any | None = None) -> dict[str, str]:
    """Rename a Drive file/folder via ``files().update``. Returns ``{"id","name"}``."""
    if not file_id:
        raise DriveIntegrationError("A Drive file id is required to rename.")
    rename_to = str(new_name or "").strip()
    if not rename_to:
        raise DriveIntegrationError("A new name is required to rename.")
    drive_service = service or _drive_service(owner_user_id)
    try:
        updated = drive_service.files().update(
            fileId=file_id, body={"name": rename_to}, fields="id,name"
        ).execute()
    except Exception as exc:
        _raise_drive_api_error(exc, "Drive rename failed.")
    if not isinstance(updated, dict):
        updated = {}
    return {"id": str(updated.get("id") or file_id), "name": str(updated.get("name") or rename_to)}


def sync_matter_folder(
    *,
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str = "",
    drive_token_owner_user_id: str | None = None,
    root_folder_id: str = "",
    synced_at: str = "",
    signed_via: str = "",
    service: Any | None = None,
    get_artifact_bytes: Any | None = None,
) -> dict[str, Any]:
    """Mirror a matter's artifacts into a structured Drive folder (idempotent).

    Builds (or finds) the ``{root}/{counterparty}/{matter}/`` tree plus a
    ``metadata/`` subfolder, then for each artifact in
    :func:`artifact_registry.matter_artifacts` order computes its grammar filename,
    fetches its bytes, and uploads it (skipping any already present). Finally it
    writes ``metadata/matter_summary.json``.

    Two DISTINCT owner ids are threaded here because they answer different
    questions:

    * ``owner_user_id`` — the MATTER/artifact owner. Used to read the artifact
      bytes back (``get_artifact_bytes``) from the owner-scoped document store.
    * ``drive_token_owner_user_id`` — the GOOGLE-token owner whose Drive credential
      the upload authenticates as. Resolved the same way the deliberate
      Save-to-Drive route resolves it (``google_connection.connected_owner_user_id``
      → the per-user token; empty string → the server-global token for the
      no-login / local-demo path). Defaults to ``owner_user_id`` for backward
      compatibility when a caller does not distinguish the two.

    ``root_folder_id`` (the admin setting) is used as the PARENT of the app-created
    ``NDAs`` root when provided; the ``NDAs`` root itself is always app-created so
    the whole tree is app-owned and visible under ``drive.file``.

    ``synced_at`` is passed in (the route stamps it) so this helper stays pure and
    testable. ``signed_via`` ("docusign" / "uploaded") records HOW a now-executed
    matter was signed; it tags the signed file's name (``_uploaded`` suffix for an
    externally-signed paper copy) and the durable summary's ``signed_via`` facet.
    ``get_artifact_bytes`` defaults to the live artifact service but is injectable
    for tests.

    Returns ``{matter_folder_id, matter_folder_url, synced_count, total_count,
    artifacts: [...]}`` where ``synced_count`` is the number of NEWLY uploaded
    artifact files (re-syncs report 0).
    """
    token_owner = owner_user_id if drive_token_owner_user_id is None else drive_token_owner_user_id
    drive_service = service or _drive_service(token_owner)
    if get_artifact_bytes is None:
        get_artifact_bytes = artifact_service.get_artifact_bytes

    artifacts = artifact_registry.matter_artifacts(matter)
    if not artifacts:
        raise DriveIntegrationError("Matter has no artifacts to save to Drive.")

    counterparty = derive_counterparty(matter)
    matter_folder_name = derive_matter_folder_name(matter, matter_id, counterparty)

    # Build the {root}/{counterparty}/{matter}/ tree. Each find_or_create is
    # idempotent, so re-running reuses the existing folders.
    root_id = _resolve_root_folder(
        root_folder_id=root_folder_id,
        owner_user_id=owner_user_id,
        service=drive_service,
    )
    counterparty_id = find_or_create_folder(
        name=counterparty, parent_id=root_id, service=drive_service
    )
    matter_folder_id = find_or_create_folder(
        name=matter_folder_name, parent_id=counterparty_id, service=drive_service
    )
    metadata_folder_id = find_or_create_folder(
        name=METADATA_FOLDER_NAME, parent_id=matter_folder_id, service=drive_service
    )

    synced_count = 0
    artifact_records: list[dict[str, Any]] = []
    for sequence, artifact in enumerate(artifacts, start=1):
        filename = _drive_filename_for_artifact(sequence, artifact, signed_via=signed_via)
        file_bytes = get_artifact_bytes(matter_id, artifact.id, owner_user_id=owner_user_id)
        if not file_bytes:
            # An artifact without retrievable bytes is recorded (so the summary
            # is complete) but cannot be uploaded; skip the upload.
            artifact_records.append(
                _artifact_record(artifact, sequence, filename, {"file_id": "", "web_link": ""})
            )
            continue
        uploaded = upload_or_replace_file(
            file_bytes=file_bytes,
            filename=filename,
            parent_id=matter_folder_id,
            mimetype=_mimetype_for_ext(artifact.ext),
            service=drive_service,
        )
        if uploaded.get("created"):
            synced_count += 1
        artifact_records.append(_artifact_record(artifact, sequence, filename, uploaded))

    matter_folder_url = folder_web_url(matter_folder_id)
    summary = _matter_summary(
        matter=matter,
        matter_id=matter_id,
        counterparty=counterparty,
        matter_folder_url=matter_folder_url,
        synced_at=synced_at,
        artifact_records=artifact_records,
        signed_via=signed_via,
    )
    summary_bytes = _json_bytes(summary)
    # The summary carries a FIXED name (``matter_summary.json``) but mutable
    # CONTENT — it must be truly overwritten on every re-sync (e.g. once a matter
    # becomes executed/signed) instead of skipped on the name hit, or a /tmp-wipe
    # corpus rebuild would read a stale, pre-signed summary and show the matter
    # unsigned. ``replace_existing`` routes it through ``files().update``.
    upload_or_replace_file(
        file_bytes=summary_bytes,
        filename=MATTER_SUMMARY_FILENAME,
        parent_id=metadata_folder_id,
        mimetype=JSON_MIME,
        service=drive_service,
        replace_existing=True,
    )

    return {
        "matter_folder_id": matter_folder_id,
        "matter_folder_url": matter_folder_url,
        "synced_count": synced_count,
        "total_count": len(artifacts),
        "artifacts": [_public_artifact_record(record) for record in artifact_records],
    }


def drive_token_owner_for_matter(owner_user_id: str) -> str:
    """Resolve which Google-token owner authenticates the Drive upload for a matter.

    Used by the unattended executed-transition paths that have NO session/handler
    to read ``current_user`` from (the DocuSign webhook; the lifecycle archivers).
    The matter's own ``owner_user_id`` is the matter/artifact owner, NOT necessarily
    the Drive-token owner — so threading it straight into the Drive layer is the
    wrong-identity bug this resolves.

    Resolution mirrors the deliberate Save-to-Drive route's
    ``google_connection.connected_owner_user_id`` outcome WITHOUT a request object:

    * If the matter owner has a usable per-user Drive credential, archive as them.
    * Otherwise fall back to the SERVER-GLOBAL token (``""``) — which is also the
      no-login / local-demo path (``drive_connected("")`` resolves the env/local
      ``data/google`` token). This keeps the local demo working unchanged.

    Returns ``""`` when neither a per-user nor a server-global token exists; the
    caller's connected-gate then skips the archive cleanly.
    """
    owner = str(owner_user_id or "").strip()
    if owner and drive_connected(owner):
        return owner
    return ""


def archive_executed_matter(
    *,
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str,
    repository: Any,
    drive_token_owner_user_id: str | None = None,
    signed_via: str = "",
) -> None:
    """Mirror a fully-executed matter into its Drive folder (STRICTLY best-effort).

    The single archiver behind every executed transition (DocuSign poll + webhook,
    signed-upload, manual mark-executed). Gated on Drive being connected for the
    resolved token owner AND Drive auto-intake being enabled; on success it runs
    :func:`sync_matter_folder` (executed PDF + a freshly-overwritten
    ``matter_summary.json``) and stamps the ``drive`` pointer back onto the matter.

    Two distinct owners (see :func:`sync_matter_folder`): ``owner_user_id`` is the
    MATTER owner (reads artifact bytes + write-back); ``drive_token_owner_user_id``
    is the GOOGLE-token owner the upload authenticates as. When the latter is not
    supplied it is resolved via :func:`drive_token_owner_for_matter` (per-user token
    → else server-global ``""``), which is the #10 wrong-identity fix.

    This function NEVER raises and is always logged: a skip (not connected /
    auto-intake off / no token) or a failure (sync raised) is recorded via
    telemetry AND a log line so the previously-silent miss is observable. It must
    not break the executed transition that already persisted before this ran.
    """
    from . import app_settings, telemetry

    token_owner = (
        drive_token_owner_for_matter(owner_user_id)
        if drive_token_owner_user_id is None
        else drive_token_owner_user_id
    )

    try:
        connected = drive_connected(token_owner)
        auto_intake = app_settings.drive_auto_intake_enabled()
    except Exception:
        telemetry.increment("drive_oncomplete_skipped")
        logger.warning(
            "Drive archive skipped for matter %s (signed_via=%s): gate read failed.",
            matter_id,
            signed_via or "unknown",
        )
        return
    if not connected or not auto_intake:
        telemetry.increment("drive_oncomplete_skipped")
        logger.info(
            "Drive archive skipped for matter %s (signed_via=%s): "
            "drive_connected=%s auto_intake=%s token_owner=%r.",
            matter_id,
            signed_via or "unknown",
            connected,
            auto_intake,
            token_owner,
        )
        return

    try:
        root_folder_id = str(app_settings.drive_settings().get("folder_id") or "")
    except Exception:
        root_folder_id = ""

    # Read artifact bytes from the SAME repository that holds this matter (not a
    # fresh default DiskMatterRepository), so the signed PDF resolves whatever
    # repository the executed transition threaded through.
    def _bytes_from_repository(an_matter_id, artifact_id, *, owner_user_id=""):
        return artifact_service.get_artifact_bytes(
            an_matter_id,
            artifact_id,
            repository=repository,
            owner_user_id=owner_user_id,
        )

    try:
        synced_at = _now_iso()
        synced = sync_matter_folder(
            matter=matter,
            matter_id=matter_id,
            owner_user_id=owner_user_id,
            drive_token_owner_user_id=token_owner,
            root_folder_id=root_folder_id,
            synced_at=synced_at,
            signed_via=signed_via,
            get_artifact_bytes=_bytes_from_repository,
        )
        repository.update_matter_fields(
            matter_id,
            {
                "drive": {
                    "matter_folder_id": synced["matter_folder_id"],
                    "matter_folder_url": synced["matter_folder_url"],
                    "synced_at": synced_at,
                    "artifacts": synced["artifacts"],
                },
                # Record the SUCCESSFUL archive outcome so the UI can clear any prior
                # failed-archive warning (and never shows a stale one). Written from
                # the same point that stamps the ``drive`` pointer, so the two never
                # disagree. Best-effort: a write-back failure here is swallowed below.
                "drive_archive": {
                    "status": "ok",
                    "error": "",
                    "attempted_at": synced_at,
                },
            },
            owner_user_id=owner_user_id,
        )
        telemetry.increment("drive_oncomplete_synced")
        telemetry.increment("drive_files_synced", amount=int(synced.get("synced_count") or 0))
    except Exception as error:
        telemetry.increment("drive_oncomplete_failed")
        logger.warning(
            "Drive archive failed for matter %s (signed_via=%s, token_owner=%r); "
            "executed transition is unaffected.",
            matter_id,
            signed_via or "unknown",
            token_owner,
            exc_info=True,
        )
        # Record the FAILED archive outcome onto the matter so the previously-silent
        # miss becomes user-visible (a non-blocking "Signed, but Drive archive
        # failed" warning + Retry). STRICTLY best-effort + isolated: the executed
        # transition already persisted, and this recorder must never raise out of the
        # archiver (a failed write-back just leaves no block — fail-open to no
        # warning, never a crash).
        try:
            repository.update_matter_fields(
                matter_id,
                {
                    "drive_archive": {
                        "status": "failed",
                        "error": _short_archive_error(error),
                        "attempted_at": _now_iso(),
                    }
                },
                owner_user_id=owner_user_id,
            )
        except Exception:  # pragma: no cover - defensive; recorder must never raise
            logger.warning(
                "Failed to record drive_archive failure onto matter %s.",
                matter_id,
                exc_info=True,
            )


def _short_archive_error(error: BaseException) -> str:
    """A short, user-safe reason string for a failed Drive archive.

    Keeps the matter-stored ``drive_archive.error`` compact (the UI shows it inline)
    and never leaks a multi-line stack — that detail stays in the logged warning.
    """
    text = " ".join(str(error or "").split()).strip()
    if not text:
        text = type(error).__name__
    return text[:200]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _resolve_root_folder(
    *,
    root_folder_id: str,
    owner_user_id: str = "",
    service: Any | None = None,
) -> str:
    """Resolve the parent under which the matter tree is filed.

    Always returns an app-created ``NDAs`` folder id. When ``root_folder_id`` (the
    admin setting) is set we create ``NDAs`` INSIDE it (so the app owns ``NDAs``
    and everything below, satisfying drive.file, while still nesting under the
    admin-chosen folder when that folder is itself writable). When it is empty we
    create ``NDAs`` in My Drive root.
    """
    parent = str(root_folder_id or "").strip()
    return find_or_create_folder(
        name=DEFAULT_ROOT_FOLDER_NAME,
        parent_id=parent,
        owner_user_id=owner_user_id,
        service=service,
    )


# The counterparty-name derivation lives in :mod:`artifact_registry` (a neutral
# leaf module that already owns artifact metadata) so the Drive filing layer and
# the UI's public_matter view read the SAME best-available name from ONE source of
# truth. Re-exported here under its historical name for callers in this module and
# any importers of ``drive_integration.derive_counterparty``. Folder naming is
# unchanged: ``_counterparty_safe_name`` in the registry is the same sanitiser as
# this module's ``_drive_safe_name``, so the derived counterparty is byte-identical.
derive_counterparty = artifact_registry.derive_counterparty


# Human-readable per-matter folder name: ``{YYYY-MM-DD} · {title} · {ref}``.
# The separator is a spaced middle dot; ``·`` survives ``_drive_safe_name`` (it is
# neither a path separator nor a control char) and is a valid Drive/local filename
# character. The leading date keeps folders chronologically sorted within a
# counterparty; the short ref disambiguates same-day, same-title NDAs.
FOLDER_NAME_SEPARATOR = " · "
# A title longer than this is trimmed so folder names stay manageable.
_MAX_TITLE_LENGTH = 60
# Length of the short disambiguating ref code appended to a folder name.
_REF_CODE_LENGTH = 4
# Placeholder titles that upstream code emits when nothing better is known; these
# must never leak into a folder name as if they were the document's real title.
_PLACEHOLDER_TITLES = {"untitled nda"}


def derive_matter_folder_name(matter: dict[str, Any], matter_id: str, counterparty: str) -> str:
    """``{YYYY-MM-DD} · {document title} · {ref}`` — a name a person can read.

    ``counterparty`` is accepted for call-site compatibility but deliberately
    excluded from the name: it is already the PARENT folder, so echoing it inside
    adds no information. The pieces are:

    * **date** — the matter's ``created_at`` date (omitted when unrecorded);
    * **title** — the document title (the original filename stem), falling back to
      the email subject, then ``"NDA"``;
    * **ref** — a short, stable disambiguator derived from the matter id so two
      same-day, same-title NDAs get distinct folders that never collide (Drive
      matches folders by exact name, and a collision would merge two matters into
      one folder).
    """
    date_part = _created_date(matter)
    title = _matter_title(matter)
    ref = _matter_ref_code(str(matter_id or "").strip() or str(matter.get("id") or "").strip())
    parts = [part for part in (date_part, title, ref) if part]
    return _drive_safe_name(FOLDER_NAME_SEPARATOR.join(parts)) or ref or "NDA"


def _matter_title(matter: dict[str, Any]) -> str:
    """The best human label for a matter: document title, else subject, else "NDA".

    Both ``document_title`` (the original filename stem) and ``subject`` default to
    a placeholder upstream when nothing better is known; that placeholder is treated
    as "no title" so it never reaches a folder name.
    """
    for field in ("document_title", "subject"):
        # Replace the field separator inside a title with a hyphen so it cannot be
        # mistaken for a real field boundary in the folder name.
        value = _drive_safe_name(matter.get(field)).replace("·", "-")
        value = " ".join(value.split())
        if value and value.casefold() not in _PLACEHOLDER_TITLES:
            return _truncate_title(value)
    return "NDA"


def _truncate_title(value: str) -> str:
    """Cap a title at ``_MAX_TITLE_LENGTH`` without slicing a word in half.

    A naive ``[:60]`` slice can leave a dangling fragment (e.g. ``"... 2025) ("``).
    Instead we cut back to the last whole word inside the cap and strip any opening
    bracket / separator left at the edge.
    """
    if len(value) <= _MAX_TITLE_LENGTH:
        return value
    cut = value[:_MAX_TITLE_LENGTH]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    cut = cut.rstrip(" -–—([{·/,;:")
    return cut or value[:_MAX_TITLE_LENGTH].strip()


def _matter_ref_code(key: str) -> str:
    """A short, stable, lowercase disambiguator derived from the matter id.

    Uses the trailing alphanumerics of the id (the random part of ``matter_<hex>``);
    for an empty or too-short id it falls back to a deterministic hash so the code is
    always exactly ``_REF_CODE_LENGTH`` characters.
    """
    cleaned = "".join(ch for ch in str(key or "") if ch.isalnum()).lower()
    if len(cleaned) >= _REF_CODE_LENGTH:
        return cleaned[-_REF_CODE_LENGTH:]
    digest = hashlib.sha1(str(key or "matter").encode("utf-8")).hexdigest()
    return digest[:_REF_CODE_LENGTH]


def _created_date(matter: dict[str, Any]) -> str:
    created_at = str(matter.get("created_at") or "").strip()
    if not created_at:
        return ""
    # created_at is an ISO-8601 timestamp; the date is the leading YYYY-MM-DD.
    candidate = created_at[:10]
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return candidate
    return ""


def _drive_safe_name(value: object) -> str:
    """Sanitise a folder/file display name: strip control chars + path separators.

    Keeps the name human-readable (spaces and most punctuation preserved) but
    removes characters that break a Drive path or a local mirror: slashes,
    backslashes, NUL/control characters. Collapses whitespace and trims.
    """
    text = str(value or "")
    cleaned = []
    for ch in text:
        if ch in ("/", "\\", "\x00") or ord(ch) < 32:
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    return " ".join("".join(cleaned).split()).strip()


def _drive_filename_for_artifact(
    sequence: int,
    artifact: artifact_registry.Artifact,
    *,
    signed_via: str = "",
) -> str:
    """Build the lifecycle-grammar filename ``{NN}_{stage}[_v{N}].{ext}``.

    The registry stays the source of truth for sequence/actor/role/version/ext;
    the chronological ``stage`` is derived from the artifact's ``(role, actor)``
    via :func:`artifact_registry.stage_for` (e.g. counterparty ``original`` ->
    ``received``; our-org ``original``/``generated`` -> ``draft``). Repeatable
    stages carry a ``_v{N}`` suffix; one-shot stages (received, draft, signed) do
    not. The chronological ``sequence`` is the enumeration order at sync time.

    When the matter was executed via an externally-signed UPLOAD
    (``signed_via == "uploaded"``) the SIGNED artifact's stage is suffixed
    ``signed_uploaded`` so the archived file reads ``NN_signed_uploaded.pdf`` —
    visibly distinct in Drive from a plain DocuSign-signed ``NN_signed.pdf``.
    """
    stage = artifact_registry.stage_for(artifact.role, artifact.actor)
    if stage == artifact_registry.STAGE_SIGNED and str(signed_via or "").strip().lower() == "uploaded":
        stage = f"{stage}_uploaded"
    return artifact_registry.stage_filename(sequence, stage, artifact.version, artifact.ext)


def _mimetype_for_ext(ext: str) -> str:
    return DOCX_MIME if str(ext or "").lower() != "pdf" else "application/pdf"


def _artifact_record(
    artifact: artifact_registry.Artifact,
    sequence: int,
    filename: str,
    uploaded: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "sequence": sequence,
        "actor": artifact.actor,
        "role": artifact.role,
        "version": artifact.version,
        "filename": filename,
        "drive_file_id": str(uploaded.get("file_id") or ""),
        "drive_file_url": str(uploaded.get("web_link") or ""),
        "based_on_artifact_id": artifact.based_on_artifact_id,
        "created_at": artifact.created_at,
    }


def _public_artifact_record(record: dict[str, Any]) -> dict[str, Any]:
    # The response + persisted-summary records share the same shape.
    return dict(record)


def _matter_summary(
    *,
    matter: dict[str, Any],
    matter_id: str,
    counterparty: str,
    matter_folder_url: str,
    synced_at: str,
    artifact_records: list[dict[str, Any]],
    signed_via: str = "",
) -> dict[str, Any]:
    workflow_state: dict[str, Any] = {}
    try:
        from . import workflow

        workflow_state = workflow.workflow_state(matter)
    except Exception:
        workflow_state = {}
    return {
        "matter_id": matter_id,
        "counterparty": counterparty,
        "created_at": str(matter.get("created_at") or ""),
        "gmail_thread_id": str(matter.get("gmail_thread_id") or ""),
        "workflow_state": workflow_state,
        "matter_folder_url": matter_folder_url,
        "synced_at": synced_at,
        # Durable rich facets so a /tmp-wiped matter still searches by governing law,
        # signed-state, clauses and term after a Drive re-sync (corpus_index reads
        # this block; its presence is what flips facets_available true). Computed from
        # the same matter; any derivation hiccup is swallowed (like workflow_state) so
        # a facet failure never breaks the sync.
        "facets": _summary_facets(matter, workflow_state, signed_via=signed_via),
        "artifacts": [_public_artifact_record(record) for record in artifact_records],
    }


def _summary_facets(
    matter: dict[str, Any],
    workflow_state: dict[str, Any],
    *,
    signed_via: str = "",
) -> dict[str, Any]:
    """Compute the durable ``facets`` block for matter_summary.json.

    Mirrors corpus_index's app-state derivation but lands the values on disk so a
    Drive-only matter (after a /tmp wipe) keeps them. Each facet degrades to its
    empty/null value on failure; the whole block is best-effort.
    """
    governing_law = ""
    try:
        from . import governing_law_view

        governing_law = governing_law_view.derive_governing_law(matter)
    except Exception:
        governing_law = ""
    failed, needs_review = _summary_requirement_counts(matter)
    has_clauses = _summary_clause_ids(matter)
    term_years = _summary_term_years(matter)
    master = _summary_master_filter_facets(matter, has_clauses, term_years)
    return {
        "governing_law": governing_law,
        "signed": _summary_signed(workflow_state),
        # HOW a now-signed matter was executed: "docusign" (e-signed through our
        # DocuSign flow) vs "uploaded" (an externally / paper-signed PDF was
        # uploaded). "" when not yet signed or unknown. Persisted so a Drive-only
        # matter (after a /tmp wipe) keeps the provenance at a glance.
        "signed_via": _summary_signed_via(matter, signed_via),
        "has_clauses": has_clauses,
        "term_years": term_years,
        # The review requirement counts the has_issues facet reads. Persisted here so
        # a Drive-only matter (after a /tmp wipe) keeps the signal; corpus_index's
        # _drive_facets reads them back from this block.
        "requirements_failed": failed,
        "requirements_needs_review": needs_review,
        # Whether an AI (ai_first) review actually ran. Persisted so the has_issues
        # consumer can gate on it even for a Drive-only matter; corpus_index's
        # _drive_facets reads it back (and treats its absence as False).
        "ai_review_ran": _summary_ai_review_ran(matter),
        # Master-filter facets, derived by the SAME corpus_index helpers the app-state
        # pass uses (single source of truth), landed on disk so a Drive-only matter
        # (after a /tmp wipe) keeps them. corpus_index._drive_facets reads them back.
        "mutuality": master["mutuality"],
        "term_band": master["term_band"],
        "restraint_types": master["restraint_types"],
        "review_outcome": master["review_outcome"],
        "clauses_present": master["clauses_present"],
        "origin": master["origin"],
        "schema_version": 1,
    }


def _summary_master_filter_facets(
    matter: dict[str, Any], has_clauses: list[str], term_years: float | None
) -> dict[str, Any]:
    """Derive the 6 master-filter facets for the durable summary.

    Delegates to ``corpus_index`` so the durable values and the app-state values come
    from ONE implementation and cannot drift. Lazy-imported (corpus_index imports this
    module) and fully best-effort: any hiccup degrades every facet to its null/empty
    default so a sync never breaks.
    """
    try:
        from . import corpus_index

        review_result = matter.get("review_result")
        return {
            "mutuality": corpus_index._mutuality_from_result(review_result),
            "term_band": corpus_index._term_band_from_years(term_years),
            "restraint_types": corpus_index._restraint_types_from_result(review_result),
            "review_outcome": corpus_index._review_outcome_from_result(review_result),
            "clauses_present": list(has_clauses),
            "origin": corpus_index._origin_from_source(
                matter.get("source_type"), has_gmail=bool(matter.get("gmail_message_id"))
            ),
        }
    except Exception:
        return {
            "mutuality": None,
            "term_band": None,
            "restraint_types": [],
            "review_outcome": None,
            "clauses_present": list(has_clauses) if isinstance(has_clauses, list) else [],
            "origin": None,
        }


def _summary_ai_review_ran(matter: dict[str, Any]) -> bool:
    """True when an AI (ai_first) review actually ran for this matter.

    Best-effort wrapper over ``review_state.review_was_ai_executed`` (the same signal
    matter_view gates ai_review_ran on). Persisted into the durable facets block so a
    Drive-only matter keeps the signal; never raises (a bad review_result -> False).
    """
    try:
        from . import review_state

        return review_state.review_was_ai_executed(matter.get("review_result"))
    except Exception:
        return False


def _summary_requirement_counts(matter: dict[str, Any]) -> tuple[int, int]:
    """The (failed, needs_review) requirement counts from the stored review result.

    Best-effort, mirroring corpus_index's app-state derivation; absent/odd shapes
    degrade to (0, 0). Never raises.

    GATE: surface the counts ONLY when an AI (ai_first) review actually ran (same gate
    as corpus_index._app_requirement_counts). A deterministic-only matter would
    otherwise persist deterministic requirement counts to matter_summary.json that
    leak into the corpus "has issues" search; gating returns (0, 0) so the durable
    summary carries no deterministic verdict.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return 0, 0
    if not _summary_ai_review_ran(matter):
        return 0, 0

    def _coerce(value: object) -> int:
        try:
            result = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    return _coerce(review_result.get("requirements_failed")), _coerce(review_result.get("requirements_needs_review"))


def _summary_signed(workflow_state: dict[str, Any]) -> bool | None:
    """status -> signed bool/null. fully_signed=true; sent/awaiting/counter/sending=
    false; pre-send=null (so a signed filter never silently includes it)."""
    status = ""
    if isinstance(workflow_state, dict):
        status = str(workflow_state.get("status") or "").strip().lower()
    if status == "fully_signed":
        return True
    if status in ("sent_awaiting_counterparty", "counter_received", "sending"):
        return False
    return None


def _summary_signed_via(matter: dict[str, Any], signed_via: str = "") -> str:
    """How the matter was executed: "docusign" / "uploaded" / "".

    Prefers the explicit ``signed_via`` the executed transition passed in. When a
    re-sync supplies none, falls back to the matter's own durable signals: a
    DocuSign envelope on the matter means "docusign"; a captured signed-upload
    artifact (``metadata.captured_via == "signed_upload"`` with no DocuSign
    envelope) means "uploaded". Empty when not yet signed / unknown.
    """
    explicit = str(signed_via or "").strip().lower()
    if explicit in ("docusign", "uploaded"):
        return explicit
    if not isinstance(matter, dict):
        return ""
    signature = matter.get("docusign")
    if isinstance(signature, dict) and signature.get("envelope_id"):
        return "docusign"
    try:
        for artifact in artifact_registry.matter_artifacts(matter):
            if artifact.role != artifact_registry.ROLE_SIGNED:
                continue
            metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
            if str(metadata.get("captured_via") or "") == "signed_upload":
                return "uploaded"
    except Exception:
        return ""
    return ""


def _summary_clause_ids(matter: dict[str, Any]) -> list[str]:
    """Flatten the matter's review_state clause_ids (pass+review+check), preferring a
    stored review_state and re-deriving from review_result only when absent."""
    try:
        from . import review_state

        clause_ids: dict[str, Any] | None = None
        stored = matter.get("review_state")
        if isinstance(stored, dict) and isinstance(stored.get("clause_ids"), dict):
            clause_ids = stored["clause_ids"]
        else:
            review_result = matter.get("review_result")
            if isinstance(review_result, dict):
                derived = review_state.review_state_from_result(review_result)
                if isinstance(derived, dict) and isinstance(derived.get("clause_ids"), dict):
                    clause_ids = derived["clause_ids"]
        if not isinstance(clause_ids, dict):
            return []
        seen: dict[str, None] = {}
        for bucket in ("pass", "review", "check"):
            ids = clause_ids.get(bucket)
            if isinstance(ids, list):
                for clause_id in ids:
                    token = str(clause_id or "").strip()
                    if token:
                        seen.setdefault(token, None)
        return list(seen)
    except Exception:
        return []


def _summary_term_years(matter: dict[str, Any]) -> float | None:
    """Best-effort ordinary term in years from the stored term_and_survival clause
    result's persisted ``term_years`` scalar; absent/odd -> None."""
    try:
        review_result = matter.get("review_result")
        if not isinstance(review_result, dict):
            return None
        clauses = review_result.get("clauses")
        if not isinstance(clauses, list):
            return None
        for clause in clauses:
            if not isinstance(clause, dict) or str(clause.get("id") or "") != "term_and_survival":
                continue
            value = clause.get("term_years")
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        return None
    except Exception:
        return None


def _json_bytes(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


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
        google_connection.credentials_for_role("drive", owner_user_id=owner_user_id, integration_label="Drive")
    except google_connection.GoogleConnectionError:
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
