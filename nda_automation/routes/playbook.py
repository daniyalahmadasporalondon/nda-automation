from __future__ import annotations

import os

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

__all__ = [
    "collect_playbook_validation_errors",
    "handle_playbook_draft_discard",
    "handle_playbook_draft_get",
    "handle_playbook_draft_save",
    "handle_playbook_get",
    "handle_playbook_publish",
    "handle_playbook_restore",
    "handle_playbook_save",
    "handle_playbook_validate_draft",
]


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
    payload = handler._read_json_payload() or {}
    try:
        response = discard_playbook_draft(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)


def handle_playbook_publish(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
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
    payload = handler._read_json_payload()
    if payload is None:
        return
    try:
        response = restore_playbook_history_entry(payload, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError as error:
        handler._send_json(error.payload, status=error.status)
        return
    handler._send_json(response)
