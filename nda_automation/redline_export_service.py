from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from . import (
    artifact_registry,
    artifact_service,
    docx_package_renderer,
    export_service,
    fill_export,
    pdf_docx_reconstruction,
    telemetry,
)
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
from .review_result_contract import body_extracted_text_from_paragraphs
from .review_staleness import review_result_staleness, stale_review_message
from .source_document_policy import (
    source_filename_is_pdf,
)

# Review-result field carrying the BODY-ONLY extracted text (header/footer/footnote
# paragraphs excluded) used as the expected side of the native-DOCX content-coverage
# gate. Distinct from ``extracted_text`` (the canonical supplemental-INCLUSIVE source
# text every other consumer reads). Stamped transiently by ``_review_result_for_export``
# (it is never persisted on the matter -- it is re-derived per export from the source
# paragraphs/bytes) and read by ``_body_expected_source_text``. A matter MAY also carry a
# value under this key if a future ingest persists one; the resolver prefers it when
# present, otherwise re-extracts the body from the source bytes (retroactive, so this
# fixes already-ingested letterhead/footer matters with no migration).
BODY_EXTRACTED_TEXT_FIELD = "body_extracted_text"

VERIFIED_EXPORT_HEADER = "word-package; track-revisions"

# Response header + honest value set on the NO-MATTER export paths (direct DOCX upload
# and the bare-text fallback). Those paths have NO stored AI review to preserve, so the
# redline is produced by the bare deterministic checker (``review_nda``) -- it must NOT
# masquerade as the AI Review-tab result a reviewer approved. The marker lets the route /
# client label it honestly as a deterministic-only redline. The MATTER export path never
# sets this: it always carries (and now always preserves) the stored AI review_result.
DETERMINISTIC_ONLY_EXPORT_MARKER_HEADER = "X-Export-Deterministic-Only"
DETERMINISTIC_ONLY_EXPORT_HEADER = "deterministic-checker; no-ai-review"

