"""Lifecycle stage: COUNTER (a counterparty redline/counter, repeatable + versioned).

STUB — the ``hook/counter`` hook agent implements this module. The CORE agent
owns the shared surfaces (the artifact-registry ``counter`` role, the Drive
naming grammar, and the registered POST ``/api/matters/{id}/counter`` route ->
this module's :func:`handle_counter_upload`); this stub exists so those shared
edits land green. The hook agent fills ONLY this module and its own test file
(``tests/test_lifecycle_counter.py``); it must not touch a sibling hook module or
any shared file the core already owns.

Contract the hook agent implements:
    capture_counter_artifact(repository, matter_id, owner_user_id, counter_bytes,
                             filename) -> Artifact | None
        Register a COUNTER artifact (versioned via
        ``artifact_registry.next_version_for_role(matter, ROLE_COUNTER)``) and
        store the counter-document bytes so ``artifact_service.get_artifact_bytes``
        returns them. Returns the new ``Artifact`` (or ``None`` when there is
        nothing to capture).

    handle_counter_upload(handler) -> None
        The route body for POST ``/api/matters/{id}/counter`` (upload a
        counterparty counter). Owner-scoped like the sibling matter routes; reads
        the counter document off the request, calls
        :func:`capture_counter_artifact`, and responds with the updated public
        matter.

Until implemented both are safe: :func:`capture_counter_artifact` returns
``None`` without raising, and :func:`handle_counter_upload` returns a clear 501
not-yet-implemented JSON response.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .artifact_registry import Artifact
    from .matter_repository import MatterRepository

NOT_IMPLEMENTED_MESSAGE = "Counter upload is not implemented yet."


def capture_counter_artifact(
    repository: "MatterRepository | None",
    matter_id: str,
    owner_user_id: str,
    counter_bytes: bytes,
    filename: str,
) -> "Artifact | None":
    """STUB — hook-agent-implements-this. Safe no-op: returns None, never raises.

    See the module docstring for the full contract (register a versioned COUNTER
    artifact, storing the counter-document bytes).
    """
    _ = (repository, matter_id, owner_user_id, counter_bytes, filename)
    return None


def handle_counter_upload(handler, path: str) -> None:
    """STUB — hook-agent-implements-this. Returns a clear 501 not-yet-implemented.

    Route body for POST ``/api/matters/{id}/counter``. The CORE agent has already
    registered the route to this handler; the hook agent implements the body
    (owner-scoped, calling :func:`capture_counter_artifact` with the counter
    document bytes).
    """
    _ = path
    handler._send_json({"error": NOT_IMPLEMENTED_MESSAGE}, status=501)
