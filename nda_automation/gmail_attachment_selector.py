from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from . import app_settings
from .ai_review import OPENROUTER_API_KEY_ENV, OPENROUTER_CHAT_COMPLETIONS_ENDPOINT, _trusted_https_context

GMAIL_TRIAGE_MODEL_ENV = "NDA_GMAIL_TRIAGE_MODEL"
DEFAULT_GMAIL_TRIAGE_MODEL = "google/gemini-3.5-flash"
DEFAULT_GMAIL_TRIAGE_TIMEOUT_SECONDS = 20
MIN_SELECTOR_CONFIDENCE = 0.70
MAX_CANDIDATE_TEXT_CHARS = 1800


class GmailAttachmentSelectorError(RuntimeError):
    pass


def selector_configured() -> bool:
    return bool(_configured_api_key())


def select_nda_attachments(
    *,
    message_metadata: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not candidates:
        return {"status": "not_needed", "selected_attachment_ids": []}
    api_key = _configured_api_key()
    if not api_key:
        return {"status": "not_configured", "selected_attachment_ids": []}

    request = urllib.request.Request(
        OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
        data=json.dumps(_request_body(message_metadata, candidates)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "nda-automation/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=DEFAULT_GMAIL_TRIAGE_TIMEOUT_SECONDS,
            context=_trusted_https_context(),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")[:500]
        raise GmailAttachmentSelectorError(f"OpenRouter API returned HTTP {error.code}: {message}") from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise GmailAttachmentSelectorError(f"OpenRouter API request failed: {error}") from error

    parsed = _parse_response(payload)
    candidate_ids = {str(candidate.get("attachment_id") or "") for candidate in candidates}
    selected_ids = [
        attachment_id
        for attachment_id in parsed["selected_attachment_ids"]
        if attachment_id in candidate_ids
    ]
    if not parsed.get("should_import") or not selected_ids or parsed["confidence"] < MIN_SELECTOR_CONFIDENCE:
        return {
            **parsed,
            "selected_attachment_ids": [],
            "status": "uncertain",
        }
    return {
        **parsed,
        "selected_attachment_ids": selected_ids,
        "status": "selected",
    }


def _configured_api_key() -> str:
    return (
        os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        or app_settings.stored_ai_api_key()
    )


def _configured_model() -> str:
    return os.environ.get(GMAIL_TRIAGE_MODEL_ENV, "").strip() or DEFAULT_GMAIL_TRIAGE_MODEL


def _request_body(message_metadata: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "model": _configured_model(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You select the actual NDA/confidentiality-agreement attachment from a Gmail message. "
                    "Reject project proposals, programme manager docs, statements of work, pricing, invoices, "
                    "questionnaires, and collateral documents. Return only JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(_selection_packet(message_metadata, candidates), ensure_ascii=False, indent=2),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def _selection_packet(message_metadata: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "task": "select_gmail_nda_attachment",
        "instructions": [
            "Return selected_attachment_ids as a list of attachment_id values.",
            "Set should_import true only when the email is asking for NDA/confidentiality-agreement review.",
            "Select only attachments that are actual NDAs or confidentiality agreements.",
            "If multiple attachments are collateral and only one is the NDA, select only the NDA.",
            "If unsure, return should_import false, an empty selected_attachment_ids list, and confidence below 0.70.",
            "Use filename, email subject/snippet/body signals, deterministic score/reasons, and text preview.",
        ],
        "message": {
            "subject": str(message_metadata.get("subject") or "")[:300],
            "sender": str(message_metadata.get("sender") or "")[:300],
            "snippet": str(message_metadata.get("message_snippet") or "")[:1000],
            "body_preview": str(message_metadata.get("message_body_preview") or "")[:2000],
            "detection_sources": str(message_metadata.get("gmail_detection_sources") or "")[:300],
            "detection_terms": str(message_metadata.get("gmail_detection_terms") or "")[:300],
        },
        "candidates": [_candidate_record(candidate) for candidate in candidates],
        "required_response_schema": {
            "should_import": "boolean",
            "selected_attachment_ids": ["attachment id strings"],
            "confidence": "number between 0 and 1",
            "reason": "short explanation",
        },
    }


def _candidate_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), Mapping) else {}
    return {
        "attachment_id": str(candidate.get("attachment_id") or ""),
        "filename": str(candidate.get("filename") or "")[:300],
        "part_id": str(candidate.get("part_id") or "")[:80],
        "deterministic_score": validation.get("score"),
        "deterministic_sources": validation.get("sources") if isinstance(validation.get("sources"), list) else [],
        "deterministic_terms": validation.get("terms") if isinstance(validation.get("terms"), list) else [],
        "deterministic_reason": str(validation.get("reason") or "")[:700],
        "deterministic_excerpt": str(validation.get("excerpt") or "")[:700],
        "text_preview": str(candidate.get("text_preview") or "")[:MAX_CANDIDATE_TEXT_CHARS],
    }


def _parse_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    content = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else {}
        if isinstance(message, Mapping):
            content = str(message.get("content") or "")
    if not content:
        raise GmailAttachmentSelectorError("OpenRouter API returned no message content.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise GmailAttachmentSelectorError("OpenRouter API returned non-JSON text.") from error
    if not isinstance(parsed, Mapping):
        raise GmailAttachmentSelectorError("OpenRouter API returned a non-object JSON response.")
    raw_ids = parsed.get("selected_attachment_ids")
    if isinstance(raw_ids, str):
        selected_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        selected_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    else:
        selected_ids = []
    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    should_import = parsed.get("should_import")
    if not isinstance(should_import, bool):
        should_import = bool(selected_ids)
    return {
        "should_import": should_import,
        "selected_attachment_ids": selected_ids,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(parsed.get("reason") or "")[:500],
        "model": _configured_model(),
    }
