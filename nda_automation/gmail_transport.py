from __future__ import annotations

import os
from typing import Any

from . import app_settings, gmail_attachment_selector, gmail_intake_classifier
from .matter_repository import DiskMatterRepository

# --------------------------------------------------------------------------- #
# Gmail History-API incremental inbound sync -- config (env var NAMES only).
#
# The whole subsystem is default-OFF: with NDA_GMAIL_HISTORY_SYNC_ENABLED unset (or
# 0/false/no/off), history.list is NEVER called and the historyId store is NEVER
# touched -- byte-identical to the current HEAD/DRAIN window-scan behaviour.
# --------------------------------------------------------------------------- #
# Master flag. Default OFF (only 1/true/yes/on enables). The incremental path is the
# MOST storm-prone subsystem; it stays dark until explicitly turned on.
NDA_GMAIL_HISTORY_SYNC_ENABLED_ENV = "NDA_GMAIL_HISTORY_SYNC_ENABLED"
# Optional per-owner allowlist of ``google:<sub>`` ids (comma/space separated),
# mirroring NDA_ADMIN_USERS sub-based gating. Empty/unset = all owners.
NDA_GMAIL_HISTORY_SYNC_OWNERS_ENV = "NDA_GMAIL_HISTORY_SYNC_OWNERS"
# Mandatory full-sweep cadence: every Nth successful incremental poll re-runs the
# full HEAD/DRAIN window-scan to catch re-labeled/un-archived old mail that
# messagesAdded never fires for. Default 6. A value < 1 CLAMPS to the default -- the
# full-sweep is the only gap-closer for that class of mail and MUST NOT be disabled.
NDA_GMAIL_HISTORY_FULLSWEEP_EVERY_ENV = "NDA_GMAIL_HISTORY_FULLSWEEP_EVERY"
DEFAULT_GMAIL_HISTORY_FULLSWEEP_EVERY = 6


def _legacy() -> Any:
    from . import gmail_integration

    return gmail_integration


