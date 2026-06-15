"""Lifecycle stage: SIGNED (the executed copy — terminal, no version).

STUB — the ``hook/signed`` hook agent implements this module. The CORE agent owns
the shared surfaces (the artifact-registry ``signed`` role, the Drive naming
grammar, and the registered POST ``/api/matters/{id}/signed`` route -> this
module's :func:`handle_signed_upload`); this stub exists so those shared edits
land green. The hook agent fills ONLY this module and its own test file
(``tests/test_lifecycle_signed.py``); it must not touch a sibling hook module or
any shared file the core already owns.

Contract the hook agent implements:
    capture_signed_artifact(repository, matter_id, owner_user_id, signed_bytes,
                            filename) -> Artifact | None
        Register a SIGNED artifact (TERMINAL — no version suffix) and store the
        executed-document bytes so ``artifact_service.get_artifact_bytes`` returns
        them. Returns the new ``Artifact`` (or ``None`` when there is nothing to
        capture).

    handle_signed_upload(handler) -> None
        The route body for POST ``/api/matters/{id}/signed`` (upload the executed
        PDF). Owner-scoped like the sibling matter routes; reads the executed
        document off the request, calls :func:`capture_signed_artifact`, and
        responds with the updated public matter.

Until implemented both are safe: :func:`capture_signed_artifact` returns ``None``
without raising, and :func:`handle_signed_upload` returns a clear 501
not-yet-implemented JSON response.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository

NOT_IMPLEMENTED_MESSAGE = "Signed-copy upload is not implemented yet."


def capture_signed_artifact(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    signed_bytes: bytes,
    filename: str,
) -> "Artifact | None":
    """STUB — hook-agent-implements-this. Safe no-op: returns None, never raises.

    See the module docstring for the full contract (register a terminal SIGNED
    artifact, no version suffix, storing the executed-document bytes).
    """
    _ = (repository, matter_id, owner_user_id, signed_bytes, filename)
    return None


def handle_signed_upload(handler, path: str) -> None:
    """STUB — hook-agent-implements-this. Returns a clear 501 not-yet-implemented.

    Route body for POST ``/api/matters/{id}/signed``. The CORE agent has already
    registered the route to this handler; the hook agent implements the body
    (owner-scoped, calling :func:`capture_signed_artifact` with the executed
    document bytes).
    """
    _ = path
    handler._send_json({"error": NOT_IMPLEMENTED_MESSAGE}, status=501)
