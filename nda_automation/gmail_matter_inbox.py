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
    if not transport.app_settings.gmail_role_enabled("inbound"):
        raise transport.GmailIntegrationError("Gmail inbound is disabled in Admin.")
    owner_user_id = transport._clean_user_token_segment(owner_user_id)
    service = transport._gmail_service_for_owner("inbound", owner_user_id)
    profile = transport._gmail_profile_for_role("inbound", service=service, owner_user_id=owner_user_id)
    inbound_query = query.strip() if isinstance(query, str) and query.strip() else transport._default_inbound_query()
    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    import_limit = max(1, min(requested_limit, transport.MAX_GMAIL_IMPORT_LIMIT))

    account_email = str(profile.get("emailAddress") or "")
    selector_enabled = transport.gmail_attachment_selector.selector_configured()

    try:
        result = service.users().messages().list(
            userId="me",
            q=inbound_query,
            maxResults=import_limit,
        ).execute()
    except Exception as exc:
        transport._raise_gmail_api_error(exc, "Gmail inbound sync could not list messages.")

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for message_stub in result.get("messages") or []:
        message_id = str(message_stub.get("id") or "")
        if not message_id:
            continue
        try:
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as exc:
            if transport._gmail_retry_after_epoch(exc):
                transport._raise_gmail_api_error(exc, "Gmail inbound sync could not load a message.")
            skipped.append({"message_id": message_id, "reason": "message_unavailable"})
            continue

        if transport._is_self_or_outbound_message(message, account_email):
            skipped.append({"message_id": message_id, "reason": "self_sent_or_outbound"})
            continue

        attachments = list(transport._reviewable_attachments(message.get("payload") or {}))
        if not attachments:
            skipped.append({"message_id": message_id, "reason": "no_reviewable_attachment"})
            continue

        detection = transport._message_nda_detection(message, attachments)
        if not detection["matched"] and not selector_enabled:
            detection = transport._attachment_nda_detection(service, message_id, attachments)
        if not detection["matched"] and not selector_enabled:
            skipped.append({"message_id": message_id, "reason": "no_nda_signal"})
            continue

        metadata = message_selector_metadata(
            message,
            transport._message_metadata(message, account_email, detection=detection if detection["matched"] else None),
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
    selector_enabled = transport.gmail_attachment_selector.selector_configured()
    for attachment in attachments:
        candidate, skip = prepare_inbound_attachment(
            service,
            message_id,
            attachment,
            metadata,
            transport=transport,
            owner_user_id=owner_user_id,
            require_deterministic_acceptance=not selector_enabled,
        )
        if skip is not None:
            skipped.append(skip)
        elif candidate is not None:
            prepared.append(candidate)

    selected_ids, selector_metadata = selected_candidate_attachment_ids(metadata, prepared, transport=transport)
    deterministic_fallback = selected_ids is None
    imported: list[dict[str, Any]] = []
    for candidate in prepared:
        attachment_id = str(candidate.get("attachment_id") or "")
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        if deterministic_fallback and not validation.get("accepted"):
            skipped.append(gmail_attachment_skip(
                message_id,
                str(candidate.get("filename") or ""),
                "non_nda_attachment",
                detail=str(validation.get("reason") or ""),
                score=str(validation.get("score") or "0"),
            ))
            continue
        if selected_ids is not None and attachment_id not in selected_ids:
            skipped.append(gmail_attachment_skip(
                message_id,
                str(candidate.get("filename") or ""),
                "ai_not_selected_attachment",
                detail=selector_metadata.get("reason", ""),
                model=selector_metadata.get("model", ""),
                confidence=selector_metadata.get("confidence", ""),
            ))
            continue
        matter, skip = create_matter_from_prepared_attachment(
            candidate,
            metadata,
            transport=transport,
            selector_metadata=selector_metadata if selected_ids is not None else None,
            owner_user_id=owner_user_id,
        )
        if skip is not None:
            skipped.append(skip)
        elif matter is not None:
            imported.append(matter)
    return {"imported": imported, "skipped": skipped}


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
    require_deterministic_acceptance: bool = True,
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
        document_bytes = transport._attachment_bytes(service, message_id, attachment)
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
            transport._pdf_attachment_skip_reason(error),
            detail=str(error),
        )
    except transport.DocxExtractionError as error:
        return None, gmail_attachment_skip(
            message_id,
            attachment_filename,
            "review_failed",
            detail=str(error),
        )

    validation = transport._attachment_nda_validation(
        attachment_filename,
        paragraphs,
        message_metadata=metadata,
    )
    if require_deterministic_acceptance and not validation["accepted"]:
        return None, gmail_attachment_skip(
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

    metadata = transport._attachment_validation_metadata(metadata, validation)
    if selector_metadata:
        metadata = transport._attachment_selector_metadata(metadata, selector_metadata)

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
    if not prepared or not transport.gmail_attachment_selector.selector_configured():
        return None, {}
    try:
        selection = transport.gmail_attachment_selector.select_nda_attachments(
            message_metadata=metadata,
            candidates=prepared,
        )
    except transport.gmail_attachment_selector.GmailAttachmentSelectorError:
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
    body_preview = transport._message_body_text(message.get("payload") or {})
    if not body_preview:
        return metadata
    return {
        **metadata,
        "message_body_preview": body_preview[:transport.GMAIL_BODY_PREVIEW_LIMIT],
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
    return transport.matter_store.find_gmail_attachment(
        message_id,
        attachment_id,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ) is not None


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
