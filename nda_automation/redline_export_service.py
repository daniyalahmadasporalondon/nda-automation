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
from .docx_health import (
    validate_docx_open_health,
    verify_export_content_coverage,
    verify_pdf_reconstruction_redline_coverage,
)
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_repository import DiskMatterRepository, MatterRepository
from .review_staleness import review_result_staleness, stale_review_message
from .source_document_policy import (
    source_filename_is_pdf,
)

VERIFIED_EXPORT_HEADER = "word-package; track-revisions"

# Honest "verified" value for the zero-change case: when a PDF-source matter has NO
# accepted redlines (and no clean fills), the faithful reviewed output IS the original
# document, served unchanged. There is no lossy reconstruction to content-check, so we
# must NOT claim a reconstruction was verified -- we mark it as the original instead.
ORIGINAL_UNCHANGED_EXPORT_HEADER = "original; no-redlines-applied"

# Response header set on a no-redline original export so the route serves it with the
# original content type / honest verified value rather than the reconstruction stamp.
ORIGINAL_EXPORT_MARKER_HEADER = "X-Export-Original"

PDF_CONTENT_TYPE = "application/pdf"


@dataclass(frozen=True)
class RedlineExport:
    data: bytes
    filename: str
    saved_path: Path | None = None
    headers: dict[str, str] | None = None
    content_type: str | None = None


# Generic, leak-free copy shown to users when the reviewed Word document fails its
# open-health / coverage integrity check. The raw ``details`` (OOXML internals such
# as "document.xml is missing w:body") are logged server-side but MUST NOT reach the
# client response body.
DOCX_HEALTH_CLIENT_MESSAGE = (
    "The reviewed Word document failed an integrity check and was not produced. "
    "Please contact support."
)


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
        # Zero-change short circuit: with NO accepted redlines and NO clean fills there
        # is nothing to apply, so the faithful reviewed output IS the original PDF. The
        # lossy pdf2docx reconstruction (which can differ from the original text) would
        # produce a changed-looking document for a no-change matter AND -- because the
        # post-render coverage gate short-circuits to "pass" when there are no redlines
        # -- get stamped "verified" with no content check at all. Serve the original PDF
        # bytes unchanged instead, marked honestly as the original (NOT a verified
        # reconstruction).
        if not _has_pending_export_changes(review_result, clean_mode_fills):
            return _original_pdf_export(
                source_document_bytes, source_filename, persist=persist, clean=bool(payload.get("clean"))
            )
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
        # Post-render coverage gate adapted to the reconstruction. The strong DOCX
        # sequence gate is off for PDF (the rebuilt body's paragraph/whitespace model
        # differs from the PDF extractor, so a positional sequence match would
        # false-positive). The anchor check above is fail-closed PRE-render; this
        # verifies POST-render that every reviewer redline actually landed in the
        # output bytes, so a dropped redline fails loudly instead of shipping silently.
        _raise_for_pdf_redline_coverage(package_result.data, review_result, source_filename)
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
            raise MatterNotFoundError("NDA not found.")
        review_result = matter.get("review_result")
        if not isinstance(review_result, dict):
            raise DocxExtractionError("NDA does not have a stored review result.")
        staleness = review_result_staleness(review_result)
        if staleness["stale"]:
            raise StaleMatterReviewError(staleness)
        source_document_bytes = repository.get_source_document_bytes(matter)
        source_filename = str(matter.get("source_filename") or "")
        if source_document_bytes is None:
            raise DocxExtractionError("NDA source document is missing from storage.")
        _apply_saved_redline_draft(payload, matter)
        submitted_text = _submitted_matter_source_text(payload)
        if _matter_source_text_changed(submitted_text, matter, review_result):
            if not _has_manual_redline_payload(payload):
                raise MatterSourceTextChangedError(
                    "NDA source text was edited after the source document was ingested. "
                    "Export or send after those viewer edits are represented as manual redlines."
                )
            review_result = review_nda(submitted_text)
            review_result["extracted_text"] = submitted_text
        else:
            review_result = deepcopy(review_result)
            # Deferred-review matters store the engine result verbatim, and neither
            # the deterministic checker nor the active-engine review sets
            # extracted_text -- only the (now test-only) eager create path's
            # attach_document_source does. So for every real matter (manual upload +
            # inbound both create defer_ai_review=True), review_result has NO
            # extracted_text here, the renderer below is handed expected_source_text=""
            # and verify_export_content_coverage short-circuits to [] -- the length /
            # accepted-paragraph-SEQUENCE / structural-count checks ALL skip, so a
            # redline that dropped/reordered/duplicated source clauses ships unverified.
            # The matter ALWAYS carries the authoritative source text (matter[
            # "extracted_text"], the SAME extracted_text_from_paragraphs value the
            # eager path stores on the review_result and the redline is built against),
            # so stamp it on so the coverage gate actually runs. setdefault never
            # overwrites the eager path's already-present value.
            review_result.setdefault("extracted_text", str(matter.get("extracted_text") or ""))
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
) -> None:
    health_errors = validate_docx_open_health(report_bytes, require_styles=require_styles)
    if health_errors:
        telemetry.increment("docx_export_health_failures")
        print(f"DOCX export health check failed: {len(health_errors)} issue(s)")
        raise DocxOpenHealthError("The exported Word document failed its open-health check.", health_errors)
    content_errors = verify_export_content_coverage(report_bytes, "")
    if content_errors:
        telemetry.increment("docx_export_content_failures")
        print(f"DOCX export content check failed: {len(content_errors)} issue(s)")
        raise DocxOpenHealthError("The exported Word document failed its content-coverage check.", content_errors)


