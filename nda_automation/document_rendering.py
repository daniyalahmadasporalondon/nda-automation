from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import matter_store
from .durable_io import fsync_parent_directory

DOCUMENT_RENDER_CACHE_VERSION = "document-rendering:v1"
DOCUMENT_RENDER_METADATA_VERSION = 1
DEFAULT_CONVERSION_TIMEOUT_SECONDS = 30
DOCUMENT_RENDER_CACHE_DIRNAME = "document-rendering"

PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PAGE_IMAGE_CONTENT_TYPE = "image/png"
PAGE_IMAGE_DIRNAME = "pages"
PAGE_IMAGE_MANIFEST_FILENAME = "manifest.json"
PAGE_IMAGE_METADATA_VERSION = 1
DEFAULT_PAGE_IMAGE_DPI = 192
PDF_POINTS_PER_INCH = 72

READY_STATUS = "ready"
UNAVAILABLE_STATUS = "unavailable"
UNSUPPORTED_STATUS = "unsupported"
ERROR_STATUS = "error"


class DocumentRenderingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DocxConverterUnavailable(DocumentRenderingError):
    def __init__(self) -> None:
        super().__init__(
            "converter_unavailable",
            "DOCX rendering requires LibreOffice/soffice, but no executable was found.",
        )


class PdfPageRendererUnavailable(DocumentRenderingError):
    def __init__(self) -> None:
        super().__init__(
            "page_renderer_unavailable",
            "PDF page image rendering requires PyMuPDF/fitz, but it is not installed.",
        )


class DocxConverter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path: ...


@dataclass(frozen=True)
class RenderedDocument:
    status: str
    cache_key: str
    source_sha256: str
    source_kind: str
    cache_dir: Path
    pdf_path: Path | None = None
    metadata_path: Path | None = None
    cached: bool = False
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "cache_key": self.cache_key,
            "source_sha256": self.source_sha256,
            "source_kind": self.source_kind,
            "cached": self.cached,
        }
        if self.pdf_path is not None:
            payload["pdf_path"] = str(self.pdf_path)
        if self.metadata_path is not None:
            payload["metadata_path"] = str(self.metadata_path)
        if self.error_code:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message,
            }
        return payload


@dataclass(frozen=True)
class RenderedPdfPageImage:
    page_number: int
    image_path: Path | None = None
    width: int | None = None
    height: int | None = None
    dpi: int | None = None
    scale: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"page_number": self.page_number}
        if self.image_path is not None:
            payload["image_path"] = str(self.image_path)
        if self.width is not None:
            payload["width"] = self.width
        if self.height is not None:
            payload["height"] = self.height
        if self.dpi is not None:
            payload["dpi"] = self.dpi
        if self.scale is not None:
            payload["scale"] = self.scale
        return payload


@dataclass(frozen=True)
class RenderedPdfPageImageManifest:
    status: str
    cache_key: str
    cache_dir: Path
    pdf_path: Path | None = None
    manifest_path: Path | None = None
    pages: tuple[RenderedPdfPageImage, ...] = ()
    cached: bool = False
    dpi: int | None = None
    scale: float | None = None
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "cache_key": self.cache_key,
            "cached": self.cached,
            "pages": [page.to_dict() for page in self.pages],
        }
        if self.pdf_path is not None:
            payload["pdf_path"] = str(self.pdf_path)
        if self.manifest_path is not None:
            payload["manifest_path"] = str(self.manifest_path)
        if self.dpi is not None:
            payload["dpi"] = self.dpi
        if self.scale is not None:
            payload["scale"] = self.scale
        if self.error_code:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message,
            }
        return payload


class PdfPageRenderer(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]: ...


