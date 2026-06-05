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
