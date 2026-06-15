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
from typing import Any

from . import artifact_registry, artifact_service, gmail_integration, google_connection

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


def _drive_filename_for_artifact(sequence: int, artifact: artifact_registry.Artifact) -> str:
    """Build the lifecycle-grammar filename ``{NN}_{stage}[_v{N}].{ext}``.

    The registry stays the source of truth for sequence/actor/role/version/ext;
    the chronological ``stage`` is derived from the artifact's ``(role, actor)``
    via :func:`artifact_registry.stage_for` (e.g. counterparty ``original`` ->
    ``received``; our-org ``original``/``generated`` -> ``draft``). Repeatable
    stages carry a ``_v{N}`` suffix; one-shot stages (received, draft, signed) do
    not. The chronological ``sequence`` is the enumeration order at sync time.
    """
    stage = artifact_registry.stage_for(artifact.role, artifact.actor)
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
        "facets": _summary_facets(matter, workflow_state),
        "artifacts": [_public_artifact_record(record) for record in artifact_records],
    }


def _summary_facets(matter: dict[str, Any], workflow_state: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "governing_law": governing_law,
        "signed": _summary_signed(workflow_state),
        "has_clauses": _summary_clause_ids(matter),
        "term_years": _summary_term_years(matter),
        # The review requirement counts the has_issues facet reads. Persisted here so
        # a Drive-only matter (after a /tmp wipe) keeps the signal; corpus_index's
        # _drive_facets reads them back from this block.
        "requirements_failed": failed,
        "requirements_needs_review": needs_review,
        "schema_version": 1,
    }


def _summary_requirement_counts(matter: dict[str, Any]) -> tuple[int, int]:
    """The (failed, needs_review) requirement counts from the stored review result.

    Best-effort, mirroring corpus_index's app-state derivation; absent/odd shapes
    degrade to (0, 0). Never raises.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
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
