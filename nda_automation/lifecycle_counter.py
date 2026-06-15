"""Lifecycle stage: COUNTER (a counterparty redline/counter, repeatable + versioned).

The ``hook/counter`` hook agent owns this module. The CORE agent owns the shared
surfaces (the artifact-registry ``counter`` role, the Drive naming grammar, and
the registered POST ``/api/matters/{id}/counter`` route -> this module's
:func:`handle_counter_upload`); this module fills ONLY the lifecycle behaviour
behind that route. It must not touch a sibling hook module or any shared file the
core already owns.

Contract implemented here:
    capture_counter_artifact(repository, matter_id, owner_user_id, counter_bytes,
                             filename) -> Artifact | None
        Register a COUNTER artifact (versioned via
        ``artifact_registry.next_version_for_role(matter, ROLE_COUNTER)``) and
        store the counter-document bytes so ``artifact_service.get_artifact_bytes``
        returns them. Returns the new ``Artifact`` (or ``None`` when there is
        nothing to capture). Owner-scoped: an unowned matter resolves to None.

    handle_counter_upload(handler, path) -> None
        The route body for POST ``/api/matters/{id}/counter`` (upload a
        counterparty counter). Owner-scoped like the sibling matter routes; reads
        the counter document (``filename`` + base64 ``content_base64``) off the
        request, calls :func:`capture_counter_artifact`, and responds with the
        updated public matter.
"""
from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from . import artifact_service, matter_view
from .artifact_registry import (
    ACTOR_COUNTERPARTY,
    ROLE_COUNTER,
    ROLE_REVIEWED,
    ROLE_SENT,
    SOURCE_UPLOAD,
    latest_artifact_for_role,
)
from .document_limits import (
    DOCUMENT_TOO_LARGE_MESSAGE,
    DocumentSizeError,
    ensure_document_size,
)
from .matter_repository import DiskMatterRepository
from .routes.common import parse_matter_id, request_owner_user_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository

# A counter is a counterparty revision; the negotiation loop sends a .docx out and
# receives a .docx counter back. Kept deliberately narrow to match the send path.
COUNTER_EXTENSION = ".docx"


def capture_counter_artifact(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    counter_bytes: bytes,
    filename: str,
) -> "Artifact | None":
    """Register a versioned COUNTER artifact, storing the counter-document bytes.

    Source = upload, actor = counterparty, role = counter. The version is assigned
    automatically by the registry (one past the highest existing counter version,
    so a first counter is v1 and the next is v2). Lineage (``based_on``) points at
    the doc the counterparty was revising: the latest SENT artifact when one
    exists, else the latest LEGAL_REVIEW (reviewed) artifact -- so the timeline
    reads ...sent -> counter. The bytes are written through the repository so
    ``artifact_service.get_artifact_bytes`` (and the Drive sync) can read them.

    Owner-scoped: ``add_artifact`` resolves the matter with ``owner_user_id``, so a
    caller can never capture a counter against another tenant's matter -- that
    resolves to a missing matter and returns ``None``. Returns ``None`` (never
    raises) when there is nothing to capture (no bytes) or the matter is not
    found/owned.
    """
    if not counter_bytes:
        return None

    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return None

    # Lineage: the counterparty is revising what we last sent (else the approved
    # legal-review doc). A best-effort lineage hint; never required.
    based_on = latest_artifact_for_role(matter, ROLE_SENT) or latest_artifact_for_role(
        matter, ROLE_REVIEWED
    )

    metadata: dict[str, object] = {"stage": "counter"}
    clean_filename = str(filename or "").strip()
    if clean_filename:
        metadata["source_filename"] = clean_filename

    try:
        return artifact_service.add_artifact(
            matter_id,
            source=SOURCE_UPLOAD,
            actor=ACTOR_COUNTERPARTY,
            role=ROLE_COUNTER,
            document_bytes=counter_bytes,
            based_on_artifact_id=(based_on.id if based_on is not None else ""),
            make_current=True,
            metadata=metadata,
            repository=repository,
            owner_user_id=owner_user_id,
        )
    except artifact_service.ArtifactMatterNotFoundError:
        # The matter vanished (or was never owned) between the resolve above and
        # the persist -- treat as nothing to capture rather than a 500.
        return None


def handle_counter_upload(handler, path: str) -> None:
    """Route body for POST ``/api/matters/{id}/counter`` (upload a counter).

    Owner-scoped like the sibling matter routes (it runs after the server's
    ``_authorize_request``; ownership is enforced again at the repository by
    passing the request's ``owner_user_id``). Reads the counter document
    (``filename`` + base64 ``content_base64``) off the request, validates +
    decodes + size-checks it, registers it as a versioned COUNTER artifact via
    :func:`capture_counter_artifact`, and responds with the updated public matter.
    """
    matter_id = parse_matter_id(path, suffix="/counter")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    if not _is_supported_counter_filename(filename):
        handler._send_json({"error": "Attach a .docx Word document as the counter."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Attach a counter document."}, status=400)
        return

    try:
        counter_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": "The attached document could not be decoded."}, status=400)
        return
    if not counter_bytes:
        handler._send_json({"error": "Attach a counter document."}, status=400)
        return

    try:
        ensure_document_size(counter_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    owner_user_id = request_owner_user_id(handler)
    artifact = capture_counter_artifact(
        DiskMatterRepository(),
        matter_id,
        owner_user_id,
        counter_bytes,
        filename,
    )
    if artifact is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    matter = DiskMatterRepository().get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    handler._send_json(
        {
            "filename": filename,
            "artifact_id": artifact.id,
            "artifact_name": artifact.name,
            "version": artifact.version,
            "matter": matter_view.public_matter(matter),
        },
        status=201,
    )


def _is_supported_counter_filename(filename: object) -> bool:
    return isinstance(filename, str) and filename.lower().endswith(COUNTER_EXTENSION)
