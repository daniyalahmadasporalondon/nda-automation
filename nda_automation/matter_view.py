from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .gmail_integration import matter_reply_recipient, recipient_email
from .reference_resolver import resolve_document_references
from .review_document import split_document_paragraphs
from .review_state import aggregate_review_state, result_requires_human_review, review_state_from_result
from .workflow import workflow_state


class PublicMatter(TypedDict, total=False):
    approved_at: str
    approver: str
    attachment_filename: str
    board_column: str
    can_send_redline: bool
    created_at: str
    document_title: str
    gmail_account: str
    gmail_attachment_selector: str
    gmail_attachment_selector_confidence: str
    gmail_attachment_selector_model: str
    gmail_attachment_selector_reason: str
    has_redline_draft: bool
    human_reviewed: bool
    id: str
    issue_count: int
    last_outbound_account: str
    last_outbound_at: str
    last_outbound_filename: str
    last_outbound_message_id: str
    last_outbound_subject: str
    last_outbound_thread_id: str
    last_outbound_to: str
    matter_timeline: list[dict[str, Any]]
    message_snippet: str
    next_action: str
    recipient_email: str
    received_at: str
    recipient_redirected_from_reply_to: bool
    recipient_warning: str
    requirements_failed: int
    requirements_needs_review: int
    requirements_passed: int
    reply_to: str
    review_state: dict[str, Any]
    reviewer_decisions: dict[str, Any]
    sender: str
    send_block_reason: str
    source_filename: str
    source_type: str
    status: str
    subject: str
    triage_status: str
    updated_at: str
    workflow_state: dict[str, Any]


PUBLIC_MATTER_FIELDS = {
    "approved_at",
    "approver",
    "attachment_filename",
    "board_column",
    "created_at",
    "document_title",
    "gmail_account",
    "gmail_attachment_selector",
    "gmail_attachment_selector_confidence",
    "gmail_attachment_selector_model",
    "gmail_attachment_selector_reason",
    "human_reviewed",
    "id",
    "issue_count",
    "last_outbound_account",
    "last_outbound_at",
    "last_outbound_filename",
    "last_outbound_message_id",
    "last_outbound_subject",
    "last_outbound_thread_id",
    "last_outbound_to",
    "matter_timeline",
    "message_snippet",
    "next_action",
    "received_at",
    "requirements_failed",
    "requirements_needs_review",
    "requirements_passed",
    "reply_to",
    "review_state",
    "reviewer_decisions",
    "sender",
    "send_block_reason",
    "source_filename",
    "source_type",
    "status",
    "subject",
    "triage_status",
    "updated_at",
}


def public_matter(matter: dict[str, Any], *, detail: bool = True) -> PublicMatter:
    recipient = matter_reply_recipient(matter)
    send_block_reason = ""
    if recipient and _same_email_address(recipient, str(matter.get("gmail_account") or "")):
        send_block_reason = (
            "Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline "
            f"back to {recipient}."
        )
    elif matter_needs_human_review(matter) and not matter.get("human_reviewed"):
        send_block_reason = "Matter needs human review before a redline can be sent."
    elif not recipient:
        send_block_reason = "Matter does not have a valid reply recipient email address."
    public = {
        key: value
        for key, value in matter.items()
        if key in PUBLIC_MATTER_FIELDS
    }
    public.update({
        "recipient_email": recipient,
        "can_send_redline": bool(recipient and not send_block_reason),
        "has_redline_draft": isinstance(matter.get("redline_draft"), dict),
        "human_reviewed": bool(matter.get("human_reviewed")),
    })
    recipient_warning = _recipient_redirect_warning(matter, recipient)
    if recipient_warning:
        public["recipient_redirected_from_reply_to"] = True
        public["recipient_warning"] = recipient_warning
    review_state = matter_review_state(matter)
    if review_state:
        public["review_state"] = review_state
    # The canonical workflow state (phase/status/next_action/human_gate/
    # needs_attention) -- one derived source the UI and automation read instead of
    # guessing from the overlapping status/board/triage fields. Its
    # next_action SUPERSEDES the legacy free-text top-level next_action.
    workflow = workflow_state(matter)
    public["workflow_state"] = workflow
    public["next_action"] = workflow["next_action"]["label"]
    if send_block_reason:
        public["send_block_reason"] = send_block_reason
    return public


