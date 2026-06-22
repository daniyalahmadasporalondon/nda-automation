from __future__ import annotations

import base64
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from . import (
    app_settings,
    gmail_attachment_selector,
    gmail_intake_classifier,
    gmail_matter_inbox,
    gmail_matter_outbox,
    gmail_transport,
    google_connection,
    matter_store,  # noqa: F401 - transport dependency for gmail_matter_inbox
    user_store,
)
from .checker import ParagraphAlignmentError  # noqa: F401 - transport dependency for gmail_matter_inbox
from .document_limits import DocumentSizeError, ensure_document_size
from .docx_text import DocxExtractionError
from .durable_io import fsync_parent_directory
from .ingestion_service import (
    create_matter_from_document,  # noqa: F401 - transport dependency for gmail_matter_inbox
    extract_document_paragraphs,
    is_supported_document_filename,
    schedule_inbound_ai_review,  # noqa: F401 - transport dependency for gmail_matter_inbox
)
from .pdf_text import PdfExtractionError
from .review_engine import ActiveReviewEngineError  # noqa: F401 - transport dependency for gmail_matter_inbox

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
# Attachments that fall below the confident bar but still carry a genuine (merely
# uncertain) NDA content basis are routed to the human triage lane instead of
# being silently dropped. 40 admits "has some NDA vocabulary but didn't clear the
# strict bar" (the lowest single strong content signal is ~45) while a pure
# proposal/invoice (collateral-penalised to ~0) stays in the terminal SKIP band.
TRIAGE_MIN_NDA_SCORE = 40
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


# The DEFAULT fetch window for the inbound scan. Extracted to a named constant so
# tests and the fallback path have a single source of truth. The EFFECTIVE window is
# now admin-configurable (app_settings.gmail_inbound_window_days); this constant is
# the value used when no setting is stored or the stored value is corrupt.
GMAIL_INBOUND_WINDOW_DAYS = 90


def _inbound_envelope_query(window_days: int) -> str:
    # The structural envelope: every docx/pdf attachment in the inbox in-window that
    # was not sent by us. The NDA keyword group is intentionally NOT part of this
    # query -- it is only a scoring/ranking hint (see NDA_MESSAGE_QUERY), never a
    # fetch gate. Gmail only indexes subject/body/snippet/filename, so AND-appending
    # the keyword group would hide attachment-only NDAs with a neutral subject; the
    # deterministic content scorer is the real gate after the fetch.
    return f"in:inbox has:attachment (filename:docx OR filename:pdf) -from:me newer_than:{window_days}d"


# The import-time STATIC envelope at the default window. Retained as the
# fallback/back-compat constant for any consumer that reads it directly; the
# EFFECTIVE query is built at call time from the configured window (see
# _default_inbound_query) so a stored window change takes effect without a reimport.
GMAIL_INBOUND_BASE_QUERY = _inbound_envelope_query(GMAIL_INBOUND_WINDOW_DAYS)
# Retained as the scoring/ranking-hint vocabulary input (deterministic content
# scorer + gmail_inbound_parsing_summary). NOT appended to the fetch query.
NDA_MESSAGE_QUERY = _gmail_search_terms_query(app_settings.DEFAULT_GMAIL_INBOUND_SEARCH_TERMS)
# The fetch query is now the structural envelope only; keyword terms never gate
# the fetch. The "...WITH_AI_SELECTOR" alias stays so existing references keep
# resolving to the same envelope.
DEFAULT_INBOUND_QUERY = GMAIL_INBOUND_BASE_QUERY
DEFAULT_INBOUND_QUERY_WITH_AI_SELECTOR = DEFAULT_INBOUND_QUERY

# DocuSign envelope-notification emails (signature requests/completions, "your
# document is ready", reminders) arrive from the docusign.net family and carry a
# PDF attachment, so the structural fetch query above happily surfaces them. They
# are never inbound counterparty NDAs to triage -- importing them spawns phantom
# matters. The match is DOMAIN-ONLY (never subject/body): a real NDA that merely
# mentions DocuSign must still pass. Covers dse@docusign.net, dse_demo@,
# dse_na1..4@, dse_eu1@, eumail.docusign.net, mail.docusign.net, etc.
DOCUSIGN_NOTIFICATION_DOMAINS = ("docusign.net",)