class LibreOfficeDocxConverter:
    name = "libreoffice"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("soffice") or shutil.which("libreoffice")

    def is_available(self) -> bool:
        return bool(self.executable)

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        if not self.executable:
            raise DocxConverterUnavailable()

        command = [
            self.executable,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(output_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DocxConverterUnavailable() from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentRenderingError(
                "conversion_timeout",
                f"DOCX to PDF conversion timed out after {timeout_seconds} seconds.",
            ) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            detail = stderr or stdout or f"LibreOffice exited with status {completed.returncode}."
            raise DocumentRenderingError("conversion_failed", _truncate_error_detail(detail))

        expected_output = output_dir / f"{source_path.stem}.pdf"
        if expected_output.is_file():
            return expected_output
        pdf_outputs = sorted(output_dir.glob("*.pdf"))
        if pdf_outputs:
            return pdf_outputs[0]
        raise DocumentRenderingError("conversion_missing_output", "DOCX conversion finished but did not produce a PDF.")


class PyMuPdfPageRenderer:
    name = "pymupdf"

    def __init__(self, fitz_module: Any | None = None) -> None:
        self._fitz = fitz_module if fitz_module is not None else _load_fitz_module()

    def is_available(self) -> bool:
        return self._fitz is not None

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]:
        if self._fitz is None:
            raise PdfPageRendererUnavailable()

        output_dir.mkdir(parents=True, exist_ok=True)
        scale = _page_image_scale(dpi)
        document = self._fitz.open(str(pdf_path))
        try:
            page_count = getattr(document, "page_count", None)
            if page_count is None:
                page_count = len(document)
            matrix = self._fitz.Matrix(scale, scale)
            pages: list[RenderedPdfPageImage] = []
            for page_index in range(int(page_count)):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image_path = output_dir / _page_image_filename(page_index + 1)
                _write_bytes_atomic(image_path, pixmap.tobytes("png"))
                pages.append(
                    RenderedPdfPageImage(
                        page_number=page_index + 1,
                        image_path=image_path,
                        width=int(pixmap.width),
                        height=int(pixmap.height),
                        dpi=dpi,
                        scale=scale,
                    )
                )
            return pages
        finally:
            close = getattr(document, "close", None)
            if close is not None:
                close()


def render_source_path_to_pdf(
    source_path: Path,
    *,
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
) -> RenderedDocument:
    source_path = Path(source_path)
    return render_source_document_to_pdf(
        source_path.read_bytes(),
        source_filename=source_path.name,
        content_type=content_type,
        cache_dir=cache_dir,
        converter=converter,
        timeout_seconds=timeout_seconds,
    )


def render_source_document_to_pdf(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
) -> RenderedDocument:
    source_kind = detect_source_kind(source_bytes, source_filename=source_filename, content_type=content_type)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    cache_key = document_render_cache_key(source_bytes, source_kind=source_kind)
    cache_root = document_render_cache_dir(cache_dir)
    entry_dir = cache_entry_dir(cache_root, cache_key)
    pdf_path = entry_dir / "document.pdf"
    metadata_path = entry_dir / "metadata.json"

    cached_metadata = _read_metadata(metadata_path)
    if _is_ready_cache_hit(cached_metadata, pdf_path, source_sha256=source_sha256, source_kind=source_kind, cache_key=cache_key):
        return RenderedDocument(
            status=READY_STATUS,
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            metadata_path=metadata_path,
            cached=True,
        )

    if source_kind == "pdf":
        try:
            _write_bytes_atomic(pdf_path, source_bytes)
            _write_metadata(
                metadata_path,
                _metadata_payload(
                    status=READY_STATUS,
                    cache_key=cache_key,
                    source_sha256=source_sha256,
                    source_kind=source_kind,
                    cache_root=cache_root,
                    pdf_path=pdf_path,
                ),
            )
        except OSError as exc:
            return _error_result(
                cache_key=cache_key,
                source_sha256=source_sha256,
                source_kind=source_kind,
                cache_dir=cache_root,
                metadata_path=metadata_path,
                code="cache_write_failed",
                message=f"Renderable PDF cache could not be written: {exc}",
            )
        return RenderedDocument(
            status=READY_STATUS,
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            metadata_path=metadata_path,
            cached=False,
        )

    if source_kind != "docx":
        result = _error_result(
            status=UNSUPPORTED_STATUS,
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            metadata_path=metadata_path,
            code="unsupported_source_kind",
            message="Only PDF and DOCX documents can be rendered to PDF.",
        )
        _persist_failure_metadata(result)
        return result

    active_converter = converter or LibreOfficeDocxConverter()
    if not active_converter.is_available():
        result = _error_result(
            status=UNAVAILABLE_STATUS,
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            metadata_path=metadata_path,
            code="converter_unavailable",
            message="DOCX rendering requires LibreOffice/soffice, but no executable was found.",
        )
        _persist_failure_metadata(result, converter_name=getattr(active_converter, "name", "unknown"))
        return result

    try:
        entry_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="work-", dir=str(entry_dir)) as work_dir_name:
            work_dir = Path(work_dir_name)
            source_path = work_dir / "source.docx"
            source_path.write_bytes(source_bytes)
            converted_path = active_converter.convert_docx_to_pdf(
                source_path,
                work_dir,
                timeout_seconds=timeout_seconds,
            )
            converted_path = _safe_converted_output_path(converted_path, work_dir)
            _write_bytes_atomic(pdf_path, converted_path.read_bytes())
        _write_metadata(
            metadata_path,
            _metadata_payload(
                status=READY_STATUS,
                cache_key=cache_key,
                source_sha256=source_sha256,
                source_kind=source_kind,
                cache_root=cache_root,
                pdf_path=pdf_path,
                converter_name=getattr(active_converter, "name", "unknown"),
            ),
        )
    except DocumentRenderingError as exc:
        result = _error_result(
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            metadata_path=metadata_path,
            code=exc.code,
            message=exc.message,
        )
        _persist_failure_metadata(result, converter_name=getattr(active_converter, "name", "unknown"))
        return result
    except OSError as exc:
        result = _error_result(
            cache_key=cache_key,
            source_sha256=source_sha256,
            source_kind=source_kind,
            cache_dir=cache_root,
            metadata_path=metadata_path,
            code="cache_write_failed",
            message=f"Renderable PDF cache could not be written: {exc}",
        )
        _persist_failure_metadata(result, converter_name=getattr(active_converter, "name", "unknown"))
        return result

    return RenderedDocument(
        status=READY_STATUS,
        cache_key=cache_key,
        source_sha256=source_sha256,
        source_kind=source_kind,
        cache_dir=cache_root,
        pdf_path=pdf_path,
        metadata_path=metadata_path,
        cached=False,
    )


