from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import (
    artifact_registry,
    artifact_service,
    document_rendering,
    matter_render_job,
    pdf_docx_reconstruction,
    workflow,
)
from .matter_repository import DiskMatterRepository, MatterRepository

PDF_EXPORT_MIME = document_rendering.PDF_CONTENT_TYPE
PDF_EXPORT_VERIFICATION_HEADER = "document-to-pdf"
PDF_CONVERTER_UNAVAILABLE_MESSAGE = (
    "PDF export requires LibreOffice/soffice for Word documents, but no converter executable was found."
)
DOCX_DOWNLOAD_MIME = document_rendering.DOCX_CONTENT_TYPE
PDF_DOCX_RECONSTRUCTION_MIME = pdf_docx_reconstruction.DOCX_CONTENT_TYPE
PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE = (
    pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE
)


class PdfExportError(RuntimeError):
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(str(payload.get("error") or "PDF export failed."))
        self.payload = payload
        self.status = status
        self.headers = headers or {}


@dataclass(frozen=True)
class MatterPdfExport:
    path: Path
    filename: str
    content_type: str
    headers: dict[str, str]


@dataclass(frozen=True)
class MatterDocxExport:
    data: bytes
    filename: str
    content_type: str
    headers: dict[str, str]


def converter_health(converter: document_rendering.DocxConverter | None = None) -> dict[str, object]:
    active_converter = converter or document_rendering.LibreOfficeDocxConverter()
    available = active_converter.is_available()
    return {
        "available": available,
        "converter": getattr(active_converter, "name", "unknown"),
        "message": (
            "DOCX to PDF export is available."
            if available
            else PDF_CONVERTER_UNAVAILABLE_MESSAGE
        ),
    }


def pdf_docx_converter_health(
    converter: pdf_docx_reconstruction.PdfToDocxConverter | None = None,
) -> dict[str, object]:
    return pdf_docx_reconstruction.converter_health(converter)


def matter_pdf_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/source-pdf" if matter_id else ""


def matter_source_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/source" if matter_id else ""


def matter_source_docx_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/source-docx" if matter_id else ""


def matter_working_docx_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/working-docx" if matter_id else ""


def matter_reviewed_docx_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/reviewed-docx" if matter_id else ""


def matter_reviewed_pdf_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/reviewed-pdf" if matter_id else ""


def matter_reviewed_export_ready(matter: dict[str, Any]) -> bool:
    """Whether a matter's reviewed redline exports (DOCX/PDF) should be offered.

    Approval is the reviewer sign-off that first makes the reviewed artifact
    downloadable. Executing the NDA (DocuSign completion / manual mark -- status
    ``fully_signed`` / ``executed``) is a strictly LATER lifecycle state that
    presupposes that approval, so a SIGNED matter must keep its reviewed export
    downloadable rather than losing it. This never widens access to an UNREVIEWED
    matter: a matter cannot reach approved or executed without a completed review,
    and the reviewed-artifact build still fails closed when no redline can be
    composed. Kept as the single source of truth so the download-menu contract and
    the /reviewed-pdf route agree on the allowed states.
    """
    status = str(matter.get("status") or "").strip().lower()
    if status in (workflow.STATUS_APPROVED, workflow.STATUS_FULLY_SIGNED):
        return True
    if matter.get("approved_at"):
        return True
    return workflow.is_matter_executed(matter)


