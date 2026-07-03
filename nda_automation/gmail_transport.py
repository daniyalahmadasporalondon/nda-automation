from __future__ import annotations

from typing import Any

from . import app_settings, gmail_attachment_selector, gmail_intake_classifier
from .matter_repository import DiskMatterRepository


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


_DEFAULT_TRANSPORT = GmailTransport()


def default_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT


def inbox_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT


def outbox_transport() -> GmailTransport:
    return _DEFAULT_TRANSPORT
