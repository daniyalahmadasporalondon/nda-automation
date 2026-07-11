from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from . import matter_store
from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open
from .durable_io import fsync_parent_directory

try:  # POSIX-only; absent on Windows. Guards the per-child RLIMIT preexec_fn.
    import resource
except ImportError:  # pragma: no cover - non-POSIX fallback
    resource = None  # type: ignore[assignment]

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

# pdf2docx hardening bounds. pdf2docx runs INLINE on the request thread with no
# bound of its own; a sub-10 MB but page-dense PDF can pin CPU/RAM for tens of
# seconds, and a flood of distinct PDFs or a loop on the same one would otherwise
# spawn unbounded concurrent conversions and OOM a small instance. This mirrors
# the soffice discipline in document_rendering.py and gates the reconstruction
# five ways:
#   * MAX_CONCURRENT_PDF_DOCX_CONVERSIONS bounds how many run at once; callers
#     that cannot acquire a slot within PDF_DOCX_QUEUE_WAIT_SECONDS get a
#     PdfDocxReconstructionBusy (the route maps this to 503 backpressure).
#   * MAX_PDF_DOCX_PAGES rejects a PDF with more pages than we will convert,
#     measured cheaply (PyMuPDF page_count) BEFORE the heavy convert runs.
#   * Each conversion runs in its own Python child process group with
#     RLIMIT_AS/RLIMIT_CPU so a runaway convert cannot exhaust memory or CPU.
#   * A hard wall-clock timeout SIGKILLs the whole child process group.
#   * An owner-keyed LRU cache so a repeat GET of the same source by the same
#     tenant reuses the prior reconstruction instead of re-converting.
def _bounded_env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


MAX_CONCURRENT_PDF_DOCX_CONVERSIONS = _bounded_env_int(
    "NDA_PDF_DOCX_MAX_CONCURRENCY", 2, minimum=1
)
PDF_DOCX_QUEUE_WAIT_SECONDS = float(os.environ.get("NDA_PDF_DOCX_QUEUE_WAIT_SECONDS", "5") or 5)
PDF_DOCX_TIMEOUT_SECONDS = _bounded_env_int("NDA_PDF_DOCX_TIMEOUT_SECONDS", 90, minimum=1)
PDF_DOCX_MEMORY_LIMIT_BYTES = _bounded_env_int(
    "NDA_PDF_DOCX_MEMORY_LIMIT_BYTES", 2 * 1024 * 1024 * 1024, minimum=128 * 1024 * 1024
)
PDF_DOCX_CPU_LIMIT_SECONDS = _bounded_env_int("NDA_PDF_DOCX_CPU_LIMIT_SECONDS", 120, minimum=1)
MAX_PDF_DOCX_PAGES = _bounded_env_int("NDA_PDF_DOCX_MAX_PAGES", 200, minimum=1)

# Reconstruction cache bounds. Keyed on (owner, source bytes) so two tenants
# never share an entry (no cross-tenant leak) and bounded by entry count with
# LRU eviction by directory mtime.
PDF_DOCX_CACHE_VERSION = "pdf-docx-reconstruction:v1"
PDF_DOCX_CACHE_DIRNAME = "pdf-docx-reconstruction"
MAX_PDF_DOCX_CACHE_ENTRIES = _bounded_env_int(
    "NDA_PDF_DOCX_CACHE_ENTRIES", 256, minimum=1
)
ANONYMOUS_CACHE_OWNER = "anonymous"


class PdfDocxReconstructionError(RuntimeError):
    pass


class PdfDocxReconstructionUnavailableError(PdfDocxReconstructionError):
    pass


class PdfDocxReconstructionFailedError(PdfDocxReconstructionError):
    pass


class PdfDocxReconstructionBusy(PdfDocxReconstructionError):
    """Raised when no conversion slot is free within the queue wait window.

    The route layer should map this to HTTP 503 with Retry-After so a burst of
    reconstruction requests sheds load instead of spawning an unbounded number of
    pdf2docx conversions.
    """

    def __init__(self) -> None:
        super().__init__(
            "PDF-to-Word reconstruction is at capacity; please retry shortly."
        )


