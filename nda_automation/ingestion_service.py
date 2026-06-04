from __future__ import annotations

from typing import Any

from .checker import ParagraphAlignmentError
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_engine import review_nda_with_active_engine
from .triage import triage_review_result


SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}


def create_matter_from_document(
    *,
    filename: str,
    document_bytes: bytes,
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
    dedupe_gmail: bool = False,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> dict[str, Any]:
    repository = repository or DiskMatterRepository()
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
    review_result = review_nda_with_active_engine(extracted_text, paragraphs=extracted_paragraphs)
    review_result["source"] = {
        "filename": filename,
        "type": document_type,
        "extracted_characters": len(extracted_text),
        "extracted_paragraphs": len(extracted_paragraphs),
    }
    if extraction_quality:
        review_result["source"]["extraction_quality"] = extraction_quality
        _append_extraction_warnings(review_result, extraction_quality)
    review_result["extracted_text"] = extracted_text
    return repository.create_matter(
        source_filename=filename,
        document_bytes=document_bytes,
        extracted_text=extracted_text,
        review_result=review_result,
        triage=triage_review_result(review_result),
        source_type=source_type,
        board_column=board_column,
        intake_metadata=intake_metadata,
        dedupe_gmail=dedupe_gmail,
        owner_user_id=owner_user_id,
    )


def extract_document_paragraphs(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    document_type, paragraphs, _quality = extract_document(filename, document_bytes)
    return document_type, paragraphs


def extract_document(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]], dict[str, object] | None]:
    lower_filename = filename.lower()
    if lower_filename.endswith(".docx"):
        return "docx", extract_docx_paragraphs(document_bytes), None
    if lower_filename.endswith(".pdf"):
        extraction = extract_pdf_document(document_bytes)
        return "pdf", extraction.paragraphs, extraction.quality
    raise ValueError("Upload a .docx Word document or text-based PDF.")


def is_supported_document_filename(filename: object) -> bool:
    if not isinstance(filename, str):
        return False
    return any(filename.lower().endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


def _append_extraction_warnings(review_result: dict[str, Any], extraction_quality: dict[str, object]) -> None:
    warnings = extraction_quality.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return
    review_warnings = review_result.setdefault("review_warnings", [])
    if isinstance(review_warnings, list):
        review_warnings.extend(warnings)


__all__ = [
    "DocxExtractionError",
    "ParagraphAlignmentError",
    "PdfExtractionError",
    "create_matter_from_document",
    "extract_document",
    "extract_document_paragraphs",
    "is_supported_document_filename",
]
