"""The artifact-registry service — registry operations against a repository.

This is the thin facade callers use to track documents on a matter. It wires the
pure ``artifact_registry`` grammar (naming, versioning, lineage, hashing) to the
``MatterRepository`` seam (which persists the ``artifacts`` list + pointer and
stores/reads the underlying bytes). Swapping a Drive-backed repository in later
requires no change here.

Nothing in this module rips out the existing document handling. Registering an
artifact for a matter's *original* NDA reuses the matter's existing
``stored_filename`` — the bytes are never duplicated. Only genuinely new
documents (redlines, reviewed DOCX, generated NDAs, counterparty versions) have
their bytes written through ``put_artifact_document``.
"""
from __future__ import annotations

from typing import Any

from . import artifact_registry
from .artifact_registry import (
    ACTOR_AI,
    ACTOR_HUMAN,
    Artifact,
    ArtifactRegistryError,
    ROLE_ORIGINAL,
    ROLE_REDLINE,
    ROLE_REVIEWED,
    ROLE_WORKING,
    SOURCE_GENERATED,
    SOURCE_GMAIL,
    SOURCE_UPLOAD,
    hash_bytes,
    latest_artifact_for_role,
)
from .matter_repository import DiskMatterRepository, MatterRepository


class ArtifactMatterNotFoundError(ArtifactRegistryError):
    """The matter an artifact operation targeted does not exist (or is not owned)."""