class PdfDocxReconstructionTooLargeError(PdfDocxReconstructionFailedError):
    """Raised when a PDF exceeds the page cap, before the heavy convert runs."""


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


# Bounds concurrent pdf2docx conversions process-wide. BoundedSemaphore so an
# over-release is a programming error rather than silently widening the cap.
_PDF_DOCX_CONVERSION_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_PDF_DOCX_CONVERSIONS)


def _pdf_docx_resource_preexec() -> None:  # pragma: no cover - runs in the child
    """Constrain the pdf2docx child before exec: own session + RLIMIT_AS/CPU.

    Mirrors document_rendering._soffice_resource_preexec. os.setsid() puts the
    child in a fresh process group so a timeout can signal the whole group;
    RLIMIT_AS caps address space and RLIMIT_CPU caps CPU-seconds as a backstop to
    the wall-clock timeout. Failures here must not crash the parent.
    """
    try:
        os.setsid()
    except OSError:
        pass
    if resource is None:
        return
    for limit_name, limit_value in (
        ("RLIMIT_AS", PDF_DOCX_MEMORY_LIMIT_BYTES),
        ("RLIMIT_CPU", PDF_DOCX_CPU_LIMIT_SECONDS),
    ):
        limit = getattr(resource, limit_name, None)
        if limit is None:
            continue
        try:
            soft, hard = resource.getrlimit(limit)
            new_hard = limit_value if hard == resource.RLIM_INFINITY else min(limit_value, hard)
            resource.setrlimit(limit, (min(limit_value, new_hard), new_hard))
        except (ValueError, OSError):
            # Some platforms (notably macOS RLIMIT_AS) reject the limit; the
            # wall-clock timeout + semaphore still bound the blast radius.
            pass


# The conversion runs pdf2docx in a child Python process so the same
# RLIMIT/process-group/wall-clock-kill discipline as the soffice path applies to
# an otherwise in-process library. The child reads the source PDF path and the
# output DOCX path from argv, converts page 0..end, and exits non-zero on any
# failure (which the parent maps to a reconstruction failure).
_PDF_DOCX_CHILD_SCRIPT = r"""
import sys
try:
    from pdf2docx import Converter
except Exception as exc:  # noqa: BLE001
    sys.stderr.write("pdf2docx-unavailable: %r" % (exc,))
    raise SystemExit(3)
source_path, output_path = sys.argv[1], sys.argv[2]
converter = Converter(source_path)
try:
    converter.convert(output_path, start=0, end=None)
finally:
    converter.close()
"""

PDF_DOCX_CHILD_UNAVAILABLE_RETURNCODE = 3

# Sanitize the pdf2docx child's stderr before it reaches a user-facing error. The
# child can emit a full Python traceback (absolute tmp paths + source-line
# snippets) plus pdf2docx's own [INFO]/[WARNING] log lines; surfacing that verbatim
# leaks internal filesystem paths and buries the actual cause in log noise. We keep
# only a final exception-summary line with any filesystem paths redacted.
_MAX_RECONSTRUCTION_DETAIL_CHARS = 200
_RECONSTRUCTION_LOG_PREFIX = re.compile(r"^\[(?:INFO|WARNING|DEBUG|ERROR|CRITICAL)\]", re.IGNORECASE)
_RECONSTRUCTION_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):")
_RECONSTRUCTION_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt):")
# Absolute POSIX (/foo/bar) or Windows (C:\foo\bar) path tokens.
_RECONSTRUCTION_PATH_TOKEN = re.compile(r"(?:/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)")


