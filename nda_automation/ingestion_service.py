from __future__ import annotations

from typing import Any

from .checker import ParagraphAlignmentError, review_nda
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_store import create_matter
from .pdf_text import PdfExtractionError, extract_pdf_paragraphs
from .triage import triage_review_result


SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}


def create_matter_from_document(
    *,
    filename: str,
    document_bytes: bytes,
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs = extract_document_paragraphs(filename, document_bytes)
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
    review_result = review_nda(extracted_text, paragraphs=extracted_paragraphs)
    review_result["source"] = {
        "filename": filename,
        "type": document_type,
        "extracted_characters": len(extracted_text),
        "extracted_paragraphs": len(extracted_paragraphs),
    }
    review_result["extracted_text"] = extracted_text
    return create_matter(
        source_filename=filename,
        document_bytes=document_bytes,
        extracted_text=extracted_text,
        review_result=review_result,
        triage=triage_review_result(review_result),
        source_type=source_type,
        board_column=board_column,
        intake_metadata=intake_metadata,
    )


def create_matter_from_docx(**kwargs: Any) -> dict[str, Any]:
    return create_matter_from_document(**kwargs)


def extract_document_paragraphs(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    lower_filename = filename.lower()
    if lower_filename.endswith(".docx"):
        return "docx", extract_docx_paragraphs(document_bytes)
    if lower_filename.endswith(".pdf"):
        return "pdf", extract_pdf_paragraphs(document_bytes)
    raise ValueError("Upload a .docx Word document or text-based PDF.")


def is_supported_document_filename(filename: object) -> bool:
    if not isinstance(filename, str):
        return False
    return any(filename.lower().endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


__all__ = [
    "DocxExtractionError",
    "ParagraphAlignmentError",
    "PdfExtractionError",
    "create_matter_from_docx",
    "create_matter_from_document",
    "extract_document_paragraphs",
    "is_supported_document_filename",
]
