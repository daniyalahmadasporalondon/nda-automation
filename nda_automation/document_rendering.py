from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:  # POSIX-only; absent on Windows. Guards the per-child RLIMIT preexec_fn.
    import resource
except ImportError:  # pragma: no cover - non-POSIX fallback
    resource = None  # type: ignore[assignment]

from . import matter_store
from .durable_io import fsync_parent_directory
from .phase_observability import RENDER_PHASE_EVENT, PhaseTimer

LOGGER = logging.getLogger(__name__)

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
PDF_POINTS_PER_INCH = 72

# On-screen preview page-image DPI. 200 DPI is visually indistinguishable from
# 288 at display zoom for a screen preview, while cutting the rasterized pixmap
# area — and therefore the rasterize-phase cost and on-disk page-image cache —
# by ~(200/288)**2 ~= 52% (a ~48% reduction). This is purely a raster-resolution
# knob and does NOT affect export or evidence fidelity: the marked-up-PDF EXPORT
# bakes annotations onto the ORIGINAL PDF bytes in PDF coordinate space (see
# annotated_pdf_export.build_annotated_pdf, which opens the source PDF and calls
# page.add_highlight_annot on rects), and the AI-evidence-highlight path maps via
# fitz TEXT SEARCH (page.search_for / get_text("words")) — neither reads the
# rasterized PNGs. Deploy-time override: NDA_PAGE_IMAGE_DPI (clamped to
# [MIN_PAGE_IMAGE_DPI, MAX_PAGE_IMAGE_DPI]).
DEFAULT_PAGE_IMAGE_DPI_FALLBACK = 200
PAGE_IMAGE_DPI_ENV_VAR = "NDA_PAGE_IMAGE_DPI"
MAX_PAGE_IMAGE_DPI = 600

# Rasterization resource bounds. A pixmap costs width_px * height_px * channels
# bytes, and width_px = mediabox_pts / 72 * dpi. An attacker-controlled MediaBox
# rendered at a fixed DPI can therefore demand multi-gigabyte pixmaps and OOM a
# small instance, so every page is bounded two ways before it is rasterized:
#   * MAX_RASTERIZED_PAGES caps how many pages we will ever rasterize.
#   * MAX_PAGE_PIXMAP_BYTES caps a single page's pixmap; the DPI is clamped down
#     to whatever fits the budget, and a page whose budget cannot be met even at
#     MIN_PAGE_IMAGE_DPI is rejected rather than rasterized.
MAX_RASTERIZED_PAGES = 200
MAX_PAGE_PIXMAP_BYTES = 96 * 1024 * 1024
MIN_PAGE_IMAGE_DPI = 36
RASTERIZED_PIXMAP_CHANNELS = 3  # RGB, alpha=False; pinned via fitz.csRGB at render

# Process-wide rasterization concurrency bound. The per-page pixmap budget
# (MAX_PAGE_PIXMAP_BYTES = 96 MiB) and MAX_RASTERIZED_PAGES cap bound a SINGLE
# render, and the MatterRenderCoordinator dedupes concurrent renders of the SAME
# matter -- but nothing bounded rasterize ACROSS matters. N users opening N
# different matters ran N concurrent full-document rasterize loops, each
# transiently holding a ~96 MiB pixmap PLUS its same-order PNG encode buffer, so
# N could stack ~N * ~200 MiB with no ceiling -- the top OOM risk on the 2 GiB /
# single-CPU box. We serialize rasterize to ONE slot process-wide: peak pixmap
# memory is a single page's budget regardless of how many matters are viewed at
# once. soffice conversion uses 2 slots, but each of those is RLIMIT_AS-capped at
# 1 GiB in a separate process; rasterize runs IN-PROCESS against the shared Python
# heap, so 1 (not 2) is the deliberate choice. A viewer that cannot get the slot
# within the wait window gets the RENDERING status the FE poller already retries
# on -- never a 500 and never an unbounded wait.
MAX_CONCURRENT_RASTERIZE_RENDERS = 1
RASTERIZE_QUEUE_WAIT_SECONDS = 5.0


