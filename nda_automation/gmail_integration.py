from __future__ import annotations

import base64
from contextlib import contextmanager
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

from . import app_settings, gmail_attachment_selector, google_identity, matter_store, user_store
from .checker import ParagraphAlignmentError
from .document_limits import DocumentSizeError, ensure_document_size
from .docx_text import DocxExtractionError
from .durable_io import fsync_parent_directory
from .ingestion_service import (
    create_matter_from_document,
    extract_document_paragraphs,
    is_supported_document_filename,
)
from .pdf_text import PdfExtractionError
from .review_engine import ActiveReviewEngineError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NDA_DETECTION_TERMS = (
    ("non-disclosure agreement", r"\bnon[-\s]?disclosure\s+agreement\b"),
    ("non-disclosure", r"\bnon[-\s]?disclosure\b"),
    ("confidentiality agreement", r"\bconfidentiality\s+agreement\b"),
    ("confidentiality", r"\bconfidentiality\b"),
    ("confidential", r"\bconfidential\b"),
    ("NDA", r"\bNDA\b"),
)
EXPLICIT_NDA_TERMS = {"non-disclosure agreement", "non-disclosure", "confidentiality agreement", "NDA"}
ATTACHMENT_FILENAME_NDA_SIGNALS = (
    ("non-disclosure agreement", r"\bnon[-\s]?disclosure\s+agreement\b", 90, True),
    ("confidentiality agreement", r"\bconfidentiality\s+agreement\b", 80, True),
    ("non-disclosure", r"\bnon[-\s]?disclosure\b", 75, True),
    ("NDA", r"\bM?NDA\b", 80, True),
    ("confidentiality", r"\bconfidentiality\b", 35, False),
    ("confidential", r"\bconfidential\b", 25, False),
    ("agreement", r"\bagreement\b", 10, False),
    ("contract", r"\bcontract\b", 8, False),
    ("document", r"\bdocument\b", 5, False),
)
ATTACHMENT_CONTENT_NDA_SIGNALS = (
    ("non-disclosure agreement", r"\bnon[-\s]?disclosure\s+agreement\b", 95, True),
    ("confidentiality and non-disclosure", r"\bconfidentiality\s+and\s+non[-\s]?disclosure\b", 90, True),
    ("confidentiality agreement", r"\bconfidentiality\s+agreement\b", 85, True),
    ("mutual confidentiality", r"\bmutual\s+confidentiality\b", 70, True),
    ("non-disclosure", r"\bnon[-\s]?disclosure\b", 45, False),
    ("confidential information", r"\bconfidential\s+information\b", 35, False),
    ("disclosing party", r"\bdisclosing\s+party\b", 20, False),
    ("receiving party", r"\breceiving\s+party\b", 20, False),
    ("confidentiality obligations", r"\bconfidentiality\s+obligations?\b", 25, False),
    ("not disclose", r"\b(?:shall|must|may)\s+not\s+disclose\b", 20, False),
    ("keep confidential", r"\bkeep\s+(?:all\s+)?(?:confidential\s+information\s+)?confidential\b", 20, False),
)
ATTACHMENT_COLLATERAL_SIGNALS = (
    ("project proposal", r"\bproject\s+proposal\b", 70),
    ("proposal", r"\bproposal\b", 45),
    ("proposal form", r"\bproposal\s+form\b", 80),
    ("programme manager", r"\bprogramme\s+manager\b", 55),
    ("program manager", r"\bprogram\s+manager\b", 55),
    ("expectations", r"\bexpectations\b", 50),
    ("questionnaire", r"\bquestionnaire\b", 55),
    ("pricing", r"\bpricing\b", 50),
    ("deck", r"\bdeck\b", 45),
    ("presentation", r"\bpresentation\b", 45),
    ("business plan", r"\bbusiness\s+plan\b", 45),
    ("statement of work", r"\bstatement\s+of\s+work\b|\bSOW\b", 60),
    ("invoice", r"\binvoice\b", 80),
    ("purchase order", r"\bpurchase\s+order\b|\bPO\b", 80),
    ("client contact details", r"\bclient\s+contact\s+details\b", 35),
    ("questions answers", r"\bquestions\s+answers\b", 30),
)
MIN_ATTACHMENT_NDA_SCORE = 70
MIN_MESSAGE_BACKED_CONTENT_SCORE = 55


def _gmail_search_terms_query(terms: list[str]) -> str:
    query_terms: list[str] = []
    for term in terms:
        query_term = _gmail_search_query_term(term)
        if query_term:
            query_terms.append(query_term)
    if not query_terms:
        query_terms = [
            query_term
            for term in app_settings.DEFAULT_GMAIL_INBOUND_SEARCH_TERMS
            if (query_term := _gmail_search_query_term(term))
        ]
    return f"({' OR '.join(query_terms)})"


def _gmail_search_query_term(term: str) -> str:
    cleaned = " ".join(str(term or "").split()).strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace('"', "")
    if re.search(r"[^A-Za-z0-9_]", cleaned):
        return f'"{cleaned}"'
    return cleaned


GMAIL_INBOUND_BASE_QUERY = "in:inbox has:attachment (filename:docx OR filename:pdf) newer_than:30d -from:me"
NDA_MESSAGE_QUERY = _gmail_search_terms_query(app_settings.DEFAULT_GMAIL_INBOUND_SEARCH_TERMS)
DEFAULT_INBOUND_QUERY = f"{GMAIL_INBOUND_BASE_QUERY} {NDA_MESSAGE_QUERY}"
DEFAULT_INBOUND_QUERY_WITH_AI_SELECTOR = DEFAULT_INBOUND_QUERY
MAX_GMAIL_IMPORT_LIMIT = 25
EMAIL_IN_TEXT_PATTERN = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
GMAIL_BODY_PREVIEW_LIMIT = 5000
GMAIL_PROFILE_CACHE_SECONDS = 15 * 60

ROLE_TOKEN_ENV = {
    "inbound": "NDA_GMAIL_INBOUND_TOKEN_PATH",
    "outbound": "NDA_GMAIL_OUTBOUND_TOKEN_PATH",
}
ROLE_LOCAL_TOKEN_FILENAME = {
    "inbound": "inbound-token.json",
    "outbound": "outbound-token.json",
}
GMAIL_OAUTH_REDIRECT_URI_ENV = "NDA_GMAIL_OAUTH_REDIRECT_URI"
GMAIL_OAUTH_AUTH_URL = google_identity.GOOGLE_AUTH_URL
GMAIL_OAUTH_TOKEN_URL = google_identity.GOOGLE_TOKEN_URL
GMAIL_OAUTH_SCOPES_BY_ROLE = {
    "inbound": ("https://www.googleapis.com/auth/gmail.readonly",),
    "outbound": ("https://www.googleapis.com/auth/gmail.send",),
}
_TOKEN_LOCK = threading.RLock()
_PROFILE_CACHE_LOCK = threading.RLock()
_PROFILE_CACHE: dict[str, dict[str, Any]] = {}