# Transient private marker key stamped on a review_result by ``_review_result_for_export``
# for the no-matter deterministic paths, read by ``_build_redline_export`` to set the
# ``DETERMINISTIC_ONLY_EXPORT_MARKER_HEADER`` on the produced ``RedlineExport``. Never
# persisted (the no-matter paths build a throwaway review_result per request) and stripped
# before the result reaches the renderer.
_DETERMINISTIC_ONLY_EXPORT_FLAG = "_deterministic_only_export"

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
    # ``kind`` distinguishes the two integrity failures that share this type:
    #   * "open_health"     -- the OOXML package itself is malformed (corrupt zip,
    #                          missing w:body, unreadable styles). A genuine fault.
    #   * "content_coverage" -- the package is well-formed but the redline
    #                          reconstruction dropped/reordered/duplicated source
    #                          paragraphs (the "33 vs 114" sequence mismatch). For a
    #                          PDF-source matter this is the EXPECTED imperfect-
    #                          reconstruction case, not a server fault -- the producer
    #                          boundary translates it to PdfSourceRedlineUnavailableError
    #                          (503 + annotated-PDF recovery) rather than a 500.
    def __init__(self, message: str, details: list[str], *, kind: str = "open_health"):
        super().__init__(message)
        self.details = details
        self.kind = kind


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
    repository = repository or DiskMatterRepository()
    try:
        return _build_redline_export(
            payload,
            "",
            title=title,
            persist=persist,
            repository=repository,
            owner_user_id=owner_user_id,
        )
    except DocxOpenHealthError as exc:
        # A content-coverage failure (the redline reconstruction dropped/reordered
        # source paragraphs -- the "N vs M" sequence mismatch) on a PDF-SOURCE matter
        # is NOT a server fault: the Approach-C working-DOCX substitution routes the
        # PDF through the native-DOCX index-anchored branch + strong sequence gate, and
        # an imperfect PDF->DOCX reconstruction can legitimately fail that gate. The gate
        # is RIGHT to reject it (dropped content must never ship), but the caller should
        # fall back to the marked-up source PDF, not crash. Translate it to the typed
        # PdfSourceRedlineUnavailableError (-> 503 + annotated-PDF recovery payload),
        # consistent with the other PDF-source failure modes. Open-health failures
        # (corrupt OOXML) and ALL native-DOCX matters keep raising DocxOpenHealthError
        # untouched so the route's existing mapping (logged, leak-free 500) is preserved.
        if exc.kind == "content_coverage":
            source_filename = _matter_source_filename(
                matter_id, repository=repository, owner_user_id=owner_user_id
            )
            if source_filename_is_pdf(source_filename):
                telemetry.increment("pdf_reviewed_docx_coverage_translated")
                raise PdfSourceRedlineUnavailableError(
                    PDF_SOURCE_REDLINE_UNEXPECTED_MESSAGE,
                    source_filename=source_filename,
                    reason="reconstruction_coverage_shortfall",
                ) from exc
        raise
    except DocxExportError:
        # Already a typed redline-export error (PdfSourceRedlineUnavailableError /
        # MatterSourceTextChangedError / StaleMatterReviewError / MatterNotFoundError /
        # DocxOpenHealthError / PdfRedlineAnchorError / the plain DocxExportError 400s).
        # The route layer maps each to its own status; leave it untouched.
        raise
    except (DocxExtractionError, pdf_docx_reconstruction.PdfDocxReconstructionBusy):
        # Classified upstream (extraction -> 400; reconstruction-busy -> retryable 503).
        # Pass through so the route's existing mappings keep their meaning.
        raise
    except Exception as exc:  # noqa: BLE001 -- producer-boundary translation
        # An UNCLASSIFIED exception escaped the producer (e.g. a KeyError/TypeError/
        # IndexError/AttributeError/ValueError, or an OSError, raised deep in the
        # PDF-source reconstruction-and-anchor path). For a PDF-source matter this is
        # not a server fault -- the lossy PDF->Word reconstruction simply could not
        # produce a faithful tracked redline -- so translate it into the typed
        # PdfSourceRedlineUnavailableError (-> 503 + the source-PDF annotation recovery
        # payload), CONSISTENT with the other PDF-source failure modes that already
        # surface as that error. Non-PDF (native DOCX) matters keep raising the raw
        # exception so the route's top-level guard still turns a genuinely-unforeseen
        # fault into its logged clean 500.
        source_filename = _matter_source_filename(
            matter_id, repository=repository, owner_user_id=owner_user_id
        )
        if source_filename_is_pdf(source_filename):
            telemetry.increment("pdf_reviewed_docx_unexpected_translated")
            raise PdfSourceRedlineUnavailableError(
                PDF_SOURCE_REDLINE_UNEXPECTED_MESSAGE,
                source_filename=source_filename,
                reason="reconstruction_failed",
            ) from exc
        raise


# User-facing message for the catch-all PDF-source producer failure (the
# reconstruction-and-anchor pipeline raised something we did not anticipate). The
# recovery path is identical to the other PdfSourceRedlineUnavailableError reasons:
# mark up the preserved source PDF rather than ship an incomplete reconstructed Word
# doc.
PDF_SOURCE_REDLINE_UNEXPECTED_MESSAGE = (
    "The reviewed Word document could not be produced from this PDF source. "
    "Use the marked-up source PDF for the proposed changes."
)


def _matter_source_filename(
    matter_id: str, *, repository: MatterRepository, owner_user_id: str = ""
) -> str:
    """Best-effort lookup of a matter's ORIGINAL source filename.

    Reads the matter's stored ``source_filename`` (the original upload -- NOT the
    Approach-C working-DOCX substitution that ``_review_result_for_export`` swaps in
    internally), so PDF-source detection keys on what the user actually uploaded.
    Returns ``""`` if the matter is missing or the lookup itself fails, in which case
    the caller treats it as non-PDF and re-raises the original exception.
    """
    try:
        matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    except Exception:  # noqa: BLE001 -- never let the diagnostic lookup mask the real error
        return ""
    if not isinstance(matter, dict):
        return ""
    return str(matter.get("source_filename") or "")


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
    # Pop the transient deterministic-only marker BEFORE the renderer/gate see the
    # review_result (it is a private export-service flag, not a review field). Reattached
    # as an honest response header on the produced export below.
    deterministic_only = bool(review_result.pop(_DETERMINISTIC_ONLY_EXPORT_FLAG, False))
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
            # The content-coverage gate measures only the EXPORTED BODY
            # (word/document.xml); header/footer/footnote parts are copied through
            # verbatim and never reconstructed. The full extracted_text joins body
            # AND supplemental paragraphs, so handing it to that body-only gate
            # false-rejects a faithful letterhead/footer NDA as having dropped
            # content. Scope the expected side to the body so the comparison is
            # body-against-body; a genuine BODY drop/reorder/duplication still
            # fails. Falls back to the supplemental-inclusive extracted_text only
            # when the paragraph records (which carry source_kind) are absent, so
            # behaviour is unchanged on that degenerate path.
            expected_source_text=_body_expected_source_text(review_result),
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
        # No-matter deterministic-only paths (direct DOCX upload / bare-text fallback):
        # label the output honestly so it is never mistaken for the AI Review-tab result.
        headers=(
            {DETERMINISTIC_ONLY_EXPORT_MARKER_HEADER: DETERMINISTIC_ONLY_EXPORT_HEADER}
            if deterministic_only
            else None
        ),
    )