def _resolve_default_page_image_dpi() -> int:
    """Preview DPI default, overridable via NDA_PAGE_IMAGE_DPI.

    Garbage or out-of-range values fall back to / are clamped into
    [MIN_PAGE_IMAGE_DPI, MAX_PAGE_IMAGE_DPI] so a bad env value can never request
    a zero/negative DPI (which the budget math rejects) or an unbounded pixmap.
    """
    raw = os.environ.get(PAGE_IMAGE_DPI_ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_PAGE_IMAGE_DPI_FALLBACK
    try:
        dpi = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_IMAGE_DPI_FALLBACK
    if dpi <= 0:
        return DEFAULT_PAGE_IMAGE_DPI_FALLBACK
    return max(MIN_PAGE_IMAGE_DPI, min(dpi, MAX_PAGE_IMAGE_DPI))


# Resolved once at import. The preview DPI is a deploy-time knob, not a per-request
# one, so reading the env at module load keeps the ~15 default-arg call sites
# consistent within a process.
DEFAULT_PAGE_IMAGE_DPI = _resolve_default_page_image_dpi()

# LibreOffice/soffice conversion bounds. soffice runs inline on the request
# thread, so N concurrent viewers would otherwise spawn N heavyweight processes
# and OOM a small instance. Conversions are gated three ways:
#   * MAX_CONCURRENT_SOFFICE_CONVERSIONS bounds how many run at once; callers
#     that cannot acquire a slot within CONVERSION_QUEUE_WAIT_SECONDS get a
#     DocxConverterBusy (the route maps this to 503 backpressure).
#   * Each child is launched in its own process group with RLIMIT_AS/RLIMIT_CPU
#     so a runaway conversion cannot exhaust memory or CPU.
#   * A hard wall-clock timeout kills the whole process group (soffice forks
#     children that outlive a plain Popen.kill()).
MAX_CONCURRENT_SOFFICE_CONVERSIONS = 2
CONVERSION_QUEUE_WAIT_SECONDS = 5.0
CONVERSION_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024  # 1 GiB address space per child
CONVERSION_CPU_LIMIT_SECONDS = 60  # CPU-seconds; backstops the wall-clock timeout

# Render-cache bounds. The cache was unbounded, never evicted, and keyed only on
# document bytes — so two tenants uploading the same NDA shared one entry (a
# cross-tenant leak) and a flood of distinct documents grew the cache without
# limit. The key now folds in the owner (no cross-tenant hit is possible) and
# the cache is bounded by entry count with LRU eviction by directory mtime.
MAX_RENDER_CACHE_ENTRIES = 256
ANONYMOUS_CACHE_OWNER = "anonymous"

READY_STATUS = "ready"
UNAVAILABLE_STATUS = "unavailable"
UNSUPPORTED_STATUS = "unsupported"
ERROR_STATUS = "error"
RENDERING_STATUS = "rendering"  # background render in flight; poller should retry


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


class DocxConverterBusy(DocumentRenderingError):
    """Raised when no conversion slot is free within the queue wait window.

    The route layer maps this to HTTP 503 with Retry-After so a burst of viewers
    sheds load instead of spawning an unbounded number of soffice processes.
    """

    def __init__(self) -> None:
        super().__init__(
            "converter_busy",
            "DOCX conversion is at capacity; please retry shortly.",
        )


class PdfPageRendererUnavailable(DocumentRenderingError):
    def __init__(self) -> None:
        super().__init__(
            "page_renderer_unavailable",
            "PDF page image rendering requires PyMuPDF/fitz, but it is not installed.",
        )


class PdfPageTooLargeToRasterize(DocumentRenderingError):
    def __init__(self, message: str) -> None:
        super().__init__("page_too_large_to_rasterize", message)


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


@dataclass(frozen=True)
class DocumentRenderResult:
    rendered: RenderedDocument
    page_manifest: RenderedPdfPageImageManifest | None = None


class PdfPageRenderer(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]: ...


# Bounds concurrent soffice processes process-wide. BoundedSemaphore so an
# over-release is a programming error rather than silently widening the cap.
_SOFFICE_CONVERSION_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_SOFFICE_CONVERSIONS)

# Bounds concurrent PyMuPDF rasterize loops process-wide (see the
# MAX_CONCURRENT_RASTERIZE_RENDERS rationale). Same idiom as the soffice
# semaphore: a bounded acquire with a timeout, degrading to a retryable status
# rather than blocking unbounded. BoundedSemaphore so an over-release trips loudly.
_RASTERIZE_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_RASTERIZE_RENDERS)