# Per-poll import ceiling. This is the GENTLE CATCH-UP knob: it bounds how many
# inbound messages a SINGLE poll cycle downloads + triages (deepseek-v4-pro
# attachment selector) + classifies (flash intake) + text-extracts (PyMuPDF) ON
# THE POLL THREAD before queuing AI reviews. When Gmail is (re)connected the first
# poll would otherwise catch up the whole 90-day backlog at once -- a one-time
# burst that strains/OOMs the single 2 GB worker. Keeping this small drains the
# backlog a batch at a time: the dedup index (message_attachments_all_already_
# imported) PERSISTS across polls and short-circuits already-imported messages
# BEFORE any download/extract, so each subsequent poll makes real forward progress
# on the next batch until the backlog is empty. Override with NDA_GMAIL_IMPORT_LIMIT
# (a higher value trades a faster drain for a larger per-poll burst). The default
# is deliberately modest so re-enabling Gmail can never overwhelm the worker.
NDA_GMAIL_IMPORT_LIMIT_ENV = "NDA_GMAIL_IMPORT_LIMIT"
_DEFAULT_GMAIL_IMPORT_LIMIT = 20
# Upper clamp on the per-poll NEW-work limit. Gmail allows ~6,000 quota-units per
# user per minute; a messages.get() costs ~5 units and the per-poll probe issues
# roughly one get() per scanned stub, so an operator pushing the knob to 60+ could
# drive a single poll past the per-minute budget and trip rate-limits. Clamping at
# 40 keeps even the heaviest poll comfortably inside quota while still letting an
# operator trade burst for a faster drain. (With the drain cursor the steady-state
# get() count collapses to ~the new-work batch, so this is defense-in-depth.)
_MAX_GMAIL_IMPORT_LIMIT_CLAMP = 40


