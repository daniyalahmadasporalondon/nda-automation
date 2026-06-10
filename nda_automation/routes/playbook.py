from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..checker import PLAYBOOK_PATH, PlaybookTemplateError, validate_playbook
from ..playbook_rules import PlaybookRulesError, validate_playbook_rules
from ..playbook_runtime import (
    PlaybookDraftConflict,
    discard_playbook_draft,
    load_playbook_state,
    publish_playbook,
    public_playbook_draft,
    public_playbook_draft_payload,
    public_playbook_history,
    public_playbook_runtime,
    read_playbook_from_path as read_playbook_from_path,
    restore_playbook,
    save_playbook,
    save_playbook_draft,
)


def handle_playbook_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        state = load_playbook_state(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError):
        handler._send_json({"error": "Playbook could not be loaded."}, status=500, send_body=send_body)
        return
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    playbook = state["playbook"]
    runtime = state["runtime"]
    draft = state["draft"]
    history = state["history"]
    handler._send_json(
        {
            "playbook": playbook,
            "active": {
                "playbook": playbook,
                "metadata": public_playbook_runtime(runtime),
            },
            "draft": public_playbook_draft_payload(runtime, draft),
            "history": public_playbook_history(history),
        },
        send_body=send_body,
    )


def handle_playbook_draft_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        state = load_playbook_state(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError):
        handler._send_json({"error": "Playbook draft could not be loaded."}, status=500, send_body=send_body)
        return
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    playbook = state["playbook"]
    runtime = state["runtime"]
    draft = state["draft"]
    history = state["history"]

    handler._send_json(
        {
            "active": {
                "playbook": playbook,
                "metadata": public_playbook_runtime(runtime),
            },
            "draft": public_playbook_draft_payload(runtime, draft),
            "history": public_playbook_history(history),
        },
        send_body=send_body,
    )


_PLAYBOOK_RULES_FAILURE_PREFIX = "Playbook rules validation failed: "
_TOP_LEVEL_PLAYBOOK_FIELDS = ("name", "version", "clauses")

# Field paths the validators name in their messages, longest-first so that
# "rules.approved_options" wins over "rules" and "approved_options".
_KNOWN_PLAYBOOK_FIELD_PATHS = (
    "rules.evidence_requirements.minimum_evidence_for_pass",
    "rules.evidence_requirements.minimum_evidence_for_fail",
    "rules.evidence_requirements.quote_required",
    "rules.evidence_requirements.guidance",
    "rules.evidence_requirements",
    "rules.redline_guidance.default_action",
    "rules.redline_guidance",
    "rules.approved_options",
    "rules.pass_conditions",
    "rules.fail_conditions",
    "rules.review_triggers",
    "rules.acceptable_position",
    "rules.clause_type",
    "rules.version",
    "rules",
    "approved_laws",
    "preferred_law",
    "law_phrases",
    "max_term_years",
    "indefinite_terms",
    "longer_survival_carve_out_terms",
    "search_terms",
    "taxonomy_groups",
    "semantic_signals",
    "redline_template",
    "preferred_position",
    "check_trigger",
    "requirement",
    "name",
    "type",
    "id",
)


def collect_playbook_validation_errors(playbook: Any) -> list[dict[str, Any]]:
    """Return every validation error for a candidate Playbook as structured records.

    The full contract validator (``validate_playbook``) fails fast and raises a
    single message (sometimes wrapping several rule errors), while the clause-rule
    validator aggregates many. We gather messages from both, split the wrapped
    form, de-duplicate, and parse each into ``{location, clause, field, message,
    severity}`` so the editor can surface errors per clause and per field before a
    publish. Nothing is persisted.
    """
    if not isinstance(playbook, dict):
        return [_structured_playbook_error("Playbook payload must include a playbook object.")]

    messages: list[str] = []
    seen: set[str] = set()

    def _add(raw_message: str) -> None:
        message = str(raw_message or "").strip()
        if not message or message in seen:
            return
        seen.add(message)
        messages.append(message)

    try:
        validate_playbook(playbook)
    except PlaybookTemplateError as error:
        for part in _split_template_error(str(error)):
            _add(part)

    try:
        validate_playbook_rules(playbook)
    except PlaybookRulesError as error:
        for rule_error in error.errors:
            _add(rule_error)

    return [_structured_playbook_error(message) for message in messages]


def _split_template_error(message: str) -> list[str]:
    """Unwrap the aggregated ``Playbook rules validation failed: a; b`` form.

    The contract validator re-raises clause-rule failures as one joined string.
    Splitting it lets the parts de-duplicate against the clause-rule validator's
    own per-error messages instead of appearing twice.
    """
    text = str(message or "").strip()
    if not text:
        return []
    if text.startswith(_PLAYBOOK_RULES_FAILURE_PREFIX):
        remainder = text[len(_PLAYBOOK_RULES_FAILURE_PREFIX):]
        parts = [part.strip() for part in remainder.split(";")]
        return [part for part in parts if part]
    return [text]


