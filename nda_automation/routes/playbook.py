from __future__ import annotations

import os
from urllib.parse import unquote

from ..checker import PLAYBOOK_PATH
from ..playbook_authoring import (
    PlaybookAuthoringError,
    collect_playbook_validation_errors,
    discard_playbook_draft,
    load_playbook_workspace,
    publish_playbook,
    restore_playbook_history_entry,
    save_active_playbook,
    save_playbook_draft,
    validate_playbook_draft,
)
from ..playbook_suggest_wording import suggest_clause_wording
from .common import require_admin

__all__ = [
    "collect_playbook_validation_errors",
    "handle_playbook_draft_discard",
    "handle_playbook_draft_get",
    "handle_playbook_draft_save",
    "handle_playbook_get",
    "handle_playbook_publish",
    "handle_playbook_restore",
    "handle_playbook_save",
    "handle_playbook_suggest_wording",
    "handle_playbook_validate_draft",
    "parse_suggest_wording_clause_id",
]


def parse_suggest_wording_clause_id(path: str) -> str | None:
    """Extract ``<clause_id>`` from ``/api/playbook/clause/<clause_id>/suggest-wording``.

    Returns None when the path is not that route, or when the clause id is empty or
    contains a path separator (an obviously malformed id).
    """
    prefix = "/api/playbook/clause/"
    suffix = "/suggest-wording"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    raw = path[len(prefix):-len(suffix)]
    clause_id = unquote(raw).strip("/")
    if not clause_id or "/" in clause_id:
        return None
    return clause_id


def handle_playbook_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        payload = load_playbook_workspace(playbook_path=playbook_path, include_playbook=True)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def handle_playbook_draft_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        payload = load_playbook_workspace(playbook_path=playbook_path, include_playbook=False)
    except PlaybookAuthoringError as error:
        if error.status == 500 and error.payload.get("error") == "Playbook could not be loaded.":
            handler._send_json({"error": "Playbook draft could not be loaded."}, status=500, send_body=send_body)
            return
        handler._send_json(error.payload, status=error.status, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def handle_playbook_validate_draft(handler, *, playbook_path=PLAYBOOK_PATH) -> None:
    if not require_admin(handler):
        return
    del playbook_path
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = validate_playbook_draft(payload)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_draft_save(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = save_playbook_draft(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_draft_discard(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload() or {}
    try:
        response = discard_playbook_draft(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_publish(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = publish_playbook(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_save(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = save_active_playbook(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_restore(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = restore_playbook_history_entry(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_suggest_wording(handler, path: str, *, playbook_path=PLAYBOOK_PATH) -> None:
    """POST /api/playbook/clause/<clause_id>/suggest-wording -- admin-only.

    Proposes MINIMAL AI edits to a clause's dependent free-text so it reflects the
    admin's in-progress list change. Never persists; the publish flow still owns
    saving. Fail-soft: an AI error returns empty ``suggestions`` + a warning, never a
    500 that would lose the admin's draft.
    """
    if not require_admin(handler):
        return
    clause_id = parse_suggest_wording_clause_id(path)
    if clause_id is None:
        handler._send_json({"error": "Not found"}, status=404)
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    if not isinstance(payload, dict):
        handler._send_json({"error": "Request body must be a JSON object."}, status=400)
        return
    clause = payload.get("clause")
    if not isinstance(clause, dict):
        handler._send_json(
            {"error": "Request must include a 'clause' object."}, status=400
        )
        return
    # The path id is authoritative: stamp it onto the clause so a body/path mismatch
    # cannot validate (or propose) against a different clause than the URL names.
    clause = {**clause, "id": clause_id}
    raw_fields = payload.get("fields")
    fields = (
        [field for field in raw_fields if isinstance(field, str)]
        if isinstance(raw_fields, list)
        else []
    )
    response = suggest_clause_wording(
        clause=clause, fields=fields, playbook_path=playbook_path
    )
    handler._send_json(response)
