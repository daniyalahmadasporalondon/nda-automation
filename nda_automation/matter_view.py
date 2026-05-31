from __future__ import annotations

from typing import Any, TypedDict

from .gmail_integration import recipient_email


class PublicMatter(TypedDict, total=False):
    attachment_filename: str
    board_column: str
    can_send_redline: bool
    counterparty_name: str
    created_at: str
    document_title: str
    extracted_text: str
    id: str
    issue_count: int
    last_outbound_account: str
    last_outbound_at: str
    last_outbound_filename: str
    last_outbound_message_id: str
    last_outbound_subject: str
    last_outbound_thread_id: str
    last_outbound_to: str
    message_snippet: str
    next_action: str
    recipient_email: str
    received_at: str
    requirements_failed: int
    requirements_passed: int
    review_result: dict[str, Any]
    sender: str
    source_filename: str
    source_type: str
    status: str
    subject: str
    triage_status: str
    updated_at: str


PUBLIC_MATTER_FIELDS = {
    "attachment_filename",
    "board_column",
    "counterparty_name",
    "created_at",
    "document_title",
    "extracted_text",
    "id",
    "issue_count",
    "last_outbound_account",
    "last_outbound_at",
    "last_outbound_filename",
    "last_outbound_message_id",
    "last_outbound_subject",
    "last_outbound_thread_id",
    "last_outbound_to",
    "message_snippet",
    "next_action",
    "received_at",
    "requirements_failed",
    "requirements_passed",
    "review_result",
    "sender",
    "source_filename",
    "source_type",
    "status",
    "subject",
    "triage_status",
    "updated_at",
}


def public_matter(matter: dict[str, Any]) -> PublicMatter:
    recipient = recipient_email(matter.get("sender"))
    public = {
        key: value
        for key, value in matter.items()
        if key in PUBLIC_MATTER_FIELDS
    }
    public.update({
        "recipient_email": recipient,
        "can_send_redline": bool(recipient),
    })
    return public


def public_matters(matters: list[dict[str, Any]]) -> list[PublicMatter]:
    return [public_matter(matter) for matter in matters]