def _soffice_resource_preexec() -> None:  # pragma: no cover - runs in the child
    """Constrain the soffice child before exec: own session + RLIMIT_AS/CPU.

    Runs between fork and exec in the child only. os.setsid() puts the child in
    a fresh process group so a timeout can signal the whole group (soffice forks
    helpers). RLIMIT_AS caps address space; RLIMIT_CPU caps CPU-seconds as a
    backstop to the wall-clock timeout. Failures here must not crash the parent.
    """
    try:
        os.setsid()
    except OSError:
        pass
    if resource is None:
        return
    for limit_name, limit_value in (
        ("RLIMIT_AS", CONVERSION_MEMORY_LIMIT_BYTES),
        ("RLIMIT_CPU", CONVERSION_CPU_LIMIT_SECONDS),
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


class LibreOfficeDocxConverter:
    name = "libreoffice"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("soffice") or shutil.which("libreoffice")

    def is_available(self) -> bool:
        return bool(self.executable)

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        if not self.executable:
            raise DocxConverterUnavailable()

        # Backpressure: shed load rather than spawn an unbounded number of
        # processes. A short queue absorbs transient overlap; past that the
        # caller gets a busy signal the route turns into a 503.
        if not _SOFFICE_CONVERSION_SEMAPHORE.acquire(timeout=CONVERSION_QUEUE_WAIT_SECONDS):
            raise DocxConverterBusy()
        try:
            return self._convert_within_slot(source_path, output_dir, timeout_seconds=timeout_seconds)
        finally:
            _SOFFICE_CONVERSION_SEMAPHORE.release()

    def _convert_within_slot(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        command = [
            self.executable,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source_path),
        ]
        returncode, stdout_bytes, stderr_bytes = _run_soffice_command(
            command,
            cwd=str(output_dir),
            timeout_seconds=timeout_seconds,
        )

        if returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            detail = stderr or stdout or f"LibreOffice exited with status {returncode}."
            raise DocumentRenderingError("conversion_failed", _truncate_error_detail(detail))

        expected_output = output_dir / f"{source_path.stem}.pdf"
        if expected_output.is_file():
            return expected_output
        pdf_outputs = sorted(output_dir.glob("*.pdf"))
        if pdf_outputs:
            return pdf_outputs[0]
        raise DocumentRenderingError("conversion_missing_output", "DOCX conversion finished but did not produce a PDF.")


def _run_soffice_command(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: int,
) -> tuple[int, bytes, bytes]:
    """Run soffice in its own process group; kill the whole group on timeout.

    soffice forks helper processes that survive a plain Popen.kill(), so a hung
    conversion is killed via the process group. Returns (returncode, stdout,
    stderr). Raises DocxConverterUnavailable if the executable is missing and
    DocumentRenderingError("conversion_timeout") if the wall-clock budget is hit.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_soffice_resource_preexec,
        )
    except FileNotFoundError as exc:
        raise DocxConverterUnavailable() from exc

    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        # Drain pipes so the killed child does not leak file descriptors.
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise DocumentRenderingError(
            "conversion_timeout",
            f"DOCX to PDF conversion timed out after {timeout_seconds} seconds.",
        ) from exc

    return process.returncode, stdout_bytes, stderr_bytes


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's process group, falling back to the child itself."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def _form_widget_has_value(widget: Any) -> bool:
    """True when a form-field widget carries a value worth baking into the page.

    A checkbox/radio *off* state (``Off``/``/Off``) and an empty value carry no
    information for the reviewer, so they are not treated as "filled" — an empty
    template must stay byte-identical (see ``_flatten_acroform_widgets``).
    """
    value = getattr(widget, "field_value", None)
    if value is None or value is False:
        return False
    if value is True:
        # A checked checkbox/radio exports as True in fitz; it renders a mark.
        return True
    text = str(value).strip()
    if not text:
        return False
    if text in ("Off", "/Off"):
        return False
    return True


def _flatten_acroform_widgets(document: Any, fitz_module: Any) -> bool:
    """Bake filled AcroForm (fillable-PDF) field values into page content.

    A fillable NDA carries the party's entries — names, dates, amounts, checkbox
    marks — in interactive form-field ``/V`` values that live ONLY in widget
    annotations, not in the page content stream. The rasterized "Original" page a
    reviewer sees is therefore a BLANK template unless those values are rendered
    in. ``fitz.Document.bake(widgets=True)`` draws each field's value into the page
    content at its widget ``/Rect`` on the correct page — text fields, checkbox
    marks and choice-field selections alike — using the field's own appearance, so
    the reviewer sees the COMPLETED document.

    CONSERVATIVE + fail-open by construction:

      * No-op (returns ``False``, document untouched) when the PDF has no AcroForm,
        or when no widget carries a value — so a NORMAL PDF and an EMPTY template
        both rasterize byte-identically.
      * ``bake(annots=False, ...)`` bakes ONLY the form widgets; every other
        annotation is left exactly as the existing rasterize path rendered it.
      * Any error leaves the document untouched and returns ``False``; rasterize
        then proceeds exactly as before (and the values still reach the AI via the
        extractor's "Form field values" section — the B1 fallback).

    Returns ``True`` only when a bake actually ran.
    """

    try:
        if not getattr(document, "is_form_pdf", False):
            return False
    except Exception:
        return False

    # Only bake when at least one widget is actually filled. Iterating widgets is
    # cheap relative to rasterizing and guarantees an all-empty form template is
    # never mutated (byte-identical).
    try:
        has_value = False
        for page in document:
            widgets = page.widgets()
            if not widgets:
                continue
            for widget in widgets:
                if _form_widget_has_value(widget):
                    has_value = True
                    break
            if has_value:
                break
        if not has_value:
            return False
    except Exception:
        return False

    try:
        # widgets=True bakes form-field appearances into content; annots=False so
        # non-widget annotations keep the exact rendering the rasterize path
        # already produced. fitz regenerates a missing/stale appearance from /V
        # during the bake, so a value shows even when the source appearance stream
        # was blank (the classic "filled form renders empty" case).
        document.bake(annots=False, widgets=True)
        return True
    except Exception:
        return False


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
        # soffice-produced (DOCX->PDF) PDFs carry a tagged-PDF structure tree that
        # is frequently malformed (e.g. "No common ancestor in structure tree").
        # MuPDF walks that tree while opening and rasterizing and prints a
        # non-fatal "MuPDF error: ..." line to stderr for each defect, which is
        # harmless (the pixmap still renders byte-identically) but floods the prod
        # logs. We silence MuPDF's stderr printing only for the duration of the
        # open+rasterize, then restore it, so genuine MuPDF errors elsewhere still
        # surface. The rendered image bytes are unaffected by this toggle.
        with _suppressed_mupdf_errors(self._fitz):
            document = self._fitz.open(str(pdf_path))
            try:
                # Fillable-PDF prefill: bake filled AcroForm field values into the
                # page content BEFORE rasterizing so a fillable NDA renders as a
                # COMPLETED document (the party's names/dates/amounts appear in the
                # blanks), not a blank template. No-op + byte-identical for a normal
                # PDF (no AcroForm) or an empty template; never raises.
                _flatten_acroform_widgets(document, self._fitz)
                page_count = getattr(document, "page_count", None)
                if page_count is None:
                    page_count = len(document)
                page_count = int(page_count)
                if page_count > MAX_RASTERIZED_PAGES:
                    raise PdfPageTooLargeToRasterize(
                        f"PDF has {page_count} pages, exceeding the {MAX_RASTERIZED_PAGES}-page rasterization cap."
                    )
                pages: list[RenderedPdfPageImage] = []
                for page_index in range(page_count):
                    page = document.load_page(page_index)
                    # Clamp the DPI per page so the pixmap stays within the byte
                    # budget; an attacker-sized MediaBox is rendered at a reduced
                    # DPI rather than blowing up the pixmap, and is rejected
                    # outright if even MIN_PAGE_IMAGE_DPI would not fit.
                    width_pts, height_pts = _page_rect_points(page)
                    effective_dpi = _budgeted_page_dpi(width_pts, height_pts, requested_dpi=dpi)
                    scale = _page_image_scale(effective_dpi)
                    matrix = self._fitz.Matrix(scale, scale)
                    # Pin RGB (3 channels, no alpha) explicitly so the pixmap's
                    # byte cost matches RASTERIZED_PIXMAP_CHANNELS=3 that the
                    # budget math (_budgeted_page_dpi) assumes. Without this the
                    # colorspace is fitz's default, which a future build could
                    # change to 4-channel RGBA — silently under-counting the
                    # budget by 33% and letting an out-of-budget pixmap through.
                    pixmap = page.get_pixmap(matrix=matrix, colorspace=self._fitz.csRGB, alpha=False)
                    image_path = output_dir / _page_image_filename(page_index + 1)
                    _write_bytes_atomic(image_path, pixmap.tobytes("png"))
                    pages.append(
                        RenderedPdfPageImage(
                            page_number=page_index + 1,
                            image_path=image_path,
                            width=int(pixmap.width),
                            height=int(pixmap.height),
                            dpi=effective_dpi,
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
    owner_user_id: str = "",
) -> RenderedDocument:
    source_path = Path(source_path)
    return render_source_document_to_pdf(
        source_path.read_bytes(),
        source_filename=source_path.name,
        content_type=content_type,
        cache_dir=cache_dir,
        converter=converter,
        timeout_seconds=timeout_seconds,
        owner_user_id=owner_user_id,
    )


def render_source_path_result(
    source_path: Path,
    *,
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    page_renderer: PdfPageRenderer | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult:
    # Per-job render phase timing (convert vs. rasterize vs. total). Best-effort and
    # fail-open — see render_source_document_result for the rationale.
    timer = PhaseTimer(RENDER_PHASE_EVENT)
    with timer.phase("convert"):
        rendered = render_source_path_to_pdf(
            source_path,
            content_type=content_type,
            cache_dir=cache_dir,
            converter=converter,
            timeout_seconds=timeout_seconds,
            owner_user_id=owner_user_id,
        )
    with timer.phase("rasterize"):
        result = document_render_result(
            rendered,
            include_page_images=include_page_images,
            page_renderer=page_renderer,
            dpi=dpi,
        )
    timer.total()
    return result


def render_source_document_result(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    page_renderer: PdfPageRenderer | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult:
    # Per-job phase/wait-time observability for the (often slow) render path: time
    # the DOCX->PDF convert (soffice) and the page rasterize separately, plus the
    # total, with a correlating job id. Best-effort and fail-open — never changes
    # behavior or raises. A cache hit makes a phase near-instant, which is itself
    # the useful signal (the slow time was elsewhere or already paid).
    timer = PhaseTimer(RENDER_PHASE_EVENT)
    with timer.phase("convert"):
        rendered = render_source_document_to_pdf(
            source_bytes,
            source_filename=source_filename,
            content_type=content_type,
            cache_dir=cache_dir,
            converter=converter,
            timeout_seconds=timeout_seconds,
            owner_user_id=owner_user_id,
        )
    with timer.phase("rasterize"):
        result = document_render_result(
            rendered,
            include_page_images=include_page_images,
            page_renderer=page_renderer,
            dpi=dpi,
        )
    timer.total()
    return result


def peek_rendered_source_document(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    owner_user_id: str = "",
) -> RenderedDocument | None:
    source_kind = detect_source_kind(source_bytes, source_filename=source_filename, content_type=content_type)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    cache_key = document_render_cache_key(source_bytes, source_kind=source_kind, owner_user_id=owner_user_id)
    cache_root = document_render_cache_dir(cache_dir)
    entry_dir = cache_entry_dir(cache_root, cache_key)
    pdf_path = entry_dir / "document.pdf"
    metadata_path = entry_dir / "metadata.json"
    cached_metadata = _read_metadata(metadata_path)
    if not _is_ready_cache_hit(
        cached_metadata, pdf_path, source_sha256=source_sha256, source_kind=source_kind, cache_key=cache_key
    ):
        return None
    _touch_cache_entry(entry_dir)
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


def peek_rendered_document(
    source_path: Path,
    *,
    content_type: str = "",
    cache_dir: Path | None = None,
    owner_user_id: str = "",
) -> RenderedDocument | None:
    """Return a ready RenderedDocument from cache WITHOUT rendering, else None.

    Read-only: never converts DOCX or writes the cache, so it is safe to call on
    a polled endpoint. ``None`` means "not yet rendered" — the caller should
    schedule a background render rather than block.
    """
    source_path = Path(source_path)
    try:
        source_bytes = source_path.read_bytes()
    except OSError:
        return None
    return peek_rendered_source_document(
        source_bytes,
        source_filename=source_path.name,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=owner_user_id,
    )


def peek_source_path_render_result(
    source_path: Path,
    *,
    content_type: str = "",
    cache_dir: Path | None = None,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult | None:
    rendered = peek_rendered_document(
        source_path,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=owner_user_id,
    )
    if rendered is None:
        return None
    page_manifest = None
    if include_page_images:
        page_manifest = peek_page_image_manifest(rendered, dpi=dpi)
        if page_manifest is None:
            return None
    return DocumentRenderResult(rendered=rendered, page_manifest=page_manifest)


def peek_source_document_render_result(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult | None:
    rendered = peek_rendered_source_document(
        source_bytes,
        source_filename=source_filename,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=owner_user_id,
    )
    if rendered is None:
        return None
    page_manifest = None
    if include_page_images:
        page_manifest = peek_page_image_manifest(rendered, dpi=dpi)
        if page_manifest is None:
            return None
    return DocumentRenderResult(rendered=rendered, page_manifest=page_manifest)


def poll_source_document_render_result(
    render_id: str,
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    page_renderer: PdfPageRenderer | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
    wait_timeout_seconds: float = 0.0,
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult | None:
    cached = peek_source_document_render_result(
        source_bytes,
        source_filename=source_filename,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=owner_user_id,
        include_page_images=include_page_images,
        dpi=dpi,
    )
    if cached is not None:
        return cached
    job = ensure_source_document_render_in_flight(
        render_id,
        source_bytes,
        source_filename=source_filename,
        content_type=content_type,
        cache_dir=cache_dir,
        converter=converter,
        page_renderer=page_renderer,
        timeout_seconds=timeout_seconds,
        owner_user_id=owner_user_id,
        include_page_images=include_page_images,
        dpi=dpi,
    )
    job.done.wait(timeout=wait_timeout_seconds)
    if not job.is_finished():
        return None
    result = job.result
    if isinstance(result, DocumentRenderResult):
        # Either a READY result or a terminal error-status result -- both are
        # terminal and safe to surface (the FE stops polling and reports status).
        return result
    if job.error is not None:
        # The render raised (rather than returning an error-status result). This
        # is a terminal failure too: synthesize an error render result so the
        # poller surfaces a terminal state instead of restarting the render.
        return _error_document_render_result_for_source(
            source_bytes,
            source_filename=source_filename,
            content_type=content_type,
            cache_dir=cache_dir,
            owner_user_id=owner_user_id,
            error=job.error,
        )
    # A transient no-op (e.g. converter busy -> None). Keep polling.
    return None


def _error_document_render_result_for_source(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    owner_user_id: str = "",
    error: BaseException,
) -> DocumentRenderResult:
    """Build a terminal error DocumentRenderResult for a render that RAISED.

    Used when the background render raised instead of returning an error-status
    result, so a poll can report a terminal (error) state rather than looping on
    "rendering". Carries the source's cache identity for parity with the normal
    render results; no page manifest (there is nothing rendered to paginate).
    """
    source_kind = detect_source_kind(source_bytes, source_filename=source_filename, content_type=content_type)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    cache_key = document_render_cache_key(source_bytes, source_kind=source_kind, owner_user_id=owner_user_id)
    cache_root = document_render_cache_dir(cache_dir)
    metadata_path = cache_entry_dir(cache_root, cache_key) / "metadata.json"
    rendered = _error_result(
        cache_key=cache_key,
        source_sha256=source_sha256,
        source_kind=source_kind,
        cache_dir=cache_root,
        metadata_path=metadata_path,
        code="render_failed",
        message=f"Document rendering failed: {_truncate_error_detail(str(error))}",
    )
    return DocumentRenderResult(rendered=rendered, page_manifest=None)


def peek_page_image_manifest(
    rendered: RenderedDocument,
    *,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> RenderedPdfPageImageManifest | None:
    """Return a ready page-image manifest from cache WITHOUT rasterizing, else None."""
    if rendered.status != READY_STATUS or rendered.pdf_path is None:
        return None
    cache_root = Path(rendered.cache_dir).expanduser().resolve()
    entry_dir = cache_entry_dir(cache_root, rendered.cache_key)
    page_dir = entry_dir / PAGE_IMAGE_DIRNAME
    manifest_path = page_dir / PAGE_IMAGE_MANIFEST_FILENAME
    try:
        pdf_sha256 = _file_sha256(rendered.pdf_path)
    except OSError:
        return None
    return _page_image_manifest_from_metadata(
        _read_metadata(manifest_path),
        cache_key=rendered.cache_key,
        cache_dir=cache_root,
        pdf_path=rendered.pdf_path,
        manifest_path=manifest_path,
        pdf_sha256=pdf_sha256,
        dpi=dpi,
    )


def document_render_result(
    rendered: RenderedDocument,
    *,
    include_page_images: bool = True,
    page_renderer: PdfPageRenderer | None = None,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> DocumentRenderResult:
    page_manifest = None
    if include_page_images and rendered.status == READY_STATUS and rendered.pdf_path is not None:
        page_manifest = render_pdf_page_image_manifest(rendered, renderer=page_renderer, dpi=dpi)
    return DocumentRenderResult(rendered=rendered, page_manifest=page_manifest)


def render_source_document_to_pdf(
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
) -> RenderedDocument:
    source_kind = detect_source_kind(source_bytes, source_filename=source_filename, content_type=content_type)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    cache_key = document_render_cache_key(source_bytes, source_kind=source_kind, owner_user_id=owner_user_id)
    cache_root = document_render_cache_dir(cache_dir)
    entry_dir = cache_entry_dir(cache_root, cache_key)
    pdf_path = entry_dir / "document.pdf"
    metadata_path = entry_dir / "metadata.json"

    cached_metadata = _read_metadata(metadata_path)
    if _is_ready_cache_hit(cached_metadata, pdf_path, source_sha256=source_sha256, source_kind=source_kind, cache_key=cache_key):
        _touch_cache_entry(entry_dir)
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
        _enforce_render_cache_bound(cache_root, keep=entry_dir)
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
    except DocxConverterBusy:
        # Transient backpressure, not a render failure: propagate to the route
        # (-> 503) and do NOT persist it, so the document can still render once
        # capacity frees up rather than being cached as permanently broken.
        raise
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

    _enforce_render_cache_bound(cache_root, keep=entry_dir)
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
    # Process-wide rasterize backpressure. The memory-heavy rasterize loop runs
    # under a single-slot semaphore so N matters cannot rasterize concurrently and
    # OOM the box. A caller that cannot acquire within the wait window gets the
    # RENDERING status the FE poller already retries on (NOT a 500, NOT an
    # unbounded wait). The per-matter dedupe (MatterRenderCoordinator) is unchanged
    # and orthogonal -- this bounds concurrency ACROSS matters.
    if not _RASTERIZE_SEMAPHORE.acquire(timeout=RASTERIZE_QUEUE_WAIT_SECONDS):
        LOGGER.info(
            "PDF rasterize at capacity (%d slot(s)); returning RENDERING for cache_key %s",
            MAX_CONCURRENT_RASTERIZE_RENDERS,
            cache_key,
        )
        return _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code="page_render_busy",
            message="PDF page image rendering is at capacity; please retry shortly.",
            status=RENDERING_STATUS,
        )
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
        # Distinguish a full/read-only/over-quota data dir (the secondary
        # blank-page cause: an unmounted or exhausted /var/data) from a generic
        # write failure, and log it clearly so prod can tell "disk is full" from
        # "something else broke". A storage-exhaustion failure is operational and
        # logged at ERROR; a generic one stays a warning.
        disk_full = _is_storage_exhaustion_error(exc)
        code = "page_cache_storage_exhausted" if disk_full else "page_cache_write_failed"
        if disk_full:
            LOGGER.error(
                "Page image cache write failed: render storage is full/read-only/over-quota "
                "(%s: %s) under %s; rendered pages cannot be cached and may render blank.",
                errno.errorcode.get(exc.errno, exc.errno),
                exc.strerror or exc,
                cache_root,
            )
        else:
            LOGGER.warning("Page image cache write failed (%s): %s", code, exc)
        result = _page_image_error_result(
            cache_key=cache_key,
            cache_dir=cache_root,
            pdf_path=pdf_path,
            manifest_path=manifest_path,
            dpi=dpi,
            code=code,
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
    finally:
        # Released on EVERY exit that acquired the slot (success return above, or
        # any except path falling through here). The acquire-timeout path returned
        # earlier without acquiring, so it never reaches this release.
        _RASTERIZE_SEMAPHORE.release()
    _persist_page_image_failure_metadata(result, pdf_sha256=pdf_sha256, renderer_name=renderer_name)
    return result


def page_image_for_page_number(manifest: RenderedPdfPageImageManifest, page_number: int) -> RenderedPdfPageImage | None:
    for page in manifest.pages:
        if page.page_number == page_number:
            return page
    return None


def page_image_for_render_result(result: DocumentRenderResult, page_number: int) -> RenderedPdfPageImage | None:
    if result.page_manifest is None:
        return None
    return page_image_for_page_number(result.page_manifest, page_number)


def ensure_source_path_render_in_flight(
    render_id: str,
    source_path: Path,
    *,
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    page_renderer: PdfPageRenderer | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> RenderJob:
    def _render():
        try:
            return render_source_path_result(
                source_path,
                content_type=content_type,
                cache_dir=cache_dir,
                converter=converter,
                page_renderer=page_renderer,
                timeout_seconds=timeout_seconds,
                owner_user_id=owner_user_id,
                include_page_images=include_page_images,
                dpi=dpi,
            )
        except DocxConverterBusy:
            return None

    return matter_render_coordinator().ensure_in_flight(render_id, _render)


def ensure_source_document_render_in_flight(
    render_id: str,
    source_bytes: bytes,
    *,
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
    converter: DocxConverter | None = None,
    page_renderer: PdfPageRenderer | None = None,
    timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    owner_user_id: str = "",
    include_page_images: bool = True,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
) -> RenderJob:
    def _render():
        try:
            return render_source_document_result(
                source_bytes,
                source_filename=source_filename,
                content_type=content_type,
                cache_dir=cache_dir,
                converter=converter,
                page_renderer=page_renderer,
                timeout_seconds=timeout_seconds,
                owner_user_id=owner_user_id,
                include_page_images=include_page_images,
                dpi=dpi,
            )
        except DocxConverterBusy:
            return None

    # Fold the source identity into the job so a retained terminal failure is
    # invalidated once this matter's source document changes.
    identity = hashlib.sha256(source_bytes).hexdigest()
    return matter_render_coordinator().ensure_in_flight(render_id, _render, identity=identity)


@dataclass
class RenderJob:
    """A single in-flight background render for one matter.

    ``done`` is set when the worker finishes; ``result``/``error`` hold the
    outcome. Pollers attach to an existing job instead of starting their own, so
    one matter never has more than one render running at a time.
    """

    matter_id: str
    thread: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    started_at: float = field(default_factory=time.monotonic)
    # Source-identity token (e.g. source sha256) captured when the job started, so
    # a retained terminal failure for this matter can be invalidated once the
    # matter's source changes. Empty when the caller did not supply one.
    identity: str = ""

    def is_finished(self) -> bool:
        return self.done.is_set()

    def is_terminal_failure(self) -> bool:
        """True when a finished job represents a terminal render FAILURE.

        A terminal failure is one that re-polling cannot resolve: the render
        raised, or it produced a render result whose status is not READY (error /
        unavailable / unsupported). A ``None`` result is transient backpressure
        (e.g. the DOCX converter was busy) and is NOT terminal -- the poller
        should retry it, not cache it as broken.
        """
        if not self.done.is_set():
            return False
        if self.error is not None:
            return True
        result = self.result
        if isinstance(result, DocumentRenderResult):
            return result.rendered.status != READY_STATUS
        return False


class MatterRenderCoordinator:
    """Runs the expensive render pipeline off the polled status endpoint.

    Invariant: at most one render thread per matter_id is in flight. Concurrent
    pollers for the same matter attach to the existing job (in-flight de-dup)
    rather than each kicking off a duplicate convert+rasterize. The polled
    endpoint never blocks on rasterization — it observes job state and returns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RenderJob] = {}
        # Terminal FAILED jobs are retained here (keyed by matter_id) so a poll
        # arriving AFTER a render failed between polls surfaces the error instead
        # of re-spawning a fresh render -> otherwise the status endpoint can loop
        # on "rendering" forever. A successful render is NOT retained: its warm
        # on-disk cache is the source of truth (peek returns READY).
        self._failed: dict[str, RenderJob] = {}

    def in_flight(self, matter_id: str) -> RenderJob | None:
        with self._lock:
            job = self._jobs.get(matter_id)
            if job is not None and not job.is_finished():
                return job
            return None

    def ensure_in_flight(self, matter_id: str, render_fn: Any, *, identity: str = "") -> RenderJob:
        """Return the running (or retained-failed) job for matter_id, else start one.

        ``render_fn`` is a zero-arg callable performing the full convert +
        rasterize; it runs in a worker thread. If a render is already running
        for this matter the caller attaches to it (de-dup) and no new thread is
        started. If the last render for this matter FAILED terminally, that
        retained failure is returned (no re-spawn) so the poller surfaces a
        terminal error -- unless ``identity`` shows the source has changed since
        the failure, in which case the stale failure is discarded and a fresh
        render starts.
        """
        with self._lock:
            existing = self._jobs.get(matter_id)
            if existing is not None and not existing.is_finished():
                return existing
            retained = self._failed.get(matter_id)
            if retained is not None:
                # A changed source (new identity) invalidates the stale failure so
                # the fixed/replaced document can render; the same source keeps its
                # terminal error rather than pointlessly re-rendering to fail again.
                if identity and retained.identity and identity != retained.identity:
                    del self._failed[matter_id]
                else:
                    return retained
            job = RenderJob(matter_id=matter_id, identity=identity)
            job.thread = threading.Thread(
                target=self._run_job,
                args=(matter_id, job, render_fn),
                name=f"render-{matter_id}",
                daemon=True,
            )
            self._jobs[matter_id] = job
            job.thread.start()
            return job

    def _run_job(self, matter_id: str, job: RenderJob, render_fn: Any) -> None:
        try:
            job.result = render_fn()
        except BaseException as exc:  # noqa: BLE001 - recorded and surfaced to the poller
            job.error = exc
        finally:
            job.done.set()
            with self._lock:
                if self._jobs.get(matter_id) is job:
                    del self._jobs[matter_id]
                    if job.is_terminal_failure():
                        # Retain the terminal failure so the next poll surfaces it
                        # (a terminal state) instead of re-spawning the render.
                        self._failed[matter_id] = job
                    else:
                        # A success (warm cache) or transient no-op clears any
                        # prior retained failure and lets a later poll re-check
                        # the cache / start a fresh render.
                        self._failed.pop(matter_id, None)

    def forget(self, matter_id: str) -> None:
        """Drop any tracked job for a matter (e.g. on matter delete)."""
        with self._lock:
            self._jobs.pop(matter_id, None)
            self._failed.pop(matter_id, None)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._failed.clear()


_MATTER_RENDER_COORDINATOR = MatterRenderCoordinator()


def matter_render_coordinator() -> MatterRenderCoordinator:
    return _MATTER_RENDER_COORDINATOR


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
    owner_user_id: str = "",
    cache_version: str = DOCUMENT_RENDER_CACHE_VERSION,
) -> str:
    # Fold the owner into the key so identical bytes from different tenants never
    # collide on one cache entry. The owner is hashed (not concatenated raw) so
    # it cannot influence the final key's character set or length.
    owner_token = _normalized_cache_owner(owner_user_id)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    key_hash = hashlib.sha256(
        f"{cache_version}:{owner_token}:{source_kind}:{source_hash}".encode("utf-8")
    ).hexdigest()
    return f"{source_kind}-{key_hash}"