def public_matter_document_downloads(
    matter: dict[str, Any],
    *,
    converter: document_rendering.DocxConverter | None = None,
    pdf_docx_converter: pdf_docx_reconstruction.PdfToDocxConverter | None = None,
) -> dict[str, Any]:
    """UI-facing contract for one Download menu with format choices.

    Existing route targets stay split by artifact/format; this payload tells the
    browser which DOCX/PDF choices to show or disable without probing routes.
    """
    matter_id = str(matter.get("id") or "")
    source_filename = str(matter.get("source_filename") or "")
    source_ext = Path(source_filename).suffix.lower()
    source_is_docx = source_ext == ".docx"
    source_is_pdf = source_ext == ".pdf"
    generated = str(matter.get("source_type") or "") == "generated"
    # Reviewed exports stay downloadable once the matter is approved AND after it is
    # executed/fully_signed -- a signed NDA must not lose its reviewed redline (D5).
    reviewed_ready = matter_reviewed_export_ready(matter)
    reviewed_source_supported = reviewed_ready and source_is_docx
    health = converter_health(converter)
    pdf_docx_health = pdf_docx_converter_health(pdf_docx_converter)
    reconstructed_pdf_docx_available = source_is_pdf and bool(pdf_docx_health["available"])
    source_pdf_docx_fidelity = (
        pdf_docx_reconstruction.reconstruction_fidelity_payload(output_format="docx")
        if source_is_pdf
        else None
    )
    reviewed_pdf_docx_fidelity = (
        pdf_docx_reconstruction.reconstruction_fidelity_payload(output_format="reviewed_docx")
        if source_is_pdf
        else None
    )
    reviewed_pdf_fidelity = (
        pdf_docx_reconstruction.reconstruction_fidelity_payload(output_format="reviewed_pdf")
        if source_is_pdf
        else None
    )

    source_label = "Generated document" if generated else "Original document"
    return {
        "source": {
            "label": source_label,
            "formats": {
                "docx": _download_option(
                    "docx",
                    available=source_is_docx or reconstructed_pdf_docx_available,
                    download_url=(
                        matter_source_download_url(matter_id)
                        if source_is_docx
                        else matter_source_docx_download_url(matter_id)
                        if reconstructed_pdf_docx_available
                        else ""
                    ),
                    filename=(
                        source_filename
                        if source_is_docx
                        else pdf_docx_reconstruction.reconstructed_docx_filename(source_filename)
                    ),
                    content_type=PDF_DOCX_RECONSTRUCTION_MIME if source_is_pdf else DOCX_DOWNLOAD_MIME,
                    unavailable_reason=(
                        ""
                        if source_is_docx or reconstructed_pdf_docx_available
                        else str(pdf_docx_health["message"])
                        if source_is_pdf
                        else "A Word download is not available for this source document."
                    ),
                    converter=pdf_docx_health if source_is_pdf else None,
                    transform="pdf_to_reconstructed_docx" if source_is_pdf else "",
                    label="Reconstructed Word" if source_is_pdf else "",
                    fidelity=source_pdf_docx_fidelity,
                ),
                "pdf": _download_option(
                    "pdf",
                    available=source_is_pdf or (source_is_docx and bool(health["available"])),
                    download_url=matter_pdf_download_url(matter_id)
                    if source_is_pdf or (source_is_docx and bool(health["available"]))
                    else "",
                    filename=pdf_download_filename(source_filename),
                    content_type=PDF_EXPORT_MIME,
                    unavailable_reason=(
                        ""
                        if source_is_pdf or (source_is_docx and bool(health["available"]))
                        else str(health["message"])
                        if source_is_docx
                        else "A PDF download is not available for this source document."
                    ),
                    converter=health if source_is_docx else None,
                ),
            },
        },
        "reviewed": {
            "label": "Reviewed redline",
            "formats": {
                "docx": _download_option(
                    "docx",
                    available=reviewed_source_supported or (reviewed_ready and reconstructed_pdf_docx_available),
                    download_url=(
                        matter_reviewed_docx_download_url(matter_id)
                        if reviewed_source_supported or (reviewed_ready and reconstructed_pdf_docx_available)
                        else ""
                    ),
                    filename=(
                        pdf_download_filename(source_filename).replace(".pdf", "-redlined.docx")
                        if source_is_docx
                        else pdf_docx_reconstruction.reconstructed_docx_filename(source_filename).replace(".docx", "-reviewed.docx")
                        if source_is_pdf
                        else "document-redlined.docx"
                    ),
                    content_type=DOCX_DOWNLOAD_MIME,
                    unavailable_reason=(
                        ""
                        if reviewed_source_supported or (reviewed_ready and reconstructed_pdf_docx_available)
                        else "Reviewed downloads are available after the NDA is approved."
                        if not reviewed_ready
                        else str(pdf_docx_health["message"])
                        if source_is_pdf
                        else "Reviewed DOCX export is not available for this source document."
                    ),
                    converter=pdf_docx_health if source_is_pdf else None,
                    transform="pdf_to_reconstructed_reviewed_docx" if source_is_pdf else "",
                    label="Reconstructed reviewed Word" if source_is_pdf else "",
                    fidelity=reviewed_pdf_docx_fidelity,
                ),
                "pdf": _download_option(
                    "pdf",
                    available=(
                        reviewed_source_supported
                        and bool(health["available"])
                        or reviewed_ready
                        and reconstructed_pdf_docx_available
                        and bool(health["available"])
                    ),
                    download_url=matter_reviewed_pdf_download_url(matter_id)
                    if (
                        reviewed_source_supported
                        and bool(health["available"])
                        or reviewed_ready
                        and reconstructed_pdf_docx_available
                        and bool(health["available"])
                    )
                    else "",
                    filename=pdf_download_filename(source_filename).replace(".pdf", "-redlined.pdf"),
                    content_type=PDF_EXPORT_MIME,
                    unavailable_reason=(
                        ""
                        if (
                            reviewed_source_supported
                            and bool(health["available"])
                            or reviewed_ready
                            and reconstructed_pdf_docx_available
                            and bool(health["available"])
                        )
                        else "Reviewed downloads are available after the NDA is approved."
                        if not reviewed_ready
                        else str(pdf_docx_health["message"])
                        if source_is_pdf and not reconstructed_pdf_docx_available
                        else str(health["message"])
                        if source_is_pdf or source_is_docx
                        else "Reviewed PDF export is not available for this source document."
                    ),
                    converter={
                        "docx_to_pdf": health,
                        "pdf_to_docx": pdf_docx_health,
                    }
                    if source_is_pdf
                    else health,
                    transform="pdf_to_reconstructed_docx_to_pdf" if source_is_pdf else "",
                    label="PDF from reconstructed Word" if source_is_pdf else "",
                    fidelity=reviewed_pdf_fidelity,
                ),
            },
        },
    }

