from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .docx_export import SourceRedlinePackage
from .docx_health import validate_docx_open_health, verify_export_content_coverage
from .source_redline_docx import build_source_redline_package


def build_source_redline_docx(
    source_docx: bytes,
    review_result: dict[str, Any],
    *,
    clean_fills: object = None,
    strict: bool = True,
) -> SourceRedlinePackage:
    """Module-level seam the renderer calls (and tests patch) to build the package.

    Returns a :class:`SourceRedlinePackage` (bytes + any PDF redlines that could not
    be anchored). Tests historically patch THIS name with a spy that returns raw
    bytes; ``render_source_redline_package`` normalizes either shape, so such spies
    keep working (an empty unplaceable list) without change.
    """
    return build_source_redline_package(
        source_docx, review_result, clean_fills=clean_fills, strict=strict
    )


def _as_source_redline_package(rendered: object) -> SourceRedlinePackage:
    if isinstance(rendered, SourceRedlinePackage):
        return rendered
    # A patched/legacy builder returned raw bytes: there is no anchor classification
    # to carry, so treat it as a fully-placed package.
    return SourceRedlinePackage(data=rendered, anchor_uncertain_redlines=[])


@dataclass(frozen=True)
class DocxPackageRenderResult:
    data: bytes
    health_errors: list[str]
    content_errors: list[str]
    # PDF-source redlines that could not be confidently anchored. Always empty in
    # strict (fail-closed) mode -- strict raises before reaching here. Populated only
    # in lenient mode (preview/draft/diagnostic), where the package is still produced
    # but is an INCOMPLETE redline and must be labelled as such by the caller.
    anchor_uncertain_redlines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.health_errors and not self.content_errors

    @property
    def anchor_incomplete(self) -> bool:
        return bool(self.anchor_uncertain_redlines)


def render_source_redline_package(
    source_docx: bytes,
    review_result: dict[str, Any],
    *,
    clean_fills: object = None,
    expected_source_text: str = "",
    expected_redline_edits: object = None,
    strict: bool = True,
) -> DocxPackageRenderResult:
    """Render and validate a source-redline DOCX package.

    This facade owns the package-level contract for source DOCX exports: callers
    provide the already-normalized review result/redline payload and receive the
    final bytes plus Word-open/content validation metadata. The lower-level
    ``docx_export`` module still owns XML construction details.

    ``strict`` (fail-closed, default) makes the build RAISE ``PdfRedlineAnchorError``
    when a PDF-source redline cannot be confidently anchored into the reconstructed
    body, so send/approve/export never ship an incomplete redline. ``strict=False``
    (preview/draft/diagnostic) still renders the package but reports the unplaceable
    PDF redlines via ``anchor_uncertain_redlines`` so the caller can label the file
    incomplete.
    """
    package = _as_source_redline_package(
        build_source_redline_docx(
            source_docx, review_result, clean_fills=clean_fills, strict=strict
        )
    )
    data = package.data
    health_errors = validate_docx_open_health(data, require_styles=False)
    content_errors = (
        []
        if health_errors
        else verify_export_content_coverage(
            data,
            expected_source_text,
            expected_redline_edits=expected_redline_edits,
            clean_fills=clean_fills,
            source_docx=source_docx,
        )
    )
    return DocxPackageRenderResult(
        data=data,
        health_errors=health_errors,
        content_errors=content_errors,
        anchor_uncertain_redlines=list(package.anchor_uncertain_redlines),
    )