def _working_docx_for_matter(
    matter: dict,
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str = "",
) -> tuple[bytes | None, str]:
    """Return ``(bytes, .docx filename)`` of a converted PDF matter's working DOCX.

    Returns ``(None, "")`` when the matter has no role="working" artifact (a native
    DOCX matter, or a PDF whose ingest-time conversion failed and stayed legacy).
    The returned filename carries a ``.docx`` suffix so the redline export routes the
    matter through the native-DOCX index-anchored branch.
    """
    if not matter_id:
        return None, ""
    artifact = artifact_registry.latest_artifact_for_role(matter, artifact_registry.ROLE_WORKING)
    if artifact is None:
        return None, ""
    working_bytes = artifact_service.get_artifact_bytes(
        matter_id, artifact.id, repository=repository, owner_user_id=owner_user_id
    )
    if not working_bytes:
        return None, ""
    filename = artifact.name or "working.docx"
    if not filename.lower().endswith(".docx"):
        filename = f"{Path(filename).stem or 'working'}.docx"
    return working_bytes, filename


def _body_expected_source_text(review_result: dict) -> str:
    """The expected source text for the native-DOCX content-coverage gate, scoped to
    the document BODY (header/footer/footnote/endnote paragraphs excluded).

    The export reconstructs only the body (``word/document.xml``) and copies supplemental
    parts through verbatim, so ``verify_export_content_coverage`` measures only body text
    on the exported side. The matter's ``extracted_text`` is the canonical,
    supplemental-INCLUSIVE serialization (body + header/footer/footnote text), which is
    the WRONG scope for this body-only comparison: a company-letterhead/footer NDA tripped
    the gate because the expected side required footer text the body-only export can never
    produce. This resolves a body-only expected string, in precedence:

    1. ``review_result[BODY_EXTRACTED_TEXT_FIELD]`` -- the body-only text stamped by
       ``_review_result_for_export`` (from the persisted matter field, the submitted
       viewer text, or a re-extraction of the source body). Authoritative when present.
    2. ``body_extracted_text_from_paragraphs(review_result["paragraphs"])`` -- when the
       paragraph records carry the ``source_kind`` marker (the direct-upload / eager
       paths whose records came straight from ``extract_docx_paragraphs``).
    3. ``extracted_text`` verbatim -- the prior behaviour, used only when neither
       body-only source is available. For an NDA with no header/footer this equals the
       body text, so the fallback is exact; the only residual is a pre-fix matter with a
       header/footer AND no source bytes, which is strictly no worse than today.

    Note (1)/(2) are body-only BASE text; the gate still layers ``clean_fills`` and the
    tracked redline edits onto it, so filled/edited body paragraphs reconcile exactly as
    before -- this change only removes the supplemental text from the expected side, it
    does not touch how body edits are verified."""
    body_text = review_result.get(BODY_EXTRACTED_TEXT_FIELD)
    if isinstance(body_text, str) and body_text.strip():
        return body_text
    paragraphs = review_result.get("paragraphs")
    if isinstance(paragraphs, list) and any(
        isinstance(paragraph, dict) and paragraph.get("source_kind") for paragraph in paragraphs
    ):
        return body_extracted_text_from_paragraphs(paragraphs)
    return str(review_result.get("extracted_text") or "")


