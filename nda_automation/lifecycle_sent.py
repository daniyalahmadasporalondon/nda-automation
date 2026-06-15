"""Lifecycle stage: SENT (an outbound copy emailed to the counterparty).

This is the ``hook/sent`` module. The CORE agent owns the shared surfaces (the
artifact-registry ``sent`` role, the Drive naming grammar, and the send-success
CALL to :func:`capture_sent_artifact` on ``RepositoryMatterLifecycle``); this
module fills in the capture itself. It touches ONLY this file and its own test
file (``tests/test_lifecycle_sent.py``).

Contract:
    capture_sent_artifact(repository, matter_id, owner_user_id, sent_bytes,
                          filename, recipient) -> Artifact | None
        Register a SENT artifact (role ``sent``, source ``generated``, actor
        ``human`` — we sent it after our review) with the EXACT emailed bytes,
        versioned via ``artifact_registry.next_version_for_role(matter, sent)``
        (so a second send becomes ``sent`` v2), and stored through the repository
        so ``artifact_service.get_artifact_bytes`` returns the same bytes the
        Drive sync uploads. Lineage (``based_on``) points at the most-recent
        upstream artifact — the latest ``reviewed`` (legal_review) doc, else the
        latest ``redline``, else the ``original``. ``recipient`` and ``filename``
        are recorded on the artifact metadata. Returns the new ``Artifact``.

Best-effort: returns ``None`` (never raises) when there is nothing to capture —
no bytes, no matter id, or the matter is not found/owned — so the send-success
path stays inert in those cases. The send already happened; capture is additive.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import artifact_service
from .artifact_registry import (
    ACTOR_HUMAN,
    ROLE_ORIGINAL,
    ROLE_REDLINE,
    ROLE_REVIEWED,
    ROLE_SENT,
    SOURCE_GENERATED,
    latest_artifact_for_role,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository


def capture_sent_artifact(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    sent_bytes: bytes,
    filename: str,
    recipient: str,
) -> "Artifact | None":
    """Register the emailed document as a SENT lifecycle artifact.

    See the module docstring for the full contract. The version is derived by the
    registry (one past the highest existing ``sent`` version), so a first send is
    ``sent`` v1 and a second send is ``sent`` v2. Returns the new ``Artifact``,
    or ``None`` when there is nothing to capture.
    """
    matter_id = str(matter_id or "")
    if not matter_id or not sent_bytes:
        return None

    repository = repository or _default_repository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return None

    based_on = (
        latest_artifact_for_role(matter, ROLE_REVIEWED)
        or latest_artifact_for_role(matter, ROLE_REDLINE)
        or latest_artifact_for_role(matter, ROLE_ORIGINAL)
    )

    metadata: dict[str, Any] = {}
    recipient = str(recipient or "").strip()
    if recipient:
        metadata["recipient"] = recipient
    filename = str(filename or "").strip()
    if filename:
        metadata["sent_filename"] = filename

    # Each SENT version keeps its OWN bytes. ``add_artifact`` now provisions a
    # VERSION-AWARE storage key, so a plain ``document_bytes=`` is sufficient — a
    # second send becomes ``sent`` v2 and stores under its own key (no overwrite
    # of v1). The version is derived by the registry (next_version_for_role).
    return artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_SENT,
        document_bytes=bytes(sent_bytes),
        based_on_artifact_id=(based_on.id if based_on is not None else ""),
        make_current=True,
        metadata=metadata,
        repository=repository,
        owner_user_id=owner_user_id,
    )


def _default_repository() -> "MatterRepository":
    from .matter_repository import DiskMatterRepository

    return DiskMatterRepository()
