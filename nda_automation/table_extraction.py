"""Additive, default-OFF table recovery for NDA PDF extraction.

The proven prose path (:func:`nda_automation.pdf_text.extract_pdf_document`)
walks pypdf ``visitor_text`` geometry and a never-merge splitter. That path is
deliberately one-dimensional: it groups text into vertical lines and has no
notion of *columns*. A 2-column signature / notice / term table therefore
flattens — "Signature: Signature: / Name: Name: / Title: Title:" — because the
left and right cells share a baseline and read as one line.

This module recovers those cells WITHOUT touching the prose path. It exposes a
single tool-agnostic interface :func:`extract_tables` that returns structured
cells, so the backend tool can be swapped behind one stable seam.

Backend choice (bake-off outcome)
---------------------------------
The empirical bake-off on the real NDAs (Pismo signature block, Real Transfer
term table) OVERTURNED the initial PyMuPDF guess:

* PyMuPDF ``find_tables`` failed both ways — ``strategy="lines"`` found ZERO of
  these BORDERLESS whitespace tables, and ``strategy="text"`` shattered prose
  words mid-character. Unusable for these signature/term tables.
* **camelot ``stream`` is the winner** — it cleanly recovers the 2-column
  signature columns and the term dates with no word-shatter, and ``stream`` needs
  NO Ghostscript. It is therefore the DEFAULT backend. Its cost is a ~223MB
  dependency tail (opencv + pandas + numpy), so it is an OPTIONAL dependency
  (``pip install nda-automation[tables]``) behind a lazy import.
* **pdfplumber** is the lighter pure-Python alternative, swappable behind the
  same interface (``pip install nda-automation[tables-lite]``).

Default-OFF + graceful degradation
-----------------------------------
Everything is gated behind ``NDA_TABLE_AUGMENTATION_ENABLED`` (default false).
When OFF, :func:`augment_quality_with_tables` is a strict no-op that returns the
quality block unchanged. When ON but the chosen backend's library is not
installed, the pass logs once and no-ops (``status="unavailable"``) — zero
behavior change for the prose path either way. Any backend exception likewise
degrades to "no tables".

Page-region targeting (critical false-positive guard)
-----------------------------------------------------
NEVER run a table extractor blind over the whole document: camelot ``stream`` and
pdfplumber both treat genuine 2-column PROSE pages (e.g. a definitions section)
as tables and shatter them. :func:`select_table_pages` gates extraction to only
the pages whose text contains a structural marker — "Signature", "Initial Term",
"Notice to", etc. A page with no marker is never handed to the extractor.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

LOGGER = logging.getLogger(__name__)

TABLE_AUGMENTATION_ENABLED_ENV = "NDA_TABLE_AUGMENTATION_ENABLED"
TABLE_AUGMENTATION_VERSION = 2

# CONCURRENCY GUARD (load-bearing). Bounds concurrent camelot extractions
# process-wide to ONE, mirroring document_rendering._SOFFICE_CONVERSION_SEMAPHORE.
# Each camelot `stream` pass holds ~40-60MB of transient pandas/opencv buffers; a
# burst of table-bearing uploads running in parallel would stack those peaks and
# threaten the 2GB fit. With the semaphore the transient peak is bounded to a
# single extraction at a time. BoundedSemaphore so an over-release surfaces as a
# programming error rather than silently widening the cap.
_TABLE_EXTRACTION_SEMAPHORE = threading.BoundedSemaphore(1)
# How long a waiting extraction will queue for the single slot before it gives up
# and FAILS OPEN (skips augmentation). The augmentation is a non-critical additive
# pass, so under contention we shed it rather than block the review request.
_EXTRACTION_QUEUE_WAIT_SECONDS = 20.0

# Structural markers that flag a page as likely to carry a recoverable
# signature / notice / term table. The page-selector gate (``select_table_pages``)
# only hands a page to the table extractor when its text matches one of these, so
# a genuine 2-column PROSE page (e.g. a definitions section) is never shattered.
# Word-boundaried, case-insensitive.
_TABLE_PAGE_MARKERS = (
    r"signature",
    r"signed\s+by",
    r"for\s+and\s+on\s+behalf",
    r"in\s+witness\s+whereof",
    r"name\s*:",
    r"title\s*:",
    r"notice(?:s)?\s+to",
    r"notice(?:s)?\s+(?:shall|must|may)\s+be",
    r"initial\s+term",
    r"term\s+of\s+(?:this\s+)?agreement",
    r"effective\s+date",
    r"address\s+for\s+(?:notice|service)",
)
_TABLE_PAGE_MARKER_RE = re.compile("|".join(_TABLE_PAGE_MARKERS), re.IGNORECASE)

# A real recovered table needs at least this many rows AND columns. A 1xN or a
# single-column "table" is almost always a stray strip, not a 2-column
# signature/notice/term block — so we drop it.
_MIN_TABLE_ROWS = 1
_MIN_TABLE_COLS = 2
# A table must carry at least this many non-empty cells of real text to count.
_MIN_NON_EMPTY_CELLS = 2
# Cap how many tables / cells we keep so a pathological PDF cannot blow memory.
_MAX_TABLES = 40
_MAX_CELLS_PER_TABLE = 400
_MAX_PAGES_SCANNED = 100


@dataclass(frozen=True)
class RecoveredTable:
    """A single recovered table: its location plus its row/column cell grid.

    ``cells`` is a list of rows; each row is a list of cell strings (empty cells
    are preserved as ``""`` so column alignment is not lost). ``page_number`` is
    1-based to match the prose-path ``page_number``.
    """

    page_number: int
    bbox: Optional[tuple[float, float, float, float]]
    row_count: int
    col_count: int
    cells: List[List[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "row_count": self.row_count,
            "col_count": self.col_count,
            "cells": [list(row) for row in self.cells],
        }


@dataclass(frozen=True)
class TableExtractionResult:
    """Outcome of a table-extraction pass over a PDF.

    ``status`` mirrors the visual-profile vocabulary: ``"ready"`` when the
    backend ran (even if it found nothing) and ``"unavailable"`` when the backend
    could not run (e.g. the library is not installed). ``tables`` is always a
    list. ``pages_scanned`` records which 1-based pages the page-gate selected, so
    a run that recovered nothing is still auditable.
    """

    status: str
    backend: str
    tables: List[RecoveredTable] = field(default_factory=list)
    reason: Optional[str] = None
    pages_scanned: List[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "backend": self.backend,
            "version": TABLE_AUGMENTATION_VERSION,
            "table_count": len(self.tables),
            "pages_scanned": list(self.pages_scanned),
            "tables": [table.to_dict() for table in self.tables],
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


# A backend takes the raw PDF bytes plus the page-gate-selected 1-based page
# numbers and returns a TableExtractionResult. The default is the camelot
# ``stream`` implementation; a pdfplumber backend is supplied behind the same
# signature as the lighter alternative.
TableBackend = Callable[[bytes, Sequence[int]], TableExtractionResult]


def table_augmentation_enabled() -> bool:
    """True iff ``NDA_TABLE_AUGMENTATION_ENABLED`` is set to a truthy value.

    Default is OFF: an unset / empty / non-truthy value disables the feature
    entirely, so the prose path keeps its proven behavior with zero change.
    """

    return str(os.environ.get(TABLE_AUGMENTATION_ENABLED_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _page_texts(pdf_bytes: bytes) -> Optional[List[str]]:
    """Best-effort per-page plain text for the page-selector gate.

    Uses PyMuPDF (already a declared dependency, only for reading text here — NOT
    for table detection) and falls back to pypdf. Returns ``None`` when no page
    text can be read, in which case the caller fails CLOSED (selects no pages)
    rather than scanning blind.
    """

    try:
        import fitz
    except ImportError:
        fitz = None  # type: ignore[assignment]
    if fitz is not None:
        try:
            document = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            document = None
        if document is not None:
            try:
                texts = []
                for page_index in range(min(document.page_count, _MAX_PAGES_SCANNED)):
                    try:
                        texts.append(document[page_index].get_text("text") or "")
                    except Exception:
                        texts.append("")
                return texts
            except Exception:
                return None
            finally:
                document.close()

    try:
        from io import BytesIO

        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = []
        for page in reader.pages[:_MAX_PAGES_SCANNED]:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        return texts
    except Exception:
        return None


def select_table_pages(pdf_bytes: bytes) -> List[int]:
    """Return the 1-based pages worth handing to a table extractor.

    The CRITICAL false-positive guard: a page is selected ONLY when its text
    contains a structural marker (``_TABLE_PAGE_MARKERS``) such as "Signature",
    "Initial Term" or "Notice to". A genuine 2-column PROSE page (a definitions
    section, dense recitals) carries no such marker and is never handed to the
    extractor, so it can never be shattered into fake tables. When page text
    cannot be read at all we fail CLOSED and select no pages.
    """

    texts = _page_texts(pdf_bytes)
    if not texts:
        return []
    return [
        index + 1
        for index, text in enumerate(texts)
        if _TABLE_PAGE_MARKER_RE.search(text or "")
    ]


def extract_tables(
    pdf_bytes: bytes,
    *,
    backend: Optional[TableBackend] = None,
    pages: Optional[Sequence[int]] = None,
) -> TableExtractionResult:
    """Recover structured table cells from ``pdf_bytes``.

    Tool-agnostic entry point. ``backend`` defaults to the camelot ``stream``
    implementation; pass an alternative (e.g. :func:`pdfplumber_backend`) to swap
    the tool without changing any caller.

    ``pages`` (1-based) restricts extraction; when omitted the keyword
    page-selector gate (:func:`select_table_pages`) chooses them so a prose page
    is never scanned. If the gate selects no pages the extractor is never
    invoked and a ``status="ready"``/empty result is returned.

    This NEVER raises: a missing backend library, malformed PDF, or backend
    exception degrades to an ``"unavailable"``/empty result, so callers can
    attach the result unconditionally.
    """

    chosen = backend or camelot_stream_backend
    selected = list(pages) if pages is not None else select_table_pages(pdf_bytes)
    if not selected:
        # No page passed the keyword gate -> nothing to scan. This is the normal
        # outcome for prose-only PDFs and must not be treated as an error.
        return TableExtractionResult(
            status="ready",
            backend=getattr(chosen, "__name__", "unknown"),
            pages_scanned=[],
        )
    try:
        result = chosen(pdf_bytes, selected)
    except Exception:  # pragma: no cover - defensive; backends already guard
        LOGGER.exception("Table extraction backend raised; degrading to no tables")
        return TableExtractionResult(
            status="unavailable",
            backend=getattr(chosen, "__name__", "unknown"),
            reason="backend_exception",
            pages_scanned=selected,
        )
    # Make sure the gated pages ride through even if a backend forgot to set them.
    if not result.pages_scanned:
        return TableExtractionResult(
            status=result.status,
            backend=result.backend,
            tables=result.tables,
            reason=result.reason,
            pages_scanned=selected,
        )
    return result


def camelot_stream_backend(pdf_bytes: bytes, pages: Sequence[int]) -> TableExtractionResult:
    """Default backend: camelot ``stream`` flavor over the gated pages.

    camelot is an OPTIONAL dependency (``[tables]`` extra, ~223MB tail). It is
    imported lazily so the import only happens when the feature is enabled AND a
    page passed the keyword gate. If camelot is not installed we log once and
    return ``status="unavailable"`` — a clean no-op with zero behavior change.

    The ``stream`` flavor needs NO Ghostscript system binary (only ``lattice``
    does), so enabling this adds NO apt package to the image — only the pip deps.
    camelot reads from a file path, so the bytes are written to a
    NamedTemporaryFile for the duration of the call. The actual ``read_pdf`` is
    serialized behind a process-wide BoundedSemaphore(1) so two extractions never
    stack their transient buffers.
    """

    backend_name = "camelot_stream"
    selected = [page for page in pages if page >= 1]
    if not selected:
        return TableExtractionResult(status="ready", backend=backend_name, pages_scanned=[])
    try:
        import camelot
    except ImportError:
        LOGGER.info(
            "Table augmentation is enabled but camelot is not installed; "
            "install nda-automation[tables] to recover tables. No-op."
        )
        return TableExtractionResult(
            status="unavailable",
            backend=backend_name,
            reason="camelot_not_installed",
            pages_scanned=selected,
        )

    import tempfile

    # CONCURRENCY GUARD: serialize the (memory-heavy) extraction. Under contention
    # we FAIL OPEN — shed the augmentation rather than block the review request —
    # because this is a non-critical additive pass.
    if not _TABLE_EXTRACTION_SEMAPHORE.acquire(timeout=_EXTRACTION_QUEUE_WAIT_SECONDS):
        LOGGER.info("Table extraction slot busy; skipping augmentation for this document.")
        return TableExtractionResult(
            status="unavailable",
            backend=backend_name,
            reason="extractor_busy",
            pages_scanned=selected,
        )

    page_spec = ",".join(str(page) for page in selected)
    tables: List[RecoveredTable] = []
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(pdf_bytes)
            tmp_path = handle.name
        try:
            table_list = camelot.read_pdf(tmp_path, flavor="stream", pages=page_spec)
        except Exception:
            LOGGER.exception("camelot.read_pdf failed; degrading to no tables")
            return TableExtractionResult(
                status="unavailable",
                backend=backend_name,
                reason="read_failed",
                pages_scanned=selected,
            )
        for table in table_list or []:
            if len(tables) >= _MAX_TABLES:
                break
            recovered = _coerce_camelot_table(table)
            if recovered is not None and _table_is_substantive(recovered):
                tables.append(recovered)
    finally:
        _TABLE_EXTRACTION_SEMAPHORE.release()
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return TableExtractionResult(
        status="ready",
        backend=backend_name,
        tables=tables,
        pages_scanned=selected,
    )


def pdfplumber_backend(pdf_bytes: bytes, pages: Sequence[int]) -> TableExtractionResult:
    """Lighter alternative backend: pdfplumber ``extract_tables`` over gated pages.

    pdfplumber is an OPTIONAL dependency (``[tables-lite]`` extra, pure-Python).
    Imported lazily; a missing library is a clean ``status="unavailable"`` no-op.
    Same tool-agnostic signature as the camelot default, so it can be passed to
    :func:`extract_tables` as ``backend=pdfplumber_backend``.
    """

    backend_name = "pdfplumber"
    selected = [page for page in pages if page >= 1]
    if not selected:
        return TableExtractionResult(status="ready", backend=backend_name, pages_scanned=[])
    try:
        import pdfplumber
    except ImportError:
        LOGGER.info(
            "Table augmentation is enabled but pdfplumber is not installed; "
            "install nda-automation[tables-lite] to recover tables. No-op."
        )
        return TableExtractionResult(
            status="unavailable",
            backend=backend_name,
            reason="pdfplumber_not_installed",
            pages_scanned=selected,
        )

    from io import BytesIO

    wanted = set(selected)
    tables: List[RecoveredTable] = []
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as document:
            for page_index, page in enumerate(document.pages, start=1):
                if page_index not in wanted:
                    continue
                if len(tables) >= _MAX_TABLES:
                    break
                try:
                    raw_tables = page.extract_tables() or []
                except Exception:
                    continue
                for raw_table in raw_tables:
                    if len(tables) >= _MAX_TABLES:
                        break
                    recovered = _coerce_row_grid(raw_table, page_number=page_index)
                    if recovered is not None and _table_is_substantive(recovered):
                        tables.append(recovered)
    except Exception:
        LOGGER.exception("pdfplumber failed; degrading to no tables")
        return TableExtractionResult(
            status="unavailable",
            backend=backend_name,
            reason="read_failed",
            pages_scanned=selected,
        )

    return TableExtractionResult(
        status="ready",
        backend=backend_name,
        tables=tables,
        pages_scanned=selected,
    )


def _coerce_camelot_table(table: Any) -> Optional[RecoveredTable]:
    """Normalize one camelot ``Table`` into a :class:`RecoveredTable`."""

    page_number = _safe_dimension(getattr(table, "page", None), default=1) or 1
    rows: Optional[List[List[Any]]] = None
    data = getattr(table, "data", None)
    if isinstance(data, list):
        rows = data
    else:
        frame = getattr(table, "df", None)
        if frame is not None:
            try:
                rows = frame.values.tolist()
            except Exception:
                rows = None
    if rows is None:
        return None
    return _coerce_row_grid(rows, page_number=page_number)


def _coerce_row_grid(raw_rows: Any, *, page_number: int) -> Optional[RecoveredTable]:
    """Normalize a list-of-rows grid (camelot data / pdfplumber output) into a
    :class:`RecoveredTable`. ``None`` cells become ``""`` to preserve alignment."""

    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    cells: List[List[str]] = []
    cell_budget = _MAX_CELLS_PER_TABLE
    for raw_row in raw_rows:
        if not isinstance(raw_row, (list, tuple)):
            continue
        row: List[str] = []
        for raw_cell in raw_row:
            row.append(_normalize_cell(raw_cell))
            cell_budget -= 1
            if cell_budget <= 0:
                break
        cells.append(row)
        if cell_budget <= 0:
            break
    if not cells:
        return None
    col_count = max((len(row) for row in cells), default=0)
    return RecoveredTable(
        page_number=page_number,
        bbox=None,
        row_count=len(cells),
        col_count=col_count,
        cells=cells,
    )


def _table_is_substantive(table: RecoveredTable) -> bool:
    """False-positive guard: keep only tables that look genuinely tabular.

    A real recovered signature / notice / term table has at least two columns,
    at least one row, and at least two non-empty real-text cells. A stray strip,
    an all-empty grid, or a single-column column is dropped here so the
    augmentation never attaches noise.
    """

    effective_cols = max(table.col_count, max((len(row) for row in table.cells), default=0))
    if table.row_count < _MIN_TABLE_ROWS or effective_cols < _MIN_TABLE_COLS:
        return False
    non_empty = sum(1 for row in table.cells for cell in row if cell.strip())
    return non_empty >= _MIN_NON_EMPTY_CELLS


def _normalize_cell(value: Any) -> str:
    """Whitespace-normalize a raw cell value; ``None``/empty become ``""``."""

    if value is None:
        return ""
    return " ".join(str(value).split())


def _safe_dimension(value: Any, *, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def augment_quality_with_tables(
    quality: dict[str, Any],
    pdf_bytes: bytes,
    *,
    backend: Optional[TableBackend] = None,
) -> dict[str, Any]:
    """Attach recovered tables to ``quality`` ADDITIVELY when the flag is ON.

    * Flag OFF -> returns ``quality`` UNCHANGED (the exact same object). ZERO
      behavior change for the proven prose path.
    * Flag ON  -> returns ``quality`` with a ``recovered_tables`` block added
      under ``visual_profile`` (creating a minimal ``visual_profile`` only if one
      is not already present). Nothing in the prose ``paragraphs`` is touched —
      this only annotates the quality/visual-profile metadata. If no tables are
      recovered the block still records ``table_count: 0`` so the run is
      auditable.

    The augmentation NEVER raises and NEVER removes or rewrites existing keys.
    """

    if not table_augmentation_enabled():
        return quality

    result = extract_tables(pdf_bytes, backend=backend)
    payload = result.to_dict()

    visual_profile = quality.get("visual_profile")
    if not isinstance(visual_profile, dict):
        # No visual profile present (e.g. PyMuPDF unavailable when it was built).
        # Create a minimal container so the recovered-tables block has a home,
        # WITHOUT touching the prose path or claiming a visual fidelity verdict.
        visual_profile = {"status": "augmented"}
        quality["visual_profile"] = visual_profile

    # ADDITIVE: only ever set our own namespaced key. Never reorder, never delete.
    visual_profile["recovered_tables"] = payload
    return quality