def render_pdf_page_image_manifest(
    rendered: RenderedDocument,
    *,
    renderer: PdfPageRenderer | None = None,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> RenderedPdfPageImageManifest:
    entry_dir = cache_entry_dir(rendered.cache_dir, rendered.cache_key)
    page_dir = entry_dir / PAGE_IMAGE_DIRNAME
    manifest_path = page_dir / PAGE_IMAGE_MANIFEST_FILENAME
    if rendered.status != READY_STATUS or rendered.pdf_path is None:
        return _page_image_error_result(
            cache_key=rendered.cache_key,
            cache_dir=rendered.cache_dir,
            pdf_path=rendered.pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="rendered_pdf_unavailable",
            message="Rendered PDF is not available for page image rendering.",
            status=UNAVAILABLE_STATUS,
        )
    return render_pdf_to_page_image_manifest(
        rendered.pdf_path,
        cache_key=rendered.cache_key,
        cache_dir=rendered.cache_dir,
        renderer=renderer,
        dpi=dpi,
    )


def render_pdf_to_page_image_manifest(
    pdf_path: Path,
    *,
    cache_key: str,
    cache_dir: Path,
    renderer: PdfPageRenderer | None = None,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> RenderedPdfPageImageManifest:
    if dpi <= 0:
        raise ValueError("Page image DPI must be a positive integer.")
    cache_root = Path(cache_dir).expanduser().resolve()
    entry_dir = cache_entry_dir(cache_root, cache_key)
    page_dir = entry_dir / PAGE_IMAGE_DIRNAME
    manifest_path = page_dir / PAGE_IMAGE_MANIFEST_FILENAME
    pdf_path = Path(pdf_path)
    try:
        pdf_sha256 = _file_sha256(pdf_path)
    except OSError as exc:
        return _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="pdf_read_failed",
            message=f"Rendered PDF could not be read for page image rendering: {exc}",
        )

    cached_manifest = _page_image_manifest_from_metadata(
        _read_metadata(manifest_path),
        cache_key=cache_key,
        cache_dir=cache_root,
        pdf_path=pdf_path,
        manifest_path=manifest_path,
        pdf_sha256=pdf_sha256,
        dpi=dpi,
    )
    if cached_manifest is not None:
        return cached_manifest

    active_renderer = renderer or PyMuPdfPageRenderer()
    if not active_renderer.is_available():
        result = _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="page_renderer_unavailable",
            message="PDF page image rendering requires PyMuPDF/fitz, but it is not installed.",
            status=UNAVAILABLE_STATUS,
        )
        _persist_page_image_failure_metadata(result, pdf_sha256=pdf_sha256, renderer_name=getattr(active_renderer, "name", "unknown"))
        return result

    renderer_name = getattr(active_renderer, "name", "unknown")
    try:
        page_dir.mkdir(parents=True, exist_ok=True)
        rendered_pages = active_renderer.render_pdf_to_page_images(pdf_path, page_dir, dpi=dpi)
        pages = tuple(_safe_rendered_page_image(page, page_dir, dpi=dpi) for page in rendered_pages)
        if not pages:
            raise DocumentRenderingError("page_render_no_pages", "PDF page image rendering did not produce any page images.")
        manifest = RenderedPdfPageImageManifest(
            status=READY_STATUS,
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            pages=pages,
            cached=False,
            dpi=dpi,
            scale=_page_image_scale(dpi),
        )
        _write_metadata(
            manifest_path,
            _page_image_metadata_payload(
                status=READY_STATUS,
                cache_key=cache_key,
                cache_root=cache_root,
                pdf_sha256=pdf_sha256,
                pages=pages,
                dpi=dpi,
                renderer_name=renderer_name,
            ),
        )
        return manifest
    except DocumentRenderingError as exc:
        result = _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code=exc.code,
            message=exc.message,
        )
    except OSError as exc:
        result = _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="page_cache_write_failed",
            message=f"Page image cache could not be written: {exc}",
        )
    except Exception as exc:
        result = _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="page_render_failed",
            message=f"PDF page image rendering failed: {_truncate_error_detail(str(exc))}",
        )
    _persist_page_image_failure_metadata(result, pdf_sha256=pdf_sha256, renderer_name=renderer_name)
    return result