class GmailIntegrationError(RuntimeError):
    pass


class GmailRateLimitError(GmailIntegrationError):
    def __init__(self, message: str, *, retry_after_epoch: float = 0.0):
        super().__init__(message)
        self.retry_after_epoch = retry_after_epoch


def gmail_status(owner_user_id: str = "") -> dict[str, Any]:
    settings = app_settings.gmail_settings()
    owner_user_id = _clean_user_token_segment(owner_user_id)
    status: dict[str, Any] = {
        "connect_url": "/auth/gmail/start" if owner_user_id else "",
        "disconnect_url": "/api/gmail/disconnect" if owner_user_id else "",
        "settings": settings,
        "sync": user_store.gmail_sync_status(owner_user_id) if owner_user_id else _global_gmail_sync_status(settings),
        "account_match": True,
        "user_scoped": bool(owner_user_id),
    }
    for role in ("inbound", "outbound"):
        enabled = bool(settings.get(f"{role}_enabled", True))
        role_status: dict[str, Any] = {
            "configured": False,
            "connect_url": f"/auth/gmail/start?role={role}" if owner_user_id else "",
            "email": "",
            "enabled": enabled,
            "ready": False,
            "role": role,
            "token": gmail_role_token_status(role, owner_user_id=owner_user_id),
        }
        if role == "inbound":
            role_status["query"] = _default_inbound_query()
            role_status["parsing"] = gmail_inbound_parsing_summary()
        setup_error = gmail_role_setup_error(role, owner_user_id=owner_user_id)
        if setup_error:
            role_status["error"] = setup_error
            status[role] = role_status
            continue
        role_status["configured"] = True
        try:
            profile = _gmail_profile_for_role(role, owner_user_id=owner_user_id)
        except GmailIntegrationError as error:
            role_status["error"] = str(error)
        else:
            role_status["email"] = str(profile.get("emailAddress") or "")
            if enabled and _is_valid_email_address(role_status["email"]):
                role_status["ready"] = True
            elif enabled:
                role_status["error"] = f"Gmail {role} profile did not include a valid email address."
            else:
                role_status["error"] = f"Gmail {role} is disabled in Admin."
        status[role] = role_status
    _apply_account_consistency(status)
    return status


def gmail_inbound_parsing_summary() -> dict[str, object]:
    selector_enabled = gmail_attachment_selector.selector_configured()
    search_terms = app_settings.gmail_inbound_search_terms()
    return {
        "fields": [
            "subject headers",
            "plain text email body",
            "HTML email body",
            "Gmail snippet",
            "attachment filenames",
            "attachment text content (docx/pdf)",
            "attachment-level deterministic NDA/collateral scoring",
            "Qwen contextual attachment selection" if selector_enabled else "deterministic attachment selection",
        ],
        "terms": search_terms,
        "deterministic_terms": [term for term, _pattern in NDA_DETECTION_TERMS],
        "mode": (
            "Qwen reviews subject, body, snippet, attachment names, extracted attachment text, and deterministic "
            "signals before selecting import attachments. Deterministic rules are used as fallback."
            if selector_enabled
            else "Gmail query prefilters inbox attachments; local parsing verifies each message, then validates each attachment before import."
        ),
    }


def _global_gmail_sync_status(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_sync_at": str(settings.get("last_sync_at") or ""),
        "last_sync_imported_count": int(settings.get("last_sync_imported_count") or 0),
        "last_sync_skipped_count": int(settings.get("last_sync_skipped_count") or 0),
        "sync_history": settings.get("sync_history") if isinstance(settings.get("sync_history"), list) else [],
    }


def _default_inbound_query() -> str:
    return f"{GMAIL_INBOUND_BASE_QUERY} {_gmail_search_terms_query(app_settings.gmail_inbound_search_terms())}"


def gmail_role_setup_error(role: str, owner_user_id: str = "") -> str:
    token = gmail_role_token_status(role, owner_user_id=owner_user_id)
    if token["configured"]:
        return ""
    if owner_user_id:
        return f"Connect Gmail for this user to enable the {role} Gmail account."
    if token["source"] == "environment":
        return (
            f"{ROLE_TOKEN_ENV[role]} points to a missing token file. "
            f"Fix it or unset it to use data/gmail/{ROLE_LOCAL_TOKEN_FILENAME[role]} for the {role} Gmail account."
        )
    return f"Set {ROLE_TOKEN_ENV[role]} or add data/gmail/{ROLE_LOCAL_TOKEN_FILENAME[role]} for the {role} Gmail account."


def gmail_role_token_status(role: str, owner_user_id: str = "") -> dict[str, object]:
    if role not in ROLE_TOKEN_ENV:
        raise GmailIntegrationError("Unsupported Gmail role.")
    owner_user_id = _clean_user_token_segment(owner_user_id)
    if owner_user_id:
        local_path = _user_token_path_for_role(role, owner_user_id)
        if local_path.is_file():
            return {
                "configured": True,
                "label": f"user_gmail/{role}-token.json",
                "source": "user_data",
            }
        return {
            "configured": False,
            "label": f"Connect Gmail for {role}",
            "source": "missing",
        }
    env_name = ROLE_TOKEN_ENV[role]
    local_label = f"data/gmail/{ROLE_LOCAL_TOKEN_FILENAME[role]}"
    configured_path = os.environ.get(env_name)
    if configured_path:
        return {
            "configured": Path(configured_path).expanduser().is_file(),
            "label": env_name,
            "source": "environment",
        }
    local_path = matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if local_path.is_file():
        return {
            "configured": True,
            "label": local_label,
            "source": "local_data",
        }
    return {
        "configured": False,
        "label": f"{env_name} or {local_label}",
        "source": "missing",
    }


def gmail_sync_owner_user_ids() -> list[str]:
    try:
        users = user_store.list_users()
    except user_store.UserStoreError as exc:
        raise GmailIntegrationError("User store could not be read for Gmail sync.") from exc

    owner_user_ids: list[str] = []
    for user in users:
        owner_user_id = _clean_user_token_segment(user.get("id"))
        if not owner_user_id:
            continue
        if gmail_role_token_status("inbound", owner_user_id=owner_user_id)["configured"]:
            owner_user_ids.append(owner_user_id)
    return owner_user_ids