def _sanitize_reconstruction_stderr(raw: str) -> str:
    """Reduce a pdf2docx child's decoded stderr to a short, safe detail string.

    Drops [INFO]/[WARNING]/... log lines, the ``Traceback`` header, and every
    indented traceback frame / source snippet; from what remains it prefers the
    final exception-summary line ("SomeError: message"), redacts filesystem paths to
    ``<path>``, collapses whitespace and truncates. Returns "" when nothing safe is
    left (the caller then surfaces only the generic failure message)."""
    kept: list[str] = []
    for line in str(raw or "").splitlines():
        # Indented lines are traceback File-frames and their source snippets: drop.
        if line != line.lstrip():
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _RECONSTRUCTION_LOG_PREFIX.match(stripped):
            continue
        if _RECONSTRUCTION_TRACEBACK_HEADER.match(stripped):
            continue
        kept.append(stripped)
    if not kept:
        return ""
    summary = next(
        (line for line in reversed(kept) if _RECONSTRUCTION_EXCEPTION_LINE.match(line)),
        kept[-1],
    )
    summary = _RECONSTRUCTION_PATH_TOKEN.sub("<path>", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    return summary[:_MAX_RECONSTRUCTION_DETAIL_CHARS].strip()


class Pdf2DocxConverter:
    """Subprocess-isolated pdf2docx converter.

    Runs the (in-process, unbounded) pdf2docx library inside a child Python
    process group so a hung or runaway conversion is bounded by RLIMIT_AS,
    RLIMIT_CPU, and a hard wall-clock timeout that SIGKILLs the whole group.
    """

    name = "pdf2docx"

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else PDF_DOCX_TIMEOUT_SECONDS

    def is_available(self) -> bool:
        return importlib.util.find_spec("pdf2docx") is not None

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        if not self.is_available():
            raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE)
        command = [
            self.python_executable,
            "-c",
            _PDF_DOCX_CHILD_SCRIPT,
            str(source_path),
            str(output_path),
        ]
        returncode, _stdout, stderr_bytes = _run_pdf_docx_child(
            command,
            cwd=str(source_path.parent),
            timeout_seconds=self.timeout_seconds,
        )
        if returncode == PDF_DOCX_CHILD_UNAVAILABLE_RETURNCODE:
            raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE)
        if returncode != 0:
            detail = _sanitize_reconstruction_stderr(
                stderr_bytes.decode("utf-8", errors="replace")
            )
            raise PdfDocxReconstructionFailedError(
                PDF_DOCX_RECONSTRUCTION_FAILED_MESSAGE + (f" ({detail})" if detail else "")
            )


