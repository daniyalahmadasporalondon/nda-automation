"""Lifecycle stage: SIGNED (the executed copy — terminal, no version).

The ``hook/signed`` hook agent implements this module. The CORE agent owns the
shared surfaces (the artifact-registry ``signed`` role, the Drive naming grammar,
and the registered POST ``/api/matters/{id}/signed`` route -> this module's
:func:`handle_signed_upload`); this module fills ONLY the behaviour behind those
seams (and its own test file, ``tests/test_lifecycle_signed.py``). It does not
touch a sibling hook module or any shared file the core already owns.

Contract:
    capture_signed_artifact(repository, matter_id, owner_user_id, signed_bytes,
                            filename) -> Artifact | None
        Register a SIGNED artifact (TERMINAL — no version suffix; ``signed`` is a
        one-shot stage in the naming grammar) and store the executed-document
        bytes so ``artifact_service.get_artifact_bytes`` returns them. Lineage is
        anchored to the latest in-flight version of the matter (reviewed -> sent
        -> counter -> original) so the executed copy reads as derived from the
        last working document. Returns the new ``Artifact`` (or ``None`` when
        there are no bytes to capture / the matter is not owned).

    handle_signed_upload(handler, path) -> None
        The route body for POST ``/api/matters/{id}/signed`` (upload the executed
        PDF). Owner-scoped like the sibling matter routes; reads the executed
        document off the request as base64 (the matter-upload convention), calls
        :func:`capture_signed_artifact`, and responds with the updated public
        matter.
"""
from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from . import artifact_service, matter_view
from .artifact_registry import (
    ACTOR_HUMAN,
    ROLE_COUNTER,
    ROLE_ORIGINAL,
    ROLE_REVIEWED,
    ROLE_SENT,
    ROLE_SIGNED,
    SOURCE_UPLOAD,
    ArtifactRegistryError,
    latest_artifact_for_role,
)
from .document_limits import DOCUMENT_TOO_LARGE_MESSAGE, DocumentSizeError, ensure_document_size
from .matter_repository import DiskMatterRepository
from .routes.common import parse_matter_id, request_owner_user_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository

# An executed copy comes back as a signed PDF (e.g. ``08_signed.pdf``); the
# signed stage is the only one fixed to a single extension by the design.
SIGNED_EXTENSION = ".pdf"
SIGNED_FILENAME_MESSAGE = "Upload the executed copy as a PDF."
MATTER_NOT_FOUND_MESSAGE = "Matter not found."
MISSING_DOCUMENT_MESSAGE = "Provide the executed document to capture."
DECODE_FAILED_MESSAGE = "The signed document could not be decoded."

# The chronological precedence for the document the executed copy descends from.
# We anchor lineage to the most-advanced version that exists on the matter so the
# signed artifact reads as the terminal node of the negotiation thread.
_LINEAGE_PRECEDENCE = (ROLE_COUNTER, ROLE_SENT, ROLE_REVIEWED, ROLE_ORIGINAL)


def capture_signed_artifact(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    signed_bytes: bytes,
    filename: str,
) -> "Artifact | None":
    """Register a terminal SIGNED artifact, storing the executed-document bytes.

    The bytes are written through the repository's artifact-document storage so
    ``artifact_service.get_artifact_bytes`` (and the Drive sync) can read them
    back. The artifact is ``role=signed`` / ``actor=human`` / ``source=upload``;
    ``signed`` is a one-shot stage, so the naming grammar gives it no ``_v{N}``
    suffix. Lineage is anchored to the latest in-flight version on the matter.

    Returns the new :class:`Artifact`, or ``None`` when there is nothing to
    capture: no bytes were supplied, or the matter is missing / not owned by the
    caller (the registry would have nothing to attach the artifact to).
    """
    if not signed_bytes:
        return None

    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return None

    based_on = _signed_lineage_anchor(matter)
    metadata: dict[str, str] = {"captured_via": "signed_upload"}
    cleaned_filename = str(filename or "").strip()
    if cleaned_filename:
        metadata["source_filename"] = cleaned_filename

    # Stage the executed bytes under a ``.pdf`` storage key so the registry
    # derives the PDF extension from the stored filename (``add_artifact`` only
    # auto-stores under a hardcoded ``.docx`` provisional name). Passing the key
    # back as ``stored_filename`` reuses these exact bytes — no duplication — and
    # records the content hash off them via ``add_artifact``'s own hashing.
    stored_filename = repository.put_artifact_document(
        _signed_storage_name(matter_id), signed_bytes
    )
    return artifact_service.add_artifact(
        matter_id,
        source=SOURCE_UPLOAD,
        actor=ACTOR_HUMAN,
        role=ROLE_SIGNED,
        document_bytes=signed_bytes,
        stored_filename=stored_filename,
        based_on_artifact_id=(based_on.id if based_on is not None else ""),
        make_current=True,
        metadata=metadata,
        repository=repository,
        owner_user_id=owner_user_id,
    )


def handle_signed_upload(handler, path: str) -> None:
    """Route body for POST ``/api/matters/{id}/signed`` — upload the executed PDF.

    Owner-scoped: the artifact is captured only for a matter owned by the
    authenticated caller (a cross-tenant matter resolves to ``None`` and answers
    404). The executed document is read as base64 (the matter-upload convention),
    must be a PDF, and is size-checked before capture. Responds with the updated
    public matter.
    """
    matter_id = parse_matter_id(path, suffix="/signed")
    if matter_id is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    if not _is_signed_pdf_filename(filename):
        handler._send_json({"error": SIGNED_FILENAME_MESSAGE}, status=400)
        return

    content_base64 = payload.get("content_base64", "")
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": MISSING_DOCUMENT_MESSAGE}, status=400)
        return

    try:
        signed_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": DECODE_FAILED_MESSAGE}, status=400)
        return
    if not signed_bytes:
        handler._send_json({"error": MISSING_DOCUMENT_MESSAGE}, status=400)
        return

    try:
        ensure_document_size(signed_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    owner_user_id = request_owner_user_id(handler)
    try:
        artifact = capture_signed_artifact(
            None,
            matter_id,
            owner_user_id,
            signed_bytes,
            str(filename),
        )
    except ArtifactRegistryError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    if artifact is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return

    matter = DiskMatterRepository().get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return
    handler._send_json(
        {"matter": matter_view.public_matter(matter), "artifact_id": artifact.id},
        status=201,
    )


# --- helpers ---------------------------------------------------------------
def _signed_lineage_anchor(matter: dict) -> "Artifact | None":
    """The latest in-flight artifact the executed copy descends from, if any.

    Walks the negotiation thread newest-stage-first (counter -> sent -> reviewed
    -> original) and returns the highest-version artifact of the first role that
    exists. ``None`` when the matter carries no prior artifacts (lineage is then
    left empty so the registry never dangles a ``based_on`` reference).
    """
    for role in _LINEAGE_PRECEDENCE:
        anchor = latest_artifact_for_role(matter, role)
        if anchor is not None:
            return anchor
    return None


def _is_signed_pdf_filename(filename: object) -> bool:
    return isinstance(filename, str) and filename.strip().casefold().endswith(SIGNED_EXTENSION)


def _signed_storage_name(matter_id: str) -> str:
    """Storage key for the executed copy, carrying the ``.pdf`` extension.

    The repository sanitises this into the actual storage key; the ``.pdf``
    suffix is what makes the registry stamp the artifact's extension (and Drive
    name) as a PDF rather than the default DOCX.
    """
    safe_matter = str(matter_id or "matter").strip() or "matter"
    return f"{safe_matter}-signed{SIGNED_EXTENSION}"
