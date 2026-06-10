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
    """

    try:
        playbook = read_playbook_from_path(PLAYBOOK_PATH)
    except (OSError, json.JSONDecodeError):
        playbook = None

    handler._send_json(
        entity_registry.signing_entities_payload(playbook),
        send_body=send_body,
    )