class GmailTransport:
    """Public Gmail transport boundary for inbox and outbox workflows.

    The compatibility layer deliberately delegates to ``gmail_integration`` at
    call time so existing route/test patch points keep working while workflows
    stop depending on private helper names.
    """

    @property
    def GmailIntegrationError(self):
        return _legacy().GmailIntegrationError

    @property
    def GmailRateLimitError(self):
        return _legacy().GmailRateLimitError

    @property
    def RecipientConfirmationError(self):
        return _legacy().RecipientConfirmationError

    @property
    def ActiveReviewEngineError(self):
        return _legacy().ActiveReviewEngineError

    @property
    def DocumentSizeError(self):
        return _legacy().DocumentSizeError

    @property
    def DocxExtractionError(self):
        return _legacy().DocxExtractionError

    @property
    def GmailAttachmentSelectorError(self):
        return gmail_attachment_selector.GmailAttachmentSelectorError

    @property
    def ParagraphAlignmentError(self):
        return _legacy().ParagraphAlignmentError

    @property
    def PdfExtractionError(self):
        return _legacy().PdfExtractionError

    def gmail_role_enabled(self, role: str) -> bool:
        return app_settings.gmail_role_enabled(role)

    def clean_user_token_segment(self, value: object) -> str:
        return _legacy()._clean_user_token_segment(value)

    def gmail_service_for_owner(self, role: str, owner_user_id: str = "") -> Any:
        return _legacy()._gmail_service_for_owner(role, owner_user_id)

    def gmail_profile_for_role(
        self,
        role: str,
        *,
        service: Any | None = None,
        owner_user_id: str = "",
    ) -> dict[str, Any]:
        return _legacy()._gmail_profile_for_role(role, service=service, owner_user_id=owner_user_id)

    def default_inbound_query(self) -> str:
        return _legacy()._default_inbound_query()

    def inbound_query_for_window(self, window_days: int) -> str:
        """The effective inbound query for an EXPLICIT window (days) -- used by the
        first-sync backfill cap to shrink a new user's early polls."""
        return _legacy()._inbound_query_for_window(int(window_days))

    def inbound_backfill_state(self, owner_user_id: str = "") -> dict[str, int] | None:
        """The first-sync backfill window for this owner's next poll (or None)."""
        return _legacy()._inbound_backfill_state(owner_user_id)

    def record_inbound_backfill_progress(self, owner_user_id: str, completed_through_days: int) -> None:
        """Persist the per-user backfill cursor after a poll secured the band."""
        _legacy()._record_inbound_backfill_progress(owner_user_id, completed_through_days)

    def max_import_limit(self) -> int:
        return int(_legacy().MAX_GMAIL_IMPORT_LIMIT)

    def triage_min_nda_score(self) -> int:
        return int(_legacy().TRIAGE_MIN_NDA_SCORE)

    def body_preview_limit(self) -> int:
        return int(_legacy().GMAIL_BODY_PREVIEW_LIMIT)

    def raise_gmail_api_error(self, error: Exception, fallback_message: str) -> None:
        _legacy()._raise_gmail_api_error(error, fallback_message)

    def gmail_retry_after_epoch(self, error: Exception) -> float:
        return float(_legacy()._gmail_retry_after_epoch(error))

    def is_self_or_outbound_message(self, message: dict[str, Any], account_email: str) -> bool:
        return bool(_legacy()._is_self_or_outbound_message(message, account_email))

    def is_docusign_notification(self, message: dict[str, Any]) -> bool:
        return bool(_legacy()._is_docusign_notification(message))

    def excluded_notification_sender(self, message: dict[str, Any]) -> str:
        """The sender-exclude entry this message matches, or "" (see
        gmail_integration._excluded_notification_sender): the hard DocuSign floor
        plus the admin-editable e-sign/calendar platform entries."""
        return str(_legacy()._excluded_notification_sender(message) or "")

    def esign_nda_capture_hit(
        self,
        message: dict[str, Any],
        attachments: list[dict[str, str]],
    ) -> bool:
        """Whether an e-sign platform notification carries an explicit NDA signal
        and must be captured for triage instead of terminally dropped."""
        return bool(_legacy()._esign_notification_nda_hit(message, attachments))

    def excluded_message_capture_probe(
        self,
        service: Any,
        message_id: str,
        attachments: list[dict[str, str]],
    ) -> bool:
        """Deterministic content probe (no AI) run before terminally dropping an
        excluded-sender message whose explicit-token capture missed: True when
        any attachment looks agreement-shaped (triage band) or the scorer is
        language-blind over substantial text."""
        return bool(
            _legacy()._excluded_message_content_probe(service, message_id, attachments)
        )

    def reviewable_attachments(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        return list(_legacy()._reviewable_attachments(payload))

    def message_nda_detection(
        self,
        message: dict[str, Any],
        attachments: list[dict[str, str]],
    ) -> dict[str, object]:
        return _legacy()._message_nda_detection(message, attachments)

    def attachment_nda_detection(
        self,
        service: Any,
        message_id: str,
        attachments: list[dict[str, str]],
    ) -> dict[str, object]:
        return _legacy()._attachment_nda_detection(service, message_id, attachments)

    def message_metadata(
        self,
        message: dict[str, Any],
        account_email: str,
        *,
        detection: dict[str, object] | None = None,
    ) -> dict[str, str]:
        return _legacy()._message_metadata(message, account_email, detection=detection)

    def attachment_bytes(self, service: Any, message_id: str, attachment: dict[str, str]) -> bytes:
        return _legacy()._attachment_bytes(service, message_id, attachment)

    def ensure_document_size(self, document_bytes: bytes) -> None:
        _legacy().ensure_document_size(document_bytes)

    def extract_document_paragraphs(self, filename: str, document_bytes: bytes):
        return _legacy().extract_document_paragraphs(filename, document_bytes)

    def extract_document(
        self,
        filename: str,
        document_bytes: bytes,
        *,
        include_visual_profile: bool = True,
    ):
        """Full (document_type, paragraphs, quality) extraction seam.

        Exposed so the inbound-poll prepare stage can run the CPU-heavy parse ONCE
        and hand the full result through to matter creation instead of extracting
        the same bytes twice (see gmail_matter_inbox.prepare_inbound_attachment).
        """
        return _legacy().extract_document(
            filename,
            document_bytes,
            include_visual_profile=include_visual_profile,
        )

    def pdf_attachment_skip_reason(self, error: Exception) -> str:
        return _legacy()._pdf_attachment_skip_reason(error)

    def attachment_nda_validation(
        self,
        filename: str,
        paragraphs: list[dict[str, Any]],
        *,
        message_metadata: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return _legacy()._attachment_nda_validation(
            filename,
            paragraphs,
            message_metadata=message_metadata,
        )

    def attachment_validation_metadata(
        self,
        metadata: dict[str, str],
        validation: dict[str, object],
    ) -> dict[str, str]:
        return _legacy()._attachment_validation_metadata(metadata, validation)

    def attachment_selector_metadata(
        self,
        metadata: dict[str, str],
        selection: dict[str, object],
    ) -> dict[str, str]:
        return _legacy()._attachment_selector_metadata(metadata, selection)

    def attachment_explicit_nda_signal(
        self,
        metadata: dict[str, str],
        candidate: dict[str, Any],
    ) -> bool:
        """The AI pre-gate's escape hatch: an explicit NDA mention in the message
        metadata (subject/body/snippet) OR a strong NDA filename signal. Presence
        of this seam is ALSO what activates the pre-gate -- a transport that
        cannot answer the escape-hatch question must not pre-gate (fail-open
        toward paying for the AI call rather than dropping a real NDA)."""
        return bool(
            _legacy()._attachment_explicit_nda_hit(
                metadata,
                str(candidate.get("filename") or ""),
            )
        )

    def selector_configured(self) -> bool:
        return bool(gmail_attachment_selector.selector_configured())

    def select_nda_attachments(
        self,
        *,
        message_metadata: dict[str, str],
        candidates: list[dict[str, Any]],
    ) -> dict[str, object]:
        return gmail_attachment_selector.select_nda_attachments(
            message_metadata=message_metadata,
            candidates=candidates,
        )

    def intake_classifier_configured(self) -> bool:
        return bool(gmail_intake_classifier.classifier_configured())

    def intake_classifier_model(self) -> str:
        return gmail_intake_classifier.configured_model()

    def gmail_intake_playbook(self) -> str:
        return gmail_intake_classifier.gmail_intake_playbook()

    def classify_intake_attachment(
        self,
        message_metadata: dict[str, Any],
        candidate: dict[str, Any],
        intake_playbook: str,
    ) -> dict[str, Any]:
        return gmail_intake_classifier.classify_intake_attachment(
            message_metadata,
            candidate,
            intake_playbook,
        )

    def resolve_intake_lane(
        self,
        det_lane: str,
        det_reason: str,
        ai_result: dict[str, Any],
    ) -> tuple[str, str]:
        return gmail_intake_classifier.resolve_intake_lane(det_lane, det_reason, ai_result)

    def message_body_text(self, payload: dict[str, Any]) -> str:
        return _legacy()._message_body_text(payload)

    def gmail_attachment_already_imported(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
        owner_user_id: str = "",
    ) -> bool:
        return DiskMatterRepository().find_gmail_attachment(
            message_id,
            attachment_id,
            attachment_filename=attachment_filename,
            attachment_sha256=attachment_sha256,
            part_id=part_id,
            owner_user_id=owner_user_id,
        ) is not None

    def inbound_drain_cursor(self, owner_user_id: str = "") -> int:
        """The persisted per-owner drain frontier (oldest reached internalDate, ms)."""
        return int(DiskMatterRepository().gmail_inbound_cursor(owner_user_id=owner_user_id))

    def advance_inbound_drain_cursor(self, owner_user_id: str, internal_date_ms: int) -> int:
        """Lower the drain frontier toward an older message (monotonic down)."""
        return int(DiskMatterRepository().advance_gmail_inbound_cursor(owner_user_id, internal_date_ms))

    def reset_inbound_drain_cursor(self, owner_user_id: str = "") -> None:
        """Drop the drain frontier once the backlog is fully drained."""
        DiskMatterRepository().reset_gmail_inbound_cursor(owner_user_id=owner_user_id)

    # -- Gmail History-API incremental inbound sync (flag-gated, default OFF) -- #
    def history_sync_enabled(self, owner_user_id: str = "") -> bool:
        """Whether the incremental History-API path is active for this owner.

        Two gates, ANDed: the master flag NDA_GMAIL_HISTORY_SYNC_ENABLED must be
        truthy (default OFF), AND -- when the optional NDA_GMAIL_HISTORY_SYNC_OWNERS
        allowlist is set -- the ``owner_user_id`` must appear in it (an empty/unset
        allowlist = all owners). Mirrors NDA_ADMIN_USERS ``google:<sub>`` gating:
        entries are comma/space separated and matched verbatim against the caller's
        cleaned owner id, so an allowlist rollout can dark-launch to one account.
        """
        raw = os.environ.get(NDA_GMAIL_HISTORY_SYNC_ENABLED_ENV, "")
        if str(raw or "").strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        allow = _history_sync_owner_allowlist()
        if not allow:
            return True
        owner = self.clean_user_token_segment(owner_user_id)
        return owner in allow

    def history_fullsweep_every(self) -> int:
        """The full-sweep cadence (every Nth clean incremental poll re-anchors).

        NDA_GMAIL_HISTORY_FULLSWEEP_EVERY, default 6. Any value < 1 (or unparseable)
        CLAMPS to the default: the mandatory full window-scan catches re-labeled/
        un-archived old mail that ``messagesAdded`` never surfaces, so it can never be
        disabled in prod.
        """
        raw = str(os.environ.get(NDA_GMAIL_HISTORY_FULLSWEEP_EVERY_ENV, "") or "").strip()
        try:
            value = int(raw) if raw else DEFAULT_GMAIL_HISTORY_FULLSWEEP_EVERY
        except (TypeError, ValueError):
            return DEFAULT_GMAIL_HISTORY_FULLSWEEP_EVERY
        return value if value >= 1 else DEFAULT_GMAIL_HISTORY_FULLSWEEP_EVERY

    def inbound_history_id(self, owner_user_id: str = "") -> str:
        """The persisted per-owner Gmail History-API frontier (``""`` if none)."""
        return str(DiskMatterRepository().gmail_inbound_history_id(owner_user_id=owner_user_id))

    def set_inbound_history_id(self, owner_user_id: str, history_id: str) -> str:
        """Store/overwrite the per-owner frontier (NOT monotonic). Returns it."""
        return str(DiskMatterRepository().set_gmail_inbound_history_id(owner_user_id, history_id))

    def inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        """The per-owner incremental-poll counter driving the full-sweep cadence."""
        return int(DiskMatterRepository().gmail_inbound_history_poll_count(owner_user_id=owner_user_id))

    def bump_inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        """Bump the per-owner incremental-poll counter; returns the new total."""
        return int(DiskMatterRepository().bump_gmail_inbound_history_poll_count(owner_user_id=owner_user_id))

    def reset_inbound_history(self, owner_user_id: str = "") -> None:
        """Drop the per-owner frontier AND poll_count (e.g. after a 404 expiry)."""
        DiskMatterRepository().reset_gmail_inbound_history(owner_user_id=owner_user_id)

    def profile_history_id(self, role: str = "inbound", *, service: Any | None = None, owner_user_id: str = "") -> str:
        """The mailbox head historyId from getProfile -- the fallback re-seed value.

        getProfile ALREADY runs once per poll (gmail_integration._gmail_profile_for_role,
        the real getProfile call is at gmail_integration.py:1039) and its response carries
        ``historyId``, so re-seeding the frontier after a fallback window-scan costs ZERO
        extra API calls. NOTE: that profile is served from a 15-minute cache
        (``_PROFILE_CACHE`` / GMAIL_PROFILE_CACHE_SECONDS), so this seed hid can LAG the
        true mailbox head by up to ~15 min. That is SAFE: a LOWER startHistoryId only
        makes the next incremental poll re-list slightly more (ledger-deduped), never
        skip mail -- a lagging seed can only cost a little re-fetch, never lose an NDA.
        Best-effort: any failure yields ``""`` so a re-seed simply does not happen (the
        next poll retries).
        """
        try:
            profile = self.gmail_profile_for_role(role, service=service, owner_user_id=owner_user_id)
        except Exception:  # pragma: no cover - re-seed is best-effort, never fails the poll
            return ""
        if not isinstance(profile, dict):
            return ""
        return str(profile.get("historyId") or "")

    def history_list(
        self,
        service: Any,
        *,
        start_history_id: str,
        label_id: str = "INBOX",
        history_types: tuple[str, ...] = ("messageAdded",),
        page_token: str = "",
    ) -> dict[str, Any]:
        """Thin wrapper over ``users().history().list`` (metadata-only, cheap).

        Lists change records since ``start_history_id``, scoped to the INBOX label and
        the ``messageAdded`` history type so the incremental path sees only newly-added
        INBOX mail. Does NOT interpret the response -- the caller drives paging and the
        terminal-only frontier advance. A 404 here means the historyId has expired
        (Gmail retains history ~1 week); the caller distinguishes that from a 429.
        """
        response = service.users().history().list(
            userId="me",
            startHistoryId=str(start_history_id),
            labelId=label_id,
            historyTypes=list(history_types),
            **({"pageToken": page_token} if page_token else {}),
        ).execute()
        return response if isinstance(response, dict) else {}

    def is_history_expired_error(self, error: Exception) -> bool:
        """True iff ``error`` is a Gmail History-API 404 (expired startHistoryId).

        Gmail purges history older than ~1 week; a startHistoryId that has aged out
        (or a mailbox that was inactive while the flag was off) returns 404. The caller
        checks ``is_rate_limit_error`` (429) FIRST, then this, so a rate-limit is never
        mistaken for an expiry (which would wrongly discard the frontier + full-scan).
        """
        status = getattr(getattr(error, "resp", None), "status", None)
        return status == 404

    def message_internal_date_ms(self, message: dict[str, Any]) -> int:
        """Gmail's server-assigned ``internalDate`` (epoch ms) for a fetched message.

        Returns ``0`` when absent/unparseable so callers treat the message as
        date-unknown and never advance the cursor on it.
        """
        try:
            return max(0, int(str(message.get("internalDate") or "0")))
        except (TypeError, ValueError):
            return 0

    def inbound_query_before(self, base_query: str, cursor_internal_date_ms: int) -> str:
        """Date-bound ``base_query`` below the drain cursor so the drained newest
        prefix never re-surfaces. ``cursor_internal_date_ms <= 0`` returns the query
        unchanged (no cursor yet). Gmail's ``before:`` takes epoch SECONDS and is
        exclusive, so we ceil the cursor (ms -> s, rounding UP) and the boundary
        message itself is re-covered by the un-bounded head scan, never dropped.
        """
        if cursor_internal_date_ms <= 0:
            return base_query
        # Round the ms->s conversion UP so a ``before:`` bound never excludes the
        # cursor message's own second; the head scan re-covers that second anyway.
        before_seconds = (cursor_internal_date_ms + 999) // 1000
        if before_seconds <= 0:
            return base_query
        return f"{base_query} before:{before_seconds}"

    def is_rate_limit_error(self, error: Exception) -> bool:
        """True when ``error`` is a Gmail 429 / rate-limit (so the poll can pace and
        keep what it imported this cycle instead of aborting the whole drain)."""
        try:
            return bool(_legacy()._gmail_retry_after_epoch(error))
        except Exception:  # pragma: no cover - probe is best-effort
            return False

    def processed_ledger_session(self, owner_user_id: str = "") -> Any:
        """A load-once / mark-many / write-once processed-message ledger session.

        Complements the drain cursor: the cursor stops the heavy re-work on
        re-surfaced mail, this stops the per-message fetch + the gmail_intake /
        gmail_triage AI calls entirely for messages that already reached a terminal
        outcome. Reads the durable per-owner ledger ONCE here; the inbox loop marks
        terminal outcomes in memory and the session writes the file ONCE at poll end.
        """
        from .gmail_processed_ledger import ProcessedLedgerSession

        return ProcessedLedgerSession(owner_user_id)

    def create_matter_from_document(self, **kwargs):
        return _legacy().create_matter_from_document(**kwargs)


def _history_sync_owner_allowlist() -> set[str]:
    """The NDA_GMAIL_HISTORY_SYNC_OWNERS allowlist (``google:<sub>`` ids), or empty.

    Comma/space separated, mirroring NDA_ADMIN_USERS parsing. An empty set means the
    allowlist is inactive (all owners pass the flag). Entries are matched verbatim
    against the caller's cleaned owner id.
    """
    raw = os.environ.get(NDA_GMAIL_HISTORY_SYNC_OWNERS_ENV, "")
    return {value.strip() for value in raw.replace(",", " ").split() if value.strip()}


_DEFAULT_TRANSPORT = GmailTransport()


def default_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT


def inbox_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT


def outbox_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT
