from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

from . import app_settings, matter_store
from .checker import ParagraphAlignmentError
from .document_limits import DocumentSizeError, ensure_document_size
from .docx_text import DocxExtractionError
from .ingestion_service import create_matter_from_document, is_supported_document_filename
from .pdf_text import PdfExtractionError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NDA_SUBJECT_QUERY = (
    '(subject:NDA OR subject:"non-disclosure" OR subject:"non disclosure" '
    'OR subject:"non-disclosure agreement" OR subject:"non disclosure agreement" '
    'OR subject:"confidentiality agreement" OR subject:confidentiality OR subject:confidential)'
)
DEFAULT_INBOUND_QUERY = f"has:attachment (filename:docx OR filename:pdf) newer_than:30d {NDA_SUBJECT_QUERY}"
MAX_GMAIL_IMPORT_LIMIT = 25
EMAIL_IN_TEXT_PATTERN = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"

ROLE_TOKEN_ENV = {
    "inbound": "NDA_GMAIL_INBOUND_TOKEN_PATH",
    "outbound": "NDA_GMAIL_OUTBOUND_TOKEN_PATH",
}
_TOKEN_LOCK = threading.RLock()


class GmailIntegrationError(RuntimeError):
    pass


def gmail_status() -> dict[str, Any]:
    settings = app_settings.gmail_settings()
    status: dict[str, dict[str, Any]] = {"settings": settings}
    for role in ("inbound", "outbound"):
        enabled = bool(settings.get(f"{role}_enabled", True))
        role_status: dict[str, Any] = {
            "configured": False,
            "email": "",
            "enabled": enabled,
            "ready": False,
            "role": role,
        }
        if role == "inbound":
            role_status["query"] = DEFAULT_INBOUND_QUERY
        try:
            token_path = _token_path_for_role(role)
        except GmailIntegrationError:
            role_status["error"] = f"Set {ROLE_TOKEN_ENV[role]} for the {role} Gmail account."
            status[role] = role_status
            continue
        role_status["configured"] = token_path.is_file()
        if not token_path.is_file():
            role_status["error"] = f"Set {ROLE_TOKEN_ENV[role]} for the {role} Gmail account."
            status[role] = role_status
            continue
        try:
            profile = _gmail_profile(_gmail_service(role))
        except GmailIntegrationError as error:
            role_status["error"] = str(error)
        else:
            role_status["email"] = str(profile.get("emailAddress") or "")
            if enabled:
                role_status["ready"] = True
            else:
                role_status["error"] = f"Gmail {role} is disabled in Admin."
        status[role] = role_status
    return status


def import_inbound_matters(*, limit: int = 10, query: str | None = None) -> dict[str, Any]:
    if not app_settings.gmail_role_enabled("inbound"):
        raise GmailIntegrationError("Gmail inbound is disabled in Admin.")
    service = _gmail_service("inbound")
    profile = _gmail_profile(service)
    inbound_query = query.strip() if isinstance(query, str) and query.strip() else DEFAULT_INBOUND_QUERY
    import_limit = max(1, min(int(limit or 10), MAX_GMAIL_IMPORT_LIMIT))

    try:
        result = service.users().messages().list(
            userId="me",
            q=inbound_query,
            maxResults=import_limit,
        ).execute()
    except Exception as exc:
        raise GmailIntegrationError("Gmail inbound sync could not list messages.") from exc

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for message_stub in result.get("messages") or []:
        message_id = str(message_stub.get("id") or "")
        if not message_id:
            continue
        try:
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception:
            skipped.append({"message_id": message_id, "reason": "message_unavailable"})
            continue

        attachments = list(_reviewable_attachments(message.get("payload") or {}))
        if not attachments:
            skipped.append({"message_id": message_id, "reason": "no_reviewable_attachment"})
            continue

        metadata = _message_metadata(message, str(profile.get("emailAddress") or ""))
        for attachment in attachments:
            attachment_id = str(attachment.get("attachment_id") or "")
            attachment_filename = str(attachment.get("filename") or "")
            part_id = str(attachment.get("part_id") or "")
            if matter_store.find_gmail_attachment(
                message_id,
                attachment_id,
                part_id=part_id,
            ) is not None:
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "duplicate_attachment",
                })
                continue

            try:
                document_bytes = _attachment_bytes(service, message_id, attachment)
            except GmailIntegrationError:
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "attachment_unavailable",
                })
                continue
            try:
                ensure_document_size(document_bytes)
            except DocumentSizeError:
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "attachment_too_large",
                })
                continue
            attachment_sha256 = hashlib.sha256(document_bytes).hexdigest()
            if matter_store.find_gmail_attachment(
                message_id,
                attachment_id,
                attachment_filename=attachment_filename,
                attachment_sha256=attachment_sha256,
                part_id=part_id,
            ) is not None:
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "duplicate_attachment",
                })
                continue

            try:
                matter = create_matter_from_document(
                    filename=attachment_filename or "nda.docx",
                    document_bytes=document_bytes,
                    source_type="gmail_inbound",
                    board_column="gmail_demo",
                    intake_metadata={
                        **metadata,
                        "attachment_filename": attachment_filename or "nda.docx",
                        "gmail_attachment_id": attachment_id,
                        "gmail_attachment_sha256": attachment_sha256,
                        "gmail_part_id": part_id,
                    },
                    dedupe_gmail=True,
                )
            except (DocxExtractionError, PdfExtractionError, ParagraphAlignmentError):
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "review_failed",
                })
                continue
            if matter.get("_existing_gmail_duplicate"):
                skipped.append({
                    "attachment_filename": attachment_filename,
                    "message_id": message_id,
                    "reason": "duplicate_attachment",
                })
                continue
            imported.append(matter)

    return {
        "account": str(profile.get("emailAddress") or ""),
        "imported": imported,
        "query": inbound_query,
        "skipped": skipped,
    }


def send_redline_email(
    matter: dict[str, Any],
    attachment_bytes: bytes,
    attachment_filename: str,
    *,
    body: str | None = None,
    subject: str | None = None,
) -> dict[str, str]:
    recipient = recipient_email(matter.get("sender"))
    if not recipient:
        raise GmailIntegrationError("Matter sender is not a valid email address.")
    if not app_settings.gmail_role_enabled("outbound"):
        raise GmailIntegrationError("Gmail outbound is disabled in Admin.")

    service = _gmail_service("outbound")
    profile = _gmail_profile(service)
    outbound_account = str(profile.get("emailAddress") or "")
    _ensure_outbound_matches_inbound(matter, outbound_account)
    outbound_subject = subject or _reply_subject(str(matter.get("subject") or matter.get("document_title") or "NDA redline"))
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = outbound_subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body or _default_outbound_body(matter))
    message.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=attachment_filename,
    )

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    gmail_payload: dict[str, Any] = {"raw": raw_message}
    thread_id = str(matter.get("gmail_thread_id") or "").strip()
    if thread_id and _can_reply_in_thread(matter, outbound_account):
        gmail_payload["threadId"] = thread_id

    try:
        sent_message = service.users().messages().send(userId="me", body=gmail_payload).execute()
    except Exception as exc:
        raise GmailIntegrationError("Gmail outbound send failed.") from exc

    return {
        "message_id": str(sent_message.get("id") or ""),
        "outbound_account": outbound_account,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "subject": outbound_subject,
        "thread_id": str(sent_message.get("threadId") or ""),
        "to": recipient,
    }


def recipient_email(value: object) -> str:
    if not isinstance(value, str):
        return ""
    addresses = [(display.strip(), email.strip()) for display, email in getaddresses([value]) if email.strip()]
    if len(addresses) != 1:
        return ""
    display_name, email_address = addresses[0]
    if not _is_valid_email_address(email_address):
        return ""
    canonical_email = email_address.lower()
    display_emails = re.findall(EMAIL_IN_TEXT_PATTERN, display_name, flags=re.IGNORECASE)
    if any(display_email.lower() != canonical_email for display_email in display_emails):
        return ""
    return canonical_email


def _is_valid_email_address(email_address: str) -> bool:
    if "@" not in email_address or any(character.isspace() for character in email_address):
        return False
    local_part, _at, domain = email_address.rpartition("@")
    if not local_part or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return True


def _gmail_service(role: str) -> Any:
    creds = _credentials_for_role(role)
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailIntegrationError("Google API packages are not installed.") from exc
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise GmailIntegrationError(f"Gmail {role} service could not start.") from exc


def _credentials_for_role(role: str) -> Any:
    token_path = _token_path_for_role(role)
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GmailIntegrationError("Google API packages are not installed.") from exc

    with _locked_token_file(token_path):
        if not token_path.is_file():
            raise GmailIntegrationError(f"Set {ROLE_TOKEN_ENV[role]} for the {role} Gmail account.")
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path))
        except Exception as exc:
            raise GmailIntegrationError(f"Gmail {role} token could not be read.") from exc

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                _write_token_json_unlocked(token_path, credentials.to_json())
            except GmailIntegrationError:
                raise
            except Exception as exc:
                raise GmailIntegrationError(f"Gmail {role} token could not refresh.") from exc
        if not credentials or not credentials.valid:
            raise GmailIntegrationError(f"Gmail {role} token is not valid.")
        return credentials


