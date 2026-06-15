from __future__ import annotations

import hashlib
import logging
from typing import Any

# Per-sync cost cap for the AI intake classifier. Imported here so the budget the
# inbox loop hands down stays in lockstep with the classifier's own cap constant.
from .gmail_intake_classifier import MAX_INTAKE_CALLS_PER_SYNC

LOGGER = logging.getLogger(__name__)

# When this fraction (or more) of the AI intake calls in a sync fail (error or
# timeout), the classifier is likely degraded (bad model slug, rate-limit,
# OpenRouter down) rather than hitting the occasional bad response, so the sync
# emits a warn-log. Below this the silent per-call fallback is fine.
_AI_DEGRADED_FRACTION = 0.5


def import_inbound_matters(
    *,
    transport: Any,
    limit: int = 10,
    query: str | None = None,
    owner_user_id: str = "",
) -> dict[str, Any]:
    if not transport.gmail_role_enabled("inbound"):
        raise transport.GmailIntegrationError("Gmail inbound is disabled in Admin.")
    owner_user_id = transport.clean_user_token_segment(owner_user_id)
    service = transport.gmail_service_for_owner("inbound", owner_user_id)
    profile = transport.gmail_profile_for_role("inbound", service=service, owner_user_id=owner_user_id)
    inbound_query = query.strip() if isinstance(query, str) and query.strip() else transport.default_inbound_query()
    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    import_limit = max(1, min(requested_limit, transport.max_import_limit()))

    account_email = str(profile.get("emailAddress") or "")

    # Paginated fetch: a single list() call only returns one Gmail page and
    # ignores nextPageToken, so a broadened (keyword-gate-free) fetch could be
    # silently truncated below import_limit. Accumulate stubs across pages,
    # capping each page at 100, until we reach import_limit or run out of pages.
    # pageToken is passed only when non-empty so single-page transport fakes that
    # do not accept the kwarg keep working.
    # Termination guards: Gmail can return a NON-empty nextPageToken on a page
    # that yielded ZERO new messages, which (combined with the import cap never
    # being reached) would spin this loop forever. Stop on a zero-progress page
    # AND enforce a hard page cap that comfortably covers import_limit even if
    # every page only returned a single message.
    message_stubs: list[dict[str, Any]] = []
    page_token = ""
    max_pages = import_limit + 25
    try:
        for _ in range(max_pages):
            if len(message_stubs) >= import_limit:
                break
            page = service.users().messages().list(
                userId="me",
                q=inbound_query,
                maxResults=min(import_limit - len(message_stubs), 100),
                **({"pageToken": page_token} if page_token else {}),
            ).execute()
            new_stubs = page.get("messages") or []
            message_stubs.extend(new_stubs)
            page_token = str(page.get("nextPageToken") or "")
            # Stop on an empty next-page token OR a zero-progress page (a page
            # that advanced the token but returned no messages).
            if not page_token or not new_stubs:
                break
    except Exception as exc:
        transport.raise_gmail_api_error(exc, "Gmail inbound sync could not list messages.")
    message_stubs = message_stubs[:import_limit]

    # The AI intake classifier reads its criteria block once per sync and shares a
    # single per-sync call budget across every message/attachment so the cost cap
    # bounds the whole sync, not each message. Both are computed defensively: if the
    # classifier transport is unavailable the playbook stays empty and the budget is
    # never drawn down, leaving the deterministic path byte-identical to today.
    intake_playbook = _intake_playbook(transport)
    intake_budget = _IntakeCallBudget(MAX_INTAKE_CALLS_PER_SYNC)

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    # Roll the per-message AI intake tallies up into a single per-sync total so the
    # sync result carries an honest health signal for the classifier.
    sync_tallies = _IntakeTallies()
    for message_stub in message_stubs:
        message_id = str(message_stub.get("id") or "")
        if not message_id:
            continue
        try:
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as exc:
            if transport.gmail_retry_after_epoch(exc):
                transport.raise_gmail_api_error(exc, "Gmail inbound sync could not load a message.")
            skipped.append({"message_id": message_id, "reason": "message_unavailable"})
            continue

        if transport.is_self_or_outbound_message(message, account_email):
            skipped.append({"message_id": message_id, "reason": "self_sent_or_outbound"})
            continue

        attachments = list(transport.reviewable_attachments(message.get("payload") or {}))
        if not attachments:
            skipped.append({"message_id": message_id, "reason": "no_reviewable_attachment"})
            continue

        # Always make the per-message detection content-aware: if subject/body/
        # snippet/filename carry no NDA signal, fall back to scanning attachment
        # content. There is NO terminal drop here anymore -- the deterministic
        # per-attachment band classifier (import_inbound_attachments) is the
        # authoritative classifier, so an attachment-only NDA with a neutral
        # subject is never dropped before its content is judged.
        detection = transport.message_nda_detection(message, attachments)
        if not detection["matched"]:
            detection = transport.attachment_nda_detection(service, message_id, attachments)

        metadata = message_selector_metadata(
            message,
            transport.message_metadata(message, account_email, detection=detection if detection["matched"] else None),
            transport=transport,
        )
        attachment_result = import_inbound_attachments(
            service,
            message_id,
            attachments,
            metadata,
            transport=transport,
            owner_user_id=owner_user_id,
            intake_playbook=intake_playbook,
            intake_budget=intake_budget,
        )
        imported.extend(attachment_result["imported"])
        skipped.extend(attachment_result["skipped"])
        sync_tallies.merge(attachment_result.get("ai_intake"))

    return {
        "account": account_email,
        "imported": imported,
        "query": inbound_query,
        "skipped": skipped,
        "ai_intake": sync_tallies.as_dict(),
    }


