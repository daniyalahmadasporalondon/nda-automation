from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from .. import document_rendering, export_service, gmail_integration, matter_store, matter_view, telemetry
from ..ai_assessor import AIAssessorError, assess_nda_with_ai
from ..checker import (
    EvidenceProvenanceError,
    ParagraphAlignmentError,
    PlaybookTemplateError,
)
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..docx_text import DocxExtractionError
from ..http_auth import _env_flag_enabled
from ..ingestion_service import create_matter_from_document, is_supported_document_filename
from ..pdf_text import PdfExtractionError
from ..review_engine import ActiveReviewEngineError, review_nda_with_active_engine
from ..review_staleness import review_result_is_stale, review_result_staleness
from ..triage import triage_review_result
from .common import parse_matter_id, request_owner_user_id

AI_FIRST_REVIEW_FEATURE_FLAG = "NDA_AI_FIRST_REVIEW_ENABLED"
HTTP_MATTER_SOURCE_COLUMNS = {"manual_upload": "in_review"}
MATTER_BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}
MANUAL_UPLOAD_BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}
MAX_REDLINE_DRAFT_ITEMS = 200


def handle_matter_list(handler, *, send_body: bool = True) -> None:
    try:
        handler._send_json(
            {"matters": matter_view.public_matters(matter_store.list_matters(request_owner_user_id(handler)))},
            send_body=send_body,
        )
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)


def handle_matter_review(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/review")
    matter = _matter_for_review_response(handler, matter_id, send_body=send_body)
    if matter is None:
        return
    handler._send_json(_matter_review_payload(matter, matter_id), send_body=send_body)


def handle_matter_review_refresh(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/review-refresh")
    matter = _matter_for_review_response(handler, matter_id, send_body=True)
    if matter is None:
        return
    was_stale = review_result_is_stale(matter.get("review_result"))
    had_redline_draft = isinstance(matter.get("redline_draft"), dict)
    refreshed_matter = refresh_stale_matter_review(matter)
    handler._send_json(_matter_review_payload(
        refreshed_matter,
        matter_id,
        was_stale=was_stale,
        had_redline_draft=had_redline_draft,
        refresh_attempted=True,
    ))


def _matter_for_review_response(handler, matter_id: str | None, *, send_body: bool) -> dict | None:
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return None
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    return matter


def _matter_review_payload(
    matter: dict,
    matter_id: str | None,
    *,
    was_stale: bool | None = None,
    had_redline_draft: bool = False,
    refresh_attempted: bool = False,
) -> dict:
    staleness = review_result_staleness(matter.get("review_result"))
    if was_stale is None:
        was_stale = bool(staleness["stale"])
    is_stale = bool(staleness["stale"])
    refreshed = bool(refresh_attempted and was_stale and not is_stale)
    redline_draft_cleared = bool(
        refreshed
        and had_redline_draft
        and not isinstance(matter.get("redline_draft"), dict)
    )
    payload = matter_view.review_matter(matter)
    payload["review_refresh"] = {
        "stale": is_stale,
        "refresh_method": "POST",
        "refresh_url": f"/api/matters/{matter_id}/review-refresh",
        "refreshed": refreshed,
        "redline_draft_cleared": redline_draft_cleared,
        "stale_reasons": staleness["stale_reasons"],
        "current_playbook": staleness["current_playbook"],
        "review_playbook": staleness["review_playbook"],
        "current_review_engine_version": staleness["current_review_engine_version"],
    }
    if is_stale and staleness.get("message"):
        payload["review_refresh"]["stale_message"] = staleness["message"]
    if redline_draft_cleared:
        payload["review_refresh"]["message"] = "Saved redline draft was cleared because the review was re-analyzed."
    return payload


def handle_matter_detail(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)}, send_body=send_body)


def refresh_stale_matter_review(matter: dict) -> dict:
    if not review_result_is_stale(matter.get("review_result")):
        return matter
    extracted_text = str(matter.get("extracted_text") or "")
    if not extracted_text.strip():
        return matter
    try:
        review_result = review_nda_with_active_engine(extracted_text)
    except (ActiveReviewEngineError, EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError, ValueError):
        return matter
    triage = triage_review_result(review_result)
    updated_matter = matter_store.update_matter_review(
        str(matter.get("id") or ""),
        review_result,
        triage,
        owner_user_id=str(matter.get("owner_user_id") or ""),
    )
    if updated_matter is not None:
        return updated_matter
    refreshed_matter = {
        **matter,
        "review_result": review_result,
        **triage,
        "human_reviewed": False,
    }
    refreshed_matter.pop("redline_draft", None)
    return refreshed_matter


