from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..checker import PLAYBOOK_PATH, PlaybookTemplateError, validate_playbook
from ..playbook_rules import PlaybookRulesError, validate_playbook_rules
from ..playbook_runtime import (
    PLAYBOOK_RUNTIME_VERSION,
    PlaybookDraftConflict,
    _DRAFT_RUNTIME_KEYS,
    _active_runtime_from_playbook,
    _actor_from_payload,
    _draft_base_conflict,
    _draft_discard_history_entry,
    _draft_history_entry,
    _draft_payload_from_playbook,
    _draft_runtime_fields,
    _expected_active_conflict,
    _history_entry,
    _publish_candidate_from_payload,
    _publish_history_entry,
    _remove_file_durably,
    _runtime_fields_for_draft,
    draft_path_for,
    ensure_active_runtime_for_playbook,
    locked_playbook,
    public_playbook_draft,
    public_playbook_draft_payload,
    public_playbook_history,
    public_playbook_runtime,
    read_playbook_draft,
    read_playbook_from_path,
    read_playbook_history,
    read_playbook_runtime,
    write_active_playbook_bundle_atomically,
    write_playbook_draft,
    write_playbook_history,
    write_playbook_runtime,
)


def handle_playbook_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        with locked_playbook(playbook_path):
            playbook = read_playbook_from_path(playbook_path)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                playbook,
                playbook_path=playbook_path,
                source="bootstrap",
            )
            draft = read_playbook_draft(playbook_path=playbook_path)
            history = read_playbook_history(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError):
        handler._send_json({"error": "Playbook could not be loaded."}, status=500, send_body=send_body)
        return
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
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
        with locked_playbook(playbook_path):
            playbook = read_playbook_from_path(playbook_path)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                playbook,
                playbook_path=playbook_path,
                source="bootstrap",
            )
            draft = read_playbook_draft(playbook_path=playbook_path)
            history = read_playbook_history(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError):
        handler._send_json({"error": "Playbook draft could not be loaded."}, status=500, send_body=send_body)
        return
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return

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
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            conflict = _expected_active_conflict(payload, runtime)
            if conflict:
                handler._send_json(conflict, status=409)
                return
            draft = _draft_payload_from_playbook(playbook, active_playbook, runtime, payload)
            write_playbook_draft(draft, playbook_path=playbook_path, replace_file=replace_file)
            runtime = {
                **runtime,
                **_runtime_fields_for_draft(draft),
            }
            write_playbook_runtime(runtime, playbook_path=playbook_path, replace_file=replace_file)
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _draft_history_entry(draft, playbook, active_playbook))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be saved."}, status=500)
        return

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
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            draft = read_playbook_draft(playbook_path=playbook_path)
            draft_id = str((draft or {}).get("draft_id") or runtime.get("draft_id") or "")
            if not draft_id:
                handler._send_json({"error": "No Playbook draft exists."}, status=404)
                return
            requested_draft_id = str(payload.get("draft_id") or "").strip()
            if requested_draft_id and requested_draft_id != draft_id:
                handler._send_json({
                    "error": "The Playbook draft changed while this request was open.",
                    "code": "playbook_draft_conflict",
                    "draft": public_playbook_draft_payload(runtime, draft),
                }, status=409)
                return

            _remove_file_durably(draft_path_for(playbook_path))
            runtime = {key: value for key, value in runtime.items() if key not in _DRAFT_RUNTIME_KEYS}
            write_playbook_runtime(runtime, playbook_path=playbook_path, replace_file=replace_file)
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _draft_discard_history_entry(active_playbook, draft, payload))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be discarded."}, status=500)
        return

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
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            conflict = _expected_active_conflict(payload, runtime)
            if conflict:
                handler._send_json(conflict, status=409)
                return

            draft = read_playbook_draft(playbook_path=playbook_path)
            publish_playbook, source_draft = _publish_candidate_from_payload(payload, draft)
            if publish_playbook is None:
                handler._send_json({"error": "Provide a Playbook draft id or playbook object to publish."}, status=400)
                return
            conflict = _draft_base_conflict(source_draft, runtime)
            if conflict:
                handler._send_json(conflict, status=409)
                return
            validate_playbook(publish_playbook)

            runtime = _active_runtime_from_playbook(
                publish_playbook,
                actor=_actor_from_payload(payload),
                source="publish",
            )
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _publish_history_entry(
                publish_playbook,
                active_playbook,
                runtime,
                payload,
                source_draft,
            ))
            write_active_playbook_bundle_atomically(
                publish_playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
            if source_draft is not None:
                _remove_file_durably(draft_path_for(playbook_path))
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PlaybookDraftConflict as error:
        handler._send_json(error.payload, status=error.status)
        return
    except OSError:
        handler._send_json({"error": "Playbook draft could not be published."}, status=500)
        return

    handler._send_json({
        "playbook": publish_playbook,
        "active": {
            "playbook": publish_playbook,
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
        with locked_playbook(playbook_path):
            validate_playbook(playbook)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            history = read_playbook_history(playbook_path=playbook_path)
            if previous_playbook and not history:
                history.append(_history_entry(
                    previous_playbook,
                    action="baseline",
                    actor="system",
                    summary="Initial playbook snapshot before version history.",
                ))
            existing_runtime = read_playbook_runtime(playbook_path=playbook_path)
            runtime = {
                "version": PLAYBOOK_RUNTIME_VERSION,
                **_active_runtime_from_playbook(
                    playbook,
                    actor=_actor_from_payload(payload),
                    source="save",
                ),
                **_draft_runtime_fields(existing_runtime),
            }
            history.insert(0, _history_entry(
                playbook,
                actor=_actor_from_payload(payload),
                action="save",
                previous_playbook=previous_playbook,
            ))
            write_active_playbook_bundle_atomically(
                playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be saved."}, status=500)
        return

    handler._send_json({
        "playbook": playbook,
        "active": {
            "playbook": playbook,
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
        with locked_playbook(playbook_path):
            history = read_playbook_history(playbook_path=playbook_path)
            source_entry = next((entry for entry in history if str(entry.get("id") or "") == history_id), None)
            if source_entry is None:
                handler._send_json({"error": "Playbook history entry was not found."}, status=404)
                return
            snapshot = source_entry.get("snapshot")
            if not isinstance(snapshot, dict):
                handler._send_json({"error": "Playbook history entry does not include a restorable snapshot."}, status=409)
                return
            validate_playbook(snapshot)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            restored_playbook = json.loads(json.dumps(snapshot))
            existing_runtime = read_playbook_runtime(playbook_path=playbook_path)
            runtime = {
                "version": PLAYBOOK_RUNTIME_VERSION,
                **_active_runtime_from_playbook(
                    restored_playbook,
                    actor=_actor_from_payload(payload),
                    source="restore",
                ),
                **_draft_runtime_fields(existing_runtime),
            }
            history.insert(0, _history_entry(
                restored_playbook,
                actor=_actor_from_payload(payload),
                action="restore",
                previous_playbook=previous_playbook,
                restored_from_id=history_id,
                summary=f"Restored playbook version from {str(source_entry.get('recorded_at') or 'history')}.",
            ))
            write_active_playbook_bundle_atomically(
                restored_playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be restored."}, status=500)
        return

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