def _structured_playbook_error(message: str) -> dict[str, Any]:
    clause_id, field = _locate_playbook_error(message)
    location = clause_id or ""
    if clause_id and field:
        location = f"{clause_id}.{field}"
    elif not clause_id and field:
        location = field
    return {
        "location": location,
        "clause": clause_id,
        "field": field,
        "message": message,
        "severity": "error",
    }


def _locate_playbook_error(message: str) -> tuple[str | None, str | None]:
    text = str(message or "").strip()
    clause_id: str | None = None
    match = re.match(r"^Playbook clause ([A-Za-z0-9_]+)\b", text)
    if match:
        clause_id = match.group(1)
    elif re.match(r"^Playbook clauses\[", text):
        # e.g. "Playbook clauses[2] must be an object." — index-addressed, no id.
        clause_id = None
    else:
        for top_field in _TOP_LEVEL_PLAYBOOK_FIELDS:
            if re.match(rf"^Playbook {top_field}\b", text):
                return None, top_field

    field = _extract_field_path(text)
    return clause_id, field


def _extract_field_path(message: str) -> str | None:
    # Attribute the error to the field named earliest in the message (the
    # grammatical subject, e.g. "... preferred_law must be in approved_laws"
    # is about preferred_law). Break ties on the longest path so
    # "rules.approved_options" wins over a bare "rules" at the same position.
    best: tuple[int, int, str] | None = None
    for field_path in _KNOWN_PLAYBOOK_FIELD_PATHS:
        match = re.search(rf"\b{re.escape(field_path)}\b", message)
        if match is None:
            continue
        candidate = (match.start(), -len(field_path), field_path)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else None


def handle_playbook_validate_draft(handler, *, playbook_path=PLAYBOOK_PATH) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        handler._send_json({"error": "Playbook draft payload must include a playbook object."}, status=400)
        return

    errors = collect_playbook_validation_errors(playbook)
    handler._send_json({"valid": not errors, "errors": errors})


def handle_playbook_draft_save(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        handler._send_json({"error": "Playbook draft payload must include a playbook object."}, status=400)
        return

    try:
        result = save_playbook_draft(
            playbook,
            payload,
            playbook_path=playbook_path,
            replace_file=replace_file,
        )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PlaybookDraftConflict as error:
        handler._send_json(error.payload, status=error.status)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be saved."}, status=500)
        return

    active_playbook = result["active_playbook"]
    runtime = result["runtime"]
    draft = result["draft"]
    history = result["history"]
    handler._send_json({
        "active": {
            "playbook": active_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": public_playbook_draft_payload(runtime, draft),
        "history": public_playbook_history(history),
        "saved_draft_at": draft["updated_at"],
    })


def handle_playbook_draft_discard(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload() or {}

    try:
        result = discard_playbook_draft(
            payload,
            playbook_path=playbook_path,
            replace_file=replace_file,
        )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PlaybookDraftConflict as error:
        handler._send_json(error.payload, status=error.status)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be discarded."}, status=500)
        return

    active_playbook = result["active_playbook"]
    runtime = result["runtime"]
    history = result["history"]
    handler._send_json({
        "active": {
            "playbook": active_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": None,
        "history": public_playbook_history(history),
        "discarded_draft_at": datetime.now(timezone.utc).isoformat(),
    })


def handle_playbook_publish(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        result = publish_playbook(
            payload,
            playbook_path=playbook_path,
            replace_file=replace_file,
        )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PlaybookDraftConflict as error:
        handler._send_json(error.payload, status=error.status)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be published."}, status=500)
        return

    published_playbook = result["playbook"]
    runtime = result["runtime"]
    history = result["history"]
    handler._send_json({
        "playbook": published_playbook,
        "active": {
            "playbook": published_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": None,
        "history": public_playbook_history(history),
        "published_at": runtime["published_at"],
    })


def handle_playbook_save(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        handler._send_json({"error": "Playbook payload must include a playbook object."}, status=400)
        return

    try:
        result = save_playbook(
            playbook,
            payload,
            playbook_path=playbook_path,
            replace_file=replace_file,
        )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be saved."}, status=500)
        return

    saved_playbook = result["playbook"]
    runtime = result["runtime"]
    history = result["history"]
    handler._send_json({
        "playbook": saved_playbook,
        "active": {
            "playbook": saved_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })


def handle_playbook_restore(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return
    history_id = str(payload.get("history_id") or "").strip()
    if not history_id:
        handler._send_json({"error": "Provide a playbook history id to restore."}, status=400)
        return

    try:
        result = restore_playbook(
            history_id,
            payload,
            playbook_path=playbook_path,
            replace_file=replace_file,
        )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PlaybookDraftConflict as error:
        handler._send_json(error.payload, status=error.status)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be restored."}, status=500)
        return

    restored_playbook = result["playbook"]
    runtime = result["runtime"]
    history = result["history"]
    handler._send_json({
        "playbook": restored_playbook,
        "active": {
            "playbook": restored_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "restored_at": datetime.now(timezone.utc).isoformat(),
    })
