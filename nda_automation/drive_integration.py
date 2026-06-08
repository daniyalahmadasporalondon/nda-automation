"""Google Drive outbound integration — "Save NDA to Google Drive".

A thin client that files a matter's NDA documents into the signed-in user's
Google Drive, reusing the role-parameterized Google OAuth machinery in
``gmail_integration``. A dedicated ``"drive"`` OAuth role (added to
``gmail_integration.GMAIL_OAUTH_SCOPES_BY_ROLE``) carries the least-privilege
``drive.file`` scope, so this app can only ever see or modify files it creates,
never the user's whole Drive.

The OAuth token storage, refresh, locking and per-user token paths are all
reused from ``gmail_integration`` via ``_credentials_for_role("drive", ...)`` —
this module only adds the Drive API calls and the Drive-specific error taxonomy
that mirrors the Gmail one.

Drive v2 — structured per-matter filing
========================================
Beyond the original flat single-file upload (``upload_docx_to_drive``), this
module mirrors the matter's :mod:`artifact_registry` into a per-matter folder
tree::

    {root}/{counterparty}/{YYYY-MM-DD - counterparty - thread-or-matter-id}/
        01_counterparty_original_v1.docx
        02_agent_redline_v1.docx
        03_legal_reviewed_v1.docx
        ...
        metadata/matter_summary.json

Every artifact in :func:`artifact_registry.matter_artifacts` order becomes one
file, named by the registry grammar (:func:`artifact_registry.artifact_name`).
:func:`sync_matter_folder` is the orchestrator; it is idempotent — re-running it
creates no duplicate folders or files.

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

from io import BytesIO
from typing import Any

from . import artifact_registry, artifact_service, gmail_integration

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JSON_MIME = "application/json"
FOLDER_MIME = "application/vnd.google-apps.folder"

# The app-created root folder under My Drive that holds the whole NDA filing tree.
# Because drive.file only exposes app-created files, this root must be app-created.
DEFAULT_ROOT_FOLDER_NAME = "NDAs"
METADATA_FOLDER_NAME = "metadata"
MATTER_SUMMARY_FILENAME = "matter_summary.json"

# Presentation mapping from the registry's source-of-truth vocabulary to the
# human-readable filename vocabulary. The registry stays authoritative; this is a
# display layer only. ``ai`` -> ``agent`` (the AI redline agent), ``human`` ->
# ``legal`` (the legal reviewer), an entity slug / generated producer -> ``aspora``
# (our org), ``counterparty`` and ``system`` pass through unchanged.
ACTOR_DISPLAY = {
    artifact_registry.ACTOR_AI: "agent",
    artifact_registry.ACTOR_HUMAN: "legal",
    artifact_registry.ACTOR_COUNTERPARTY: "counterparty",
    "system": "system",
}
# Roles map mostly 1:1. ``generated`` reads as ``draft`` in the filename (the
# generated NDA is our first draft); ``counter`` stays ``counter`` (a counterparty
# redline is meaningfully distinct from our own AI redline in the file listing).
ROLE_DISPLAY = {
    artifact_registry.ROLE_GENERATED: "draft",
}


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
) -> dict[str, Any]:
    """Upload ``file_bytes`` as ``filename`` into ``parent_id`` (idempotent by name).

    Returns ``{"file_id", "web_link", "filename", "created": bool}``. IDEMPOTENT
    by name within the parent: the grammar makes filenames unique per matter, so a
    pre-existing file of that exact name means "already synced" — we SKIP the
    upload and return the existing id with ``created=False``. Only genuinely new
    filenames are uploaded (``created=True``).
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
    if isinstance(files, list) and files:
        existing = files[0]
        if isinstance(existing, dict) and existing.get("id"):
            return {
                "file_id": str(existing["id"]),
                "web_link": str(existing.get("webViewLink") or ""),
                "filename": upload_name,
                "created": False,
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
    }


def folder_web_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


def sync_matter_folder(
    *,
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str = "",
    root_folder_id: str = "",
    synced_at: str = "",
    service: Any | None = None,
    get_artifact_bytes: Any | None = None,
) -> dict[str, Any]:
    """Mirror a matter's artifacts into a structured Drive folder (idempotent).

    Builds (or finds) the ``{root}/{counterparty}/{matter}/`` tree plus a
    ``metadata/`` subfolder, then for each artifact in
    :func:`artifact_registry.matter_artifacts` order computes its grammar filename,
    fetches its bytes, and uploads it (skipping any already present). Finally it
    writes ``metadata/matter_summary.json``.

    ``root_folder_id`` (the admin setting) is used as the PARENT of the app-created
    ``NDAs`` root when provided; the ``NDAs`` root itself is always app-created so
    the whole tree is app-owned and visible under ``drive.file``.

    ``synced_at`` is passed in (the route stamps it) so this helper stays pure and
    testable. ``get_artifact_bytes`` defaults to the live artifact service but is
    injectable for tests.

    Returns ``{matter_folder_id, matter_folder_url, synced_count, total_count,
    artifacts: [...]}`` where ``synced_count`` is the number of NEWLY uploaded
    artifact files (re-syncs report 0).
    """
    drive_service = service or _drive_service(owner_user_id)
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
        filename = _drive_filename_for_artifact(sequence, artifact)
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
    )
    summary_bytes = _json_bytes(summary)
    upload_or_replace_file(
        file_bytes=summary_bytes,
        filename=MATTER_SUMMARY_FILENAME,
        parent_id=metadata_folder_id,
        mimetype=JSON_MIME,
        service=drive_service,
    )

    return {
        "matter_folder_id": matter_folder_id,
        "matter_folder_url": matter_folder_url,
        "synced_count": synced_count,
        "total_count": len(artifacts),
        "artifacts": [_public_artifact_record(record) for record in artifact_records],
    }


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


def derive_matter_folder_name(matter: dict[str, Any], matter_id: str, counterparty: str) -> str:
    """``{YYYY-MM-DD} - {counterparty} - {gmail_thread_id or matter_id}``.

    The date is the matter's ``created_at`` date (falling back to a bare matter id
    when no date is recorded). The thread id keys the folder to the email thread
    when one exists, else the matter id — both unique, so the folder is stable
    across re-syncs.
    """
    date_part = _created_date(matter)
    key = str(matter.get("gmail_thread_id") or "").strip() or str(matter_id or "").strip() or "matter"
    name = f"{date_part} - {counterparty} - {key}" if date_part else f"{counterparty} - {key}"
    return _drive_safe_name(name) or key


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


def _drive_filename_for_artifact(sequence: int, artifact: artifact_registry.Artifact) -> str:
    """Build the grammar filename for an artifact with the display vocabulary.

    The registry stays the source of truth for sequence/actor/role/version/ext;
    this only maps the actor/role through the presentation table before handing to
    :func:`artifact_registry.artifact_name`. An unmapped actor (e.g. an entity
    slug for a generated NDA) is rendered as ``aspora`` (our org produced it).
    """
    actor = _display_actor(artifact.actor)
    role = ROLE_DISPLAY.get(artifact.role, artifact.role)
    return artifact_registry.artifact_name(sequence, actor, role, artifact.version, artifact.ext)


def _display_actor(actor: str) -> str:
    mapped = ACTOR_DISPLAY.get(actor)
    if mapped:
        return mapped
    # Any non-empty, non-standard actor is an entity slug / generated producer
    # (a generated NDA names the producing entity) -> our org.
    return "aspora" if actor else "actor"


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
        "artifacts": [_public_artifact_record(record) for record in artifact_records],
    }


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