def import_inbound_attachments(
    service: Any,
    message_id: str,
    attachments: list[dict[str, Any]],
    metadata: dict[str, str],
    *,
    transport: Any,
    owner_user_id: str = "",
    intake_playbook: str | None = None,
    intake_budget: "_IntakeCallBudget | None" = None,
) -> dict[str, list[dict[str, Any]]]:
    # The AI intake classifier (when configured) overlays the deterministic band
    # lane below. Compute the playbook/budget defensively so that, when called
    # standalone (e.g. tests, or the classifier transport being absent), the path
    # collapses to the deterministic-only behaviour.
    if intake_playbook is None:
        intake_playbook = _intake_playbook(transport)
    if intake_budget is None:
        intake_budget = _IntakeCallBudget(MAX_INTAKE_CALLS_PER_SYNC)
    intake_configured = _intake_classifier_configured(transport)

    # Per-sync AI intake telemetry. A degraded classifier (bad model slug,
    # rate-limit, OpenRouter down/timeout) silently falls back to the deterministic
    # lane per-call; without these counts a fully-broken classifier is
    # indistinguishable from a healthy one. Accumulated here and surfaced in the
    # result (and merged across messages by import_inbound_matters).
    tallies = _IntakeTallies()

    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    # Always prepare with the deterministic validation computed (no
    # require_deterministic_acceptance suppression). Classification is decided per
    # candidate by the band classifier below, never short-circuited here.
    for attachment in attachments:
        candidate, skip = prepare_inbound_attachment(
            service,
            message_id,
            attachment,
            metadata,
            transport=transport,
            owner_user_id=owner_user_id,
        )
        if skip is not None:
            skipped.append(skip)
        elif candidate is not None:
            prepared.append(candidate)

    selected_ids, selector_metadata = selected_candidate_attachment_ids(metadata, prepared, transport=transport)
    triage_min_score = _triage_min_score(transport)
    imported: list[dict[str, Any]] = []
    for candidate in prepared:
        attachment_id = str(candidate.get("attachment_id") or "")
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        selector_selected = selected_ids is not None and attachment_id in selected_ids
        # 1. The deterministic lane is the FALLBACK and the floor (pure + unit-tested).
        det_lane, det_reason = classify_attachment_lane(
            validation,
            selector_selected=selector_selected,
            selector_configured=selected_ids is not None,
            triage_min_score=triage_min_score,
        )
        # 2. + 3. AI overlay: run the classifier when configured and the per-sync cap
        # is not yet exhausted, then reconcile (fail toward triage on ambiguity). Any
        # unconfigured/error/timeout/overflow yields a non-ok status, and
        # resolve_intake_lane returns the deterministic lane verbatim.
        ai_result = _maybe_classify_intake(
            transport,
            metadata,
            candidate,
            intake_playbook,
            intake_budget,
            configured=intake_configured,
        )
        tallies.record(str(ai_result.get("status") or ""))
        lane, triage_reason = transport.resolve_intake_lane(det_lane, det_reason, ai_result)
        ai_ok = ai_result.get("status") == "ok"
        if lane == "skip":
            # An AI NOT_NDA terminal skip carries the AI reason/model so the
            # skipped-list telemetry explains the drop; otherwise the deterministic
            # / selector skip reasons are emitted byte-identically to before.
            if ai_ok and ai_result.get("verdict") == "NOT_NDA":
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "non_nda_attachment",
                    detail=str(ai_result.get("reason") or ""),
                    model=str(ai_result.get("model") or ""),
                ))
            elif selected_ids is not None and not selector_selected:
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "ai_not_selected_attachment",
                    detail=selector_metadata.get("reason", ""),
                    model=selector_metadata.get("model", ""),
                    confidence=selector_metadata.get("confidence", ""),
                ))
            else:
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "non_nda_attachment",
                    detail=str(validation.get("reason") or ""),
                    score=str(validation.get("score") or "0"),
                ))
            continue
        # When the AI is what put this candidate into triage, surface the model's
        # confidence on the matter (overriding the deterministic score) so the
        # dashboard triage card shows the model's confidence.
        triage_confidence_override = (
            _ai_triage_confidence(ai_result, triage_reason) if lane == "triage" else None
        )
        matter, skip = create_matter_from_prepared_attachment(
            candidate,
            metadata,
            transport=transport,
            selector_metadata=selector_metadata if selected_ids is not None else None,
            owner_user_id=owner_user_id,
            triage=lane == "triage",
            triage_reason=triage_reason,
            triage_confidence=triage_confidence_override,
        )
        if skip is not None:
            skipped.append(skip)
        elif matter is not None:
            imported.append(matter)
    tallies.warn_if_degraded(LOGGER, message_id, model=_intake_model(transport))
    return {"imported": imported, "skipped": skipped, "ai_intake": tallies.as_dict()}


