from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import document_rendering, matter_render_job
from .matter_repository import MatterRepository

PDF_EXPORT_MIME = document_rendering.PDF_CONTENT_TYPE
PDF_EXPORT_VERIFICATION_HEADER = "document-to-pdf"
PDF_CONVERTER_UNAVAILABLE_MESSAGE = (
    "PDF export requires LibreOffice/soffice for Word documents, but no converter executable was found."
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


def matter_pdf_download_url(matter_id: str) -> str:
    matter_id = str(matter_id or "").strip()
    return f"/api/matters/{quote(matter_id, safe='')}/source-pdf" if matter_id else ""


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
        message = rendered.error_message or "PDF export is not available for this matter."
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
