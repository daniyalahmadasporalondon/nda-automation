from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from . import docx_package_renderer, export_service, fill_export, pdf_docx_reconstruction, telemetry
from .checker import review_nda
from .document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from .docx_export import (
    DocxExportError,
    PdfRedlineAnchorError,
    accept_all_revisions,
    build_review_report_docx,
)
from .docx_health import validate_docx_open_health, verify_export_content_coverage
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_repository import DiskMatterRepository, MatterRepository
from .review_staleness import review_result_staleness, stale_review_message
from .source_document_policy import (
    source_filename_is_pdf,
)

VERIFIED_EXPORT_HEADER = "word-package; track-revisions"


@dataclass(frozen=True)
class RedlineExport:
    data: bytes
    filename: str
    saved_path: Path | None = None
    headers: dict[str, str] | None = None


class DocxOpenHealthError(DocxExportError):
    def __init__(self, message: str, details: list[str]):
        super().__init__(message)
        self.details = details


class MatterSourceTextChangedError(DocxExportError):
    """Raised when a matter source edit would not be represented in the source DOCX export."""


# The EXACT user-facing message for the fail-closed PDF-anchor case (parameterized
# by the count N of redlines that could not be confidently placed). Kept verbatim so
# the UI/tests can rely on it.
PDF_REDLINE_ANCHOR_BLOCKED_MESSAGE = (
    "Couldn't confidently place {n} proposed changes in the reconstructed Word document. "
    "Export blocked to avoid sending an incomplete redline."
)


def pdf_redline_anchor_blocked_message(count: int) -> str:
    return PDF_REDLINE_ANCHOR_BLOCKED_MESSAGE.format(n=count)


class PdfSourceRedlineUnavailableError(DocxExportError):
    """Raised when a PDF-source matter cannot produce a complete tracked-change Word
    export -- either because the PDF-to-Word reconstruction engine is unavailable, or
    (the fail-closed anchor case) because one or more accepted redlines could not be
    confidently placed in the reconstructed body. In both cases the recovery path is
    the source-PDF marked-up annotation export (``annotated_pdf_export`` /
    ``routes/pdf_markup``), surfaced in the payload so the UI can offer it."""

    def __init__(
        self,
        message: str,
        *,
        source_filename: str = "",
        reason: str = "reconstruction_unavailable",
        unplaceable_redline_count: int = 0,
    ):
        super().__init__(message)
        self.status = 503
        self.reason = reason
        self.unplaceable_redline_count = unplaceable_redline_count
        self.payload = {
            "error": message,
            "reason": reason,
            "pdf_docx_reconstruction": {
                "status": "unavailable",
                "filename": pdf_docx_reconstruction.reconstructed_docx_filename(source_filename),
                "converter": pdf_docx_reconstruction.converter_health(),
                "fidelity": pdf_docx_reconstruction.reconstruction_fidelity_payload(output_format="reviewed_docx"),
            },
            # Recovery path for both reasons: mark up the preserved source PDF directly
            # rather than the reconstructed Word doc, so no accepted change is lost.
            "recovery": {
                "path": "annotated_pdf",
                "endpoint": "/api/matters/{matter_id}/annotated-pdf",
                "message": "Download the source PDF with the proposed changes marked up as annotations.",
            },
        }
        if unplaceable_redline_count:
            self.payload["unplaceable_redline_count"] = unplaceable_redline_count

    @classmethod
    def for_unplaceable_anchors(
        cls, count: int, *, source_filename: str = ""
    ) -> "PdfSourceRedlineUnavailableError":
        return cls(
            pdf_redline_anchor_blocked_message(count),
            source_filename=source_filename,
            reason="redline_anchor_uncertain",
            unplaceable_redline_count=count,
        )


class MatterNotFoundError(DocxExportError):
    pass


class StaleMatterReviewError(DocxExportError):
    def __init__(self, summary: dict):
        reasons = summary.get("stale_reasons")
        self.reasons = [str(reason) for reason in reasons] if isinstance(reasons, list) else []
        self.summary = summary
        super().__init__(stale_review_message(self.reasons))


def build_review_export(
    payload: dict, fallback_text: str, *, title: str = "NDA Review", repository: MatterRepository | None = None
) -> RedlineExport:
    return _build_redline_export(
        payload, fallback_text, title=title, persist=True, repository=repository or DiskMatterRepository()
    )


