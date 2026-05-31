from __future__ import annotations

from typing import Any

from .checker import ParagraphAlignmentError, review_nda
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_store import create_matter
from .triage import triage_review_result


def create_matter_from_docx(
    *,
    filename: str,
    document_bytes: bytes,
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extracted_paragraphs = extract_docx_paragraphs(document_bytes)
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
    review_result = review_nda(extracted_text, paragraphs=extracted_paragraphs)
    review_result["source"] = {
        "filename": filename,
        "type": "docx",
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


__all__ = ["DocxExtractionError", "ParagraphAlignmentError", "create_matter_from_docx"]