def _normalized_cache_owner(owner_user_id: str) -> str:
    owner = str(owner_user_id or "").strip()
    return owner or ANONYMOUS_CACHE_OWNER


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


def _touch_cache_entry(entry_dir: Path) -> None:
    """Mark a cache entry as most-recently-used so LRU eviction spares it."""
    try:
        os.utime(entry_dir, None)
    except OSError:
        pass


def _enforce_render_cache_bound(
    cache_root: Path,
    *,
    keep: Path | None = None,
    max_entries: int | None = None,
) -> None:
    """Evict least-recently-used entries so the cache stays within max_entries.

    Recency is the entry directory's mtime (bumped on every cache hit via
    _touch_cache_entry); the just-written ``keep`` entry is never evicted even if
    a clock skew would otherwise sort it oldest. Best-effort: a filesystem error
    while pruning must never fail the render that triggered it. ``max_entries``
    is read from the module global at call time when not supplied, so the cap
    stays overridable (and patchable in tests).
    """
    if max_entries is None:
        max_entries = MAX_RENDER_CACHE_ENTRIES
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

    # Oldest first; drop until we are back within the bound.
    for entry in sorted(entries, key=_mtime):
        if len(entries) <= max_entries:
            break
        if keep_resolved is not None and entry.resolve() == keep_resolved:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        entries.remove(entry)