def build_matter_redline(
    matter_id: str,
    payload: dict | None = None,
    *,
    persist: bool = False,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> RedlineExport:
    payload = {**(payload or {}), "matter_id": matter_id}
    title = str(payload.get("title") or "NDA Review")
    return _build_redline_export(
        payload,
        "",
        title=title,
        persist=persist,
        repository=repository or DiskMatterRepository(),
        owner_user_id=owner_user_id,
    )


def _build_redline_export(
    payload: dict,
    fallback_text: str,
    *,
    title: str,
    persist: bool,
    repository: MatterRepository,
    owner_user_id: str = "",
) -> RedlineExport:
    review_result, source_document_bytes, source_filename = _review_result_for_export(
        payload, fallback_text, repository=repository, owner_user_id=owner_user_id
    )
    export_service.apply_selected_export_redlines(review_result, payload.get("export_redline_edits"))
    export_service.apply_manual_export_redlines(review_result, payload.get("manual_redline_edits"))
    export_service.apply_review_comments(review_result, payload.get("review_comments"))

    # Inbound-NDA fills (optional; fully backward-compatible when absent/empty).
    # Tracked fills become replace-paragraph redlines so they render as Word
    # tracked changes; clean fills are baked into the base DOCX text below, BEFORE
    # the tracked redlines, so they read as plain filled values.
    cleaned_fills = fill_export.clean_fills(payload.get("fills"))
    clean_mode_fills, tracked_mode_fills = fill_export.split_fills_by_mode(cleaned_fills)
    fill_export.merge_fill_redlines(
        review_result,
        fill_export.synthesize_tracked_fill_redlines(tracked_mode_fills, review_result),
    )

    if source_document_bytes is not None and source_filename_is_pdf(source_filename):
        try:
            reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(source_document_bytes, source_filename)
        except pdf_docx_reconstruction.PdfDocxReconstructionUnavailableError as exc:
            raise PdfSourceRedlineUnavailableError(
                pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE,
                source_filename=source_filename,
            ) from exc
        except pdf_docx_reconstruction.PdfDocxReconstructionFailedError as exc:
            raise DocxExportError(str(exc)) from exc
        try:
            package_result = docx_package_renderer.render_source_redline_package(
                reconstructed.data,
                review_result,
                clean_fills=clean_mode_fills,
                # PDF text extraction and layout reconstruction are different engines,
                # so their accepted-text sequence is not stable enough for the DOCX
                # source coverage gate. Redline anchoring fails closed below (strict
                # mode, the default): every accepted change is text-anchored into the
                # reconstructed body, and if any cannot be confidently placed the
                # renderer RAISES rather than silently dropping it (the prior P0 bug).
                expected_source_text="",
                expected_redline_edits=[],
            )
        except PdfRedlineAnchorError as exc:
            # Fail closed: one or more accepted redlines could not be confidently
            # placed in the reconstructed Word body. Block the export (do not produce
            # an incomplete redline) and point the UI at the marked-up source-PDF
            # annotation recovery path.
            telemetry.increment("pdf_redline_anchor_blocked")
            raise PdfSourceRedlineUnavailableError.for_unplaceable_anchors(
                exc.count,
                source_filename=source_filename,
            ) from exc
        _raise_for_package_result(package_result)
        download_filename = reconstructed.filename.replace(".docx", "-reviewed.docx")
        report_bytes = package_result.data
        if bool(payload.get("clean")):
            report_bytes = accept_all_revisions(report_bytes)
        return RedlineExport(
            data=report_bytes,
            filename=download_filename,
            saved_path=export_service.persist_export(report_bytes, download_filename) if (persist and not bool(payload.get("clean"))) else None,
            headers=reconstructed.headers,
        )
    if source_document_bytes is not None and source_filename.lower().endswith(".docx"):
        package_result = docx_package_renderer.render_source_redline_package(
            source_document_bytes,
            review_result,
            clean_fills=clean_mode_fills,
            expected_source_text=str(review_result.get("extracted_text") or ""),
            expected_redline_edits=review_result.get("redline_edits", []),
        )
        _raise_for_package_result(package_result)
        report_bytes = package_result.data
        download_filename = export_service.redline_download_filename(source_filename)
    else:
        report_bytes = build_review_report_docx(review_result, title=title.strip() or "NDA Review")
        download_filename = export_service.redline_download_filename(source_filename) if source_filename else "nda-review-report.docx"
        _validate_export(report_bytes, require_styles=True)
    # Clean mode (the Generator's outbound draft): the tracked redline is built and
    # validated above, then ACCEPTED into a clean document -- the recipient gets a
    # finished NDA with the edits baked in, no strike-through / insertion marks. A
    # clean export is ephemeral (a download), so it is never persisted as the
    # matter's redline artifact.
    clean = bool(payload.get("clean"))
    if clean:
        report_bytes = accept_all_revisions(report_bytes)
    return RedlineExport(
        data=report_bytes,
        filename=download_filename,
        saved_path=export_service.persist_export(report_bytes, download_filename) if (persist and not clean) else None,
    )


def _review_result_for_export(
    payload: dict, fallback_text: str, *, repository: MatterRepository, owner_user_id: str = ""
) -> tuple[dict, bytes | None, str]:
    matter_id = payload.get("matter_id")
    if isinstance(matter_id, str) and matter_id.strip():
        matter = repository.get_matter(matter_id.strip(), owner_user_id=owner_user_id)
        if matter is None:
            raise MatterNotFoundError("Matter not found.")
        review_result = matter.get("review_result")
        if not isinstance(review_result, dict):
            raise DocxExtractionError("Matter does not have a stored review result.")
        staleness = review_result_staleness(review_result)
        if staleness["stale"]:
            raise StaleMatterReviewError(staleness)
        source_document_bytes = repository.get_source_document_bytes(matter)
        source_filename = str(matter.get("source_filename") or "")
        if source_document_bytes is None:
            raise DocxExtractionError("Matter source document is missing from storage.")
        _apply_saved_redline_draft(payload, matter)
        submitted_text = _submitted_matter_source_text(payload)
        if _matter_source_text_changed(submitted_text, matter, review_result):
            if not _has_manual_redline_payload(payload):
                raise MatterSourceTextChangedError(
                    "Matter source text was edited after the source document was ingested. "
                    "Export or send after those viewer edits are represented as manual redlines."
                )
            review_result = review_nda(submitted_text)
            review_result["extracted_text"] = submitted_text
        else:
            review_result = deepcopy(review_result)
        return review_result, source_document_bytes, source_filename

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
        review_result = review_nda(extracted_text, paragraphs=extracted_paragraphs)
        # Record the source text so the export coverage gate runs its strong
        # sequence check on this direct-upload path. Without it the gate's
        # expected_source_text is empty and the sequence check is skipped, so a
        # corrupted source redline would ship silently.
        review_result["extracted_text"] = extracted_text
        return review_result, document_bytes, filename

    return review_nda(fallback_text), None, ""


def _apply_saved_redline_draft(payload: dict, matter: dict) -> None:
    draft = matter.get("redline_draft")
    if not isinstance(draft, dict):
        return
    for field in ["export_redline_edits", "manual_redline_edits", "review_comments"]:
        if field not in payload and field in draft:
            payload[field] = draft[field]


def _matter_source_text_changed(submitted_text: str, matter: dict, review_result: dict) -> bool:
    if not submitted_text:
        return False

    stored_text = str(matter.get("extracted_text") or review_result.get("extracted_text") or "")
    return _normalize_document_text(submitted_text) != _normalize_document_text(stored_text)


def _submitted_matter_source_text(payload: dict) -> str:
    for key in ("text", "reviewed_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _has_manual_redline_payload(payload: dict) -> bool:
    manual_redlines = payload.get("manual_redline_edits")
    return isinstance(manual_redlines, list) and any(
        export_service.clean_manual_export_redline(item) is not None
        for item in manual_redlines
    )


def _normalize_document_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _validate_export(
    report_bytes: bytes,
    *,
    require_styles: bool,
    expected_source_text: str = "",
    expected_redline_edits: object = None,
) -> None:
    health_errors = validate_docx_open_health(report_bytes, require_styles=require_styles)
    if health_errors:
        telemetry.increment("docx_export_health_failures")
        print(f"DOCX export health check failed: {len(health_errors)} issue(s)")
        raise DocxOpenHealthError("The exported Word document failed its open-health check.", health_errors)
    content_errors = verify_export_content_coverage(
        report_bytes,
        expected_source_text,
        expected_redline_edits=expected_redline_edits,
    )
    if content_errors:
        telemetry.increment("docx_export_content_failures")
        print(f"DOCX export content check failed: {len(content_errors)} issue(s)")
        raise DocxOpenHealthError("The exported Word document failed its content-coverage check.", content_errors)


def _raise_for_package_result(package_result: docx_package_renderer.DocxPackageRenderResult) -> None:
    if package_result.health_errors:
        telemetry.increment("docx_export_health_failures")
        print(f"DOCX export health check failed: {len(package_result.health_errors)} issue(s)")
        raise DocxOpenHealthError(
            "The exported Word document failed its open-health check.",
            package_result.health_errors,
        )
    if package_result.content_errors:
        telemetry.increment("docx_export_content_failures")
        print(f"DOCX export content check failed: {len(package_result.content_errors)} issue(s)")
        raise DocxOpenHealthError(
            "The exported Word document failed its content-coverage check.",
            package_result.content_errors,
        )