def _gmail_import_limit_from_env() -> int:
    raw = os.environ.get(NDA_GMAIL_IMPORT_LIMIT_ENV, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_GMAIL_IMPORT_LIMIT
    # A non-positive override is meaningless (and a zero would import nothing); fall
    # back to the default rather than wedging the catch-up.
    if value < 1:
        return _DEFAULT_GMAIL_IMPORT_LIMIT
    # Clamp the upper end so the knob can't be pushed into Gmail rate-limit
    # territory (a too-large per-poll burst of messages.get() probes).
    return min(value, _MAX_GMAIL_IMPORT_LIMIT_CLAMP)


MAX_GMAIL_IMPORT_LIMIT = _gmail_import_limit_from_env()
GMAIL_BODY_PREVIEW_LIMIT = 5000
GMAIL_PROFILE_CACHE_SECONDS = 15 * 60

ROLE_TOKEN_ENV = google_connection.ROLE_TOKEN_ENV
ROLE_LOCAL_TOKEN_FILENAME = google_connection.ROLE_LOCAL_TOKEN_FILENAME
GMAIL_OAUTH_REDIRECT_URI_ENV = "NDA_GMAIL_OAUTH_REDIRECT_URI"
_PROFILE_CACHE_LOCK = threading.RLock()
_PROFILE_CACHE: dict[str, dict[str, Any]] = {}


GmailIntegrationError = google_connection.GoogleConnectionError


class RecipientConfirmationError(GmailIntegrationError):
    """Raised when the outbound recipient was not explicitly confirmed by a human.

    The inbound ``Reply-To``/``From`` headers are attacker-controlled, so the
    outbound recipient must never be sent on the strength of those headers alone:
    a human must confirm the exact address the document is going to. This guards
    against a spoofed ``Reply-To`` silently redirecting a redline to an attacker.
    """


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
        # The built-in NDA-intake criteria, surfaced so the admin textarea can show
        # it as a placeholder when the editable setting is left empty.
        "intake_playbook_default": gmail_intake_classifier.DEFAULT_INTAKE_PLAYBOOK,
        # The effective inbound sync window (days) + its default/bounds, surfaced so
        # the admin "Sync window" field can show the current value and validate input.
        # The effective value is re-derived (never trusts a corrupt stored value).
        "inbound_window_days": app_settings.gmail_inbound_window_days(settings),
        "inbound_window_days_default": app_settings.DEFAULT_GMAIL_INBOUND_WINDOW_DAYS,
        "inbound_window_days_min": app_settings.MIN_GMAIL_INBOUND_WINDOW_DAYS,
        "inbound_window_days_max": app_settings.MAX_GMAIL_INBOUND_WINDOW_DAYS,
        "sync": user_store.gmail_sync_status(owner_user_id) if owner_user_id else _global_gmail_sync_status(settings),
        "account_match": True,
        "user_scoped": bool(owner_user_id),
        "setup": google_connection.connection_setup_status(
            owner_user_id=owner_user_id,
            connect_url="/auth/gmail/start",
            integration="Gmail",
        ),
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
        role_status["recovery"] = google_connection.role_recovery_status(
            role,
            owner_user_id=owner_user_id,
            connect_url=role_status["connect_url"] or "/auth/google/start",
            integration="Gmail",
        )
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
                # The profile fetch proving a *basic* call succeeds is NOT proof the
                # connection can actually import: the token may be missing the import
                # scope, or the cached profile may pre-date an expiry/revocation that
                # the real poll would hit. Enforce scope adequacy + a read-only
                # credential probe so "ready" reflects reality, not just reachability.
                block_reason = _gmail_readiness_block_reason(role, role_status["token"], owner_user_id=owner_user_id)
                if block_reason:
                    role_status["error"] = block_reason
                    role_status["reason"] = block_reason
                else:
                    role_status["ready"] = True
            elif enabled:
                role_status["error"] = f"Gmail {role} profile did not include a valid email address."
            else:
                role_status["error"] = f"Gmail {role} is disabled in Admin."
        status[role] = role_status
    _apply_account_consistency(status)
    return status


def _scope_short_name(scope: str) -> str:
    # "https://www.googleapis.com/auth/gmail.modify" -> "gmail.modify"
    tail = str(scope or "").rstrip("/").rsplit("/", 1)[-1]
    return tail or str(scope or "")


def _gmail_credential_probe(role: str, owner_user_id: str = "") -> None:
    """Read-only credential health probe for the STATUS path only.

    Loads the role's stored OAuth credential and confirms it can still authenticate
    a future poll WITHOUT performing any Gmail import/poll/list call and WITHOUT
    forcing a network round-trip. It validates structurally:
      * the token file is present and parseable as OAuth user credentials, and
      * the credential is either still valid, OR carries a refresh_token (so the
        poller's existing refresh-on-expiry path can recover it).
    A token that is expired AND has no usable refresh_token can never authenticate,
    so it is surfaced as a broken connection. Raises GmailIntegrationError when the
    credential cannot authenticate. Isolated as a seam so the status endpoint never
    triggers or alters the poller and so tests can simulate token states.
    """
    token_path = google_connection.token_path_for_role(
        role, owner_user_id=owner_user_id, integration_label="Gmail"
    )
    if not token_path.is_file():
        raise GmailIntegrationError(f"Gmail {role} token file is missing — reconnect Gmail.")
    payload = google_connection.read_token_json(token_path)
    # A token with no refresh_token cannot recover once its access token expires;
    # the poller's refresh-on-expiry path has nothing to refresh with. Surface this
    # explicitly (the OAuth credential loader also rejects such tokens).
    if not str(payload.get("refresh_token") or "").strip():
        raise GmailIntegrationError(f"Gmail {role} token expired or revoked — reconnect Gmail.")
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - exercised only without google libs
        raise GmailIntegrationError("Google API packages are not installed.") from exc
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path))
    except Exception as exc:
        raise GmailIntegrationError(f"Gmail {role} token could not be read — reconnect Gmail.") from exc
    if credentials is None:
        raise GmailIntegrationError(f"Gmail {role} token is not valid — reconnect Gmail.")
    # A valid credential needs nothing. An expired credential is only recoverable if
    # it carries a refresh_token; without one it can never re-authenticate.
    if not credentials.valid and not getattr(credentials, "refresh_token", None):
        raise GmailIntegrationError(f"Gmail {role} token expired or revoked — reconnect Gmail.")


