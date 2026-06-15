from __future__ import annotations

import hashlib
from typing import Any


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

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
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
        )
        imported.extend(attachment_result["imported"])
        skipped.extend(attachment_result["skipped"])

    return {
        "account": account_email,
        "imported": imported,
        "query": inbound_query,
        "skipped": skipped,
    }


def import_inbound_attachments(
    service: Any,
    message_id: str,
    attachments: list[dict[str, Any]],
    metadata: dict[str, str],
    *,
    transport: Any,
    owner_user_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
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
        lane, triage_reason = classify_attachment_lane(
            validation,
            selector_selected=selector_selected,
            selector_configured=selected_ids is not None,
            triage_min_score=triage_min_score,
        )
        if lane == "skip":
            # Precision lane preserved: emit the same skip reasons as before so
            # the skipped-list telemetry is unchanged for genuinely-irrelevant
            # attachments.
            if selected_ids is not None and not selector_selected:
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
        matter, skip = create_matter_from_prepared_attachment(
            candidate,
            metadata,
            transport=transport,
            selector_metadata=selector_metadata if selected_ids is not None else None,
            owner_user_id=owner_user_id,
            triage=lane == "triage",
            triage_reason=triage_reason,
        )
        if skip is not None:
            skipped.append(skip)
        elif matter is not None:
            imported.append(matter)
    return {"imported": imported, "skipped": skipped}


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
                    "triage_confidence": str(validation.get("score") or "0"),
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
