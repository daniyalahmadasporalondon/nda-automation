from __future__ import annotations

import json

from .. import entity_registry
from ..checker import PLAYBOOK_PATH
from ..entity_authoring import (
    EntityAuthoringError,
    load_entities_workspace,
    save_entities_registry,
    validate_entities_payload,
)
from ..playbook_runtime import read_playbook_from_path
from .common import request_actor, require_admin


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


def handle_admin_signing_entities(handler, *, send_body: bool = True) -> None:
    """GET /api/admin/signing-entities — the admin Entities-console workspace.

    Admin-gated read of the LIVE registry plus the playbook's approved
    governing-law options (so the editor's law dropdown is playbook-driven). A
    non-admin gets a 403, which the console renders as a calm read-only state.
    """
    if not require_admin(handler, send_body=send_body):
        return
    handler._send_json(load_entities_workspace(), send_body=send_body)


def handle_admin_signing_entities_save(handler) -> None:
    """POST /api/admin/signing-entities — publish-style save of the full registry.

    Admin-gated + CSRF-protected (the server's do_POST enforces the Origin check
    before dispatch). The body is ``{"entities": [...]}`` — the full replacement
    list. Validated (structural + orphan guard against the playbook) before any
    disk write, so a malformed save is rejected without corrupting the store.
    """
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = save_entities_registry(payload, actor=request_actor(handler))
    except EntityAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_admin_signing_entities_validate(handler) -> None:
    """POST /api/admin/signing-entities/validate — preview validation, no write.

    Admin-gated + CSRF-protected. Returns ``{"valid", "errors"}`` so the console
    can surface validation feedback before a save.
    """
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    handler._send_json(validate_entities_payload(payload))
