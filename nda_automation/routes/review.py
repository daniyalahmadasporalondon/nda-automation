from __future__ import annotations

import base64
import binascii
from urllib.parse import quote

from .. import redline_export_service, telemetry
from ..checker import (
    AIDraftValidationError,
    AISecondOpinionError,
    ParagraphAlignmentError,
    ai_second_opinion_for_clause,
    ai_validate_draft_fix,
    review_nda,
)
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..docx_export import DOCX_MIME, DocxExportError
from ..docx_text import DocxExtractionError
from ..ingestion_service import extract_document, is_supported_document_filename
from ..pdf_text import PdfExtractionError


def handle_text_review(handler, *, review_nda_func=review_nda) -> None:
    telemetry.increment("review_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip():
        handler._send_json({"error": "Provide NDA text to review."}, status=400)
        return

    handler._send_json(review_nda_func(text))


def handle_document_review(handler, *, extract_document_func=extract_document, review_nda_func=review_nda) -> None:
    telemetry.increment("document_review_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    if not is_supported_document_filename(filename):
        handler._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Provide a document to review."}, status=400)
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
        source_type, extracted_paragraphs, extraction_quality = extract_document_func(filename, document_bytes)
    except (DocxExtractionError, PdfExtractionError, ValueError) as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
    try:
        result = review_nda_func(extracted_text, paragraphs=extracted_paragraphs)
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return
    result["source"] = {
        "filename": filename,
        "type": source_type,
        "extracted_characters": len(extracted_text),
        "extracted_paragraphs": len(extracted_paragraphs),
    }
    if extraction_quality:
        result["source"]["extraction_quality"] = extraction_quality
        warnings = extraction_quality.get("warnings")
        if isinstance(warnings, list) and warnings:
            result.setdefault("review_warnings", []).extend(warnings)
    result["extracted_text"] = extracted_text
    handler._send_json(result)


def handle_ai_second_opinion(handler, *, second_opinion_func=ai_second_opinion_for_clause) -> None:
    telemetry.increment("ai_second_opinion_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    clause_id = payload.get("clause_id", "")
    if not isinstance(clause_id, str) or not clause_id.strip():
        handler._send_json({"error": "Provide a clause id for AI second opinion."}, status=400)
        return
    review_result = payload.get("review_result")
    if not isinstance(review_result, dict):
        handler._send_json({"error": "Provide the current review result for AI second opinion."}, status=400)
        return

    try:
        result = second_opinion_func(review_result, clause_id.strip())
    except AISecondOpinionError as error:
        handler._send_json({"error": str(error)}, status=error.status)
        return

    handler._send_json(result)


def handle_ai_draft_validation(handler, *, validation_func=ai_validate_draft_fix) -> None:
    telemetry.increment("ai_draft_validation_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    clause_id = payload.get("clause_id", "")
    if not isinstance(clause_id, str) or not clause_id.strip():
        handler._send_json({"error": "Provide a clause id for AI draft validation."}, status=400)
        return
    review_result = payload.get("review_result")
    if not isinstance(review_result, dict):
        handler._send_json({"error": "Provide the current review result for AI draft validation."}, status=400)
        return
    redline_edit = payload.get("redline_edit")
    if not isinstance(redline_edit, dict):
        handler._send_json({"error": "Provide a redline draft to validate."}, status=400)
        return

    try:
        result = validation_func(review_result, clause_id.strip(), redline_edit)
    except AIDraftValidationError as error:
        handler._send_json({"error": str(error)}, status=error.status)
        return

    handler._send_json(result)


def handle_review_docx_export(handler) -> None:
    telemetry.increment("review_docx_export_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    text = payload.get("text", "")
    reviewed_text = payload.get("reviewed_text", "")
    has_docx_payload = (
        isinstance(payload.get("filename"), str)
        and payload.get("filename", "").lower().endswith(".docx")
        and isinstance(payload.get("content_base64"), str)
        and bool(payload.get("content_base64"))
    )
    export_text = reviewed_text if isinstance(reviewed_text, str) and reviewed_text.strip() else text
    has_matter_payload = isinstance(payload.get("matter_id"), str) and bool(payload.get("matter_id", "").strip())
    uses_uploaded_docx_export = has_docx_payload and not has_matter_payload
    if (not isinstance(export_text, str) or not export_text.strip()) and not has_docx_payload and not has_matter_payload:
        handler._send_json({"error": "Provide NDA text to export."}, status=400)
        return
    if (
        not uses_uploaded_docx_export
        and isinstance(text, str)
        and text.strip()
        and isinstance(reviewed_text, str)
        and reviewed_text.strip()
        and text.strip() != reviewed_text.strip()
    ):
        handler._send_json({"error": "Export text must match the latest reviewed text. Run Review NDA again."}, status=409)
        return

    title = payload.get("title", "NDA Review")
    if not isinstance(title, str) or not title.strip():
        title = "NDA Review"

    try:
        if has_matter_payload:
            redline_export = redline_export_service.build_matter_redline(
                str(payload.get("matter_id", "")).strip(),
                payload,
                persist=True,
            )
        else:
            redline_export = redline_export_service.build_review_export(payload, export_text, title=title)
    except redline_export_service.DocxOpenHealthError as error:
        handler._send_json({
            "error": str(error),
            "details": error.details,
        }, status=500)
        return
    except redline_export_service.MatterSourceTextChangedError as error:
        handler._send_json({"error": str(error)}, status=409)
        return
    except redline_export_service.MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404)
        return
    except DocxExtractionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except PdfExtractionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return
    except DocxExportError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    headers = {"X-Export-Verified": redline_export_service.VERIFIED_EXPORT_HEADER}
    if redline_export.saved_path is not None:
        headers.update({
            "X-Export-URL": f"/exports/{quote(redline_export.saved_path.name)}",
        })
    handler._send_download(
        redline_export.data,
        redline_export.filename,
        DOCX_MIME,
        headers=headers,
    )
