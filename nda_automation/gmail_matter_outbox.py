from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses
from typing import Any

from . import app_settings


EMAIL_IN_TEXT_PATTERN = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"


def send_redline_email(
    matter: dict[str, Any],
    attachment_bytes: bytes,
    attachment_filename: str,
    *,
    transport: Any,
    body: str | None = None,
    subject: str | None = None,
    to: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    recipient, service, outbound_account = outbound_send_context(
        matter,
        transport=transport,
        recipient_override=to,
        confirmed_recipient=confirmed_recipient,
        owner_user_id=owner_user_id,
    )
    outbound_subject = subject or reply_subject(str(matter.get("subject") or matter.get("document_title") or "NDA redline"))
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = outbound_subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body or default_outbound_body(matter, owner_user_id=owner_user_id))
    message.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=attachment_filename,
    )

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    gmail_payload: dict[str, Any] = {"raw": raw_message}
    thread_id = str(matter.get("gmail_thread_id") or "").strip()
    if thread_id and can_reply_in_thread(matter, outbound_account):
        gmail_payload["threadId"] = thread_id

    try:
        sent_message = service.users().messages().send(userId="me", body=gmail_payload).execute()
    except Exception as exc:
        transport.raise_gmail_api_error(exc, "Gmail outbound send failed.")

    return {
        "message_id": str(sent_message.get("id") or ""),
        "outbound_account": outbound_account,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "subject": outbound_subject,
        "thread_id": str(sent_message.get("threadId") or ""),
        "to": recipient,
    }


def validate_outbound_send_ready(
    matter: dict[str, Any],
    *,
    transport: Any,
    to: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    recipient, _service, outbound_account = outbound_send_context(
        matter,
        transport=transport,
        recipient_override=to,
        confirmed_recipient=confirmed_recipient,
        owner_user_id=owner_user_id,
    )
    return {"outbound_account": outbound_account, "to": recipient}


def outbound_send_context(
    matter: dict[str, Any],
    *,
    transport: Any,
    recipient_override: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> tuple[str, Any, str]:
    operator_recipient = recipient_email(recipient_override)
    recipient = operator_recipient or matter_reply_recipient(matter)
    if not recipient:
        raise transport.GmailIntegrationError("Matter does not have a valid reply recipient email address.")
    require_confirmed_recipient(
        recipient,
        confirmed_recipient,
        transport=transport,
        recipient_from_inbound_header=not operator_recipient,
    )
    if not transport.gmail_role_enabled("outbound"):
        raise transport.GmailIntegrationError("Gmail outbound is disabled in Admin.")

    owner_user_id = transport.clean_user_token_segment(owner_user_id)
    service = transport.gmail_service_for_owner("outbound", owner_user_id)
    profile = transport.gmail_profile_for_role("outbound", service=service, owner_user_id=owner_user_id)
    outbound_account = str(profile.get("emailAddress") or "")
    if not is_valid_email_address(outbound_account):
        raise transport.GmailIntegrationError("Gmail outbound profile did not include a valid email address.")
    ensure_recipient_is_not_own_account(matter, recipient, outbound_account, transport=transport)
    ensure_outbound_matches_inbound(matter, outbound_account, transport=transport)
    return recipient, service, outbound_account


def require_confirmed_recipient(
    recipient: str,
    confirmed_recipient: str | None,
    *,
    transport: Any,
    recipient_from_inbound_header: bool = True,
) -> None:
    if confirmed_recipient is None:
        if recipient_from_inbound_header:
            raise transport.RecipientConfirmationError(
                "Confirm the outbound recipient email address before sending."
            )
        return
    confirmed = recipient_email(confirmed_recipient)
    if not confirmed:
        raise transport.RecipientConfirmationError(
            "Confirm the outbound recipient email address before sending."
        )
    if not email_addresses_match(confirmed, recipient):
        raise transport.RecipientConfirmationError(
            "The confirmed recipient does not match the matter recipient; refusing to send. "
            f"Confirm sending to {recipient}."
        )


def matter_reply_recipient(matter: dict[str, Any]) -> str:
    return recipient_email(matter.get("reply_to")) or recipient_email(matter.get("sender"))


def recipient_email(value: object) -> str:
    if not isinstance(value, str):
        return ""
    addresses = [(display.strip(), email.strip()) for display, email in getaddresses([value]) if email.strip()]
    if len(addresses) != 1:
        return ""
    display_name, email_address = addresses[0]
    if not is_valid_email_address(email_address):
        return ""
    canonical_email = email_address.lower()
    display_emails = re.findall(EMAIL_IN_TEXT_PATTERN, display_name, flags=re.IGNORECASE)
    if any(display_email.lower() != canonical_email for display_email in display_emails):
        return ""
    return canonical_email


def is_valid_email_address(email_address: str) -> bool:
    if "@" not in email_address or any(character.isspace() for character in email_address):
        return False
    local_part, _at, domain = email_address.rpartition("@")
    if not local_part or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return True


def can_reply_in_thread(matter: dict[str, Any], outbound_account: str) -> bool:
    inbound_account = str(matter.get("gmail_account") or "").strip().casefold()
    return bool(inbound_account and outbound_account and inbound_account == outbound_account.strip().casefold())


def ensure_outbound_matches_inbound(matter: dict[str, Any], outbound_account: str, *, transport: Any) -> None:
    inbound_account = str(matter.get("gmail_account") or "").strip()
    if not inbound_account:
        return
    if inbound_account.casefold() == outbound_account.strip().casefold():
        return
    raise transport.GmailIntegrationError(
        "Outbound Gmail account "
        f"{outbound_account or 'unknown'} does not match inbound Gmail account {inbound_account}. "
        f"Reconnect the outbound Gmail token for {inbound_account} before sending this redline."
    )


def ensure_recipient_is_not_own_account(
    matter: dict[str, Any],
    recipient: str,
    outbound_account: str,
    *,
    transport: Any,
) -> None:
    own_accounts = [
        str(matter.get("gmail_account") or ""),
        outbound_account,
    ]
    if any(email_addresses_match(recipient, own_account) for own_account in own_accounts):
        raise transport.GmailIntegrationError(
            "Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline "
            f"back to {recipient}."
        )


def email_addresses_match(left: str, right: str) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def reply_subject(subject: str) -> str:
    cleaned = " ".join(subject.split()) or "NDA redline"
    return cleaned if cleaned.lower().startswith("re:") else f"Re: {cleaned}"


def default_outbound_body(matter: dict[str, Any], *, owner_user_id: str = "") -> str:
    subject = str(matter.get("subject") or matter.get("document_title") or "the NDA")
    return (
        f"Hi,\n\n"
        f"Please find attached the redlined version of {subject}.\n\n"
        f"{personalisation_signature_block(owner_user_id=owner_user_id)}"
    )


def personalisation_signature_block(*, owner_user_id: str = "") -> str:
    # Resolve per-user override -> admin/global default -> built-in default so the
    # signature is always present and the sender's own personalisation wins.
    settings = app_settings.resolved_personalisation_settings(owner_user_id)
    signature_block = str(settings.get("signature_block") or "").strip()
    if signature_block:
        return signature_block
    parts = [
        str(settings.get("sign_off") or "").strip(),
        str(settings.get("signature") or "").strip(),
    ]
    cleaned_parts = [part for part in parts if part]
    return "\n".join(cleaned_parts) if cleaned_parts else "Best,\nAspora Legal"