def handle_matter_source(handler, path: str, *, send_body: bool = True) -> None:
    """Stream a matter's stored original .docx/.pdf for faithful rendering."""
    matter_id = parse_matter_id(path, suffix="/source")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    source_path = matter_store.source_document_path(matter)
    if source_path is None:
        handler._send_json({"error": "No source document for this matter."}, status=404, send_body=send_body)
        return
    ext = source_path.suffix.lower()
    if ext == ".docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext == ".pdf":
        mime = "application/pdf"
    else:
        mime = "application/octet-stream"
    handler._send_file(source_path, content_type=mime, send_body=send_body)


def handle_matter_render_status(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-status")
    render_result = _matter_render_result(handler, matter_id, send_body=send_body)
    if render_result is None:
        return
    matter, rendered = render_result
    page_manifest = None
    if rendered.status == document_rendering.READY_STATUS and rendered.pdf_path is not None:
        page_manifest = document_rendering.render_pdf_page_image_manifest(rendered)
    handler._send_json(
        {"document_render": _public_document_render(matter_id or "", rendered, matter=matter, page_manifest=page_manifest)},
        send_body=send_body,
    )


def handle_matter_render_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-pdf")
    render_result = _matter_render_result(handler, matter_id, send_body=send_body)
    if render_result is None:
        return
    matter, rendered = render_result
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        error = rendered.error_message or "Rendered PDF is not available for this matter."
        handler._send_json(
            {
                "error": error,
                "document_render": _public_document_render(matter_id or "", rendered, matter=matter),
            },
            status=409,
            send_body=send_body,
        )
        return
    handler._send_file(rendered.pdf_path, content_type=document_rendering.PDF_CONTENT_TYPE, send_body=send_body)


def handle_matter_render_page(handler, path: str, *, send_body: bool = True) -> None:
    parsed = _parse_matter_render_page_path(path)
    if parsed is None:
        handler._send_json({"error": "Page image not found."}, status=404, send_body=send_body)
        return
    matter_id, page_number = parsed
    render_result = _matter_render_result(handler, matter_id, send_body=send_body)
    if render_result is None:
        return
    matter, rendered = render_result
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        error = rendered.error_message or "Rendered PDF is not available for this matter."
        handler._send_json(
            {
                "error": error,
                "document_render": _public_document_render(matter_id, rendered, matter=matter),
            },
            status=409,
            send_body=send_body,
        )
        return
    page_manifest = document_rendering.render_pdf_page_image_manifest(rendered)
    if page_manifest.status != document_rendering.READY_STATUS:
        handler._send_json(
            {
                "error": page_manifest.error_message or "Rendered page image is not available for this matter.",
                "document_render": _public_document_render(matter_id, rendered, matter=matter, page_manifest=page_manifest),
            },
            status=409,
            send_body=send_body,
        )
        return
    page = document_rendering.page_image_for_page_number(page_manifest, page_number)
    if page is None or page.image_path is None:
        handler._send_json(
            {
                "error": "Page image not found.",
                "document_render": _public_document_render(matter_id, rendered, matter=matter, page_manifest=page_manifest),
            },
            status=404,
            send_body=send_body,
        )
        return
    handler._send_file(page.image_path, content_type=document_rendering.PAGE_IMAGE_CONTENT_TYPE, send_body=send_body)


def _matter_render_result(
    handler,
    matter_id: str | None,
    *,
    send_body: bool,
) -> tuple[dict, document_rendering.RenderedDocument] | None:
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return None
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    source_path = matter_store.source_document_path(matter)
    if source_path is None:
        handler._send_json({"error": "No source document for this matter."}, status=404, send_body=send_body)
        return None
    try:
        rendered = document_rendering.render_source_path_to_pdf(
            source_path,
            content_type=_source_document_content_type(source_path),
            owner_user_id=str(matter.get("owner_user_id") or ""),
        )
    except document_rendering.DocxConverterBusy as error:
        # Conversion capacity is saturated; shed load with retryable backpressure
        # instead of queueing another heavyweight soffice process.
        handler._send_json(
            {"error": error.message},
            status=503,
            headers={"Retry-After": "5"},
            send_body=send_body,
        )
        return None
    return matter, rendered


def _source_document_content_type(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        return document_rendering.PDF_CONTENT_TYPE
    if suffix == ".docx":
        return document_rendering.DOCX_CONTENT_TYPE
    return ""


def _parse_matter_render_page_path(path: str) -> tuple[str, int] | None:
    prefix = "/api/matters/"
    marker = "/render-page/"
    if not path.startswith(prefix) or marker not in path:
        return None
    raw_matter_id, raw_page_number = path.removeprefix(prefix).split(marker, 1)
    matter_id = unquote(raw_matter_id).strip("/")
    if not matter_id or "/" in matter_id:
        return None
    page_number_value = raw_page_number.strip("/")
    if not page_number_value.isdigit():
        return None
    page_number = int(page_number_value)
    if page_number < 1:
        return None
    return matter_id, page_number


def _public_document_render(
    matter_id: str,
    rendered: document_rendering.RenderedDocument,
    *,
    matter: dict | None = None,
    page_manifest: document_rendering.RenderedPdfPageImageManifest | None = None,
) -> dict:
    payload = {
        "status": rendered.status,
        "source_kind": rendered.source_kind,
        "source_label": "Original PDF" if rendered.source_kind == "pdf" else "Converted DOCX",
        "cached": rendered.cached,
        "cache_key": rendered.cache_key,
    }
    if rendered.status == document_rendering.READY_STATUS and rendered.pdf_path is not None:
        payload["pdf_url"] = f"/api/matters/{matter_id}/render-pdf"
        if page_manifest is None:
            page_manifest = document_rendering.render_pdf_page_image_manifest(rendered)
        _attach_public_page_image_manifest(payload, matter_id, page_manifest)
        if matter is not None:
            payload["document_overlay"] = _public_document_overlay(matter, matter_id, page_manifest)
    if rendered.error_code:
        payload["error"] = rendered.error_message
        payload["error_code"] = rendered.error_code
    return payload


def _attach_public_page_image_manifest(
    payload: dict,
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> None:
    public_page_images = _public_page_image_manifest(matter_id, page_manifest)
    payload["page_images"] = public_page_images
    payload["page_image_status"] = page_manifest.status
    payload["pages"] = public_page_images["pages"]
    if page_manifest.dpi is not None:
        payload["dpi"] = page_manifest.dpi
    if page_manifest.scale is not None:
        payload["scale"] = page_manifest.scale
    if page_manifest.error_code:
        payload["page_image_error"] = page_manifest.error_message
        payload["page_image_error_code"] = page_manifest.error_code


def _public_page_image_manifest(
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> dict:
    payload = {
        "status": page_manifest.status,
        "cached": page_manifest.cached,
        "pages": [_public_page_image(matter_id, page) for page in page_manifest.pages],
    }
    if page_manifest.dpi is not None:
        payload["dpi"] = page_manifest.dpi
    if page_manifest.scale is not None:
        payload["scale"] = page_manifest.scale
    if page_manifest.error_code:
        payload["error"] = page_manifest.error_message
        payload["error_code"] = page_manifest.error_code
    return payload


def _public_page_image(matter_id: str, page: document_rendering.RenderedPdfPageImage) -> dict:
    payload = {
        "page_number": page.page_number,
        "image_url": f"/api/matters/{matter_id}/render-page/{page.page_number}",
    }
    if page.width is not None:
        payload["width"] = page.width
    if page.height is not None:
        payload["height"] = page.height
    if page.dpi is not None:
        payload["dpi"] = page.dpi
    if page.scale is not None:
        payload["scale"] = page.scale
    return payload


def _public_document_overlay(
    matter: dict,
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> dict:
    public_pages = [_public_page_image(matter_id, page) for page in page_manifest.pages]
    if page_manifest.status != document_rendering.READY_STATUS:
        return {
            "version": 1,
            "status": "unavailable",
            "precision": "none",
            "fallback_mode": "text_dom_scroll",
            "pages": public_pages,
            "anchors": [],
            "warnings": [page_manifest.error_message or "Page image metadata is unavailable."],
        }

    review_result = matter.get("review_result") if isinstance(matter.get("review_result"), dict) else {}
    paragraphs = review_result.get("paragraphs", []) if isinstance(review_result, dict) else []
    clauses = review_result.get("clauses", []) if isinstance(review_result, dict) else []
    redlines = review_result.get("redline_edits", []) if isinstance(review_result, dict) else []
    page_numbers = {page.page_number for page in page_manifest.pages}
    paragraphs_by_id = {
        str(paragraph.get("id")): paragraph
        for paragraph in paragraphs
        if isinstance(paragraph, dict) and paragraph.get("id") is not None
    }
    anchors: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for clause in clauses if isinstance(clauses, list) else []:
        if not isinstance(clause, dict):
            continue
        clause_id = str(clause.get("id") or "")
        matched_paragraph_ids = clause.get("matched_paragraph_ids", [])
        if not isinstance(matched_paragraph_ids, list):
            continue
        for paragraph_id in matched_paragraph_ids:
            paragraph_id = str(paragraph_id)
            anchor = _page_level_overlay_anchor(
                paragraphs_by_id.get(paragraph_id),
                target_type="evidence",
                clause_id=clause_id,
                paragraph_id=paragraph_id,
                page_numbers=page_numbers,
            )
            if anchor is None:
                continue
            key = ("evidence", clause_id, paragraph_id)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(anchor)

    for redline in redlines if isinstance(redlines, list) else []:
        if not isinstance(redline, dict):
            continue
        paragraph_id = str(redline.get("paragraph_id") or "")
        redline_id = str(redline.get("id") or "")
        anchor = _page_level_overlay_anchor(
            paragraphs_by_id.get(paragraph_id),
            target_type="redline",
            clause_id=str(redline.get("clause_id") or ""),
            paragraph_id=paragraph_id,
            page_numbers=page_numbers,
            redline_id=redline_id,
        )
        if anchor is None:
            continue
        key = ("redline", redline_id, paragraph_id)
        if key in seen:
            continue
        seen.add(key)
        anchors.append(anchor)

    warnings: list[str] = []
    if not anchors:
        warnings.append("No page-level evidence anchors were available for this review.")
    return {
        "version": 1,
        "status": "partial" if anchors else "unavailable",
        "precision": "page" if anchors else "none",
        "fallback_mode": "text_dom_scroll",
        "pages": public_pages,
        "anchors": anchors,
        "warnings": warnings,
    }


def _page_level_overlay_anchor(
    paragraph: dict | None,
    *,
    target_type: str,
    clause_id: str,
    paragraph_id: str,
    page_numbers: set[int],
    redline_id: str = "",
) -> dict | None:
    if not isinstance(paragraph, dict):
        return None
    page_number = paragraph.get("page_number")
    if not isinstance(page_number, int) or page_number not in page_numbers:
        return None
    anchor = {
        "target_type": target_type,
        "clause_id": clause_id,
        "paragraph_id": paragraph_id,
        "page_number": page_number,
        "boxes": [],
        "confidence": 0.6,
        "confidence_reason": "Page-level match only; no verified text coordinates.",
        "fallback": {
            "mode": "text_dom_scroll",
            "selector": f"[data-paragraph-id=\"{paragraph_id}\"]",
        },
    }
    if redline_id:
        anchor["redline_id"] = redline_id
    return anchor


def handle_matter_upload(handler, *, create_matter_from_document_func=create_matter_from_document) -> None:
    telemetry.increment("matter_upload_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    source_type = payload.get("source_type", "manual_upload")
    if not is_supported_document_filename(filename):
        handler._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Provide a document to import."}, status=400)
        return
    if not isinstance(source_type, str) or not source_type.strip():
        source_type = "manual_upload"
    source_type = source_type.strip()
    default_board_column = HTTP_MATTER_SOURCE_COLUMNS.get(source_type)
    if default_board_column is None:
        handler._send_json({"error": "Unsupported matter source."}, status=400)
        return
    board_column = _manual_upload_board_column(payload, default_board_column)
    if board_column is None:
        handler._send_json({"error": "Unsupported manual upload stage."}, status=400)
        return

    try:
        document_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": "The uploaded document could not be decoded."}, status=400)
        return

    try:
        ensure_document_size(document_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    try:
        matter = create_matter_from_document_func(
            filename=filename,
            document_bytes=document_bytes,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=matter_intake_metadata(payload, filename),
            owner_user_id=request_owner_user_id(handler),
        )
    except (DocxExtractionError, PdfExtractionError, ValueError) as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return
    except ActiveReviewEngineError as error:
        handler._send_json({"error": str(error)}, status=502)
        return

    handler._send_json({"matter": matter_view.public_matter(matter)}, status=201)


def _manual_upload_board_column(payload: dict, default_board_column: str) -> str | None:
    requested_board_column = payload.get("board_column")
    if requested_board_column in (None, ""):
        return default_board_column
    if not isinstance(requested_board_column, str):
        return None
    requested_board_column = requested_board_column.strip()
    if requested_board_column not in MANUAL_UPLOAD_BOARD_COLUMNS:
        return None
    return requested_board_column


def matter_intake_metadata(payload: dict, filename: str) -> dict[str, str]:
    sender = clean_intake_text(payload.get("sender"))
    sender = gmail_integration.recipient_email(sender) if sender else ""
    metadata = {
        "sender": sender or "Manual upload",
        "subject": clean_intake_text(payload.get("subject")) or Path(filename).stem or "Untitled NDA",
        "received_at": clean_intake_text(payload.get("received_at")),
        "message_snippet": clean_intake_text(payload.get("message_snippet")) or f"Manual upload of {Path(filename).name or 'NDA document'}.",
        "attachment_filename": clean_intake_text(payload.get("attachment_filename")) or filename,
    }
    reply_to = clean_intake_text(payload.get("reply_to"))
    reply_to = gmail_integration.recipient_email(reply_to) if reply_to else ""
    if reply_to:
        metadata["reply_to"] = reply_to
    return metadata


def clean_intake_text(value: object, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]


def handle_matter_stage_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/stage")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    board_column = payload.get("board_column", "")
    if not isinstance(board_column, str) or board_column not in MATTER_BOARD_COLUMNS:
        handler._send_json({"error": "Unsupported matter stage."}, status=400)
        return

    matter = matter_store.update_matter_stage(matter_id, board_column, owner_user_id=request_owner_user_id(handler))
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_reviewed_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    reviewed = payload.get("reviewed", True)
    if not isinstance(reviewed, bool):
        handler._send_json({"error": "reviewed must be true or false."}, status=400)
        return

    matter = matter_store.update_matter_fields(
        matter_id,
        {"human_reviewed": reviewed},
        owner_user_id=request_owner_user_id(handler),
    )
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_ai_first_review(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/ai-first-review")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if not _env_flag_enabled(AI_FIRST_REVIEW_FEATURE_FLAG):
        handler._send_json(
            {
                "error": (
                    "AI-first matter review is disabled. "
                    f"Set {AI_FIRST_REVIEW_FEATURE_FLAG}=true to run it."
                )
            },
            status=403,
        )
        return

    matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    extracted_text = str(matter.get("extracted_text") or "")
    if not extracted_text.strip():
        handler._send_json({"error": "Matter has no extracted text to assess."}, status=400)
        return

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        ai_first_review_result = assess_nda_with_ai(
            extracted_text,
            paragraphs=review_result_paragraphs(matter.get("review_result")),
        )
    except AIAssessorError as error:
        handler._send_json({"error": str(error)}, status=502)
        return
    except (EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError, ValueError) as error:
        handler._send_json({"error": f"AI-first review could not be completed: {error}"}, status=500)
        return

    completed_at = datetime.now(timezone.utc).isoformat()
    metadata = ai_first_review_store_metadata(
        ai_first_review_result,
        started_at=started_at,
        completed_at=completed_at,
    )
    updated_matter = matter_store.update_matter_ai_first_review(
        matter_id,
        ai_first_review_result,
        metadata,
        owner_user_id=request_owner_user_id(handler),
    )
    if updated_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({
        "matter": matter_view.public_matter(updated_matter),
        "ai_first_review_metadata": updated_matter.get("ai_first_review_metadata"),
        "ai_first_review_result": ai_first_review_result,
    })


def review_result_paragraphs(review_result: object) -> list[dict] | None:
    if not isinstance(review_result, dict):
        return None
    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list):
        return None
    cleaned = [paragraph for paragraph in paragraphs if isinstance(paragraph, dict)]
    return cleaned or None


def ai_first_review_store_metadata(
    ai_first_review_result: dict,
    *,
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    result_metadata = ai_first_review_result.get("ai_first_review")
    if not isinstance(result_metadata, dict):
        result_metadata = {}
    return {
        "status": str(result_metadata.get("status") or "completed"),
        "mode": str(result_metadata.get("mode") or "ai_first_assessor"),
        "provider": str(result_metadata.get("provider") or ""),
        "model": str(result_metadata.get("model") or ""),
        "review_mode": str(ai_first_review_result.get("review_mode") or ""),
        "review_engine_version": ai_first_review_result.get("review_engine_version"),
        "started_at": started_at,
        "completed_at": completed_at,
        "requirements_passed": int(ai_first_review_result.get("requirements_passed") or 0),
        "requirements_needs_review": int(ai_first_review_result.get("requirements_needs_review") or 0),
        "requirements_failed": int(ai_first_review_result.get("requirements_failed") or 0),
    }


def handle_matter_redline_draft_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/redline-draft")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    raw_draft = payload.get("redline_draft")
    if raw_draft is None:
        draft = None
    elif isinstance(raw_draft, dict):
        draft = clean_redline_draft(raw_draft)
    else:
        handler._send_json({"error": "Redline draft must be an object or null."}, status=400)
        return

    matter = matter_store.update_redline_draft(
        matter_id,
        draft,
        owner_user_id=request_owner_user_id(handler),
    )
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def clean_redline_draft(draft: dict) -> dict:
    manual_redlines = [
        cleaned
        for cleaned in (
            export_service.clean_manual_export_redline(redline)
            for redline in clean_dict_list(draft.get("manual_redline_edits"))
        )
        if cleaned is not None
    ]
    cleaned = {
        "clause_decisions": clean_bool_map(draft.get("clause_decisions")),
        "redline_decisions": clean_bool_map(draft.get("redline_decisions")),
        "template_selections": clean_text_map(draft.get("template_selections")),
        "reviewed_clause_ids": clean_bool_map(draft.get("reviewed_clause_ids")),
        "export_redline_edits": clean_dict_list(draft.get("export_redline_edits")),
        "manual_redline_edits": manual_redlines,
        "review_comments": export_service.clean_review_comments(draft.get("review_comments")),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    cleaned["summary"] = {
        "included_redline_count": len(cleaned["export_redline_edits"]),
        "manual_redline_count": len(cleaned["manual_redline_edits"]),
        "review_comment_count": len(cleaned["review_comments"]),
    }
    return cleaned


def clean_bool_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        if key:
            cleaned[key] = bool(item)
    return cleaned


def clean_text_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        item = str(item).strip()[:240]
        if key and item:
            cleaned[key] = item
    return cleaned


def clean_dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:MAX_REDLINE_DRAFT_ITEMS]:
        if not isinstance(item, dict):
            continue
        cleaned.append(json.loads(json.dumps(item)))
    return cleaned


def handle_matter_delete(handler, path: str) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    owner_user_id = request_owner_user_id(handler)
    # Capture the source bytes before deletion unlinks them, so the render-cache
    # purge below can recompute the (content + owner) cache key. Reading is gated
    # on ownership: a non-owner caller gets None and the matter is not deleted.
    pre_delete = matter_store.get_matter(matter_id, owner_user_id=owner_user_id)
    source_bytes = matter_store.get_source_document_bytes(pre_delete) if pre_delete else None
    source_filename = str(pre_delete.get("source_filename") or "") if pre_delete else ""

    matter = matter_store.delete_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if source_bytes is not None:
        # Purge the deleted matter's rendered artifacts so they do not outlive
        # the matter (and cannot be served to a later, unrelated matter).
        document_rendering.purge_render_cache_for_source(
            source_bytes,
            owner_user_id=str(matter.get("owner_user_id") or owner_user_id),
            source_filename=source_filename,
        )
    handler._send_json({"deleted": matter_view.public_matter(matter)})


def handle_demo_reset(handler) -> None:
    removed_count = matter_store.reset_demo_repository(owner_user_id=request_owner_user_id(handler))
    handler._send_json({"removed": removed_count, "matters": []})
