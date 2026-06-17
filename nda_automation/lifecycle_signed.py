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
        matter. Uploading the executed copy IS an attestation that both parties
        signed, so it also flips the executed contract via
        :func:`mark_matter_executed` (best-effort; never blocks the artifact).

    mark_matter_executed(repository, matter_id, owner_user_id, *, actor) -> dict | None
        The MANUAL "mark as executed" primitive for an NDA signed OUTSIDE our
        DocuSign flow (uploaded / paper-signed). A human attests both parties have
        signed, and this flips the shared executed contract on the matter
        (``executed=True`` / ``status="fully_signed"`` / ``executed_at=<now>``) --
        the same three fields the DocuSign completion sync sets -- WITHOUT touching
        the AI review state. Records who/when via an append-only ``executed``
        timeline event. Idempotent guard: a matter already executed is returned
        unchanged (no re-flip, no duplicate timeline event). Returns the updated
        matter dict, or ``None`` when the matter is missing / not owned.

    handle_mark_executed(handler, path) -> None
        The route body for POST ``/api/matters/{id}/mark-executed``. Owner-scoped,
        confirm-gated by the deliberate frontend action; 409s a matter that is
        already executed, and responds with the updated public matter.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING

from . import artifact_service, matter_view, workflow
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
    next_version_for_role,
)
from .document_limits import DOCUMENT_TOO_LARGE_MESSAGE, DocumentSizeError, ensure_document_size
from .matter_repository import DiskMatterRepository
from .routes.common import parse_matter_id, request_actor, request_owner_user_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository

# An executed copy comes back as a signed PDF (e.g. ``08_signed.pdf``); the
# signed stage is the only one fixed to a single extension by the design.
SIGNED_EXTENSION = ".pdf"
SIGNED_FILENAME_MESSAGE = "Upload the executed copy as a PDF."
MATTER_NOT_FOUND_MESSAGE = "Matter not found."
MISSING_DOCUMENT_MESSAGE = "Provide the executed document to capture."
ALREADY_EXECUTED_MESSAGE = "This matter is already marked executed."
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

    # SIGNED is TERMINAL: a matter has exactly ONE signed copy (the latest). If a
    # signed artifact already exists, the new upload REPLACES it rather than
    # appending a duplicate. Capture the prior signed id before adding the new one
    # (lineage precedence — counter/sent/reviewed/original — never anchors the new
    # signed to the old one, so the prune below leaves no dangling reference).
    existing_signed = latest_artifact_for_role(matter, ROLE_SIGNED)

    # Stage the executed bytes under a version-aware ``.pdf`` storage key so the
    # registry derives the PDF extension from the stored filename (``add_artifact``
    # only auto-stores under a hardcoded ``.docx`` provisional name) and a replaced
    # copy never overwrites the (about-to-be-pruned) prior signed bytes. Passing
    # the key back as ``stored_filename`` reuses these exact bytes — no duplication
    # — and records the content hash off them via ``add_artifact``'s own hashing.
    signed_version = next_version_for_role(matter, ROLE_SIGNED)
    stored_filename = repository.put_artifact_document(
        _signed_storage_name(matter_id, signed_version), signed_bytes
    )
    artifact = artifact_service.add_artifact(
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
    if existing_signed is not None:
        # Drop the prior signed copy so the matter keeps exactly one (the latest).
        artifact_service.remove_artifact(
            matter_id,
            existing_signed.id,
            repository=repository,
            owner_user_id=owner_user_id,
        )
    return artifact


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

    # Uploading the executed copy IS a human attestation that both parties signed,
    # so flip the shared executed contract too (executed / status=fully_signed /
    # executed_at) -- reconciling the gap where a signed artifact landed without the
    # matter qualifying as executed for the board/corpus contract. Idempotent: a
    # re-upload of an already-executed matter leaves the fields untouched. The
    # mark is best-effort -- the signed artifact must persist even if the flip
    # somehow can't (we already responded 201 with the artifact below either way).
    executed = mark_matter_executed(
        None,
        matter_id,
        owner_user_id,
        actor=request_actor(handler),
    )

    matter = executed or DiskMatterRepository().get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return

    # Mirror the externally-signed copy to Drive (best-effort, like the DocuSign
    # completion path). An uploaded paper-signed PDF IS a signed document, so it
    # always archives — labelled "uploaded" (vs DocuSign "docusign") in both the
    # durable summary facet and the archived file name (``NN_signed_uploaded.pdf``).
    _archive_executed_to_drive(handler, matter, matter_id, owner_user_id, signed_via="uploaded")

    handler._send_json(
        {"matter": matter_view.public_matter(matter), "artifact_id": artifact.id},
        status=201,
    )


def mark_matter_executed(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    *,
    actor: str = "",
) -> dict | None:
    """Flip the shared executed contract on a matter (the MANUAL "mark executed").

    Sets ``executed=True`` / ``status="fully_signed"`` / ``executed_at=<now>`` --
    the exact three fields :func:`docusign_workflow.sync_signature_status` sets on
    DocuSign completion -- so a paper / externally-signed NDA qualifies as executed
    for the board (excluded) and the corpus library (included). The AI review state
    (``ai_review_ran`` and the review payload) is never touched.

    Records a who/when audit trail via an append-only ``executed`` timeline event
    (actor + detail noting the manual path). Idempotent: a matter that is already
    executed is returned unchanged -- no re-flip and no duplicate timeline event.

    Returns the updated matter dict, or ``None`` when the matter is missing / not
    owned by the caller.
    """
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return None

    # Guard: never re-flip an already-executed matter (whether executed via the
    # DocuSign sync, a prior manual mark, or a phase marker).
    if workflow._is_executed(matter):
        return matter

    now = datetime.now(timezone.utc).isoformat()
    fields: dict[str, object] = {
        "executed": True,
        "executed_at": now,
        "status": workflow.STATUS_FULLY_SIGNED,
    }
    updated = repository.update_matter_fields(matter_id, fields, owner_user_id=owner_user_id)
    if updated is None:
        # The matter vanished between the read and the write (or ownership changed).
        return None

    # Clear any stale ``workflow_error`` so an executed matter can't keep carrying a
    # contradictory failed-send marker. Without this, the two readers disagree: the
    # board drops the matter as done (is_matter_executed ignores workflow_error) while
    # the detail card/corpus render it as an active failed-send. Executed wins, so the
    # data is made clean going forward (its own dedicated writer, never folded into the
    # happy-path field write above). Best-effort: a failure here doesn't unset executed.
    if updated.get("workflow_error"):
        cleared = repository.set_matter_workflow_error(matter_id, None, owner_user_id=owner_user_id)
        if cleared is not None:
            updated = cleared

    event = workflow.build_timeline_event(
        workflow.EVENT_EXECUTED,
        phase=workflow.PHASE_EXECUTED,
        status=workflow.STATUS_FULLY_SIGNED,
        actor=actor,
        detail="Marked executed manually (signed outside DocuSign).",
        at=now,
    )
    after_event = repository.append_timeline_event(matter_id, event, owner_user_id=owner_user_id)
    return after_event or updated


def handle_mark_executed(handler, path: str) -> None:
    """Route body for POST ``/api/matters/{id}/mark-executed`` -- the manual mark.

    Owner-scoped: a cross-tenant or missing matter answers 404. A matter that is
    already executed answers 409 (the action is a deliberate, confirm-gated
    attestation, not an idempotent toggle the UI should re-submit). On success
    responds with the updated public matter and the recorded actor/timestamp.
    """
    matter_id = parse_matter_id(path, suffix="/mark-executed")
    if matter_id is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return
    if workflow._is_executed(matter):
        handler._send_json({"error": ALREADY_EXECUTED_MESSAGE}, status=409)
        return

    actor = request_actor(handler)
    updated = mark_matter_executed(repository, matter_id, owner_user_id, actor=actor)
    if updated is None:
        handler._send_json({"error": MATTER_NOT_FOUND_MESSAGE}, status=404)
        return

    # Mirror to Drive (best-effort) ONLY when there is an actual signed document to
    # archive. A bare manual mark-executed is just an attestation (no PDF), so there
    # is nothing to mirror — skip gracefully, no error. When a signed copy IS
    # present (e.g. a prior signed-upload, or a counter that already carried the
    # executed copy) it is mirrored, labelled by how it was executed.
    if latest_artifact_for_role(updated, ROLE_SIGNED) is not None:
        _archive_executed_to_drive(
            handler,
            updated,
            matter_id,
            owner_user_id,
            signed_via=_signed_via_for_matter(updated),
        )

    handler._send_json(
        {
            "matter": matter_view.public_matter(updated),
            "executed_at": str(updated.get("executed_at") or ""),
            "executed_by": actor,
        },
        status=200,
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


def _signed_storage_name(matter_id: str, version: int) -> str:
    """Storage key for the executed copy, carrying the ``.pdf`` extension.

    The repository sanitises this into the actual storage key; the ``.pdf``
    suffix is what makes the registry stamp the artifact's extension (and Drive
    name) as a PDF rather than the default DOCX. The ``-v{N}`` segment keeps a
    REPLACED signed copy's bytes distinct from the prior one's (which is pruned),
    so the storage key never collides even though SIGNED is a one-shot Drive name.
    """
    safe_matter = str(matter_id or "matter").strip() or "matter"
    version_label = max(int(version), 1)
    return f"{safe_matter}-signed-v{version_label}{SIGNED_EXTENSION}"


def _signed_via_for_matter(matter: dict) -> str:
    """How a now-executed matter was signed: "docusign" / "uploaded".

    A matter with a DocuSign envelope was e-signed through our flow; otherwise an
    executed copy that exists here arrived as an externally / paper-signed upload.
    Defaults to "uploaded" (the manual-mark path is for NDAs signed outside our
    DocuSign flow).
    """
    if isinstance(matter, dict):
        signature = matter.get("docusign")
        if isinstance(signature, dict) and signature.get("envelope_id"):
            return "docusign"
    return "uploaded"


def _archive_executed_to_drive(
    handler,
    matter: dict,
    matter_id: str,
    owner_user_id: str,
    *,
    signed_via: str,
) -> None:
    """Best-effort: mirror a freshly-executed matter to Drive from a route handler.

    Reuses the SAME shared archiver + Drive-token-owner resolution as the DocuSign
    completion path (the #10 fix), so the signed copy + an overwritten
    ``matter_summary.json`` land in the matter's Drive folder, labelled by how it
    was executed (``signed_via``). The Drive-token owner is resolved from the
    SESSION the same way the deliberate Save-to-Drive route does — the Google-scoped
    id, or "" in no-login / local-demo mode (server-global token) — NOT the raw
    matter/request id.

    STRICTLY best-effort: the shared archiver swallows + logs every failure path and
    never raises; this wrapper additionally guards the resolution itself so a Drive
    hiccup can never break the executed transition (which already persisted) or the
    HTTP response. Emits a log on any skip/failure so the miss is observable.
    """
    try:
        from . import drive_integration, google_connection

        drive_token_owner_user_id = google_connection.connected_owner_user_id(
            getattr(handler, "current_user", None),
            owner_user_id=request_owner_user_id(handler),
        )
        drive_integration.archive_executed_matter(
            matter=matter,
            matter_id=matter_id,
            owner_user_id=owner_user_id,
            repository=DiskMatterRepository(),
            drive_token_owner_user_id=drive_token_owner_user_id,
            signed_via=signed_via,
        )
    except Exception:  # pragma: no cover - defensive; the archiver itself never raises
        logger.warning(
            "Drive archive wrapper failed for matter %s (signed_via=%s); "
            "executed transition is unaffected.",
            matter_id,
            signed_via,
            exc_info=True,
        )
