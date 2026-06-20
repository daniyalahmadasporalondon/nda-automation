from __future__ import annotations

import json

from .. import entity_registry
from ..checker import PLAYBOOK_PATH
from ..playbook_runtime import read_playbook_from_path


def handle_signing_entities(handler, *, send_body: bool = True) -> None:
    """GET /api/signing-entities — the signing-entity bundles for the draft flow.

    Returns the entity bundles plus the entity -> governing-law mapping. The
    mapping carries ``matches_playbook`` drift flags and the live playbook
    governing-law option ids when the playbook loads; if the playbook cannot be
    read the entities are still returned (the registry is self-contained), just
    without the playbook-relative drift data.

    Proactive drift guard: when the playbook loads, the registry is validated
    against it (approved-option existence, cached label, AND forum reconciliation)
    so a renamed/removed approved option or a law/forum bucket divergence is caught
    HERE -- on the request the draft UI makes -- rather than only blowing up later
    at generate-time. The check is fail-soft: a drift is surfaced as a
    ``registry_drift`` diagnostic on the payload, never a 500 that breaks the draft
    flow.
    """

    try:
        playbook = read_playbook_from_path(PLAYBOOK_PATH)
    except (OSError, json.JSONDecodeError):
        playbook = None

    payload = entity_registry.signing_entities_payload(playbook)

    if playbook is not None:
        try:
            entity_registry.validate_registry_against_playbook(playbook)
        except ValueError as drift:
            # Surface the drift to the caller without failing the route. The draft
            # UI can warn; generation still hard-fails downstream on an actually
            # unapproved option, so this is an early-warning surface, not the gate.
            payload["registry_drift"] = str(drift)

    handler._send_json(payload, send_body=send_body)