class _IntakeTallies:
    """Per-sync counters for the AI intake classifier's health.

    ``ai_calls`` counts attempts that actually reached the model (``ok`` plus the
    degraded ``error`` / ``timeout`` outcomes); ``ai_errors`` / ``ai_timeouts`` are
    the degraded subsets, and ``ai_skipped_cap`` counts candidates that took the
    deterministic lane because the per-sync budget was exhausted. ``not_configured``
    is not counted (no call was attempted).
    """

    def __init__(self) -> None:
        self.ai_calls = 0
        self.ai_errors = 0
        self.ai_timeouts = 0
        self.ai_skipped_cap = 0

    def record(self, status: str) -> None:
        if status in ("ok", "error", "timeout"):
            self.ai_calls += 1
        if status == "error":
            self.ai_errors += 1
        elif status == "timeout":
            self.ai_timeouts += 1
        elif status == "skipped_cap":
            self.ai_skipped_cap += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "ai_calls": self.ai_calls,
            "ai_errors": self.ai_errors,
            "ai_timeouts": self.ai_timeouts,
            "ai_skipped_cap": self.ai_skipped_cap,
        }

    def merge(self, other: "_IntakeTallies | dict[str, Any] | None") -> None:
        if other is None:
            return
        data = other.as_dict() if isinstance(other, _IntakeTallies) else other
        self.ai_calls += int(data.get("ai_calls") or 0)
        self.ai_errors += int(data.get("ai_errors") or 0)
        self.ai_timeouts += int(data.get("ai_timeouts") or 0)
        self.ai_skipped_cap += int(data.get("ai_skipped_cap") or 0)

    def warn_if_degraded(self, logger: logging.Logger, scope: str, *, model: str = "") -> None:
        """Warn when failures are a high fraction of the calls actually attempted."""
        degraded = self.ai_errors + self.ai_timeouts
        if self.ai_calls > 0 and degraded >= self.ai_calls * _AI_DEGRADED_FRACTION:
            logger.warning(
                "Gmail intake classifier degraded over %s: %d/%d calls failed "
                "(errors=%d, timeouts=%d, model=%s); deterministic intake lane used "
                "for those candidates.",
                scope or "sync",
                degraded,
                self.ai_calls,
                self.ai_errors,
                self.ai_timeouts,
                model or "unknown",
            )