def purge_render_cache_for_source(
    source_bytes: bytes,
    *,
    owner_user_id: str = "",
    source_filename: str = "",
    content_type: str = "",
    cache_dir: Path | None = None,
) -> int:
    """Remove the render-cache entry/entries for a specific source document.

    Called when a matter is deleted so its rendered artifacts do not outlive it.
    The per-user cache key is content-derived, so we recompute it for the
    detected source kind (and defensively for both pdf/docx) and remove those
    entry directories under the owner's partition. Returns the number removed.
    Best-effort: never raises on a filesystem error.
    """
    cache_root = document_render_cache_dir(cache_dir)
    detected_kind = detect_source_kind(source_bytes, source_filename=source_filename, content_type=content_type)
    candidate_kinds = {detected_kind, "pdf", "docx"} - {"unknown"}
    removed = 0
    for source_kind in candidate_kinds:
        cache_key = document_render_cache_key(source_bytes, source_kind=source_kind, owner_user_id=owner_user_id)
        try:
            entry_dir = cache_entry_dir(cache_root, cache_key)
        except ValueError:
            continue
        if entry_dir.is_dir():
            shutil.rmtree(entry_dir, ignore_errors=True)
            removed += 1
    return removed


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


# errno values that mean "the storage cannot accept this write" rather than a
# generic IO error: no space, read-only filesystem, over disk quota. These are
# the operational signatures of a full or unmounted /var/data.
_STORAGE_EXHAUSTION_ERRNOS = {
    getattr(errno, name)
    for name in ("ENOSPC", "EROFS", "EDQUOT", "EFBIG")
    if hasattr(errno, name)
}