def build_matter_source_pdf_export(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> MatterPdfExport:
    try:
        result = matter_render_job.render_matter_document(
            matter_id,
            owner_user_id=owner_user_id,
            include_page_images=False,
            repository=repository,
        )
    except matter_render_job.MatterRenderJobError as error:
        raise PdfExportError(error.payload, status=error.status, headers=error.headers) from error

    rendered = result.rendered
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        status = 503 if rendered.error_code == "converter_unavailable" else 409
        message = rendered.error_message or "PDF export is not available for this NDA."
        raise PdfExportError(
            {
                "error": message,
                "document_pdf_export": public_matter_pdf_export(
                    matter_id or "",
                    rendered,
                    matter=result.matter,
                ),
            },
            status=status,
        )

    source_filename = str(result.matter.get("source_filename") or result.matter.get("stored_filename") or "")
    return MatterPdfExport(
        path=rendered.pdf_path,
        filename=pdf_download_filename(source_filename),
        content_type=PDF_EXPORT_MIME,
        headers={
            "X-PDF-Export-Verified": PDF_EXPORT_VERIFICATION_HEADER,
            "X-PDF-Export-Source-Kind": rendered.source_kind,
        },
    )


def build_matter_pdf_source_docx_export(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
    converter: pdf_docx_reconstruction.PdfToDocxConverter | None = None,
) -> MatterDocxExport:
    matter_id = str(matter_id or "").strip()
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id) if matter_id else None
    if matter is None:
        raise PdfExportError({"error": "NDA not found."}, status=404)
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    if Path(source_filename).suffix.lower() != ".pdf":
        raise PdfExportError({"error": "PDF-to-Word reconstruction is available only for source PDFs."}, status=409)
    pdf_bytes = repository.get_source_document_bytes(matter)
    if pdf_bytes is None:
        raise PdfExportError({"error": "NDA source document is missing from storage."}, status=404)
    try:
        reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(
            pdf_bytes,
            source_filename,
            converter=converter,
        )
    except pdf_docx_reconstruction.PdfDocxReconstructionUnavailableError as error:
        raise PdfExportError(
            {
                "error": str(error),
                "pdf_docx_reconstruction": {
                    "status": "unavailable",
                    "filename": pdf_docx_reconstruction.reconstructed_docx_filename(source_filename),
                    "converter": pdf_docx_converter_health(converter),
                },
            },
            status=503,
        ) from error
    except pdf_docx_reconstruction.PdfDocxReconstructionFailedError as error:
        raise PdfExportError(
            {
                "error": str(error),
                "pdf_docx_reconstruction": {
                    "status": "failed",
                    "filename": pdf_docx_reconstruction.reconstructed_docx_filename(source_filename),
                    "converter": pdf_docx_converter_health(converter),
                },
            },
            status=422,
        ) from error

    return MatterDocxExport(
        data=reconstructed.data,
        filename=reconstructed.filename,
        content_type=reconstructed.content_type,
        headers=reconstructed.headers or {},
    )