def _intake_model(transport: Any) -> str:
    getter = getattr(transport, "intake_classifier_model", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # pragma: no cover - model probe is best-effort
            return ""
    return ""


class _IntakeCallBudget:
    """A mutable per-sync counter that caps how many AI intake calls are made.

    Shared across every message in a single sync so the cost cap bounds the whole
    sync rather than each message. Once exhausted, candidates take the deterministic
    lane (the classifier is reported as ``skipped_cap``).
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.used = 0

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _intake_playbook(transport: Any) -> str:
    getter = getattr(transport, "gmail_intake_playbook", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # pragma: no cover - playbook read is best-effort
            return ""
    return ""


def _intake_classifier_configured(transport: Any) -> bool:
    getter = getattr(transport, "intake_classifier_configured", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:  # pragma: no cover - configuration probe is best-effort
            return False
    return False


def _maybe_classify_intake(
    transport: Any,
    metadata: dict[str, str],
    candidate: dict[str, Any],
    intake_playbook: str,
    intake_budget: "_IntakeCallBudget",
    *,
    configured: bool,
) -> dict[str, Any]:
    """Run the AI intake classifier, honouring the configuration + per-sync budget.

    Returns the classifier result dict (``status`` ``ok`` / ``not_configured`` /
    ``error`` / ``timeout`` / ``skipped_cap``). The reconciliation in
    :func:`gmail_intake_classifier.resolve_intake_lane` only acts on ``ok``, so any
    other status transparently falls back to the deterministic lane.
    """
    if not configured:
        return {"status": "not_configured"}
    classify = getattr(transport, "classify_intake_attachment", None)
    if not callable(classify):
        return {"status": "not_configured"}
    if not intake_budget.consume():
        return {"status": "skipped_cap"}
    try:
        result = classify(metadata, candidate, intake_playbook)
    except Exception:  # pragma: no cover - any classifier failure -> deterministic
        return {"status": "error"}
    if not isinstance(result, dict):
        return {"status": "error"}
    return result


def _ai_triage_confidence(ai_result: dict[str, Any], triage_reason: str) -> str | None:
    """The model confidence (0-100, as a string) when the AI drove the triage.

    Only the AI-originated triage reasons override the deterministic score; a
    deterministic/selector triage keeps its own score-derived confidence.
    """
    from .gmail_intake_classifier import (
        REASON_AI_NDA_NO_DET_BASIS,
        REASON_AI_NOT_NDA_VS_DET_NDA,
        REASON_AI_UNCERTAIN,
    )

    if ai_result.get("status") != "ok":
        return None
    if triage_reason not in {
        REASON_AI_UNCERTAIN,
        REASON_AI_NOT_NDA_VS_DET_NDA,
        REASON_AI_NDA_NO_DET_BASIS,
    }:
        return None
    try:
        confidence = float(ai_result.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    return str(int(round(max(0.0, min(1.0, confidence)) * 100)))


def _triage_min_score(transport: Any) -> int:
    getter = getattr(transport, "triage_min_nda_score", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError):
            pass
    return 40


def classify_attachment_lane(
    validation: dict[str, Any],
    *,
    selector_selected: bool,
    selector_configured: bool,
    triage_min_score: int = 40,
) -> tuple[str, str]:
    """Band-classify a prepared attachment into one of three lanes.

    Returns ``(lane, triage_reason)`` where ``lane`` is one of:

    - ``"confident"``: auto-ingest, no flag. The deterministic validation
      ``accepted`` bar (score >= MIN_ATTACHMENT_NDA_SCORE AND has_content_basis)
      is met, OR the AI selector is configured and selected this attachment
      (the selector promotes a below-confident attachment).
    - ``"triage"``: import anyway but flag ``needs_triage``. Not accepted, but
      there is a real NDA content basis that is merely uncertain
      (``has_content_basis`` true OR ``triage_min_score <= score < confident``),
      OR it reached here via selector-not-selected while still carrying a
      content basis.
    - ``"skip"``: clearly not an NDA (terminal precision lane).
    """
    score = _coerce_int(validation.get("score"))
    accepted = bool(validation.get("accepted"))
    has_content_basis = bool(validation.get("has_content_basis"))
    deterministic_triage = has_content_basis or score >= triage_min_score

    if selector_configured:
        # The selector is the ranking authority over candidates that already
        # cleared the deterministic floor: a selected attachment is promoted to
        # confident (even below the deterministic confident band), while a
        # non-selected attachment is demoted out of confident. A non-selected
        # attachment with a content basis becomes a flagged-for-human triage
        # matter rather than the old terminal ai_not_selected_attachment drop;
        # one with no content basis stays skip.
        if selector_selected:
            return "confident", ""
        if deterministic_triage:
            return "triage", "ai_selector_not_selected"
        return "skip", ""

    # No selector authority: the deterministic acceptance bar governs the
    # confident lane, the uncertain-but-content-bearing middle band is triaged,
    # and the rest is terminally skipped.
    if accepted:
        return "confident", ""
    if deterministic_triage:
        return "triage", "low_confidence_nda_content"
    return "skip", ""


def _coerce_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def import_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    candidate, skip = prepare_inbound_attachment(service, message_id, attachment, metadata, transport=transport)
    if skip is not None or candidate is None:
        return None, skip
    return create_matter_from_prepared_attachment(candidate, metadata, transport=transport)


def prepare_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
    owner_user_id: str = "",
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    attachment_id = str(attachment.get("attachment_id") or "")
    attachment_filename = str(attachment.get("filename") or "")
    part_id = str(attachment.get("part_id") or "")

    if gmail_attachment_already_imported(
        message_id,
        attachment_id,
        transport=transport,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    try:
        document_bytes = transport.attachment_bytes(service, message_id, attachment)
    except transport.GmailIntegrationError:
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    try:
        transport.ensure_document_size(document_bytes)
    except transport.DocumentSizeError:
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_too_large")

    attachment_sha256 = hashlib.sha256(document_bytes).hexdigest()
    if gmail_attachment_already_imported(
        message_id,
        attachment_id,
        transport=transport,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    try:
        _document_type, paragraphs = transport.extract_document_paragraphs(attachment_filename, document_bytes)
    except transport.PdfExtractionError as error:
        return None, gmail_attachment_skip(
            message_id,
            attachment_filename,
            transport.pdf_attachment_skip_reason(error),
            detail=str(error),
        )
    except transport.DocxExtractionError as error:
        return None, gmail_attachment_skip(
            message_id,
            attachment_filename,
            "review_failed",
            detail=str(error),
        )

    # Always compute the deterministic validation and return it on the candidate;
    # the caller band-classifies (confident / triage / skip). No terminal skip
    # here, so a below-confident-but-content-bearing attachment is never dropped
    # before classification.
    validation = transport.attachment_nda_validation(
        attachment_filename,
        paragraphs,
        message_metadata=metadata,
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
        "text_preview": attachment_text_preview(paragraphs),
        "validation": validation,
    }, None


def create_matter_from_prepared_attachment(
    candidate: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
    selector_metadata: dict[str, object] | None = None,
    owner_user_id: str = "",
    triage: bool = False,
    triage_reason: str = "",
    triage_confidence: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    message_id = str(candidate.get("message_id") or "")
    attachment_id = str(candidate.get("attachment_id") or "")
    attachment_filename = str(candidate.get("filename") or "")
    attachment_sha256 = str(candidate.get("attachment_sha256") or "")
    document_bytes = candidate.get("document_bytes")
    part_id = str(candidate.get("part_id") or "")
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}

    if not isinstance(document_bytes, bytes):
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    metadata = transport.attachment_validation_metadata(metadata, validation)
    if selector_metadata:
        metadata = transport.attachment_selector_metadata(metadata, selector_metadata)

    try:
        matter = transport.create_matter_from_document(
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
                **({
                    "needs_triage": "true",
                    # When the AI drove the triage its confidence overrides the
                    # deterministic score so the dashboard card shows the model's
                    # number; otherwise the deterministic score stands.
                    "triage_confidence": (
                        triage_confidence
                        if triage_confidence is not None
                        else str(validation.get("score") or "0")
                    ),
                    "triage_reason": triage_reason,
                } if triage else {}),
            },
            dedupe_gmail=True,
            owner_user_id=owner_user_id,
        )
    except (
        transport.ActiveReviewEngineError,
        transport.DocxExtractionError,
        transport.PdfExtractionError,
        transport.ParagraphAlignmentError,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "review_failed")

    if matter.get("_existing_gmail_duplicate"):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")
    return matter, None


def selected_candidate_attachment_ids(
    metadata: dict[str, str],
    prepared: list[dict[str, Any]],
    *,
    transport: Any,
) -> tuple[set[str] | None, dict[str, object]]:
    if not prepared or not transport.selector_configured():
        return None, {}
    try:
        selection = transport.select_nda_attachments(
            message_metadata=metadata,
            candidates=prepared,
        )
    except transport.GmailAttachmentSelectorError:
        return None, {}
    if selection.get("status") != "selected":
        return None, {}
    selected_ids = {
        str(attachment_id)
        for attachment_id in selection.get("selected_attachment_ids", [])
        if str(attachment_id)
    }
    return (selected_ids or None), selection


def message_selector_metadata(message: dict[str, Any], metadata: dict[str, str], *, transport: Any) -> dict[str, str]:
    body_preview = transport.message_body_text(message.get("payload") or {})
    if not body_preview:
        return metadata
    return {
        **metadata,
        "message_body_preview": body_preview[:transport.body_preview_limit()],
    }


def attachment_text_preview(paragraphs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for paragraph in paragraphs[:12]:
        text = " ".join(str(paragraph.get("text") or "").split())
        if text:
            chunks.append(text)
    return "\n".join(chunks)[:3000]


def gmail_attachment_already_imported(
    message_id: str,
    attachment_id: str,
    *,
    transport: Any,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
    owner_user_id: str = "",
) -> bool:
    return transport.gmail_attachment_already_imported(
        message_id,
        attachment_id,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    )


def gmail_attachment_skip(message_id: str, attachment_filename: str, reason: str, **details: object) -> dict[str, str]:
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
