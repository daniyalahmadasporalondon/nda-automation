"""Lifecycle stage: SENT (an outbound copy emailed to the counterparty).

STUB — the ``hook/sent`` hook agent implements this module. The CORE agent owns
the shared surfaces (the artifact-registry ``sent`` role, the Drive naming
grammar, and the send-success CALL to :func:`capture_sent_artifact`); this stub
exists so those shared edits land green. The hook agent fills ONLY this module
and its own test file (``tests/test_lifecycle_sent.py``); it must not touch a
sibling hook module or any shared file the core already owns.

Contract the hook agent implements:
    capture_sent_artifact(repository, matter_id, owner_user_id, sent_bytes,
                          filename, recipient) -> Artifact | None
        Register a SENT artifact (versioned via
        ``artifact_registry.next_version_for_role(matter, ROLE_SENT)``) and store
        the EXACT emailed bytes so ``artifact_service.get_artifact_bytes`` returns
        them. ``recipient`` is recorded on the artifact metadata. Returns the new
        ``Artifact`` (or ``None`` when there is nothing to capture).

Until implemented this is a safe NO-OP: it returns ``None`` without raising, so
the existing send-success path stays inert and the existing send tests stay
green.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

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
    """STUB — hook-agent-implements-this. Safe no-op: returns None, never raises.

    See the module docstring for the full contract. The CORE agent has already
    added the CALL to this on the send-success path; with this no-op stub that
    call is inert.
    """
    _ = (repository, matter_id, owner_user_id, sent_bytes, filename, recipient)
    return None
