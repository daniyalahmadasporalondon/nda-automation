from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .docx_export import build_source_redline_docx
from .docx_health import validate_docx_open_health, verify_export_content_coverage


@dataclass(frozen=True)
class DocxPackageRenderResult:
    data: bytes
    health_errors: list[str]
    content_errors: list[str]

    @property
    def valid(self) -> bool:
        return not self.health_errors and not self.content_errors


def render_source_redline_package(
    source_docx: bytes,
    review_result: dict[str, Any],
    *,
    clean_fills: object = None,
    expected_source_text: str = "",
    expected_redline_edits: object = None,
) -> DocxPackageRenderResult:
    """Render and validate a source-redline DOCX package.

    This facade owns the package-level contract for source DOCX exports: callers
    provide the already-normalized review result/redline payload and receive the
    final bytes plus Word-open/content validation metadata. The lower-level
    ``docx_export`` module still owns XML construction details.
    """
    data = build_source_redline_docx(source_docx, review_result, clean_fills=clean_fills)
    health_errors = validate_docx_open_health(data, require_styles=False)
    content_errors = (
        []
        if health_errors
        else verify_export_content_coverage(
            data,
            expected_source_text,
            expected_redline_edits=expected_redline_edits,
            clean_fills=clean_fills,
        )
    )
    return DocxPackageRenderResult(
        data=data,
        health_errors=health_errors,
        content_errors=content_errors,
    )
