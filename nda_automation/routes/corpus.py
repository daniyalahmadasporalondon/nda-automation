"""Corpus tab routes — a read-only filing-cabinet view of the user's NDA corpus.

``GET /api/corpus`` returns the counterparty -> matter -> artifact index built by
:mod:`corpus_index` (app-state + Drive reconciliation). ``?refresh=1`` forces a
fresh Drive crawl; otherwise a warm per-owner cache is served. There are no other
params in v1 (no search/filter).

``GET /api/corpus/artifacts/<matter_id>/<artifact_id>`` streams an app-state
artifact's bytes for download. Drive-only artifacts have no app-state bytes — the
frontend links out to their ``drive_file_url`` instead.

Both routes run AFTER ``_authorize_request`` + ``_rate_limit_request`` (auth +
rate-limit; CSRF is N/A for GET), exactly like ``/api/matters``. Tenancy mirrors
the dashboard: app-state is read with ``request_owner_user_id`` (already
tenant-filtered by the repository), and the Drive crawl uses the signed-in user's
own ``drive`` OAuth token whose ``drive.file`` scope only exposes folders this app
created for this user — so cross-tenant Drive leakage is structurally impossible.

This module is pure orchestration; the index-building logic lives in the
:mod:`corpus_index` leaf so it is testable without HTTP.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from .. import (
    artifact_registry,
    artifact_service,
    corpus_index,
    google_connection,
    telemetry,
)
from ..docx_export import DOCX_MIME
from ..matter_repository import DiskMatterRepository, MatterRepository, MatterRepositoryError
from .common import request_owner_user_id


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def _google_owner_user_id(handler) -> str:
    return google_connection.connected_owner_user_id(
        getattr(handler, "current_user", None),
        owner_user_id=request_owner_user_id(handler),
    )


def handle_corpus(handler, *, send_body: bool = True) -> None:
    """Return the corpus index for the signed-in user (owner-scoped)."""
    telemetry.increment("corpus_index_requests")
    owner_user_id = request_owner_user_id(handler)
    drive_owner_user_id = _google_owner_user_id(handler)
    repository = _repository(handler)
    force_refresh = _wants_refresh(handler.path)

    try:
        payload = corpus_index.build_corpus(
            repository,
            owner_user_id,
            drive_owner_user_id,
            drive_service=getattr(handler, "corpus_drive_service", None),
            force_refresh=force_refresh,
        )
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return

    handler._send_json(payload, send_body=send_body)


def handle_corpus_artifact_download(handler, path: str, *, send_body: bool = True) -> None:
    """Stream an app-state artifact's bytes for the Corpus tab Download action."""
    matter_id, artifact_id = _parse_artifact_path(path)
    if not matter_id or not artifact_id:
        handler._send_json({"error": "Artifact not found."}, status=404, send_body=send_body)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    try:
        matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return

    artifact = artifact_registry.find_artifact(matter, artifact_id)
    if artifact is None:
        handler._send_json({"error": "Artifact not found."}, status=404, send_body=send_body)
        return

    try:
        artifact_bytes = artifact_service.get_artifact_bytes(
            matter_id, artifact_id, repository=repository, owner_user_id=owner_user_id
        )
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if not artifact_bytes:
        # A Drive-only artifact (or one whose bytes were wiped) has nothing to
        # download here; the FE uses its drive_file_url instead.
        handler._send_json({"error": "No document bytes for this artifact."}, status=404, send_body=send_body)
        return

    handler._send_download(
        artifact_bytes,
        artifact.name or f"{artifact_id}.{artifact.ext or 'docx'}",
        _content_type_for(artifact.ext),
        send_body=send_body,
    )


# --- helpers ---------------------------------------------------------------
def _wants_refresh(path: str) -> bool:
    query = urlparse(path).query
    if not query:
        return False
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == "refresh" and value in ("1", "true", "yes"):
            return True
    return False


def _parse_artifact_path(path: str) -> tuple[str, str]:
    """Parse ``/api/corpus/artifacts/<matter_id>/<artifact_id>`` -> (matter_id, artifact_id)."""
    prefix = "/api/corpus/artifacts/"
    only_path = urlparse(path).path
    if not only_path.startswith(prefix):
        return "", ""
    remainder = only_path.removeprefix(prefix).strip("/")
    parts = remainder.split("/")
    if len(parts) != 2:
        return "", ""
    matter_id = unquote(parts[0]).strip()
    artifact_id = unquote(parts[1]).strip()
    return matter_id, artifact_id


def _content_type_for(ext: str) -> str:
    return "application/pdf" if str(ext or "").strip().lower() == "pdf" else DOCX_MIME