def _is_storage_exhaustion_error(exc: OSError) -> bool:
    return exc.errno in _STORAGE_EXHAUSTION_ERRNOS


def _load_fitz_module() -> Any | None:
    """Import PyMuPDF (``fitz``), logging — not swallowing — any import failure.

    A page-image render that comes back blank in prod is almost always this:
    PyMuPDF fails to LOAD in the deployed image (e.g. the [pdf,gmail,tables]
    co-resolution lands a broken/ABI-mismatched fitz, or a shared library is
    missing) and this import raises. Returning None silently made a BROKEN fitz
    indistinguishable from an ABSENT one, with zero log trail — so the renderer
    reported "unavailable" and the page came back empty with nothing to debug.
    We now log the exact module + message (at ERROR for a genuine load failure,
    DEBUG for a plain absence) before returning None, so the defect is
    self-evident from prod logs. Behavior is otherwise unchanged: callers still
    treat None as "renderer unavailable".
    """
    try:
        import fitz  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        # PyMuPDF genuinely not installed: expected when the [pdf] extra is off.
        LOGGER.debug("PyMuPDF/fitz is not installed: %s", exc)
        return None
    except Exception as exc:
        # Installed but failed to import (ABI/shared-lib/partial-resolution).
        # This is the silent-blank-page root cause; surface it loudly.
        LOGGER.error(
            "PyMuPDF/fitz is installed but failed to import (%s: %s); "
            "PDF page-image rendering will report unavailable and pages may render blank.",
            exc.__class__.__name__,
            exc,
        )
        return None
    return fitz