def matter_needs_human_review(matter: dict[str, Any]) -> bool:
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        return result_requires_human_review(review_result)
    review_state = matter.get("review_state")
    if isinstance(review_state, dict):
        if bool(review_state.get("requires_human_review")) or bool(review_state.get("blocks_send")):
            return True
        if str(review_state.get("state") or "") == "review":
            return True
    try:
        return int(matter.get("requirements_needs_review") or 0) > 0
    except (TypeError, ValueError):
        return True


def matter_review_state(matter: dict[str, Any]) -> dict[str, Any]:
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        return review_state_from_result(review_result)
    existing = matter.get("review_state")
    if isinstance(existing, dict) and existing.get("state"):
        return existing
    if any(key in matter for key in ["requirements_passed", "requirements_needs_review", "requirements_failed"]):
        return aggregate_review_state(
            [],
            pass_count=_safe_count(matter.get("requirements_passed")),
            review_count=_safe_count(matter.get("requirements_needs_review")),
            check_count=_safe_count(matter.get("requirements_failed")),
        )
    return {}


def review_matter(matter: dict[str, Any]) -> dict[str, Any]:
    extracted_text = str(matter.get("extracted_text") or "")
    review_payload = {
        "matter": public_matter(matter),
        "extracted_text": extracted_text,
    }
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        review_payload["review_result"] = review_result_with_structure(review_result, extracted_text)
    ai_first_review_metadata = matter.get("ai_first_review_metadata")
    if isinstance(ai_first_review_metadata, dict):
        review_payload["ai_first_review_metadata"] = ai_first_review_metadata
    ai_first_review_result = matter.get("ai_first_review_result")
    if isinstance(ai_first_review_result, dict):
        review_payload["ai_first_review_result"] = review_result_with_structure(ai_first_review_result, extracted_text)
    redline_draft = matter.get("redline_draft")
    if isinstance(redline_draft, dict):
        review_payload["redline_draft"] = redline_draft
    return review_payload


def _safe_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def review_result_with_structure(review_result: dict[str, Any], extracted_text: str = "") -> dict[str, Any]:
    if (
        isinstance(review_result.get("contract_structure"), dict)
        and isinstance(review_result.get("reference_resolver"), dict)
        and isinstance(review_result.get("concept_classifier"), dict)
    ):
        return review_result

    enriched = deepcopy(review_result)
    paragraphs = enriched.get("paragraphs")
    if not isinstance(paragraphs, list):
        paragraphs = split_document_paragraphs(extracted_text)
        if paragraphs:
            enriched["paragraphs"] = paragraphs
    if not isinstance(enriched.get("contract_structure"), dict):
        enriched["contract_structure"] = build_contract_structure(paragraphs if isinstance(paragraphs, list) else [])
    if not isinstance(enriched.get("reference_resolver"), dict):
        enriched["reference_resolver"] = resolve_document_references(
            paragraphs if isinstance(paragraphs, list) else [],
            enriched["contract_structure"],
        )
    if not isinstance(enriched.get("concept_classifier"), dict):
        enriched["concept_classifier"] = classify_document_concepts(
            paragraphs if isinstance(paragraphs, list) else [],
            enriched["contract_structure"],
        )
    return enriched


def public_matters(matters: list[dict[str, Any]]) -> list[PublicMatter]:
    return [public_matter(matter, detail=False) for matter in matters]


def _same_email_address(left: str, right: str) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def _recipient_redirect_warning(matter: dict[str, Any], recipient: str) -> str:
    """Warn when the resolved recipient came from an untrusted ``Reply-To``.

    The inbound ``Reply-To`` header is attacker-controlled. When it points at a
    different address than the verified ``From`` sender, honouring it silently
    would let a spoofed ``Reply-To`` redirect the outbound document. We still
    surface the matter so a human can decide, but we flag the divergence so the
    operator confirms the destination deliberately rather than by default.

    We fail toward warning: if the ``From`` sender is absent or unparseable we
    cannot confirm the Reply-To matches a verified participant, so we treat that
    as a divergence and warn. Otherwise an attacker could suppress the warning by
    pairing a spoofed Reply-To with a malformed From header.
    """
    if not recipient:
        return ""
    reply_to_recipient = recipient_email(matter.get("reply_to"))
    if not reply_to_recipient or not _same_email_address(recipient, reply_to_recipient):
        return ""
    sender_recipient = recipient_email(matter.get("sender"))
    if sender_recipient and _same_email_address(reply_to_recipient, sender_recipient):
        return ""
    sender_label = sender_recipient or "an unverified sender"
    return (
        f"Reply-To ({reply_to_recipient}) differs from the sender ({sender_label}). "
        "Confirm the recipient before sending."
    )