def build_matter_working_docx_export(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> MatterDocxExport:
    """Serve the canonical working DOCX (Approach C) for a converted PDF matter.

    Owner-scoped + fail-closed exactly like the other matter document endpoints:
    ``get_matter`` returns None on an owner mismatch (a past P0 treated an ownerless
    matter as a wildcard -- this stays closed). 404 until the ingest-time PDF→DOCX
    conversion has produced + persisted a role="working" artifact (e.g. a native
    DOCX matter, or a PDF whose conversion failed/has not run, has none).
    """
    matter_id = str(matter_id or "").strip()
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id) if matter_id else None
    if matter is None:
        raise PdfExportError({"error": "NDA not found."}, status=404)
    artifact = artifact_registry.latest_artifact_for_role(matter, artifact_registry.ROLE_WORKING)
    if artifact is None:
        raise PdfExportError({"error": "A working Word document is not available for this NDA."}, status=404)
    working_bytes = artifact_service.get_artifact_bytes(
        matter_id, artifact.id, repository=repository, owner_user_id=owner_user_id
    )
    if not working_bytes:
        raise PdfExportError({"error": "A working Word document is not available for this NDA."}, status=404)
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    download_name = (
        pdf_docx_reconstruction.reconstructed_docx_filename(source_filename)
        if source_filename
        else "working.docx"
    )
    return MatterDocxExport(
        data=working_bytes,
        filename=download_name,
        content_type=DOCX_DOWNLOAD_MIME,
        headers={"X-Working-Docx-Artifact-ID": artifact.id},
    )


def build_docx_pdf_export(
    docx_bytes: bytes,
    filename: str,
    *,
    owner_user_id: str = "",
    converter: document_rendering.DocxConverter | None = None,
) -> MatterPdfExport:
    rendered = document_rendering.render_source_document_to_pdf(
        docx_bytes,
        source_filename=filename,
        owner_user_id=owner_user_id,
        converter=converter,
    )
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        status = 503 if rendered.error_code in {"converter_unavailable", "conversion_busy"} else 500
        message = (
            PDF_CONVERTER_UNAVAILABLE_MESSAGE
            if rendered.error_code == "converter_unavailable"
            else rendered.error_message or "DOCX to PDF export failed."
        )
        raise PdfExportError(
            {
                "error": message,
                "document_pdf_export": {
                    "status": rendered.status,
                    "source_kind": rendered.source_kind,
                    "filename": pdf_download_filename(filename),
                    "error_code": rendered.error_code,
                    "error_message": rendered.error_message or message,
                    "converter": converter_health(converter),
                },
            },
            status=status,
        )

    return MatterPdfExport(
        path=rendered.pdf_path,
        filename=pdf_download_filename(filename),
        content_type=PDF_EXPORT_MIME,
        headers={
            "X-PDF-Export-Verified": PDF_EXPORT_VERIFICATION_HEADER,
            "X-PDF-Export-Source-Kind": rendered.source_kind,
        },
    )


def public_matter_pdf_export(
    matter_id: str,
    rendered: document_rendering.RenderedDocument,
    *,
    matter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_kind = rendered.source_kind
    payload: dict[str, Any] = {
        "status": rendered.status,
        "matter_id": matter_id,
        "source_kind": source_kind,
        "source_label": "Original PDF" if source_kind == "pdf" else "Converted DOCX",
    }
    if matter is not None:
        source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
        payload["filename"] = pdf_download_filename(source_filename)
    if rendered.status == document_rendering.READY_STATUS and rendered.pdf_path is not None:
        payload["download_url"] = matter_pdf_download_url(matter_id)
    if rendered.error_code:
        payload["error_code"] = rendered.error_code
        payload["error_message"] = rendered.error_message
    if source_kind == "docx":
        payload["converter"] = converter_health()
    return payload


def pdf_download_filename(filename: str) -> str:
    source_name = Path(filename).stem if filename else ""
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in source_name)
    safe_name = safe_name.strip("-_") or "document"
    return f"{safe_name}.pdf"


def _download_option(
    output_format: str,
    *,
    available: bool,
    download_url: str,
    filename: str,
    content_type: str,
    unavailable_reason: str = "",
    converter: dict[str, object] | None = None,
    transform: str = "",
    label: str = "",
    fidelity: dict[str, str] | None = None,
) -> dict[str, Any]:
    option: dict[str, Any] = {
        "format": output_format,
        "available": bool(available),
        "filename": _fallback_filename(filename),
        "content_type": content_type,
    }
    if transform:
        option["source_transform"] = transform
    if label:
        option["label"] = label
    if fidelity is not None:
        option["fidelity"] = fidelity
    if available and download_url:
        option["download_url"] = download_url
    if not available and unavailable_reason:
        option["unavailable_reason"] = unavailable_reason
    if converter is not None:
        option["converter"] = converter
    return option


def _fallback_filename(filename: str) -> str:
    return str(filename or "").strip() or "document"