def _gmail_readiness_block_reason(
    role: str,
    token_status: dict[str, object] | None,
    *,
    owner_user_id: str = "",
) -> str:
    """Return a specific reason the Gmail role is NOT actually ready, or "".

    Status-reporting only. This enforces what ``gmail_status`` already computes but
    never consulted: (1) the token's required-scope adequacy, and (2) that the
    credential can still authenticate (read-only structural probe). It performs NO
    Gmail import/poll/list call and runs only when the status endpoint is read,
    never on the poller's schedule.
    """
    scope_status = (token_status or {}).get("scope_status")
    if isinstance(scope_status, dict) and scope_status.get("ok") is False:
        missing = scope_status.get("missing")
        missing_names = (
            ", ".join(_scope_short_name(scope) for scope in missing)
            if isinstance(missing, list) and missing
            else "required Gmail scope"
        )
        return f"Reconnect Gmail — missing permission: {missing_names}"

    try:
        _gmail_credential_probe(role, owner_user_id=owner_user_id)
    except GmailIntegrationError as error:
        message = str(error).strip()
        return message or "Gmail connection cannot authenticate — reconnect Gmail."
    return ""


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
            "OpenRouter contextual attachment selection" if selector_enabled else "deterministic attachment selection",
        ],
        "terms": search_terms,
        "deterministic_terms": [term for term, _pattern in NDA_DETECTION_TERMS],
        "mode": (
            "OpenRouter reviews subject, body, snippet, attachment names, extracted attachment text, and deterministic "
            "signals before selecting import attachments. Deterministic rules are used as fallback."
            if selector_enabled
            else "Gmail fetches the structural attachment envelope (no keyword prefilter); local parsing then judges every attachment by its content before import."
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
    # Only the structural envelope. The keyword terms are no longer a fetch gate;
    # they live on as the deterministic content-scoring/ranking vocabulary.
    #
    # The window (``newer_than:{N}d``) is admin-configurable, so build the envelope
    # at CALL time from the stored setting. The reader already falls back to the
    # default (90) on a missing/corrupt/out-of-band value; wrap it once more so even
    # a settings-read failure degrades to the static GMAIL_INBOUND_BASE_QUERY rather
    # than ever raising on this hot inbound path.
    try:
        window_days = app_settings.gmail_inbound_window_days()
    except Exception:  # pragma: no cover - defensive: settings read must never break fetch
        return GMAIL_INBOUND_BASE_QUERY
    return _inbound_envelope_query(window_days)


def gmail_role_setup_error(role: str, owner_user_id: str = "") -> str:
    token = gmail_role_token_status(role, owner_user_id=owner_user_id)
    if token["configured"]:
        return ""
    if owner_user_id:
        return f"Connect Gmail for this user to enable the {role} Gmail account."
    if token["source"] == "environment":
        return (
            f"{ROLE_TOKEN_ENV[role]} points to a missing token file. "
            f"Fix it or unset it to use data/google/{ROLE_LOCAL_TOKEN_FILENAME[role]} for the {role} Google connection."
        )
    return f"Set {ROLE_TOKEN_ENV[role]} or add data/google/{ROLE_LOCAL_TOKEN_FILENAME[role]} for the {role} Google connection."


def gmail_role_token_status(role: str, owner_user_id: str = "") -> dict[str, object]:
    try:
        token_status = google_connection.role_token_status(role, owner_user_id=owner_user_id)
    except google_connection.GoogleConnectionError as exc:
        raise GmailIntegrationError(str(exc)) from exc
    if not owner_user_id and token_status.get("source") == "missing":
        token_status = {
            **token_status,
            "label": f"{ROLE_TOKEN_ENV[role]} or data/google/{ROLE_LOCAL_TOKEN_FILENAME[role]}",
        }
    if owner_user_id and token_status.get("source") == "missing":
        token_status = {**token_status, "label": f"Connect Gmail for {role}"}
    return token_status


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
    return gmail_matter_inbox.import_inbound_matters(
        transport=_gmail_inbox_transport(),
        limit=limit,
        query=query,
        owner_user_id=owner_user_id,
    )


def _import_inbound_attachments(
    service: Any,
    message_id: str,
    attachments: list[dict[str, Any]],
    metadata: dict[str, str],
    *,
    owner_user_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
    return gmail_matter_inbox.import_inbound_attachments(
        service,
        message_id,
        attachments,
        metadata,
        transport=_gmail_inbox_transport(),
        owner_user_id=owner_user_id,
    )


def _import_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    return gmail_matter_inbox.import_inbound_attachment(
        service,
        message_id,
        attachment,
        metadata,
        transport=_gmail_inbox_transport(),
    )


def _gmail_attachment_already_imported(
    message_id: str,
    attachment_id: str,
    *,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
    owner_user_id: str = "",
) -> bool:
    return gmail_matter_inbox.gmail_attachment_already_imported(
        message_id,
        attachment_id,
        transport=_gmail_inbox_transport(),
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    )


def _gmail_outbox_transport() -> Any:
    return gmail_transport.outbox_transport()


def _gmail_inbox_transport() -> Any:
    return gmail_transport.inbox_transport()


def send_redline_email(
    matter: dict[str, Any],
    attachment_bytes: bytes,
    attachment_filename: str,
    *,
    body: str | None = None,
    subject: str | None = None,
    to: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    return gmail_matter_outbox.send_redline_email(
        matter,
        attachment_bytes,
        attachment_filename,
        transport=_gmail_outbox_transport(),
        body=body,
        subject=subject,
        to=to,
        confirmed_recipient=confirmed_recipient,
        owner_user_id=owner_user_id,
    )


def validate_outbound_send_ready(
    matter: dict[str, Any],
    *,
    to: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> dict[str, str]:
    return gmail_matter_outbox.validate_outbound_send_ready(
        matter,
        transport=_gmail_outbox_transport(),
        to=to,
        confirmed_recipient=confirmed_recipient,
        owner_user_id=owner_user_id,
    )


def _outbound_send_context(
    matter: dict[str, Any],
    *,
    recipient_override: str | None = None,
    confirmed_recipient: str | None = None,
    owner_user_id: str = "",
) -> tuple[str, Any, str]:
    return gmail_matter_outbox.outbound_send_context(
        matter,
        transport=_gmail_outbox_transport(),
        recipient_override=recipient_override,
        confirmed_recipient=confirmed_recipient,
        owner_user_id=owner_user_id,
    )


def matter_reply_recipient(matter: dict[str, Any]) -> str:
    return gmail_matter_outbox.matter_reply_recipient(matter)


def recipient_email(value: object) -> str:
    return gmail_matter_outbox.recipient_email(value)


def _is_valid_email_address(email_address: str) -> bool:
    return gmail_matter_outbox.is_valid_email_address(email_address)


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
    return google_connection.credentials_for_role(role, owner_user_id=owner_user_id, integration_label="Gmail")


def _write_token_atomically(token_path: Path, token_json: str) -> None:
    with _gmail_fsync_parent_directory_patch():
        google_connection.write_token_atomically(token_path, token_json)


def _gmail_fsync_parent_directory_patch():
    class _Patch:
        def __enter__(self):
            self._original = google_connection.fsync_parent_directory
            google_connection.fsync_parent_directory = fsync_parent_directory

        def __exit__(self, exc_type, exc, traceback):
            google_connection.fsync_parent_directory = self._original
            return False

    return _Patch()


def configured_gmail_redirect_uri() -> str:
    return os.environ.get(GMAIL_OAUTH_REDIRECT_URI_ENV, "").strip()


def _clean_user_token_segment(value: object) -> str:
    return google_connection.clean_user_token_segment(value)


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


def _is_self_or_outbound_message(message: dict[str, Any], account_email: str) -> bool:
    label_ids = {str(label).upper() for label in message.get("labelIds") or []}
    if "SENT" in label_ids or "DRAFT" in label_ids:
        return True
    headers = message.get("payload", {}).get("headers") or []
    sender = recipient_email(_header(headers, "From"))
    return bool(sender and _email_addresses_match(sender, account_email))


def _is_docusign_notification(message: dict[str, Any]) -> bool:
    """Return True iff the message is a DocuSign envelope-notification email.

    DOMAIN-ONLY match on the raw ``From`` header: the sender's domain must be
    ``docusign.net`` or a subdomain (``*.docusign.net``). Subject/body/filename
    are intentionally NOT consulted -- a genuine counterparty NDA that merely
    mentions DocuSign must still be imported.

    Fail-open: a malformed ``From`` (no parseable address, or more than one
    address) returns False so a real NDA is never wrongly skipped.
    """
    headers = message.get("payload", {}).get("headers") or []
    raw_from = _header(headers, "From")
    if not raw_from:
        return False
    parsed = getaddresses([raw_from])
    # Exactly one address; anything ambiguous (0 or 2+) fails open.
    if len(parsed) != 1:
        return False
    _name, address = parsed[0]
    address = (address or "").strip().lower()
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].strip()
    if not domain:
        return False
    return any(domain == base or domain.endswith("." + base) for base in DOCUSIGN_NOTIFICATION_DOMAINS)


def _email_addresses_match(left: str, right: str) -> bool:
    return gmail_matter_outbox.email_addresses_match(left, right)


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
        "has_content_basis": has_content_basis,
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
    updated["gmail_attachment_selector"] = "openrouter_gemini"
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