@contextlib.contextmanager
def _suppressed_mupdf_errors(fitz_module: Any):
    """Silence MuPDF's non-fatal stderr error printing for the wrapped scope.

    MuPDF prints lines like ``MuPDF error: format error: No common ancestor in
    structure tree`` to stderr when it walks the malformed tagged-PDF structure
    tree that LibreOffice/soffice emits. These are non-fatal (the render still
    completes) but flood the logs. We toggle ``fitz.TOOLS.mupdf_display_errors``
    off for the duration and restore the prior value afterwards, so genuine
    MuPDF errors raised outside this scope still surface.

    Fail-open: if the fitz build lacks the toggle (or it raises), we yield
    without changing anything rather than break rendering.
    """
    tools = getattr(fitz_module, "TOOLS", None)
    toggle = getattr(tools, "mupdf_display_errors", None)
    if toggle is None:
        yield
        return
    try:
        previous = toggle()  # read current state (no-arg getter)
    except Exception:
        # Unexpected signature/build; don't risk rendering on a logging tweak.
        yield
        return
    try:
        toggle(False)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            toggle(previous)


def _page_image_filename(page_number: int) -> str:
    return f"page-{page_number}.png"


def _page_image_scale(dpi: int) -> float:
    return round(float(dpi) / PDF_POINTS_PER_INCH, 4)