def _run_pdf_docx_child(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: int,
) -> tuple[int, bytes, bytes]:
    """Run the pdf2docx child in its own process group; kill the group on timeout.

    Returns (returncode, stdout, stderr). Raises PdfDocxReconstructionFailedError
    if the wall-clock budget is hit (the whole child process group is SIGKILLed
    first), and PdfDocxReconstructionUnavailableError if the interpreter cannot be
    launched.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_pdf_docx_resource_preexec if os.name == "posix" else None,
        )
    except FileNotFoundError as exc:
        raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE) from exc

    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise PdfDocxReconstructionFailedError(
            f"PDF-to-Word reconstruction timed out after {timeout_seconds} seconds."
        ) from exc

    return process.returncode, stdout_bytes, stderr_bytes


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's process group, falling back to the child itself."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


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
    owner_user_id: str = "",
    cache_dir: Path | None = None,
    max_pages: int | None = None,
) -> ReconstructedDocx:
    """Reconstruct a PDF into an editable DOCX, hardened against DoS.

    Backwards compatible: callers that pass only (pdf_bytes, source_filename)
    keep the prior behavior plus the new bounds (semaphore + page cap +
    subprocess RLIMIT/timeout). Pass ``owner_user_id`` (and optionally
    ``cache_dir``) to enable the owner-keyed LRU cache so a repeat GET of the
    same source by the same tenant reuses the prior reconstruction.

    Raises:
        PdfDocxReconstructionUnavailableError - the pdf2docx engine is missing.
        PdfDocxReconstructionBusy - no conversion slot freed within the wait.
        PdfDocxReconstructionTooLargeError - the PDF exceeds the page cap.
        PdfDocxReconstructionFailedError - conversion failed / timed out / output
            was not a valid DOCX.
    """
    active_converter = converter or Pdf2DocxConverter()
    if not active_converter.is_available():
        raise PdfDocxReconstructionUnavailableError(PDF_DOCX_RECONSTRUCTION_UNAVAILABLE_MESSAGE)

    page_cap = MAX_PDF_DOCX_PAGES if max_pages is None else max(1, int(max_pages))
    # Cheap structural guard BEFORE the expensive convert: an attacker-supplied
    # page-dense PDF is rejected on page count rather than burning a slot.
    _enforce_page_cap(pdf_bytes, page_cap)

    converter_name = getattr(active_converter, "name", "unknown")

    # Cache lookup (owner-keyed). A hit skips both the slot and the convert.
    cache_lookup = _cache_paths(pdf_bytes, owner_user_id=owner_user_id, cache_dir=cache_dir)
    if cache_lookup is not None:
        cached = _read_cached_reconstruction(cache_lookup, source_filename, converter_name)
        if cached is not None:
            return cached

    # Backpressure: shed load rather than spawn an unbounded number of children.
    if not _PDF_DOCX_CONVERSION_SEMAPHORE.acquire(timeout=PDF_DOCX_QUEUE_WAIT_SECONDS):
        raise PdfDocxReconstructionBusy()
    try:
        data = _convert_within_slot(active_converter, pdf_bytes)
    finally:
        _PDF_DOCX_CONVERSION_SEMAPHORE.release()

    result = ReconstructedDocx(
        data=data,
        filename=reconstructed_docx_filename(source_filename),
        headers={
            "X-PDF-DOCX-Reconstruction": PDF_DOCX_RECONSTRUCTION_HEADER,
            "X-PDF-DOCX-Converter": converter_name,
        },
    )

    if cache_lookup is not None:
        _write_cached_reconstruction(cache_lookup, data)

    return result


def _convert_within_slot(active_converter: PdfToDocxConverter, pdf_bytes: bytes) -> bytes:
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
    return data


def _enforce_page_cap(pdf_bytes: bytes, page_cap: int) -> None:
    """Reject a PDF with more pages than the cap, BEFORE the heavy convert.

    Uses PyMuPDF (already a runtime dependency) to count pages cheaply. If
    PyMuPDF is unavailable or the PDF cannot be opened, we fail open on the
    page-count guard (the convert path's own validation + timeout still bound the
    blast radius) rather than block a reconstruction the engine could handle.
    """
    fitz = _load_fitz_module()
    if fitz is None:
        return
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            page_count = getattr(document, "page_count", None)
            if page_count is None:
                page_count = len(document)
            page_count = int(page_count)
    except Exception:
        return
    if page_count > page_cap:
        raise PdfDocxReconstructionTooLargeError(
            f"PDF has {page_count} pages, exceeding the {page_cap}-page reconstruction cap."
        )


def _load_fitz_module():
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None
    return fitz


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


# --------------------------------------------------------------------------- #
# Owner-keyed LRU reconstruction cache
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _CachePaths:
    cache_root: Path
    entry_dir: Path
    docx_path: Path
    metadata_path: Path
    source_sha256: str


def _normalized_cache_owner(owner_user_id: str) -> str:
    owner = str(owner_user_id or "").strip()
    return owner or ANONYMOUS_CACHE_OWNER


def pdf_docx_cache_dir(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return (matter_store.DATA_DIR / "cache" / PDF_DOCX_CACHE_DIRNAME).resolve()


def pdf_docx_cache_key(pdf_bytes: bytes, *, owner_user_id: str = "") -> str:
    owner_token = _normalized_cache_owner(owner_user_id)
    source_hash = hashlib.sha256(pdf_bytes).hexdigest()
    key_hash = hashlib.sha256(
        f"{PDF_DOCX_CACHE_VERSION}:{owner_token}:{source_hash}".encode("utf-8")
    ).hexdigest()
    return f"pdfdocx-{key_hash}"


def _cache_entry_dir(cache_root: Path, cache_key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+", cache_key):
        raise ValueError("PDF-to-Word reconstruction cache key contains unsafe characters.")
    resolved_root = Path(cache_root).resolve()
    resolved_entry = (resolved_root / cache_key).resolve()
    if resolved_entry != resolved_root and resolved_root not in resolved_entry.parents:
        raise ValueError("PDF-to-Word reconstruction cache path escapes the cache root.")
    return resolved_entry


def _cache_paths(
    pdf_bytes: bytes,
    *,
    owner_user_id: str,
    cache_dir: Path | None,
) -> _CachePaths | None:
    """Resolve cache paths, or None when caching is disabled.

    Caching is opt-in: it is enabled when the caller supplies an explicit
    ``cache_dir`` OR an ``owner_user_id`` (so the existing positional callers
    that pass neither keep their prior non-caching behavior unchanged).
    """
    if cache_dir is None and not str(owner_user_id or "").strip():
        return None
    try:
        cache_root = pdf_docx_cache_dir(cache_dir)
        cache_key = pdf_docx_cache_key(pdf_bytes, owner_user_id=owner_user_id)
        entry_dir = _cache_entry_dir(cache_root, cache_key)
    except (ValueError, OSError):
        return None
    return _CachePaths(
        cache_root=cache_root,
        entry_dir=entry_dir,
        docx_path=entry_dir / "reconstructed.docx",
        metadata_path=entry_dir / "metadata.json",
        source_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )


def _read_cached_reconstruction(
    paths: _CachePaths,
    source_filename: str,
    converter_name: str,
) -> ReconstructedDocx | None:
    try:
        metadata = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(metadata, dict)
        or metadata.get("cache_version") != PDF_DOCX_CACHE_VERSION
        or metadata.get("source_sha256") != paths.source_sha256
        or not paths.docx_path.is_file()
    ):
        return None
    try:
        data = paths.docx_path.read_bytes()
    except OSError:
        return None
    _touch_cache_entry(paths.entry_dir)
    return ReconstructedDocx(
        data=data,
        filename=reconstructed_docx_filename(source_filename),
        headers={
            "X-PDF-DOCX-Reconstruction": PDF_DOCX_RECONSTRUCTION_HEADER,
            "X-PDF-DOCX-Converter": converter_name,
        },
    )


def _write_cached_reconstruction(paths: _CachePaths, data: bytes) -> None:
    """Persist a reconstruction to the cache, best-effort (never raises)."""
    try:
        paths.entry_dir.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomic(paths.docx_path, data)
        metadata = json.dumps(
            {
                "cache_version": PDF_DOCX_CACHE_VERSION,
                "source_sha256": paths.source_sha256,
            },
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _write_bytes_atomic(paths.metadata_path, metadata)
    except OSError:
        return
    _enforce_cache_bound(paths.cache_root, keep=paths.entry_dir)


def _touch_cache_entry(entry_dir: Path) -> None:
    try:
        os.utime(entry_dir, None)
    except OSError:
        pass


def _enforce_cache_bound(
    cache_root: Path,
    *,
    keep: Path | None = None,
    max_entries: int | None = None,
) -> None:
    """Evict least-recently-used entries so the cache stays within max_entries.

    Recency is the entry directory's mtime (bumped on every cache hit). The
    just-written ``keep`` entry is never evicted. Best-effort: a filesystem error
    while pruning must never fail the reconstruction that triggered it.
    """
    if max_entries is None:
        max_entries = MAX_PDF_DOCX_CACHE_ENTRIES
    try:
        entries = [child for child in Path(cache_root).iterdir() if child.is_dir()]
    except OSError:
        return
    if len(entries) <= max_entries:
        return
    keep_resolved = keep.resolve() if keep is not None else None

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for entry in sorted(entries, key=_mtime):
        if len(entries) <= max_entries:
            break
        if keep_resolved is not None and entry.resolve() == keep_resolved:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        entries.remove(entry)


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


def _reset_pdf_docx_semaphore_for_tests() -> None:  # pragma: no cover - test helper
    global _PDF_DOCX_CONVERSION_SEMAPHORE
    _PDF_DOCX_CONVERSION_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_PDF_DOCX_CONVERSIONS)