def _has_pending_export_changes(review_result: dict, clean_mode_fills: object) -> bool:
    """True when this export actually changes the source document.

    A change is either an accepted/manual redline (the reconstruction-and-redline path)
    or a clean fill baked into the body. When BOTH are empty the export is a no-op and
    the original document can be served unchanged. ``redline_edits`` already reflects the
    reviewer's accepted selection at this point (selected/manual edits applied, tracked
    fills merged), so an empty list here means nothing was accepted."""
    redline_edits = review_result.get("redline_edits")
    has_redlines = isinstance(redline_edits, list) and any(
        isinstance(edit, dict) for edit in redline_edits
    )
    has_clean_fills = bool(clean_mode_fills)
    return has_redlines or has_clean_fills


def _original_pdf_export(
    source_document_bytes: bytes,
    source_filename: str,
    *,
    persist: bool,
    clean: bool,
) -> RedlineExport:
    """Serve the original PDF unchanged for a PDF-source matter with no changes to apply.

    Because nothing is applied, the original IS the faithful reviewed output. It is
    marked honestly via ``X-Export-Original`` so the route serves it with the PDF content
    type and an honest verified value -- it must NEVER be stamped as a verified
    reconstruction (no lossy pdf2docx rebuild ran, so there was no fidelity check)."""
    headers = {ORIGINAL_EXPORT_MARKER_HEADER: ORIGINAL_UNCHANGED_EXPORT_HEADER}
    filename = _original_pdf_download_filename(source_filename)
    return RedlineExport(
        data=source_document_bytes,
        filename=filename,
        # A no-change original is not the matter's redline artifact, so it is never
        # persisted as one (mirrors the clean-export rule). The clean flag is irrelevant
        # here -- there is nothing to accept -- but honored for symmetry.
        saved_path=None,
        headers=headers,
        content_type=PDF_CONTENT_TYPE,
    )


def _original_pdf_download_filename(source_filename: str) -> str:
    stem = Path(str(source_filename or "")).stem
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem)
    safe = safe.strip("-_") or "nda"
    return f"{safe}.pdf"


def _raise_for_pdf_redline_coverage(
    report_bytes: bytes, review_result: dict, source_filename: str
) -> None:
    """Block a PDF-source reviewed export whose reconstruction silently dropped a
    reviewer redline. Fails closed: a detected shortfall blocks the send/export and
    points the UI at the source-PDF marked-up annotation recovery path (the same path
    used when anchoring fails), so no incomplete redline ships."""
    coverage_errors = verify_pdf_reconstruction_redline_coverage(
        report_bytes,
        review_result.get("redline_edits", []),
    )
    if coverage_errors:
        telemetry.increment("pdf_redline_coverage_blocked")
        print(f"PDF redline coverage check failed: {len(coverage_errors)} issue(s)")
        raise PdfSourceRedlineUnavailableError(
            "Some reviewer changes were not represented in the reconstructed Word document. "
            "Export blocked to avoid sending an incomplete redline.",
            source_filename=source_filename,
            reason="redline_coverage_shortfall",
        )


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
