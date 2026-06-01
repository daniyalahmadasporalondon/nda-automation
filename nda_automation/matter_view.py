from __future__ import annotations

from typing import Any, TypedDict

from .gmail_integration import matter_reply_recipient


class PublicMatter(TypedDict, total=False):
    attachment_filename: str
    board_column: str
    can_send_redline: bool
    created_at: str
    document_title: str
    extracted_text: str
    gmail_account: str
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
    redline_draft: dict[str, Any]
    received_at: str
    requirements_failed: int
    requirements_passed: int
    review_result: dict[str, Any]
    reply_to: str
    sender: str
    send_block_reason: str
    source_filename: str
    source_type: str
    status: str
    subject: str
    triage_status: str
    updated_at: str


PUBLIC_MATTER_FIELDS = {
    "attachment_filename",
    "board_column",
    "created_at",
    "document_title",
    "extracted_text",
    "gmail_account",
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
    "redline_draft",
    "received_at",
    "requirements_failed",
    "requirements_passed",
    "review_result",
    "reply_to",
    "sender",
    "send_block_reason",
    "source_filename",
    "source_type",
    "status",
    "subject",
    "triage_status",
    "updated_at",
}

SUMMARY_MATTER_OMIT_FIELDS = {
    "extracted_text",
    "redline_draft",
    "review_result",
}


def public_matter(matter: dict[str, Any], *, detail: bool = True) -> PublicMatter:
    recipient = matter_reply_recipient(matter)
    send_block_reason = ""
    if recipient and _same_email_address(recipient, str(matter.get("gmail_account") or "")):
        send_block_reason = (
            "Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline "
            f"back to {recipient}."
        )
    allowed_fields = PUBLIC_MATTER_FIELDS if detail else PUBLIC_MATTER_FIELDS - SUMMARY_MATTER_OMIT_FIELDS
    public = {
        key: value
        for key, value in matter.items()
        if key in allowed_fields
    }
    public.update({
        "recipient_email": recipient,
        "can_send_redline": bool(recipient and not send_block_reason),
    })
    if send_block_reason:
        public["send_block_reason"] = send_block_reason
    return public


def public_matters(matters: list[dict[str, Any]]) -> list[PublicMatter]:
    return [public_matter(matter, detail=False) for matter in matters]


def _same_email_address(left: str, right: str) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())
