from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from . import export_service, matter_store
from .checker import review_nda
from .document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from .docx_export import (
    DocxExportError,
    build_review_report_docx,
    build_source_redline_docx,
    validate_docx_open_health,
)
from .docx_text import DocxExtractionError, extract_docx_paragraphs

VERIFIED_EXPORT_HEADER = "word-package; track-revisions"


@dataclass(frozen=True)
class RedlineExport:
    data: bytes
    filename: str
    saved_path: Path | None = None


class DocxOpenHealthError(DocxExportError):
    def __init__(self, message: str, details: list[str]):
        super().__init__(message)
        self.details = details


def build_review_export(payload: dict, fallback_text: str, *, title: str = "NDA Review") -> RedlineExport:
    review_result, source_document_bytes, source_filename = _review_result_for_export(payload, fallback_text)
    export_service.apply_selected_export_redlines(review_result, payload.get("export_redline_edits"))
    export_service.apply_manual_export_redlines(review_result, payload.get("manual_redline_edits"))

    if source_document_bytes is not None:
        report_bytes = build_source_redline_docx(source_document_bytes, review_result)
        download_filename = export_service.redline_download_filename(source_filename)
        require_styles = False
    else:
        report_bytes = build_review_report_docx(review_result, title=title.strip() or "NDA Review")
        download_filename = "nda-review-report.docx"
        require_styles = True

    _validate_export(report_bytes, require_styles=require_styles)
    return RedlineExport(
        data=report_bytes,
        filename=download_filename,
        saved_path=export_service.persist_export(report_bytes, download_filename),
    )


def build_matter_redline(matter_id: str, payload: dict | None = None) -> RedlineExport:
    payload = payload or {}
    review_result, source_document_bytes, source_filename = _review_result_for_export({"matter_id": matter_id}, "")
    if source_document_bytes is None:
        raise DocxExtractionError("Matter source document is missing from storage.")

    export_service.apply_selected_export_redlines(review_result, payload.get("export_redline_edits"))
    export_service.apply_manual_export_redlines(review_result, payload.get("manual_redline_edits"))
    report_bytes = build_source_redline_docx(source_document_bytes, review_result)
    _validate_export(report_bytes, require_styles=False)
    return RedlineExport(
        data=report_bytes,
        filename=export_service.redline_download_filename(source_filename),
    )


def _review_result_for_export(payload: dict, fallback_text: str) -> tuple[dict, bytes | None, str]:
    matter_id = payload.get("matter_id")
    if isinstance(matter_id, str) and matter_id.strip():
        matter = matter_store.get_matter(matter_id.strip())
        if matter is None:
            raise DocxExtractionError("Matter not found.")
        review_result = matter.get("review_result")
        if not isinstance(review_result, dict):
            raise DocxExtractionError("Matter does not have a stored review result.")
        source_document_bytes = matter_store.get_source_document_bytes(matter)
        source_filename = str(matter.get("source_filename") or "")
        if source_document_bytes is None:
            raise DocxExtractionError("Matter source document is missing from storage.")
        return deepcopy(review_result), source_document_bytes, source_filename

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    if isinstance(filename, str) and filename.lower().endswith(".docx") and isinstance(content_base64, str) and content_base64:
        try:
            document_bytes = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DocxExtractionError("The uploaded Word document could not be decoded.") from exc

        try:
            ensure_document_size(document_bytes)
        except DocumentSizeError as exc:
            raise DocxExtractionError(DOCUMENT_TOO_LARGE_MESSAGE) from exc

        extracted_paragraphs = extract_docx_paragraphs(document_bytes)
        extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
        return review_nda(extracted_text, paragraphs=extracted_paragraphs), document_bytes, filename

    return review_nda(fallback_text), None, ""


def _validate_export(report_bytes: bytes, *, require_styles: bool) -> None:
    health_errors = validate_docx_open_health(report_bytes, require_styles=require_styles)
    if health_errors:
        print(f"DOCX export health check failed: {'; '.join(health_errors)}")
        raise DocxOpenHealthError("The exported Word document failed its open-health check.", health_errors)
