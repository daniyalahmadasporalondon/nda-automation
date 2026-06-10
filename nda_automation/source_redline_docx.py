from __future__ import annotations

from typing import Dict

from . import redline_edit_contract
from .docx_health import validate_docx_open_health

ReviewResult = Dict[str, object]


def build_source_redline_docx(
    source_docx: bytes,
    review_result: ReviewResult,
    *,
    clean_fills: object = None,
) -> bytes:
    """Render source-DOCX tracked changes from the normalized redline contract."""
    from .docx_export import DocxExportError, _build_source_redline_docx_package  # noqa: PLC0415

    normalized_review_result = _normalize_review_result_redlines(review_result)
    rendered = _build_source_redline_docx_package(
        source_docx,
        normalized_review_result,
        clean_fills=clean_fills,
    )
    health_errors = validate_docx_open_health(rendered)
    if health_errors:
        raise DocxExportError("The uploaded Word document redline failed validation: " + "; ".join(health_errors))
    return rendered


def _normalize_review_result_redlines(review_result: ReviewResult) -> ReviewResult:
    if not isinstance(review_result, dict):
        return {"redline_edits": []}
    review_result["redline_edits"] = redline_edit_contract.normalize_redline_edits(
        review_result.get("redline_edits", []),
        require_content=True,
    )
    return review_result