def import_inbound_matters(*, limit: int = 10, query: str | None = None, owner_user_id: str = "") -> dict[str, Any]:
    if not app_settings.gmail_role_enabled("inbound"):
        raise GmailIntegrationError("Gmail inbound is disabled in Admin.")
    owner_user_id = _clean_user_token_segment(owner_user_id)
    service = _gmail_service_for_owner("inbound", owner_user_id)
    profile = _gmail_profile_for_role("inbound", service=service, owner_user_id=owner_user_id)
    inbound_query = query.strip() if isinstance(query, str) and query.strip() else _default_inbound_query()
    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    import_limit = max(1, min(requested_limit, MAX_GMAIL_IMPORT_LIMIT))

    account_email = str(profile.get("emailAddress") or "")
    selector_enabled = gmail_attachment_selector.selector_configured()

    try:
        result = service.users().messages().list(
            userId="me",
            q=inbound_query,
            maxResults=import_limit,
        ).execute()
    except Exception as exc:
        _raise_gmail_api_error(exc, "Gmail inbound sync could not list messages.")

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for message_stub in result.get("messages") or []:
        message_id = str(message_stub.get("id") or "")
        if not message_id:
            continue
        try:
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as exc:
            if _gmail_retry_after_epoch(exc):
                _raise_gmail_api_error(exc, "Gmail inbound sync could not load a message.")
            skipped.append({"message_id": message_id, "reason": "message_unavailable"})
            continue

        if _is_self_or_outbound_message(message, account_email):
            skipped.append({"message_id": message_id, "reason": "self_sent_or_outbound"})
            continue

        attachments = list(_reviewable_attachments(message.get("payload") or {}))
        if not attachments:
            skipped.append({"message_id": message_id, "reason": "no_reviewable_attachment"})
            continue

        detection = _message_nda_detection(message, attachments)
        if not detection["matched"] and not selector_enabled:
            # The header/body/filename scan cannot see inside a .docx/.pdf, but
            # Gmail's query matches attachment text -- so e-signature forwards
            # (Juro/DocuSign) whose NDA wording lives only in the attachment land
            # here. Read the document content before giving up.
            detection = _attachment_nda_detection(service, message_id, attachments)
        if not detection["matched"] and not selector_enabled:
            skipped.append({"message_id": message_id, "reason": "no_nda_signal"})
            continue

        metadata = _message_selector_metadata(
            message,
            _message_metadata(message, account_email, detection=detection if detection["matched"] else None),
        )
        attachment_result = _import_inbound_attachments(
            service,
            message_id,
            attachments,
            metadata,
            owner_user_id=owner_user_id,
        )
        imported.extend(attachment_result["imported"])
        skipped.extend(attachment_result["skipped"])

    return {
        "account": account_email,
        "imported": imported,
        "query": inbound_query,
        "skipped": skipped,
    }


def _import_inbound_attachments(
    service: Any,
    message_id: str,
    attachments: list[dict[str, Any]],
    metadata: dict[str, str],
    *,
    owner_user_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    selector_enabled = gmail_attachment_selector.selector_configured()
    for attachment in attachments:
        candidate, skip = _prepare_inbound_attachment(
            service,
            message_id,
            attachment,
            metadata,
            owner_user_id=owner_user_id,
            require_deterministic_acceptance=not selector_enabled,
        )
        if skip is not None:
            skipped.append(skip)
        elif candidate is not None:
            prepared.append(candidate)

    selected_ids, selector_metadata = _selected_candidate_attachment_ids(metadata, prepared)
    deterministic_fallback = selected_ids is None
    imported: list[dict[str, Any]] = []
    for candidate in prepared:
        attachment_id = str(candidate.get("attachment_id") or "")
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        if deterministic_fallback and not validation.get("accepted"):
            skipped.append(_gmail_attachment_skip(
                message_id,
                str(candidate.get("filename") or ""),
                "non_nda_attachment",
                detail=str(validation.get("reason") or ""),
                score=str(validation.get("score") or "0"),
            ))
            continue
        if selected_ids is not None and attachment_id not in selected_ids:
            skipped.append(_gmail_attachment_skip(
                message_id,
                str(candidate.get("filename") or ""),
                "ai_not_selected_attachment",
                detail=selector_metadata.get("reason", ""),
                model=selector_metadata.get("model", ""),
                confidence=selector_metadata.get("confidence", ""),
            ))
            continue
        matter, skip = _create_matter_from_prepared_attachment(
            candidate,
            metadata,
            selector_metadata=selector_metadata if selected_ids is not None else None,
            owner_user_id=owner_user_id,
        )
        if skip is not None:
            skipped.append(skip)
        elif matter is not None:
            imported.append(matter)
    return {"imported": imported, "skipped": skipped}


def _import_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    candidate, skip = _prepare_inbound_attachment(service, message_id, attachment, metadata)
    if skip is not None or candidate is None:
        return None, skip
    return _create_matter_from_prepared_attachment(candidate, metadata)


def _prepare_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
    *,
    owner_user_id: str = "",
    require_deterministic_acceptance: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    attachment_id = str(attachment.get("attachment_id") or "")
    attachment_filename = str(attachment.get("filename") or "")
    part_id = str(attachment.get("part_id") or "")

    if _gmail_attachment_already_imported(message_id, attachment_id, part_id=part_id, owner_user_id=owner_user_id):
        return None, _gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    try:
        document_bytes = _attachment_bytes(service, message_id, attachment)
    except GmailIntegrationError:
        return None, _gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    try:
        ensure_document_size(document_bytes)
    except DocumentSizeError:
        return None, _gmail_attachment_skip(message_id, attachment_filename, "attachment_too_large")

    attachment_sha256 = hashlib.sha256(document_bytes).hexdigest()
    if _gmail_attachment_already_imported(
        message_id,
        attachment_id,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ):
        return None, _gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    try:
        _document_type, paragraphs = extract_document_paragraphs(attachment_filename, document_bytes)
    except PdfExtractionError as error:
        return None, _gmail_attachment_skip(
            message_id,
            attachment_filename,
            _pdf_attachment_skip_reason(error),
            detail=str(error),
        )
    except DocxExtractionError as error:
        return None, _gmail_attachment_skip(
            message_id,
            attachment_filename,
            "review_failed",
            detail=str(error),
        )

    validation = _attachment_nda_validation(
        attachment_filename,
        paragraphs,
        message_metadata=metadata,
    )
    if require_deterministic_acceptance and not validation["accepted"]:
        return None, _gmail_attachment_skip(
            message_id,
            attachment_filename,
            "non_nda_attachment",
            detail=str(validation.get("reason") or ""),
            score=str(validation.get("score") or "0"),
        )

    return {
        "attachment": attachment,
        "attachment_id": attachment_id,
        "attachment_sha256": attachment_sha256,
        "document_bytes": document_bytes,
        "filename": attachment_filename,
        "message_id": message_id,
        "paragraphs": paragraphs,
        "part_id": part_id,
        "text_preview": _attachment_text_preview(paragraphs),
        "validation": validation,
    }, None


def _create_matter_from_prepared_attachment(
    candidate: dict[str, Any],
    metadata: dict[str, str],
    *,
    selector_metadata: dict[str, object] | None = None,
    owner_user_id: str = "",
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    message_id = str(candidate.get("message_id") or "")
    attachment_id = str(candidate.get("attachment_id") or "")
    attachment_filename = str(candidate.get("filename") or "")
    attachment_sha256 = str(candidate.get("attachment_sha256") or "")
    document_bytes = candidate.get("document_bytes")
    part_id = str(candidate.get("part_id") or "")
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}

    if not isinstance(document_bytes, bytes):
        return None, _gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    metadata = _attachment_validation_metadata(metadata, validation)
    if selector_metadata:
        metadata = _attachment_selector_metadata(metadata, selector_metadata)

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
            owner_user_id=owner_user_id,
        )
    except (ActiveReviewEngineError, DocxExtractionError, PdfExtractionError, ParagraphAlignmentError):
        return None, _gmail_attachment_skip(message_id, attachment_filename, "review_failed")

    if matter.get("_existing_gmail_duplicate"):
        return None, _gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")
    return matter, None