def page_image_for_page_number(manifest: RenderedPdfPageImageManifest, page_number: int) -> RenderedPdfPageImage | None:
    for page in manifest.pages:
        if page.page_number == page_number:
            return page
    return None


def detect_source_kind(source_bytes: bytes, *, source_filename: str = "", content_type: str = "") -> str:
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(source_filename).suffix.lower()
    if source_bytes.startswith(b"%PDF-") or normalized_content_type == PDF_CONTENT_TYPE or suffix == ".pdf":
        return "pdf"
    if normalized_content_type == DOCX_CONTENT_TYPE or suffix == ".docx":
        return "docx"
    return "unknown"


def document_render_cache_key(
    source_bytes: bytes,
    *,
    source_kind: str,
    cache_version: str = DOCUMENT_RENDER_CACHE_VERSION,
) -> str:
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    key_hash = hashlib.sha256(f"{cache_version}:{source_kind}:{source_hash}".encode("utf-8")).hexdigest()
    return f"{source_kind}-{key_hash}"


def document_render_cache_dir(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return (matter_store.DATA_DIR / "cache" / DOCUMENT_RENDER_CACHE_DIRNAME).resolve()


def cache_entry_dir(cache_root: Path, cache_key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+", cache_key):
        raise ValueError("Document render cache key contains unsafe characters.")
    resolved_root = Path(cache_root).resolve()
    resolved_entry = (resolved_root / cache_key).resolve()
    if resolved_entry != resolved_root and resolved_root not in resolved_entry.parents:
        raise ValueError("Document render cache path escapes the cache root.")
    return resolved_entry


def _is_ready_cache_hit(
    metadata: dict[str, Any] | None,
    pdf_path: Path,
    *,
    source_sha256: str,
    source_kind: str,
    cache_key: str,
) -> bool:
    if not pdf_path.is_file() or not metadata:
        return False
    return (
        metadata.get("status") == READY_STATUS
        and metadata.get("cache_version") == DOCUMENT_RENDER_CACHE_VERSION
        and metadata.get("cache_key") == cache_key
        and metadata.get("source_sha256") == source_sha256
        and metadata.get("source_kind") == source_kind
        and metadata.get("artifact_kind") == "pdf"
    )


def _safe_converted_output_path(converted_path: Path, work_dir: Path) -> Path:
    resolved_work_dir = work_dir.resolve()
    resolved_output = Path(converted_path).resolve()
    if resolved_output != resolved_work_dir and resolved_work_dir not in resolved_output.parents:
        raise DocumentRenderingError("conversion_output_unsafe", "DOCX converter returned a path outside the render work directory.")
    if not resolved_output.is_file():
        raise DocumentRenderingError("conversion_missing_output", "DOCX conversion finished but did not produce a PDF.")
    return resolved_output


def _metadata_payload(
    *,
    status: str,
    cache_key: str,
    source_sha256: str,
    source_kind: str,
    cache_root: Path,
    pdf_path: Path | None = None,
    converter_name: str = "",
    error_code: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "version": DOCUMENT_RENDER_METADATA_VERSION,
        "cache_version": DOCUMENT_RENDER_CACHE_VERSION,
        "cache_key": cache_key,
        "source_sha256": source_sha256,
        "source_kind": source_kind,
        "artifact_kind": "pdf",
        "status": status,
        "updated_at": now,
    }
    if pdf_path is not None:
        payload["artifact_path"] = _relative_cache_path(pdf_path, cache_root)
    if converter_name:
        payload["converter"] = {"name": converter_name}
    if error_code:
        payload["error"] = {
            "code": error_code,
            "message": error_message,
        }
    return payload


def _relative_cache_path(path: Path, cache_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(cache_root.resolve()))
    except ValueError:
        return path.name


def _error_result(
    *,
    cache_key: str,
    source_sha256: str,
    source_kind: str,
    cache_dir: Path,
    metadata_path: Path,
    code: str,
    message: str,
    status: str = ERROR_STATUS,
) -> RenderedDocument:
    return RenderedDocument(
        status=status,
        cache_key=cache_key,
        source_sha256=source_sha256,
        source_kind=source_kind,
        cache_dir=cache_dir,
        metadata_path=metadata_path,
        cached=False,
        error_code=code,
        error_message=message,
    )


def _persist_failure_metadata(result: RenderedDocument, *, converter_name: str = "") -> None:
    if result.metadata_path is None:
        return
    try:
        _write_metadata(
            result.metadata_path,
            _metadata_payload(
                status=result.status,
                cache_key=result.cache_key,
                source_sha256=result.source_sha256,
                source_kind=result.source_kind,
                cache_root=result.cache_dir,
                converter_name=converter_name,
                error_code=result.error_code,
                error_message=result.error_message,
            ),
        )
    except OSError:
        pass


def _persist_page_image_failure_metadata(
    result: RenderedPdfPageImageManifest,
    *,
    pdf_sha256: str,
    renderer_name: str = "",
) -> None:
    if result.manifest_path is None:
        return
    try:
        _write_metadata(
            result.manifest_path,
            _page_image_metadata_payload(
                status=result.status,
                cache_key=result.cache_key,
                cache_root=result.cache_dir,
                pdf_sha256=pdf_sha256,
                pages=result.pages,
                dpi=result.dpi or DEFAULT_PAGE_IMAGE_DPI,
                renderer_name=renderer_name,
                error_code=result.error_code,
                error_message=result.error_message,
            ),
        )
    except OSError:
        pass


def _read_metadata(metadata_path: Path) -> dict[str, Any] | None:
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _write_metadata(metadata_path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _write_bytes_atomic(metadata_path, serialized)


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        fsync_parent_directory(path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _truncate_error_detail(detail: str, limit: int = 500) -> str:
    if len(detail) <= limit:
        return detail
    return detail[: limit - 3] + "..."


def _load_fitz_module() -> Any | None:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None
    return fitz


def _page_image_filename(page_number: int) -> str:
    return f"page-{page_number}.png"


def _page_image_scale(dpi: int) -> float:
    return round(float(dpi) / PDF_POINTS_PER_INCH, 4)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_image_error_result(
    *,
    cache_key: str,
    cache_dir: Path,
    pdf_path: Path | None,
    manifest_path: Path,
    dpi: int,
    code: str,
    message: str,
    status: str = ERROR_STATUS,
) -> RenderedPdfPageImageManifest:
    return RenderedPdfPageImageManifest(
        status=status,
        cache_key=cache_key,
        cache_dir=cache_dir,
        pdf_path=pdf_path,
        manifest_path=manifest_path,
        cached=False,
        dpi=dpi,
        scale=_page_image_scale(dpi),
        error_code=code,
        error_message=message,
    )


def _safe_rendered_page_image(page: RenderedPdfPageImage, page_dir: Path, *, dpi: int) -> RenderedPdfPageImage:
    if page.page_number < 1:
        raise DocumentRenderingError("page_render_invalid_page_number", "PDF page image renderer returned an invalid page number.")
    if page.image_path is None:
        raise DocumentRenderingError("page_render_missing_image", "PDF page image renderer returned a page without an image path.")
    resolved_page_dir = page_dir.resolve()
    resolved_image = Path(page.image_path).resolve()
    if resolved_image != resolved_page_dir and resolved_page_dir not in resolved_image.parents:
        raise DocumentRenderingError("page_image_output_unsafe", "PDF page image renderer returned a path outside the page image cache directory.")
    if not resolved_image.is_file():
        raise DocumentRenderingError("page_render_missing_image", "PDF page image renderer did not produce an expected page image.")
    return RenderedPdfPageImage(
        page_number=int(page.page_number),
        image_path=resolved_image,
        width=int(page.width) if page.width is not None else None,
        height=int(page.height) if page.height is not None else None,
        dpi=int(page.dpi) if page.dpi is not None else dpi,
        scale=float(page.scale) if page.scale is not None else _page_image_scale(dpi),
    )


def _page_image_manifest_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    cache_key: str,
    cache_dir: Path,
    pdf_path: Path,
    manifest_path: Path,
    pdf_sha256: str,
    dpi: int,
) -> RenderedPdfPageImageManifest | None:
    if not metadata:
        return None
    if (
        metadata.get("status") != READY_STATUS
        or metadata.get("version") != PAGE_IMAGE_METADATA_VERSION
        or metadata.get("cache_version") != DOCUMENT_RENDER_CACHE_VERSION
        or metadata.get("cache_key") != cache_key
        or metadata.get("pdf_sha256") != pdf_sha256
        or metadata.get("artifact_kind") != "pdf_page_images"
        or metadata.get("dpi") != dpi
    ):
        return None
    pages_payload = metadata.get("pages")
    if not isinstance(pages_payload, list) or not pages_payload:
        return None
    pages: list[RenderedPdfPageImage] = []
    for page_payload in pages_payload:
        page = _page_image_from_metadata(page_payload, cache_dir=cache_dir, default_dpi=dpi)
        if page is None:
            return None
        pages.append(page)
    return RenderedPdfPageImageManifest(
        status=READY_STATUS,
        cache_key=cache_key,
        cache_dir=cache_dir,
        pdf_path=pdf_path,
        manifest_path=manifest_path,
        pages=tuple(pages),
        cached=True,
        dpi=dpi,
        scale=float(metadata.get("scale") or _page_image_scale(dpi)),
    )


def _page_image_from_metadata(
    payload: Any,
    *,
    cache_dir: Path,
    default_dpi: int,
) -> RenderedPdfPageImage | None:
    if not isinstance(payload, dict):
        return None
    page_number = payload.get("page_number")
    image_path = payload.get("image_path")
    if not isinstance(page_number, int) or page_number < 1 or not isinstance(image_path, str) or not image_path:
        return None
    resolved_image = _cache_relative_path(cache_dir, image_path)
    if resolved_image is None or not resolved_image.is_file():
        return None
    return RenderedPdfPageImage(
        page_number=page_number,
        image_path=resolved_image,
        width=_optional_int(payload.get("width")),
        height=_optional_int(payload.get("height")),
        dpi=_optional_int(payload.get("dpi")) or default_dpi,
        scale=_optional_float(payload.get("scale")) or _page_image_scale(default_dpi),
    )


def _cache_relative_path(cache_root: Path, relative_path: str) -> Path | None:
    candidate = (Path(cache_root).resolve() / relative_path).resolve()
    resolved_root = Path(cache_root).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        return None
    return candidate


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _page_image_metadata_payload(
    *,
    status: str,
    cache_key: str,
    cache_root: Path,
    pdf_sha256: str,
    pages: tuple[RenderedPdfPageImage, ...],
    dpi: int,
    renderer_name: str = "",
    error_code: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "version": PAGE_IMAGE_METADATA_VERSION,
        "cache_version": DOCUMENT_RENDER_CACHE_VERSION,
        "cache_key": cache_key,
        "pdf_sha256": pdf_sha256,
        "artifact_kind": "pdf_page_images",
        "status": status,
        "dpi": dpi,
        "scale": _page_image_scale(dpi),
        "updated_at": now,
        "pages": [_page_image_metadata_page(page, cache_root=cache_root, dpi=dpi) for page in pages],
    }
    if renderer_name:
        payload["renderer"] = {"name": renderer_name}
    if error_code:
        payload["error"] = {
            "code": error_code,
            "message": error_message,
        }
    return payload


def _page_image_metadata_page(page: RenderedPdfPageImage, *, cache_root: Path, dpi: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page_number": page.page_number,
        "dpi": page.dpi if page.dpi is not None else dpi,
        "scale": page.scale if page.scale is not None else _page_image_scale(dpi),
    }
    if page.image_path is not None:
        payload["image_path"] = _relative_cache_path(page.image_path, cache_root)
    if page.width is not None:
        payload["width"] = page.width
    if page.height is not None:
        payload["height"] = page.height
    return payload
