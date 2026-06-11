from __future__ import annotations

from typing import Any


PDF_SOURCE_REDLINED_DOCX_UNAVAILABLE_MESSAGE = (
    "PDF matters cannot be exported as redlined Word documents because the original PDF is preserved as the "
    "visual source and only text is extracted for clause review. Upload a DOCX source for tracked-change Word "
    "export, or review the preserved PDF in Original view."
)


def source_filename_is_pdf(filename: object) -> bool:
    return isinstance(filename, str) and filename.lower().endswith(".pdf")


def matter_source_is_pdf(matter: dict[str, Any]) -> bool:
    if source_filename_is_pdf(matter.get("source_filename")):
        return True
    if str(matter.get("source_type") or "").lower() == "pdf":
        return True
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        source = review_result.get("source")
        if isinstance(source, dict) and str(source.get("type") or "").lower() == "pdf":
            return True
    return False