def _page_rect_points(page: Any) -> tuple[float, float]:
    """Return a fitz page's (width, height) in PDF points.

    The rect width/height already account for the page's MediaBox/CropBox and
    rotation, which is exactly the geometry that drives pixmap size.
    """
    rect = getattr(page, "rect", None)
    width = getattr(rect, "width", None)
    height = getattr(rect, "height", None)
    try:
        width_pts = float(width)
        height_pts = float(height)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if width_pts < 0 or height_pts < 0:
        return 0.0, 0.0
    return width_pts, height_pts


def _budgeted_page_dpi(
    width_pts: float,
    height_pts: float,
    *,
    requested_dpi: int,
    byte_budget: int = MAX_PAGE_PIXMAP_BYTES,
    channels: int = RASTERIZED_PIXMAP_CHANNELS,
    min_dpi: int = MIN_PAGE_IMAGE_DPI,
) -> int:
    """Largest DPI <= requested whose pixmap fits the per-page byte budget.

    The pixmap costs ``(width_pts/72*dpi) * (height_pts/72*dpi) * channels``
    bytes. We solve that inequality for DPI, never returning more than the
    requested DPI. If the page cannot fit the budget even at ``min_dpi`` the
    MediaBox is pathological and rasterizing it would threaten the instance, so
    we reject rather than rasterize.

    Shared-budget note (decode-RSS ceiling): this RASTERIZE path bounds a single
    page's pixmap to ``MAX_PAGE_PIXMAP_BYTES`` (96 MB, 3 channels). The TEXT
    EXTRACTION path applies the SAME 96 MB decoded-bytes ceiling, expressed as a
    pixel-area budget: ``pdf_text.MAX_PDF_IMAGE_PIXELS`` = 24 Mpix x 4 bytes/pixel
    (RGBA) ~= 96 MB. Both paths therefore cap a decode transient to ~96 MB of
    pixels — well under the 2 GB worker — from one shared number's worth of
    reasoning. The extraction guard rejects PRE-decode (from get_image_info
    dimensions); this guard clamps the DPI down to fit, or rejects only when even
    ``min_dpi`` cannot.
    """
    if requested_dpi <= 0:
        raise ValueError("Page image DPI must be a positive integer.")
    # A zero/unknown page rect cannot overflow the budget; render as requested.
    if width_pts <= 0 or height_pts <= 0:
        return requested_dpi

    area_inches = (width_pts / PDF_POINTS_PER_INCH) * (height_pts / PDF_POINTS_PER_INCH)
    requested_bytes = area_inches * (requested_dpi**2) * channels
    if requested_bytes <= byte_budget:
        return requested_dpi

    # bytes(dpi) = area_inches * dpi^2 * channels <= budget  ->  dpi <= sqrt(...)
    max_dpi = int(math.isqrt(int(byte_budget // (area_inches * channels))))
    if max_dpi < min_dpi:
        page_bytes_at_floor = int(area_inches * (min_dpi**2) * channels)
        raise PdfPageTooLargeToRasterize(
            "PDF page is too large to rasterize within the "
            f"{byte_budget} byte pixmap budget; even {min_dpi} DPI would need "
            f"~{page_bytes_at_floor} bytes."
        )
    return min(requested_dpi, max_dpi)


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
