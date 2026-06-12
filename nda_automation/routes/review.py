from __future__ import annotations

import base64
import binascii
from urllib.parse import quote

from .. import annotated_pdf_export, redline_export_service, telemetry
from ..ai_first_review import ReassessClauseError, reassess_single_clause
from ..checker import (
    AIDraftValidationError,
    AISecondOpinionError,
    ParagraphAlignmentError,
    ai_second_opinion_for_clause,
    ai_validate_draft_fix,
    review_nda,
)
from ..document_limits import (
    DocumentSizeError,
    DOCUMENT_TOO_LARGE_MESSAGE,
    ReviewTextTooLargeError,
    ensure_document_size,
    ensure_review_text_size,
)
from ..docx_export import DOCX_MIME, DocxExportError
from ..docx_text import DocxExtractionError
from ..ingestion_service import extract_document, is_supported_document_filename
from ..matter_repository import DiskMatterRepository, MatterRepository
from ..pdf_text import PdfExtractionError
from ..review_engine import ActiveReviewEngineError
from ..review_result_contract import attach_document_source, extracted_text_from_paragraphs
from .common import request_owner_user_id


def handle_text_review(handler, *, review_nda_func=review_nda) -> None:
    telemetry.increment("review_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip():
        handler._send_json({"error": "Provide NDA text to review."}, status=400)
        return
    try:
        ensure_review_text_size(text)
    except ReviewTextTooLargeError as error:
        handler._send_json({"error": str(error)}, status=413)
        return

    try:
        result = review_nda_func(text)
    except ActiveReviewEngineError as error:
        handler._send_json({"error": str(error)}, status=502)
        return

    handler._send_json(result)


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

    extracted_text = extracted_text_from_paragraphs(extracted_paragraphs)
    try:
        result = review_nda_func(extracted_text, paragraphs=extracted_paragraphs)
    except ActiveReviewEngineError as error:
        handler._send_json({"error": str(error)}, status=502)
        return
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return
    attach_document_source(
        result,
        filename=filename,
        document_type=source_type,
        extracted_paragraphs=extracted_paragraphs,
        extracted_text=extracted_text,
        extraction_quality=extraction_quality,
    )
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
        handler._send_json({"error": "Export text must match the latest reviewed text. Reload the matter review before exporting."}, status=409)
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
                owner_user_id=request_owner_user_id(handler),
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
    except redline_export_service.StaleMatterReviewError as error:
        handler._send_json({
            "error": str(error),
            "stale_reasons": error.reasons,
            "review_refresh": error.summary,
        }, status=409)
        return
    except redline_export_service.MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404)
        return
    except redline_export_service.PdfSourceRedlineUnavailableError as error:
        handler._send_json(error.payload, status=error.status)
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


def handle_annotated_pdf_export(handler) -> None:
    telemetry.increment("annotated_pdf_export_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    matter_id = payload.get("matter_id", "")
    if not isinstance(matter_id, str) or not matter_id.strip():
        handler._send_json({"error": "Provide a PDF matter id to export."}, status=400)
        return

    try:
        annotated_export = annotated_pdf_export.build_matter_annotated_pdf(
            matter_id.strip(),
            owner_user_id=request_owner_user_id(handler),
        )
    except annotated_pdf_export.StaleAnnotatedPdfReviewError as error:
        handler._send_json({
            "error": str(error),
            "stale_reasons": error.reasons,
            "review_refresh": error.summary,
        }, status=409)
        return
    except annotated_pdf_export.AnnotatedPdfMatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404)
        return
    except annotated_pdf_export.AnnotatedPdfUnsupportedSourceError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except annotated_pdf_export.AnnotatedPdfDependencyError as error:
        handler._send_json({"error": str(error)}, status=500)
        return
    except annotated_pdf_export.AnnotatedPdfExportError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    handler._send_download(
        annotated_export.data,
        annotated_export.filename,
        annotated_pdf_export.ANNOTATED_PDF_MIME,
        headers={
            "X-Export-Verified": annotated_pdf_export.ANNOTATED_PDF_VERIFICATION_HEADER,
            "X-PDF-Annotation-Count": str(annotated_export.annotation_count),
            "X-PDF-Unmatched-Evidence-Count": str(annotated_export.unmatched_evidence_count),
        },
    )


def _review_repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def handle_reassess_clause(handler, *, reassess_func=reassess_single_clause) -> None:
    """POST /api/review/reassess-clause — re-run the AI-first assessment for one clause.

    Request body (JSON):
        matter_id        str   required  — owner-scoped matter to reassess
        clause_id        str   required  — playbook clause id to re-assess
        edited_text      str   optional  — replacement full-document text (mutually exclusive with edited_paragraphs)
        edited_paragraphs list optional  — paragraph objects with updated "text" fields to overlay

    Response (200 JSON):
        clause           dict  — updated ClauseResult for the clause
        matter_id        str
        clause_id        str
        reassess_metadata dict — {clause_id, feature, has_edited_paragraphs, ai_verifier_ran}
    """
    telemetry.increment("reassess_clause_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    matter_id = str(payload.get("matter_id") or "").strip()
    if not matter_id:
        handler._send_json({"error": "Provide a matter_id to reassess."}, status=400)
        return

    clause_id = str(payload.get("clause_id") or "").strip()
    if not clause_id:
        handler._send_json({"error": "Provide a clause_id to reassess."}, status=400)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _review_repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    source_text = str(matter.get("extracted_text") or "").strip()
    if not source_text:
        handler._send_json({"error": "Matter has no extracted text to reassess."}, status=409)
        return

    # edited_text overrides the full source text (the frontend may pass the edited document).
    edited_text = payload.get("edited_text")
    if isinstance(edited_text, str) and edited_text.strip():
        source_text = edited_text.strip()

    edited_paragraphs = payload.get("edited_paragraphs")
    if not isinstance(edited_paragraphs, list):
        edited_paragraphs = None

    try:
        clause_result = reassess_func(
            clause_id,
            source_text,
            edited_paragraphs=edited_paragraphs,
        )
    except ReassessClauseError as error:
        handler._send_json({"error": str(error)}, status=error.status)
        return
    except ActiveReviewEngineError as error:
        handler._send_json({"error": str(error)}, status=502)
        return

    telemetry.increment("reassess_clause_completed")
    handler._send_json({
        "clause": clause_result,
        "matter_id": matter_id,
        "clause_id": clause_id,
        "reassess_metadata": clause_result.get("reassess_metadata") or {},
    })
