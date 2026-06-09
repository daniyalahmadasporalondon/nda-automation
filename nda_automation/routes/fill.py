"""AI blank-linking suggestions for the inbound-NDA fill flow.

POST /api/fill-suggestions is what the Review tab's Fill panel calls to get
AI-assisted, per-blank fill suggestions. The frontend has already regex-detected
the blanks; here we ask the AI (Grok via OpenRouter) which named party is the
Aspora side, classify each blank, and decide whether to auto-fill it with Aspora
registry data.

The endpoint is intentionally thin: all the substance (packet building, the
injection neutralization, the registry value-grounding) lives in ``fill_ai``. Here
we validate + size-limit the payload, drive ``classify_blanks``, and shape the
response. The handler NEVER crashes the request: a missing API key returns status
"not_configured" and any AI error returns status "error", both of which tell the
frontend to fall back to its deterministic keyword heuristic.
"""

from __future__ import annotations

from typing import Any

from .. import fill_ai, telemetry

# Defensive input caps so a hostile/oversized payload can't blow memory before
# fill_ai's own budget-capping runs.
_MAX_DOCUMENT_CHARS = 200_000
_MAX_BLANKS = fill_ai.MAX_BLANKS
_MAX_BLANK_FIELD_CHARS = 4_000


def handle_fill_suggestions(handler) -> None:
    telemetry.increment("fill_suggestions_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        entity_id, document_text, blanks = _parse_payload(payload)
    except _PayloadError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    try:
        result = fill_ai.classify_blanks(document_text, blanks, entity_id)
    except Exception as error:  # noqa: BLE001 - never leak a stack; degrade gracefully
        # classify_blanks is contracted not to raise, but stay defensive: a bug
        # there must still not 500 the fill panel. Surface "error" so the FE falls
        # back to its heuristic.
        telemetry.increment("fill_suggestions_failed")
        handler._send_json(
            {"status": "error", "aspora_party": None, "classifications": [], "error": str(error)},
            status=200,
        )
        return

    status = str(result.get("status") or "error")
    telemetry.increment(f"fill_suggestions_{status}")
    handler._send_json(
        {
            "status": status,
            "aspora_party": result.get("aspora_party"),
            "classifications": list(result.get("classifications") or []),
        },
        status=200,
    )


class _PayloadError(ValueError):
    """A client payload problem (missing/invalid field)."""


def _parse_payload(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise _PayloadError("Request body must be a JSON object.")

    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise _PayloadError("entity_id is required.")

    document_text = payload.get("document_text")
    if not isinstance(document_text, str):
        raise _PayloadError("document_text must be a string.")
    document_text = document_text[:_MAX_DOCUMENT_CHARS]

    raw_blanks = payload.get("blanks")
    if not isinstance(raw_blanks, list):
        raise _PayloadError("blanks must be a list.")

    blanks: list[dict[str, Any]] = []
    for raw in raw_blanks[:_MAX_BLANKS]:
        if not isinstance(raw, dict):
            continue
        blank_id = raw.get("id")
        if not isinstance(blank_id, str) or not blank_id.strip():
            continue
        blanks.append(
            {
                "id": blank_id.strip(),
                "paragraph_id": _str_field(raw.get("paragraph_id")),
                "find": _str_field(raw.get("find")),
                "context": _str_field(raw.get("context")),
            }
        )

    return entity_id.strip(), document_text, blanks


def _str_field(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:_MAX_BLANK_FIELD_CHARS]