def add_artifact(
    matter_id: str,
    *,
    source: str,
    actor: str,
    role: str,
    document_bytes: bytes | None = None,
    stored_filename: str = "",
    content_hash: str = "",
    based_on_artifact_id: str = "",
    version: int | None = None,
    make_current: bool = True,
    metadata: dict[str, Any] | None = None,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> Artifact:
    """Register a new artifact for a matter and persist it.

    When ``document_bytes`` is given (and no ``stored_filename`` reuses existing
    bytes), the bytes are stored through the repository and the content hash is
    computed from them. When ``stored_filename`` is supplied without bytes, it is
    treated as an already-stored document (e.g. the original NDA's
    ``stored_filename``) and reused as-is.
    """
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        raise ArtifactMatterNotFoundError(f"Matter {matter_id!r} not found.")

    stages_own_bytes = document_bytes is not None and not stored_filename

    # The whole registry step (version resolution, byte-storage-key naming,
    # sequence numbering, lineage validation, pointer update) runs INSIDE a single
    # locked read-modify-write so a concurrent registration can neither overwrite
    # this one's list (the lost-update fix #17) NOR collide on the same auto-version.
    # The version is resolved against the artifacts as FRESHLY READ under the store
    # lock, and the version-keyed byte file is staged with THAT same version — so the
    # stored-filename version always matches the registry version even under
    # concurrent same-role registrations (each gets v1, v2, v3, ... and its own key).
    # ``register_artifact`` is pure; ``put_artifact_document`` writes to the uploads
    # dir (a different path than the records dir), so staging here does not deadlock
    # the store lock.
    captured: dict[str, Any] = {}

    def _mutate(
        current_artifacts: list[dict[str, Any]], current_pointer: str
    ) -> tuple[list[dict[str, Any]], str]:
        working = {
            **matter,
            artifact_registry.ARTIFACTS_FIELD: list(current_artifacts),
            artifact_registry.CURRENT_ARTIFACT_FIELD: current_pointer,
        }
        resolved_stored_filename = stored_filename
        if stages_own_bytes:
            resolved_version = (
                version
                if version is not None
                else artifact_registry.next_version_for_role(working, role)
            )
            provisional_name = _provisional_stored_name(matter, actor, role, resolved_version)
            resolved_stored_filename = repository.put_artifact_document(
                provisional_name, document_bytes
            )
            forced_version = resolved_version
        else:
            forced_version = version
        artifact, artifacts_list, current_artifact_id = artifact_registry.register_artifact(
            working,
            source=source,
            actor=actor,
            role=role,
            document_bytes=document_bytes,
            content_hash=content_hash,
            based_on_artifact_id=based_on_artifact_id,
            stored_filename=resolved_stored_filename,
            version=forced_version,
            make_current=make_current,
            metadata=metadata,
        )
        captured["artifact"] = artifact
        return artifacts_list, current_artifact_id

    updated = repository.mutate_matter_artifacts(matter_id, _mutate, owner_user_id=owner_user_id)
    if updated is None:
        raise ArtifactMatterNotFoundError(f"Matter {matter_id!r} not found.")
    return captured["artifact"]


def register_reviewed_docx(
    matter_id: str,
    reviewed_bytes: bytes,
    *,
    review_version_hash: str = "",
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> Artifact | None:
    """Register the reviewed DOCX as a role="reviewed" artifact (idempotent).

    Called at the approval transition (eager): the one moment the reviewed DOCX
    is both materializable without a stale error and decision-complete. This
    wrapper owns the reviewed-artifact semantics so callers (the approval
    handler) pass only the bytes:

    * actor = human (the reviewed DOCX reflects human reviewer decisions),
      source = generated, role = reviewed.
    * based_on = the matter's latest redline artifact when one exists, else the
      original — so lineage reads original -> redline -> reviewed.
    * make_current = True: the reviewed version becomes "the version that
      matters now" at approval, which is what the Sent/Executed live-doc wants.

    IDEMPOTENCY is keyed on the reviewed bytes' content hash, NOT merely on
    "a reviewed artifact exists". A re-review changes the reviewer decisions,
    which changes the reviewed bytes, which changes the content hash -> a NEW
    reviewed version is registered. A bare re-approval with unchanged decisions
    produces byte-identical output -> the same content hash -> we SKIP and
    return None. ``review_version_hash`` (the locked playbook_version.hash) is
    stamped into metadata for the timeline but is not the dedupe key — content
    is authoritative even if provenance stamping is absent.

    Returns the new Artifact, or None when an identical reviewed artifact is
    already current/registered (the skip case).
    """
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        raise ArtifactMatterNotFoundError(f"Matter {matter_id!r} not found.")

    new_hash = hash_bytes(reviewed_bytes)
    existing_reviewed = latest_artifact_for_role(matter, ROLE_REVIEWED)
    if existing_reviewed is not None and existing_reviewed.content_hash == new_hash:
        # Re-approval with unchanged reviewer decisions: byte-identical output,
        # nothing new to register.
        return None

    redline = latest_artifact_for_role(matter, ROLE_REDLINE)
    original = latest_artifact_for_role(matter, ROLE_ORIGINAL)
    based_on = redline or original
    metadata: dict[str, Any] = {"materialized_at": "approval"}
    if review_version_hash:
        metadata["review_version_hash"] = str(review_version_hash)

    return add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_REVIEWED,
        document_bytes=reviewed_bytes,
        based_on_artifact_id=(based_on.id if based_on is not None else ""),
        make_current=True,
        metadata=metadata,
        repository=repository,
        owner_user_id=owner_user_id,
    )


def register_working_docx(
    matter_id: str,
    working_bytes: bytes,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> Artifact | None:
    """Register the canonical PDF→DOCX conversion as a role="working" artifact.

    Approach C reconstructs a PDF source matter to a DOCX ONCE at ingest. That
    DOCX is the index-anchorable body the faithful renderer + the redline pipeline
    treat exactly like a native DOCX source. This wrapper owns the working-artifact
    semantics so the ingest caller passes only the bytes:

    * actor = ai (the conversion engine produced it), source = upload, role =
      working.
    * based_on = the matter's original artifact when one exists, so lineage reads
      original(PDF) -> working(DOCX).
    * make_current = False: the working DOCX is an internal anchor, NOT the
      "current" lifecycle deliverable (the original PDF stays the user-facing
      source until a redline/reviewed version supersedes it).

    IDEMPOTENT on the working bytes' content hash: re-ingesting the same PDF (or a
    backfill) that reconstructs byte-identically registers nothing new and returns
    None. Returns the new Artifact, or None on the skip case.
    """
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        raise ArtifactMatterNotFoundError(f"Matter {matter_id!r} not found.")

    new_hash = hash_bytes(working_bytes)
    existing_working = latest_artifact_for_role(matter, ROLE_WORKING)
    if existing_working is not None and existing_working.content_hash == new_hash:
        return None

    original = latest_artifact_for_role(matter, ROLE_ORIGINAL)
    return add_artifact(
        matter_id,
        source=SOURCE_UPLOAD,
        actor=ACTOR_AI,
        role=ROLE_WORKING,
        document_bytes=working_bytes,
        based_on_artifact_id=(original.id if original is not None else ""),
        make_current=False,
        metadata={"materialized_at": "ingest", "transform": "pdf_to_working_docx"},
        repository=repository,
        owner_user_id=owner_user_id,
    )


def remove_artifact(
    matter_id: str,
    artifact_id: str,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Drop an artifact from a matter's registry and persist the change.

    Used to keep a one-shot role exactly singular (e.g. SIGNED is terminal: a
    second executed copy REPLACES the prior one rather than appending a duplicate).
    If the removed artifact was the ``current_artifact_id``, the pointer is
    re-anchored to the now-last remaining artifact (or cleared when none remain),
    so the pointer never dangles. Returns the updated matter, or ``None`` when the
    matter or artifact is not found.
    """
    repository = repository or DiskMatterRepository()
    target = str(artifact_id or "")
    removed_anything: dict[str, bool] = {"removed": False}

    def _mutate(
        current_artifacts: list[dict[str, Any]], current_pointer: str
    ) -> tuple[list[dict[str, Any]], str]:
        remaining = [
            item for item in current_artifacts
            if isinstance(item, dict) and str(item.get("id") or "") != target
        ]
        if len(remaining) == len(current_artifacts):
            # Nothing matched: leave the registry untouched (no spurious updated_at bump).
            return current_artifacts, current_pointer
        removed_anything["removed"] = True
        current = current_pointer
        if current == target:
            current = str(remaining[-1].get("id") or "") if remaining else ""
        return remaining, current

    updated = repository.mutate_matter_artifacts(matter_id, _mutate, owner_user_id=owner_user_id)
    if updated is None:
        return None  # matter not found / not owned
    if not removed_anything["removed"]:
        return None  # nothing removed (preserve the prior contract's None return)
    return updated


def set_current_artifact(
    matter_id: str,
    artifact_id: str,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> dict[str, Any]:
    """Point ``current_artifact_id`` at an existing artifact and persist it."""
    repository = repository or DiskMatterRepository()
    validation_error: dict[str, ArtifactRegistryError] = {}

    def _mutate(
        current_artifacts: list[dict[str, Any]], current_pointer: str
    ) -> tuple[list[dict[str, Any]], str]:
        working = {
            artifact_registry.ARTIFACTS_FIELD: list(current_artifacts),
            artifact_registry.CURRENT_ARTIFACT_FIELD: current_pointer,
        }
        try:
            new_pointer = artifact_registry.set_current_artifact(working, artifact_id)
        except ArtifactRegistryError as exc:
            # Capture and re-raise after the lock releases; the registry list is
            # left exactly as read (no write) on a bad pointer.
            validation_error["error"] = exc
            return current_artifacts, current_pointer
        return list(current_artifacts), new_pointer

    updated = repository.mutate_matter_artifacts(matter_id, _mutate, owner_user_id=owner_user_id)
    if validation_error:
        raise validation_error["error"]
    if updated is None:
        raise ArtifactMatterNotFoundError(f"Matter {matter_id!r} not found.")
    return updated


def list_artifacts(
    matter_id: str,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> list[Artifact]:
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return []
    return artifact_registry.matter_artifacts(matter)


def get_artifact_bytes(
    matter_id: str,
    artifact_id: str,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> bytes | None:
    """Read an artifact's bytes via the repository's document storage."""
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return None
    artifact = artifact_registry.find_artifact(matter, artifact_id)
    if artifact is None or not artifact.stored_filename:
        return None
    return repository.get_artifact_document(artifact.stored_filename)


def backfill_matter(
    matter: dict[str, Any],
    *,
    repository: MatterRepository,
    owner_user_id: str = "",
) -> list[Artifact]:
    """Register the documents an existing matter already has into the registry.

    Idempotent: a matter that already carries an ``artifacts`` registry is left
    untouched and its existing artifacts are returned. Otherwise we register what
    the matter records, with sensible role/version/lineage:

    * ORIGINAL (v1) — the matter's stored source document. Reuses the existing
      ``stored_filename``; the source (gmail vs upload) is inferred from
      ``source_type``; the actor is the counterparty (they sent it). Set current.
    * REDLINE (v1) — only when the matter carries a stored ``redline_draft``
      (an AI-produced redline). ``based_on`` the original. AI actor.

    A reviewed DOCX is not separately persisted today (it is exported on demand),
    so there is no stored reviewed artifact to backfill yet; the workflow effort
    will register one when a reviewed export is saved. Returns the artifacts now
    on the matter.
    """
    matter_id = str(matter.get("id") or "")
    if not matter_id:
        return []
    if isinstance(matter.get(artifact_registry.ARTIFACTS_FIELD), list) and matter[artifact_registry.ARTIFACTS_FIELD]:
        return artifact_registry.matter_artifacts(matter)

    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return []

    original_bytes = repository.get_source_document_bytes(matter)
    has_redline_draft = isinstance(matter.get("redline_draft"), dict)

    def _mutate(
        current_artifacts: list[dict[str, Any]], current_pointer: str
    ) -> tuple[list[dict[str, Any]], str]:
        # Re-check idempotency against the list as it stands UNDER THE LOCK: a
        # concurrent backfill (or any registration) that already populated the
        # registry must win, so this backfill must not clobber it with a freshly
        # rebuilt list (the lost-update fix for the backfill path).
        if current_artifacts:
            return current_artifacts, current_pointer

        working = dict(matter)
        working[artifact_registry.ARTIFACTS_FIELD] = []
        working[artifact_registry.CURRENT_ARTIFACT_FIELD] = ""

        original, artifacts_list, current_id = artifact_registry.register_artifact(
            working,
            source=_source_for_matter(matter),
            actor=artifact_registry.ACTOR_COUNTERPARTY,
            role=ROLE_ORIGINAL,
            document_bytes=original_bytes,
            stored_filename=stored_filename,
            make_current=True,
            created_at=str(matter.get("created_at") or ""),
            metadata=_backfill_origin_metadata(matter),
        )
        working[artifact_registry.ARTIFACTS_FIELD] = artifacts_list
        working[artifact_registry.CURRENT_ARTIFACT_FIELD] = current_id

        if has_redline_draft:
            _redline, artifacts_list, current_id = artifact_registry.register_artifact(
                working,
                source=artifact_registry.SOURCE_GENERATED,
                actor=artifact_registry.ACTOR_AI,
                role=ROLE_REDLINE,
                based_on_artifact_id=original.id,
                make_current=True,
                metadata={"backfilled_from": "redline_draft"},
            )
            working[artifact_registry.ARTIFACTS_FIELD] = artifacts_list
            working[artifact_registry.CURRENT_ARTIFACT_FIELD] = current_id

        return working[artifact_registry.ARTIFACTS_FIELD], working[artifact_registry.CURRENT_ARTIFACT_FIELD]

    updated = repository.mutate_matter_artifacts(matter_id, _mutate, owner_user_id=owner_user_id)
    if updated is None:
        return []
    return artifact_registry.matter_artifacts(updated)


def backfill_all_matters(
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> dict[str, int]:
    """Backfill every matter's registry. Idempotent — re-running is a no-op.

    Returns a summary ``{scanned, registered, skipped_already_registered,
    artifacts_created}``.
    """
    repository = repository or DiskMatterRepository()
    summary = {
        "scanned": 0,
        "registered": 0,
        "skipped_already_registered": 0,
        "artifacts_created": 0,
    }
    for matter in repository.list_matters(owner_user_id=owner_user_id):
        summary["scanned"] += 1
        existing = matter.get(artifact_registry.ARTIFACTS_FIELD)
        if isinstance(existing, list) and existing:
            summary["skipped_already_registered"] += 1
            continue
        artifacts = backfill_matter(matter, repository=repository, owner_user_id=owner_user_id)
        if artifacts:
            summary["registered"] += 1
            summary["artifacts_created"] += len(artifacts)
    return summary


# --- helpers ---------------------------------------------------------------
def _source_for_matter(matter: dict[str, Any]) -> str:
    source_type = str(matter.get("source_type") or "").casefold()
    if source_type.startswith("gmail") or matter.get("gmail_message_id"):
        return SOURCE_GMAIL
    return SOURCE_UPLOAD


def _backfill_origin_metadata(matter: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"backfilled_from": "stored_filename"}
    source_filename = str(matter.get("source_filename") or "")
    if source_filename:
        metadata["source_filename"] = source_filename
    return metadata


def _provisional_stored_name(matter: dict[str, Any], actor: str, role: str, version: int) -> str:
    """Version-aware storage key for auto-provisioned artifact bytes.

    Each version of a (matter, actor, role) must store under its OWN key so a
    later version never overwrites an earlier one's bytes (which
    ``get_artifact_bytes`` would otherwise return for the older version). The
    ``-v{N}`` suffix makes the key unique per version.
    """
    matter_id = str(matter.get("id") or "matter")
    actor_slug = artifact_registry._slug(actor) or "actor"
    role_slug = artifact_registry._slug(role) or "doc"
    version_label = max(int(version), 1)
    return f"{matter_id}-{actor_slug}-{role_slug}-v{version_label}.docx"
