from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open

PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE = (
    "PDF-to-Word reconstruction requires the pdf2docx engine. Install the app with the pdf extra "
    "(`python -m pip install -e \".[pdf]\"`) or enable that dependency in the runtime."
)
PDF_DOCX_RECONSTRUCTION_FAILED_MESSAGE = (
    "The PDF-to-Word reconstruction engine could not produce a valid Word document for this PDF."
)
PDF_DOCX_RECONSTRUCTION_FIDELITY_MESSAGE = (
    "PDF-to-Word reconstruction is a best-effort editable Word export. It may not preserve "
    "tables, colors, images, or page layout exactly; use the original PDF/page preview for "
    "visual fidelity."
)
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_DOCX_RECONSTRUCTION_HEADER = "pdf2docx"


class PdfDocxReconstructionError(RuntimeError):
    pass


class PdfDocxReconstructionUnavailableError(PdfDocxReconstructionError):
    pass


class PdfDocxReconstructionFailedError(PdfDocxReconstructionError):
    pass


class PdfToDocxConverter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None: ...


@dataclass(frozen=True)
class ReconstructedDocx:
    data: bytes
    filename: str
    content_type: str = DOCX_CONTENT_TYPE
    headers: dict[str, str] | None = None


class Pdf2DocxConverter:
    name = "pdf2docx"

    def is_available(self) -> bool:
        return importlib.util.find_spec("pdf2docx") is not None

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        try:
            from pdf2docx import Converter  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - covered through is_available/fakes
            raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE) from exc

        converter = Converter(str(source_path))
        try:
            converter.convert(str(output_path), start=0, end=None)
        finally:
            converter.close()


def converter_health(converter: PdfToDocxConverter | None = None) -> dict[str, object]:
    active_converter = converter or Pdf2DocxConverter()
    available = active_converter.is_available()
    return {
        "available": available,
        "converter": getattr(active_converter, "name", "unknown"),
        "mode": "pdf_to_docx_reconstruction",
        "fidelity": reconstruction_fidelity_payload(output_format="docx"),
        "message": (
            "PDF-to-Word reconstruction is available."
            if available
            else PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE
        ),
    }


def reconstruction_fidelity_payload(*, output_format: str = "docx") -> dict[str, str]:
    return {
        "source": "pdf",
        "output": output_format,
        "mode": "best_effort_pdf_to_docx_reconstruction",
        "visual_fidelity": "best_effort",
        "faithful_visual_source": "original_pdf_page_preview",
        "message": PDF_DOCX_RECONSTRUCTION_FIDELITY_MESSAGE,
    }


def reconstruct_pdf_to_docx(
    pdf_bytes: bytes,
    source_filename: str,
    *,
    converter: PdfToDocxConverter | None = None,
) -> ReconstructedDocx:
    active_converter = converter or Pdf2DocxConverter()
    if not active_converter.is_available():
        raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE)

    with tempfile.TemporaryDirectory(prefix="nda-pdf-docx-") as tmp_dir:
        work_dir = Path(tmp_dir)
        source_path = work_dir / "source.pdf"
        output_path = work_dir / "reconstructed.docx"
        source_path.write_bytes(pdf_bytes)
        try:
            active_converter.convert_pdf_to_docx(source_path, output_path)
            data = output_path.read_bytes()
            _validate_reconstructed_docx(data)
        except PdfDocxReconstructionError:
            raise
        except Exception as exc:
            raise PdfDocxReconstructionFailedError(PDF_DOCX_RECONSTRUCTION_FAILED_MESSAGE) from exc

    return ReconstructedDocx(
        data=data,
        filename=reconstructed_docx_filename(source_filename),
        headers={
            "X-PDF-DOCX-Reconstruction": PDF_DOCX_RECONSTRUCTION_HEADER,
            "X-PDF-DOCX-Converter": getattr(active_converter, "name", "unknown"),
        },
    )


def reconstructed_docx_filename(filename: str) -> str:
    source_name = Path(str(filename or "")).stem
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in source_name)
    safe_name = safe_name.strip("-_") or "document"
    return f"{safe_name}.docx"


def _validate_reconstructed_docx(docx_bytes: bytes) -> None:
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            names = set(archive.namelist())
    except (BadZipFile, DocxExtractionError) as exc:
        raise PdfDocxReconstructionFailedError(PDF_DOCX_RECONSTRUCTION_FAILED_MESSAGE) from exc

    required_parts = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
    }
    missing_parts = required_parts - names
    if missing_parts:
        raise PdfDocxReconstructionFailedError(PDF_DOCX_RECONSTRUCTION_FAILED_MESSAGE)