def _body_extracted_text_from_docx_bytes(source_document_bytes: bytes | None, source_filename: str) -> str:
    """Best-effort body-only extracted text re-derived from the source DOCX bytes.

    Used to recover a body-only expected string for matters whose stored review
    paragraphs lost the ``source_kind`` marker (the deferred -> on-demand path rebuilds
    paragraphs by splitting the supplemental-inclusive ``extracted_text`` string, which
    carries no marker). Re-extracting the source bytes restores the marker, so the
    supplemental tail can be dropped. Returns "" on any extraction failure or for a
    non-DOCX source, leaving the caller on its existing fallback (never raises -- a
    diagnostic re-extraction must not break the export)."""
    if source_document_bytes is None or not source_filename.lower().endswith(".docx"):
        return ""
    try:
        paragraphs = extract_docx_paragraphs(source_document_bytes)
    except Exception:  # noqa: BLE001 -- a re-extraction failure must not break the export.
        return ""
    return body_extracted_text_from_paragraphs(paragraphs)


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
        # Approach C: when a PDF source matter has a reconstructed working DOCX
        # (built once at ingest), the redline anchors by INDEX into THAT DOCX -- the
        # review paragraphs were re-keyed to its body. Substitute the working DOCX
        # bytes + a .docx filename so the export takes the native-DOCX branch below
        # (exact index anchoring), never the lossy per-export pdf2docx reconstruction
        # fuzzy path. Falls through to the legacy PDF path when no working DOCX exists.
        working_bytes, working_filename = _working_docx_for_matter(
            matter, matter_id.strip(), repository=repository, owner_user_id=owner_user_id
        )
        if working_bytes is not None and working_filename:
            source_document_bytes = working_bytes
            # Substitute the working DOCX bytes so the export takes the exact-index
            # native-DOCX branch, but keep the ORIGINAL source NDA name for the
            # download filename. Reusing ``working_filename`` (the internal working
            # artifact name, e.g. "NN_working_docx.docx") produced a download named
            # "NN_working_docx-redlined.docx" that lost the real NDA name. Derive a
            # .docx-suffixed name from the original source filename so the export
            # still routes to the DOCX branch AND downloads recognizably. Falls back
            # to the working artifact name when the original source name is missing.
            original_stem = Path(source_filename).stem
            source_filename = f"{original_stem}.docx" if original_stem else working_filename
        _apply_saved_redline_draft(payload, matter)
        submitted_text = _submitted_matter_source_text(payload)
        if _matter_source_text_changed(submitted_text, matter, review_result):
            if not _has_manual_redline_payload(payload):
                raise MatterSourceTextChangedError(
                    "NDA source text was edited after the source document was ingested. "
                    "Export or send after those viewer edits are represented as manual redlines."
                )
            # Option A (F4): the exported document must equal the AI review the reviewer
            # APPROVED -- never let a second engine re-decide it. Previously this branch
            # discarded the stored AI review and re-ran the BARE deterministic checker
            # (``review_nda(submitted_text)``), which structurally emits ONLY the ~5
            # native-check clauses and CANNOT emit AI-only DYNAMIC clauses (e.g.
            # non_circumvention). Every dynamic-clause redline the reviewer approved was
            # silently dropped from the counterparty-bound document, and native verdicts
            # could contradict the AI Review tab.
            #
            # Keep the stored ``matter['review_result']`` (the exact object the Review tab
            # renders) as the SINGLE source of truth for clauses/verdicts -- including
            # dynamic clauses and their proposed/redline edits. Its ``redline_edits`` carry
            # ``anchor_text``/``original_text``, so the renderer re-anchors them onto the
            # EDITED body by TEXT match downstream; a redline whose anchor no longer exists
            # after the edit is collected as ``unresolved`` and RAISES (fail closed) rather
            # than shipping a document missing an approved strike. The user's viewer edits
            # reach the document via ``manual_redline_edits`` (merged ON TOP of the stored
            # AI edits by ``apply_manual_export_redlines``), so nothing is re-scored.
            review_result = deepcopy(review_result)
            # The coverage gate's char-ratio floor compares the exported body against this
            # expected text. The submitted/viewer text IS the body the user edited (the
            # viewer never renders header/footer), so it is already body-only -- use it
            # verbatim so the ratio measures against what actually ships, not the pre-edit
            # body. The gate's authoritative accepted-sequence check is driven by
            # ``redline_edits`` against the source DOCX's own accepted view, so keeping the
            # stored AI edits (plus the merged manual redline) is what it reconciles against.
            review_result["extracted_text"] = submitted_text
            review_result[BODY_EXTRACTED_TEXT_FIELD] = submitted_text
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
            # Stamp the BODY-ONLY expected text for the content-coverage gate. The
            # gate compares the body-only export against this; the matter's
            # extracted_text is supplemental-INCLUSIVE (body + header/footer/footnote),
            # so using it directly false-rejects a faithful letterhead/footer NDA. Prefer
            # the body-only text persisted on the matter at ingest (derived where the
            # source_kind marker exists). For matters ingested before that field existed,
            # re-extract the body from the source DOCX bytes (the deferred -> on-demand
            # review rebuilds paragraphs from the supplemental-inclusive string, dropping
            # the marker, so the stored paragraphs cannot be filtered). setdefault never
            # clobbers a value an upstream path already stamped.
            persisted_body_text = str(matter.get(BODY_EXTRACTED_TEXT_FIELD) or "")
            if persisted_body_text.strip():
                review_result.setdefault(BODY_EXTRACTED_TEXT_FIELD, persisted_body_text)
            else:
                # Conservative re-extraction: only override the expected text when the
                # re-extracted body actually DIFFERS from the full extracted_text. The
                # primary case this catches is supplemental (header/footer/footnote)
                # content that was present and removed -- the letterhead/footer bug this
                # fix targets. When the two match (a native DOCX with no supplemental
                # parts -- the common case) we stamp nothing and the gate keeps using
                # extracted_text byte-for-byte.
                #
                # NOTE the override ALSO fires for a converted-PDF matter, where
                # source_document_bytes is the pdf2docx-built working DOCX but
                # extracted_text is the pypdf-derived PDF text: those two bodies diverge
                # in whitespace/run/paragraph segmentation, so the != check is true and we
                # stamp the re-extracted working-DOCX body. This is INTENTIONAL and
                # strictly safer, not a regression -- the redline and the export both
                # render from that same working DOCX, so aligning the gate's expected side
                # onto the working-DOCX body makes the comparison MORE self-consistent than
                # the prior pypdf-vs-pdf2docx mismatch. (The working DOCX has no
                # supplemental parts, so the body-only filter is a no-op on it; the change
                # here is the pypdf->pdf2docx body source, not a content drop.)
                full_extracted_text = str(review_result.get("extracted_text") or "")
                reextracted_body_text = _body_extracted_text_from_docx_bytes(
                    source_document_bytes, source_filename
                )
                if (
                    reextracted_body_text.strip()
                    and _normalize_document_text(reextracted_body_text)
                    != _normalize_document_text(full_extracted_text)
                ):
                    review_result.setdefault(BODY_EXTRACTED_TEXT_FIELD, reextracted_body_text)
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
        # The freshly-extracted paragraphs carry the source_kind marker, so stamp the
        # body-only expected text directly (the gate must not require header/footer
        # text the body-only export cannot produce).
        review_result[BODY_EXTRACTED_TEXT_FIELD] = body_extracted_text_from_paragraphs(extracted_paragraphs)
        # No stored AI review exists for a direct DOCX upload (no matter_id), so this
        # redline is produced by the bare deterministic checker. Flag it so the produced
        # export is labelled honestly (X-Export-Deterministic-Only) and never mistaken
        # for the AI Review-tab result a reviewer approved.
        review_result[_DETERMINISTIC_ONLY_EXPORT_FLAG] = True
        return review_result, document_bytes, filename

    # Bare-text fallback (no matter_id, no DOCX payload): likewise a deterministic-only
    # redline with no stored AI review to preserve -- flag it honestly.
    fallback_review = review_nda(fallback_text)
    fallback_review[_DETERMINISTIC_ONLY_EXPORT_FLAG] = True
    return fallback_review, None, ""


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
        raise DocxOpenHealthError(
            "The exported Word document failed its content-coverage check.",
            content_errors,
            kind="content_coverage",
        )


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
            kind="content_coverage",
        )