def _selected_candidate_attachment_ids(
    metadata: dict[str, str],
    prepared: list[dict[str, Any]],
) -> tuple[set[str] | None, dict[str, object]]:
    if not prepared or not gmail_attachment_selector.selector_configured():
        return None, {}
    try:
        selection = gmail_attachment_selector.select_nda_attachments(
            message_metadata=metadata,
            candidates=prepared,
        )
    except gmail_attachment_selector.GmailAttachmentSelectorError:
        return None, {}
    if selection.get("status") != "selected":
        return None, {}
    selected_ids = {
        str(attachment_id)
        for attachment_id in selection.get("selected_attachment_ids", [])
        if str(attachment_id)
    }
    return (selected_ids or None), selection


def _message_selector_metadata(message: dict[str, Any], metadata: dict[str, str]) -> dict[str, str]:
    body_preview = _message_body_text(message.get("payload") or {})
    if not body_preview:
        return metadata
    return {
        **metadata,
        "message_body_preview": body_preview[:GMAIL_BODY_PREVIEW_LIMIT],
    }


def _attachment_text_preview(paragraphs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for paragraph in paragraphs[:12]:
        text = " ".join(str(paragraph.get("text") or "").split())
        if text:
            chunks.append(text)
    return "\n".join(chunks)[:3000]


def _gmail_attachment_already_imported(
    message_id: str,
    attachment_id: str,
    *,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
    owner_user_id: str = "",
) -> bool:
    return matter_store.find_gmail_attachment(
        message_id,
        attachment_id,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ) is not None


def _gmail_attachment_skip(message_id: str, attachment_filename: str, reason: str, **details: object) -> dict[str, str]:
    skip = {
        "attachment_filename": attachment_filename,
        "message_id": message_id,
        "reason": reason,
    }
    for key, value in details.items():
        cleaned = " ".join(str(value or "").split())
        if cleaned:
            skip[key] = cleaned[:500]
    return skip


def send_redline_email(
    matter: dict[str, Any],
    attachment_bytes: bytes,
    attachment_filename: str,
    *,
    body: str | None = None,
    subject: str | None = None,
    to: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    recipient, service, outbound_account = _outbound_send_context(
        matter,
        recipient_override=to,
        owner_user_id=owner_user_id,
    )
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
        _raise_gmail_api_error(exc, "Gmail outbound send failed.")

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
    to: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    recipient, _service, outbound_account = _outbound_send_context(
        matter,
        recipient_override=to,
        owner_user_id=owner_user_id,
    )
    return {"outbound_account": outbound_account, "to": recipient}


def _outbound_send_context(
    matter: dict[str, Any],
    *,
    recipient_override: str | None = None,
    owner_user_id: str = "",
) -> tuple[str, Any, str]:
    recipient = recipient_email(recipient_override) or matter_reply_recipient(matter)
    if not recipient:
        raise GmailIntegrationError("Matter does not have a valid reply recipient email address.")
    if not app_settings.gmail_role_enabled("outbound"):
        raise GmailIntegrationError("Gmail outbound is disabled in Admin.")

    owner_user_id = _clean_user_token_segment(owner_user_id)
    service = _gmail_service_for_owner("outbound", owner_user_id)
    profile = _gmail_profile_for_role("outbound", service=service, owner_user_id=owner_user_id)
    outbound_account = str(profile.get("emailAddress") or "")
    if not _is_valid_email_address(outbound_account):
        raise GmailIntegrationError("Gmail outbound profile did not include a valid email address.")
    _ensure_recipient_is_not_own_account(matter, recipient, outbound_account)
    _ensure_outbound_matches_inbound(matter, outbound_account)
    return recipient, service, outbound_account


def matter_reply_recipient(matter: dict[str, Any]) -> str:
    return recipient_email(matter.get("reply_to")) or recipient_email(matter.get("sender"))


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


def _apply_account_consistency(status: dict[str, Any]) -> None:
    inbound = status.get("inbound") if isinstance(status.get("inbound"), dict) else {}
    outbound = status.get("outbound") if isinstance(status.get("outbound"), dict) else {}
    inbound_email = str(inbound.get("email") or "").strip()
    outbound_email = str(outbound.get("email") or "").strip()
    if not inbound_email or not outbound_email:
        return
    if not _is_valid_email_address(inbound_email) or not _is_valid_email_address(outbound_email):
        return
    if inbound_email.casefold() == outbound_email.casefold():
        return

    message = (
        f"Outbound Gmail account {outbound_email} does not match inbound Gmail account {inbound_email}. "
        f"Reconnect outbound Gmail as {inbound_email}."
    )
    status["account_match"] = False
    status["account_error"] = message
    outbound["ready"] = False
    outbound["error"] = message


def _gmail_service(role: str, owner_user_id: str = "") -> Any:
    creds = _credentials_for_role(role, owner_user_id=owner_user_id)
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailIntegrationError("Google API packages are not installed.") from exc
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise GmailIntegrationError(f"Gmail {role} service could not start.") from exc


def _gmail_service_for_owner(role: str, owner_user_id: str = "") -> Any:
    if owner_user_id:
        return _gmail_service(role, owner_user_id=owner_user_id)
    return _gmail_service(role)


def _gmail_profile_for_role(role: str, *, service: Any | None = None, owner_user_id: str = "") -> dict[str, Any]:
    cache_key = _profile_cache_key(role, owner_user_id)
    now = time.time()
    with _PROFILE_CACHE_LOCK:
        cached = _PROFILE_CACHE.get(cache_key) or {}
        profile = cached.get("profile")
        loaded_at = float(cached.get("loaded_at") or 0.0)
        if isinstance(profile, dict) and now - loaded_at <= GMAIL_PROFILE_CACHE_SECONDS:
            return dict(profile)
        retry_until = float(cached.get("rate_limit_until") or 0.0)
        if retry_until > now:
            raise GmailRateLimitError(
                str(cached.get("rate_limit_message") or _gmail_rate_limit_message(retry_until)),
                retry_after_epoch=retry_until,
            )

    gmail_service = service or _gmail_service_for_owner(role, owner_user_id)
    try:
        profile = _gmail_profile(gmail_service)
    except GmailRateLimitError as error:
        with _PROFILE_CACHE_LOCK:
            _PROFILE_CACHE[cache_key] = {
                **(_PROFILE_CACHE.get(cache_key) or {}),
                "rate_limit_message": str(error),
                "rate_limit_until": error.retry_after_epoch,
            }
        raise

    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE[cache_key] = {
            "loaded_at": now,
            "profile": dict(profile),
            "rate_limit_message": "",
            "rate_limit_until": 0.0,
        }
    return dict(profile)


def _credentials_for_role(role: str, owner_user_id: str = "") -> Any:
    token_path = _token_path_for_role(role, owner_user_id=owner_user_id)
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
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        fsync_parent_directory(token_path)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise GmailIntegrationError("Gmail token could not be saved.") from exc


def _token_path_for_role(role: str, owner_user_id: str = "") -> Path:
    if role not in ROLE_TOKEN_ENV:
        raise GmailIntegrationError("Unsupported Gmail role.")
    owner_user_id = _clean_user_token_segment(owner_user_id)
    if owner_user_id:
        return _user_token_path_for_role(role, owner_user_id)
    configured_path = os.environ.get(ROLE_TOKEN_ENV[role])
    if configured_path:
        return Path(configured_path).expanduser()
    local_path = matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if local_path.is_file():
        return local_path
    raise GmailIntegrationError(
        f"Set {ROLE_TOKEN_ENV[role]} or add data/gmail/{ROLE_LOCAL_TOKEN_FILENAME[role]} "
        f"for the {role} Gmail account."
    )


def build_gmail_authorization_url(*, redirect_uri: str, state: str, role: str = "all") -> str:
    if not google_identity.google_oauth_configured():
        raise GmailIntegrationError("Google OAuth is not configured.")
    query = urllib.parse.urlencode({
        "access_type": "offline",
        "client_id": google_identity.google_client_id(),
        "include_granted_scopes": "true",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_gmail_oauth_scopes_for_role(role)),
        "state": state,
    })
    return f"{GMAIL_OAUTH_AUTH_URL}?{query}"


def exchange_gmail_oauth_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    if not google_identity.google_oauth_configured():
        raise GmailIntegrationError("Google OAuth is not configured.")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": google_identity.google_client_id(),
        "client_secret": google_identity.google_client_secret(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    request = urllib.request.Request(
        GMAIL_OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GmailIntegrationError("Gmail OAuth token exchange failed.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GmailIntegrationError("Gmail OAuth token exchange failed.") from exc
    if not isinstance(payload, dict):
        raise GmailIntegrationError("Gmail OAuth token exchange failed.")
    return payload


def save_user_gmail_oauth_token(owner_user_id: str, token_response: dict[str, Any], *, role: str = "all") -> list[str]:
    owner_user_id = _clean_user_token_segment(owner_user_id)
    if not owner_user_id:
        raise GmailIntegrationError("A signed-in user is required to connect Gmail.")
    access_token = str(token_response.get("access_token") or "").strip()
    if not access_token:
        raise GmailIntegrationError("Gmail OAuth response did not include an access token.")
    saved_roles = _gmail_oauth_roles_for_role(role)
    token_payloads: list[tuple[str, Path, dict[str, Any]]] = []
    for save_role in saved_roles:
        token_path = _user_token_path_for_role(save_role, owner_user_id)
        existing = _read_token_json(token_path)
        refresh_token = str(token_response.get("refresh_token") or existing.get("refresh_token") or "").strip()
        if not refresh_token:
            raise GmailIntegrationError("Google did not return a Gmail refresh token. Reconnect Gmail and approve offline access.")
        token_payloads.append((save_role, token_path, {
            "client_id": google_identity.google_client_id(),
            "client_secret": google_identity.google_client_secret(),
            "refresh_token": refresh_token,
            "scopes": list(_gmail_oauth_scopes_for_role(save_role)),
            "token": access_token,
            "token_uri": GMAIL_OAUTH_TOKEN_URL,
        }))
    saved: list[str] = []
    for save_role, token_path, token_payload in token_payloads:
        _write_token_atomically(token_path, json.dumps(token_payload, indent=2) + "\n")
        saved.append(save_role)
    _clear_profile_cache_for_owner(owner_user_id)
    return saved


def disconnect_user_gmail(owner_user_id: str, *, role: str = "all") -> int:
    owner_user_id = _clean_user_token_segment(owner_user_id)
    if not owner_user_id:
        raise GmailIntegrationError("A signed-in user is required to disconnect Gmail.")
    removed = 0
    for disconnect_role in _gmail_oauth_roles_for_role(role):
        token_path = _user_token_path_for_role(disconnect_role, owner_user_id)
        try:
            token_path.unlink()
            fsync_parent_directory(token_path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GmailIntegrationError("Gmail token could not be removed.") from exc
    _clear_profile_cache_for_owner(owner_user_id)
    return removed


def configured_gmail_redirect_uri() -> str:
    return os.environ.get(GMAIL_OAUTH_REDIRECT_URI_ENV, "").strip()


def _gmail_oauth_scopes_for_role(role: str) -> tuple[str, ...]:
    roles = _gmail_oauth_roles_for_role(role)
    scopes: list[str] = []
    for role_name in roles:
        for scope in GMAIL_OAUTH_SCOPES_BY_ROLE[role_name]:
            if scope not in scopes:
                scopes.append(scope)
    return tuple(scopes)


def _gmail_oauth_roles_for_role(role: str) -> tuple[str, ...]:
    normalized_role = str(role or "all").strip().lower()
    if normalized_role in {"all", "both"}:
        return ("inbound", "outbound")
    if normalized_role in GMAIL_OAUTH_SCOPES_BY_ROLE:
        return (normalized_role,)
    raise GmailIntegrationError("Unsupported Gmail OAuth role.")


def _user_token_path_for_role(role: str, owner_user_id: str) -> Path:
    owner_segment = _clean_user_token_segment(owner_user_id)
    if owner_segment in {"", ".", ".."}:
        raise GmailIntegrationError("A valid signed-in user is required to store Gmail tokens.")
    return matter_store.DATA_DIR / "users" / "gmail" / owner_segment / ROLE_LOCAL_TOKEN_FILENAME[role]


def _clean_user_token_segment(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "-", str(value or "").strip())[:160].strip("-")


def _read_token_json(token_path: Path) -> dict[str, Any]:
    if not token_path.is_file():
        return {}
    try:
        with token_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_cache_key(role: str, owner_user_id: str = "") -> str:
    owner_user_id = _clean_user_token_segment(owner_user_id)
    return f"{owner_user_id or 'global'}:{role}"


def _clear_profile_cache_for_owner(owner_user_id: str) -> None:
    owner_user_id = _clean_user_token_segment(owner_user_id)
    with _PROFILE_CACHE_LOCK:
        for key in [
            _profile_cache_key("inbound", owner_user_id),
            _profile_cache_key("outbound", owner_user_id),
        ]:
            _PROFILE_CACHE.pop(key, None)


def _gmail_profile(service: Any) -> dict[str, Any]:
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        _raise_gmail_api_error(exc, "Gmail account profile could not load.")
    return profile if isinstance(profile, dict) else {}


def _raise_gmail_api_error(error: Exception, fallback_message: str) -> None:
    retry_after_epoch = _gmail_retry_after_epoch(error)
    if retry_after_epoch:
        raise GmailRateLimitError(
            _gmail_rate_limit_message(retry_after_epoch),
            retry_after_epoch=retry_after_epoch,
        ) from error
    raise GmailIntegrationError(fallback_message) from error


def _gmail_rate_limit_message(retry_after_epoch: float) -> str:
    retry_after_datetime = datetime.fromtimestamp(retry_after_epoch, timezone.utc)
    retry_after = retry_after_datetime.isoformat(
        timespec="milliseconds" if retry_after_datetime.microsecond else "seconds"
    ).replace("+00:00", "Z")
    return f"Gmail API rate limit exceeded. Retry after {retry_after}."


def _gmail_retry_after_epoch(error: Exception) -> float:
    status = getattr(getattr(error, "resp", None), "status", None)
    content = getattr(error, "content", None)
    content_text = ""
    if isinstance(content, bytes):
        content_text = content.decode("utf-8", errors="replace")
    elif isinstance(content, str):
        content_text = content
    reason = ""
    message = ""
    if content_text:
        try:
            payload = json.loads(content_text)
            error_payload = payload.get("error") if isinstance(payload, dict) else {}
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "")
                errors = error_payload.get("errors")
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    reason = str(errors[0].get("reason") or "")
        except (TypeError, ValueError):
            message = content_text
    if status != 429 and "rateLimitExceeded" not in reason and "rate limit" not in message.lower():
        return 0.0
    retry_after_match = re.search(r"Retry after\s+([0-9T:.\-+Z]+)", message, flags=re.IGNORECASE)
    if retry_after_match:
        retry_at = _parse_retry_after_timestamp(retry_after_match.group(1))
        if retry_at:
            return retry_at
    return time.time() + 10 * 60


def _parse_retry_after_timestamp(value: str) -> float:
    cleaned = str(value or "").strip().rstrip(".")
    if not cleaned:
        return 0.0
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _clear_gmail_profile_cache_for_tests() -> None:
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE.clear()


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


def _ensure_recipient_is_not_own_account(matter: dict[str, Any], recipient: str, outbound_account: str) -> None:
    own_accounts = [
        str(matter.get("gmail_account") or ""),
        outbound_account,
    ]
    if any(_email_addresses_match(recipient, own_account) for own_account in own_accounts):
        raise GmailIntegrationError(
            "Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline "
            f"back to {recipient}."
        )


def _is_self_or_outbound_message(message: dict[str, Any], account_email: str) -> bool:
    label_ids = {str(label).upper() for label in message.get("labelIds") or []}
    if "SENT" in label_ids or "DRAFT" in label_ids:
        return True
    headers = message.get("payload", {}).get("headers") or []
    sender = recipient_email(_header(headers, "From"))
    return bool(sender and _email_addresses_match(sender, account_email))


def _email_addresses_match(left: str, right: str) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def _message_metadata(
    message: dict[str, Any],
    account_email: str,
    *,
    detection: dict[str, object] | None = None,
) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers") or []
    sender = _header(headers, "From")
    reply_to = _header(headers, "Reply-To")
    subject = _header(headers, "Subject") or "NDA for review"
    received_at = _header(headers, "Date")
    parsed_received_at = _parse_email_date(received_at)
    metadata = {
        "gmail_account": account_email,
        "gmail_message_id": str(message.get("id") or ""),
        "gmail_thread_id": str(message.get("threadId") or ""),
        "message_snippet": str(message.get("snippet") or ""),
        "received_at": parsed_received_at or received_at,
        "sender": sender,
        "subject": subject,
    }
    if reply_to:
        metadata["reply_to"] = reply_to
    if detection:
        sources = detection.get("sources") if isinstance(detection.get("sources"), list) else []
        terms = detection.get("terms") if isinstance(detection.get("terms"), list) else []
        excerpt = str(detection.get("excerpt") or "")
        if sources:
            metadata["gmail_detection_sources"] = ", ".join(str(source) for source in sources if source)
        if terms:
            metadata["gmail_detection_terms"] = ", ".join(str(term) for term in terms if term)
        if excerpt:
            metadata["gmail_detection_excerpt"] = excerpt
    return metadata


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


def _message_nda_detection(message: dict[str, Any], attachments: list[dict[str, str]]) -> dict[str, object]:
    headers = message.get("payload", {}).get("headers") or []
    subject = _header(headers, "Subject")
    body_text = _message_body_text(message.get("payload") or {})
    attachment_filenames = " ".join(str(attachment.get("filename") or "") for attachment in attachments)
    fields = {
        "subject": subject,
        "body": body_text,
        "snippet": str(message.get("snippet") or ""),
        "attachment_filename": attachment_filenames,
    }
    sources: list[str] = []
    terms: list[str] = []
    excerpt = ""
    for source, text in fields.items():
        source_terms = _nda_terms_in_text(text)
        if not source_terms:
            continue
        sources.append(source)
        for term in source_terms:
            if term not in terms:
                terms.append(term)
        if not excerpt:
            excerpt = _detection_excerpt(text, source_terms[0])
    return {
        "matched": bool(sources),
        "sources": sources,
        "terms": terms,
        "excerpt": excerpt,
    }


def _attachment_nda_detection(
    service: Any,
    message_id: str,
    attachments: list[dict[str, str]],
) -> dict[str, object]:
    """NDA detection fallback that reads the *content* of each attachment.

    Gmail's inbox query matches NDA terms inside attachment text, but
    ``_message_nda_detection`` only sees the subject/body/snippet/filename. An
    e-signature forward (Juro, DocuSign, ...) carries a generic "review this
    document" body with the NDA wording only inside the attached .docx/.pdf, so
    it would be dropped as ``no_nda_signal`` even though it is a real NDA. This
    closes that false negative by extracting and scanning the document text.
    """
    for attachment in attachments:
        try:
            document_bytes = _attachment_bytes(service, message_id, attachment)
            ensure_document_size(document_bytes)
            _document_type, paragraphs = extract_document_paragraphs(
                str(attachment.get("filename") or ""), document_bytes
            )
        except (GmailIntegrationError, DocumentSizeError, DocxExtractionError, PdfExtractionError):
            continue
        validation = _attachment_nda_validation(str(attachment.get("filename") or ""), paragraphs)
        if validation["accepted"]:
            return {
                "matched": True,
                "sources": validation["sources"],
                "terms": validation["terms"],
                "excerpt": validation["excerpt"],
            }
    return {"matched": False, "sources": [], "terms": [], "excerpt": ""}


def _attachment_nda_validation(
    filename: str,
    paragraphs: list[dict[str, Any]],
    *,
    message_metadata: dict[str, str] | None = None,
) -> dict[str, object]:
    text = "\n".join(str(paragraph.get("text") or "") for paragraph in paragraphs)
    filename_score, filename_terms, filename_reasons, strong_filename = _attachment_signal_score(
        filename,
        ATTACHMENT_FILENAME_NDA_SIGNALS,
    )
    content_score, content_terms, content_reasons, strong_content = _attachment_signal_score(
        text,
        ATTACHMENT_CONTENT_NDA_SIGNALS,
    )
    collateral_score, collateral_reasons = _attachment_collateral_score(filename, text)
    has_role_pair = _text_matches(text, r"\bdisclosing\s+party\b") and _text_matches(text, r"\breceiving\s+party\b")
    message_signal = _metadata_has_explicit_nda_signal(message_metadata)
    # A genuine NDA frequently recites the deal's business context (proposal, SOW,
    # pricing, programme details, etc.), so collateral signals must not veto an
    # attachment that already carries a strong NDA content basis. Apply the collateral
    # penalty only when the document lacks that basis, so true collateral (proposals,
    # questionnaires) is still rejected while real NDAs with business preamble are not.
    strong_nda_content = strong_content or has_role_pair
    effective_collateral_score = 0 if strong_nda_content else collateral_score
    score = max(0, filename_score + content_score - effective_collateral_score)
    has_content_basis = (
        strong_content
        or has_role_pair
        or (strong_filename and content_score >= 25)
        or (message_signal and content_score >= MIN_MESSAGE_BACKED_CONTENT_SCORE)
    )
    accepted = score >= MIN_ATTACHMENT_NDA_SCORE and has_content_basis
    sources: list[str] = []
    stored_filename_terms: list[str] = []
    if filename_terms and filename_score >= 25:
        sources.append("attachment_filename")
        stored_filename_terms = filename_terms
    if content_terms:
        sources.append("attachment_content")
    terms = _unique_strings([*stored_filename_terms, *content_terms])
    reasons = [*filename_reasons, *content_reasons]
    if collateral_reasons:
        reasons.extend(f"collateral:{reason}" for reason in collateral_reasons)
    if not accepted and not reasons:
        reasons.append("no attachment-level NDA signal")
    return {
        "accepted": accepted,
        "excerpt": _attachment_validation_excerpt(text, terms),
        "reason": ", ".join(_unique_strings(reasons)),
        "score": score,
        "sources": sources,
        "terms": terms,
    }


def _attachment_signal_score(
    text: object,
    signals: tuple[tuple[str, str, int, bool], ...],
) -> tuple[int, list[str], list[str], bool]:
    value = str(text or "")
    score = 0
    terms: list[str] = []
    reasons: list[str] = []
    strong = False
    for term, pattern, weight, is_strong in signals:
        if not re.search(pattern, value, flags=re.IGNORECASE):
            continue
        score += weight
        terms.append(term)
        reasons.append(term)
        strong = strong or is_strong
    return score, _unique_strings(terms), _unique_strings(reasons), strong


def _attachment_collateral_score(filename: str, text: str) -> tuple[int, list[str]]:
    searchable = f"{filename}\n{text[:4000]}"
    score = 0
    reasons: list[str] = []
    for term, pattern, weight in ATTACHMENT_COLLATERAL_SIGNALS:
        if not re.search(pattern, searchable, flags=re.IGNORECASE):
            continue
        score += weight
        reasons.append(term)
    return score, _unique_strings(reasons)


def _attachment_validation_excerpt(text: str, terms: list[str]) -> str:
    for term in terms:
        if term in {"agreement", "contract", "document"}:
            continue
        excerpt = _detection_excerpt(text, term)
        if excerpt:
            return excerpt
    return _detection_excerpt(text, terms[0]) if terms else ""


def _metadata_has_explicit_nda_signal(metadata: dict[str, str] | None) -> bool:
    if not metadata:
        return False
    sources = _metadata_csv_values(metadata.get("gmail_detection_sources"))
    if not any(source in {"subject", "body", "snippet"} for source in sources):
        return False
    terms = _metadata_csv_values(metadata.get("gmail_detection_terms"))
    return any(term in EXPLICIT_NDA_TERMS for term in terms)


def _attachment_validation_metadata(metadata: dict[str, str], validation: dict[str, object]) -> dict[str, str]:
    message_sources = [
        source
        for source in _metadata_csv_values(metadata.get("gmail_detection_sources"))
        if source in {"subject", "body", "snippet"}
    ]
    validation_sources = [
        str(source)
        for source in validation.get("sources", [])
        if isinstance(source, str) and source
    ]
    message_terms = _metadata_csv_values(metadata.get("gmail_detection_terms"))
    validation_terms = [
        str(term)
        for term in validation.get("terms", [])
        if isinstance(term, str) and term
    ]
    updated = dict(metadata)
    sources = _unique_strings([*message_sources, *validation_sources])
    terms = _unique_strings([*message_terms, *validation_terms])
    if sources:
        updated["gmail_detection_sources"] = ", ".join(sources)
    if terms:
        updated["gmail_detection_terms"] = ", ".join(terms)
    excerpt = str(validation.get("excerpt") or "").strip()
    if excerpt:
        updated["gmail_detection_excerpt"] = excerpt
    updated["gmail_attachment_score"] = str(validation.get("score") or 0)
    reason = str(validation.get("reason") or "").strip()
    if reason:
        updated["gmail_attachment_reasons"] = reason
    return updated


def _attachment_selector_metadata(metadata: dict[str, str], selection: dict[str, object]) -> dict[str, str]:
    updated = dict(metadata)
    updated["gmail_attachment_selector"] = "groq_qwen"
    model = str(selection.get("model") or "").strip()
    if model:
        updated["gmail_attachment_selector_model"] = model[:120]
    reason = str(selection.get("reason") or "").strip()
    if reason:
        updated["gmail_attachment_selector_reason"] = reason[:500]
    confidence = selection.get("confidence")
    if confidence is not None:
        updated["gmail_attachment_selector_confidence"] = str(confidence)[:40]
    return updated


def _metadata_csv_values(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def _text_matches(text: object, pattern: str) -> bool:
    return bool(re.search(pattern, str(text or ""), flags=re.IGNORECASE))


def _pdf_attachment_skip_reason(error: PdfExtractionError) -> str:
    if "No readable text" in str(error):
        return "pdf_text_unreadable_needs_ocr"
    return "review_failed"


def _nda_terms_in_text(text: object) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    matches: list[str] = []
    for term, pattern in NDA_DETECTION_TERMS:
        if re.search(pattern, value, flags=re.IGNORECASE) and term not in matches:
            matches.append(term)
    return matches


def _detection_excerpt(text: object, term: str, *, radius: int = 90) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    index = value.casefold().find(term.casefold())
    if index < 0:
        return value[:180]
    start = max(0, index - radius)
    end = min(len(value), index + len(term) + radius)
    prefix = "..." if start else ""
    suffix = "..." if end < len(value) else ""
    return f"{prefix}{value[start:end]}{suffix}"


def _message_body_text(payload: dict[str, Any]) -> str:
    combined = "\n".join(part for part in _message_body_text_parts(payload) if part)
    return combined[:GMAIL_BODY_PREVIEW_LIMIT]


def _message_body_text_parts(part: dict[str, Any]) -> list[str]:
    if part.get("filename"):
        return []

    mime_type = _normalized_mime_type(part)
    child_parts = [child for child in part.get("parts") or [] if isinstance(child, dict)]
    if mime_type == "multipart/alternative" and child_parts:
        alternative_parts = _message_alternative_text_parts(child_parts)
        if alternative_parts:
            return alternative_parts

    if child_parts:
        return _message_multipart_text_parts(child_parts)

    return _message_leaf_text_part(part, mime_type)


def _message_multipart_text_parts(parts: list[dict[str, Any]]) -> list[str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    nested_parts: list[str] = []
    for part in parts:
        if part.get("filename"):
            continue
        mime_type = _normalized_mime_type(part)
        child_parts = [child for child in part.get("parts") or [] if isinstance(child, dict)]
        if not child_parts and mime_type == "text/plain":
            plain_parts.extend(_message_leaf_text_part(part, mime_type))
        elif not child_parts and mime_type == "text/html":
            html_parts.extend(_message_leaf_text_part(part, mime_type))
        else:
            nested_parts.extend(_message_body_text_parts(part))

    direct_body_parts = plain_parts if plain_parts else html_parts
    return [*direct_body_parts, *nested_parts]


def _message_alternative_text_parts(parts: list[dict[str, Any]]) -> list[str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    fallback_parts: list[str] = []
    for part in parts:
        mime_type = _normalized_mime_type(part)
        if mime_type == "text/plain":
            plain_parts.extend(_message_leaf_text_part(part, mime_type))
        elif mime_type == "text/html":
            html_parts.extend(_message_leaf_text_part(part, mime_type))
        else:
            fallback_parts.extend(_message_body_text_parts(part))
    if plain_parts:
        return plain_parts
    if html_parts:
        return html_parts
    return fallback_parts


def _message_leaf_text_part(part: dict[str, Any], mime_type: str | None = None) -> list[str]:
    normalized_mime_type = mime_type or _normalized_mime_type(part)
    if normalized_mime_type not in {"text/plain", "text/html"}:
        return []
    data = str((part.get("body") or {}).get("data") or "")
    if not data:
        return []
    decoded = _decode_message_text_part(data, part)
    if not decoded:
        return []
    if normalized_mime_type == "text/html":
        return [_html_to_text(decoded)]
    return [decoded]


def _normalized_mime_type(part: dict[str, Any]) -> str:
    return str(part.get("mimeType") or "").split(";", 1)[0].strip().lower()


def _decode_message_text_part(data: str, part: dict[str, Any]) -> str:
    try:
        raw = _decode_gmail_base64(data)
    except GmailIntegrationError:
        return ""
    charset = _part_charset(part) or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _part_charset(part: dict[str, Any]) -> str:
    headers = part.get("headers") or []
    content_type = _header(headers, "Content-Type")
    match = re.search(r"charset=[\"']?([^\"';\s]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else ""


class _HTMLTextExtractor(HTMLParser):
    IGNORED_TEXT_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name in self.IGNORED_TEXT_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag_name in {"br", "div", "li", "p", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.IGNORED_TEXT_TAGS and self._ignored_depth:
            self._ignored_depth -= 1

    def text(self) -> str:
        return unescape(" ".join(part for part in self._parts if part.strip()))


def _html_to_text(value: str) -> str:
    sanitized = _strip_ignored_html_text_blocks(value)
    parser = _HTMLTextExtractor()
    try:
        parser.feed(sanitized)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", sanitized)
    return parser.text()


def _strip_ignored_html_text_blocks(value: str) -> str:
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", value, flags=re.IGNORECASE | re.DOTALL)


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