@contextmanager
def _locked_token_file(token_path: Path):
    with _TOKEN_LOCK:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = token_path.with_name(f".{token_path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_token_atomically(token_path: Path, token_json: str) -> None:
    with _locked_token_file(token_path):
        _write_token_json_unlocked(token_path, token_json)


def _write_token_json_unlocked(token_path: Path, token_json: str) -> None:
    temporary_path = token_path.with_name(f".{token_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            handle.write(token_json)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, token_path)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise GmailIntegrationError("Gmail token could not be saved.") from exc


def _token_path_for_role(role: str) -> Path:
    if role not in ROLE_TOKEN_ENV:
        raise GmailIntegrationError("Unsupported Gmail role.")
    configured_path = os.environ.get(ROLE_TOKEN_ENV[role])
    if configured_path:
        return Path(configured_path).expanduser()
    raise GmailIntegrationError(f"Set {ROLE_TOKEN_ENV[role]} for the {role} Gmail account.")


def _gmail_profile(service: Any) -> dict[str, Any]:
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise GmailIntegrationError("Gmail account profile could not load.") from exc
    return profile if isinstance(profile, dict) else {}


def _can_reply_in_thread(matter: dict[str, Any], outbound_account: str) -> bool:
    inbound_account = str(matter.get("gmail_account") or "").strip().casefold()
    return bool(inbound_account and outbound_account and inbound_account == outbound_account.strip().casefold())


def _ensure_outbound_matches_inbound(matter: dict[str, Any], outbound_account: str) -> None:
    inbound_account = str(matter.get("gmail_account") or "").strip()
    if not inbound_account:
        return
    if inbound_account.casefold() == outbound_account.strip().casefold():
        return
    raise GmailIntegrationError(
        "Outbound Gmail account "
        f"{outbound_account or 'unknown'} does not match inbound Gmail account {inbound_account}. "
        f"Reconnect the outbound Gmail token for {inbound_account} before sending this redline."
    )


def _message_metadata(message: dict[str, Any], account_email: str) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers") or []
    sender = _header(headers, "From")
    subject = _header(headers, "Subject") or "NDA for review"
    received_at = _header(headers, "Date")
    parsed_received_at = _parse_email_date(received_at)
    return {
        "gmail_account": account_email,
        "gmail_message_id": str(message.get("id") or ""),
        "gmail_thread_id": str(message.get("threadId") or ""),
        "message_snippet": str(message.get("snippet") or ""),
        "received_at": parsed_received_at or received_at,
        "sender": sender,
        "subject": subject,
    }


def _header(headers: list[dict[str, Any]], name: str) -> str:
    for header in headers:
        if str(header.get("name") or "").lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def _parse_email_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _reviewable_attachments(payload: dict[str, Any]) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    for part in _walk_payload_parts(payload):
        filename = str(part.get("filename") or "")
        if not is_supported_document_filename(filename):
            continue
        part_id = str(part.get("partId") or "")
        body = part.get("body") or {}
        attachment_id = str(body.get("attachmentId") or "")
        inline_data = str(body.get("data") or "")
        if not attachment_id and not inline_data:
            continue
        attachments.append({
            "attachment_id": attachment_id or f"inline:{part.get('partId') or filename}",
            "data": inline_data,
            "filename": filename,
            "part_id": part_id,
        })
    return attachments


def _walk_payload_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [payload]
    for child in payload.get("parts") or []:
        if isinstance(child, dict):
            parts.extend(_walk_payload_parts(child))
    return parts


def _attachment_bytes(service: Any, message_id: str, attachment: dict[str, str]) -> bytes:
    inline_data = attachment.get("data") or ""
    if inline_data:
        return _decode_gmail_base64(inline_data)

    attachment_id = attachment.get("attachment_id") or ""
    if not attachment_id:
        raise GmailIntegrationError("Gmail attachment is missing its attachment id.")
    try:
        payload = service.users().messages().attachments().get(
            userId="me",
            messageId=message_id,
            id=attachment_id,
        ).execute()
    except Exception as exc:
        raise GmailIntegrationError("Gmail attachment could not be downloaded.") from exc
    data = str(payload.get("data") or "")
    if not data:
        raise GmailIntegrationError("Gmail attachment did not contain data.")
    return _decode_gmail_base64(data)


def _decode_gmail_base64(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise GmailIntegrationError("Gmail attachment could not be decoded.") from exc


def _reply_subject(subject: str) -> str:
    cleaned = " ".join(subject.split()) or "NDA redline"
    return cleaned if cleaned.lower().startswith("re:") else f"Re: {cleaned}"


def _default_outbound_body(matter: dict[str, Any]) -> str:
    subject = str(matter.get("subject") or matter.get("document_title") or "the NDA")
    return (
        f"Hi,\n\n"
        f"Please find attached the redlined version of {subject}.\n\n"
        f"Best,\n"
        f"Aspora Legal"
    )
