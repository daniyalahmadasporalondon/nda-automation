"""Read-only Dashboard assistant command handling.

This is intentionally narrower than a general chat assistant. It classifies a
short command/query into one of the supported Dashboard intents, answers only
from scoped repository facts, and returns action requests instead of performing
side effects.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from . import dashboard_search_intent, matter_view, workflow
from .matter_repository import MatterRepository
from .untrusted_text import neutralize_untrusted_text

DASHBOARD_ASSISTANT_VERSION = 1
MAX_QUERY_CHARS = 500
MAX_CITATIONS = 5

AssistantSearchResolver = Callable[[str], dict[str, Any]]


def handle_dashboard_assistant_command(
    query: str,
    *,
    repository: MatterRepository,
    owner_user_id: str = "",
    search_resolver: AssistantSearchResolver | None = None,
) -> dict[str, Any]:
    """Return a typed, side-effect-free Dashboard assistant response."""
    cleaned_query = _clean_query(query)
    lowered = cleaned_query.lower()
    if not cleaned_query:
        return unsupported_response(cleaned_query, message="Ask me to search matters, answer a repository question, or start an NDA draft.")

    if _looks_like_generation_request(lowered):
        return draft_action_request_response(cleaned_query)

    public_matters = _public_matters(repository.list_matters(owner_user_id=owner_user_id))
    if _looks_like_count_in_review_question(lowered):
        return count_in_review_response(cleaned_query, public_matters)
    if _looks_like_last_sent_question(lowered):
        return last_sent_response(cleaned_query, public_matters)

    if _looks_like_search_request(lowered):
        search = _search_response(cleaned_query, search_resolver=search_resolver)
        if search and not search.get("fallback") and search.get("filters") is not None:
            filters = search.get("filters")
            if isinstance(filters, Mapping) and not dashboard_search_intent.filter_spec_is_empty(filters):
                return {
                    "intent": "search_filter",
                    "version": DASHBOARD_ASSISTANT_VERSION,
                    "query": cleaned_query,
                    "message": "Search filters are ready.",
                    "search": search,
                }

    return unsupported_response(cleaned_query)


def count_in_review_response(query: str, public_matters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matches = [
        matter for matter in public_matters
        if _matter_phase(matter) == workflow.PHASE_REVIEW
    ]
    count = len(matches)
    noun = "document is" if count == 1 else "documents are"
    return {
        "intent": "repository_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": query,
        "question": "count_in_review",
        "answer": {
            "text": f"{count} {noun} in review.",
            "count": count,
            "phase": workflow.PHASE_REVIEW,
        },
        "citations": _matter_citations(matches),
    }


def last_sent_response(query: str, public_matters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sent = [
        matter for matter in public_matters
        if _parse_timestamp(matter.get("last_outbound_at")) is not None
    ]
    sent.sort(
        key=lambda matter: _parse_timestamp(matter.get("last_outbound_at")) or datetime.min,
        reverse=True,
    )
    if not sent:
        return {
            "intent": "repository_question",
            "version": DASHBOARD_ASSISTANT_VERSION,
            "query": query,
            "question": "last_sent",
            "answer": {
                "text": "No sent NDAs were found in the repository.",
                "count": 0,
            },
            "citations": [],
        }

    matter = sent[0]
    sent_at = str(matter.get("last_outbound_at") or "")
    title = _matter_title(matter)
    recipient = str(matter.get("last_outbound_to") or "").strip()
    recipient_phrase = f" to {recipient}" if recipient else ""
    return {
        "intent": "repository_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": query,
        "question": "last_sent",
        "answer": {
            "text": f'The last NDA sent was "{title}"{recipient_phrase} on {sent_at}.',
            "sent_at": sent_at,
            "recipient": recipient,
            "matter_id": str(matter.get("id") or ""),
        },
        "citations": _matter_citations([matter]),
    }


def draft_action_request_response(query: str) -> dict[str, Any]:
    return {
        "intent": "draft_action_request",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": query,
        "action": "open_generator",
        "requires_confirmation": True,
        "message": "I can help start an NDA draft. Open the Generator, review the intake, then choose Generate when you are ready.",
        "generator": {
            "prefill": _generator_prefill(query),
            "missing_fields": [
                "signing_entity",
                "counterparty_name",
                "counterparty_registered_office",
                "purpose",
            ],
        },
        "side_effects": [],
    }


def unsupported_response(query: str, *, message: str | None = None) -> dict[str, Any]:
    return {
        "intent": "unsupported",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": query,
        "message": message
        or "I can search matters, answer repository status questions, or help start an NDA draft. I cannot do that request yet.",
    }


def _search_response(query: str, *, search_resolver: AssistantSearchResolver | None) -> dict[str, Any] | None:
    if search_resolver is not None:
        return search_resolver(query)
    try:
        return dashboard_search_intent.deterministic_search_intent(query)
    except Exception:  # noqa: BLE001 - unsupported is safer than surfacing parser internals
        return None


def _public_matters(matters: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(matter_view.public_matter(matter)) for matter in matters]


def _looks_like_count_in_review_question(lowered: str) -> bool:
    return _contains_any(lowered, ("how many", "number of", "count")) and _contains_any(
        lowered,
        ("in review", "under review", "reviewing"),
    )


def _looks_like_last_sent_question(lowered: str) -> bool:
    return _contains_any(lowered, ("last", "latest", "most recent", "recent")) and _contains_any(
        lowered,
        ("sent", "sent to me", "sent out"),
    )


def _looks_like_generation_request(lowered: str) -> bool:
    return _contains_any(lowered, ("generate", "create", "draft", "prepare", "start")) and _contains_any(
        lowered,
        ("nda", "non-disclosure", "agreement"),
    )


def _looks_like_search_request(lowered: str) -> bool:
    return _contains_any(
        lowered,
        (
            "approval",
            "approved",
            "awaiting",
            "counterparty",
            "doc",
            "document",
            "find",
            "in review",
            "issue",
            "list",
            "matter",
            "nda",
            "pending",
            "review",
            "search",
            "sent",
            "show",
            "signature",
            "stuck",
        ),
    )


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _clean_query(query: str) -> str:
    return neutralize_untrusted_text(str(query or ""), max_chars=MAX_QUERY_CHARS).strip()


def _matter_phase(matter: Mapping[str, Any]) -> str:
    workflow_state = matter.get("workflow_state")
    if isinstance(workflow_state, Mapping):
        return str(workflow_state.get("phase") or "").strip().lower()
    return ""


def _matter_title(matter: Mapping[str, Any]) -> str:
    return str(
        matter.get("subject")
        or matter.get("document_title")
        or matter.get("source_filename")
        or "Untitled NDA"
    )


def _matter_citations(matters: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for matter in matters[:MAX_CITATIONS]:
        citation = {
            "matter_id": str(matter.get("id") or ""),
            "title": _matter_title(matter),
            "workflow_phase": _matter_phase(matter),
        }
        sent_at = str(matter.get("last_outbound_at") or "").strip()
        if sent_at:
            citation["last_outbound_at"] = sent_at
        citations.append(citation)
    return citations


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _generator_prefill(query: str) -> dict[str, str]:
    return {
        "source": "dashboard_assistant",
        "prompt": query,
    }
