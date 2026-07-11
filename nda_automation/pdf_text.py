from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from statistics import median
from typing import Any, List, Optional

from .document_limits import MAX_DOCUMENT_BYTES
from .review_document import Paragraph
from .table_extraction import augment_quality_with_tables


class PdfExtractionError(ValueError):
    """Raised when a PDF file cannot be converted into reviewable text."""


@dataclass(frozen=True)
class GeoLine:
    """A single extracted text line with the per-line geometry pypdf exposes.

    ``visitor_text`` gives us the line's start position (``left_x``), baseline
    ``y`` and ``font_size``. It does NOT expose glyph widths, so the line's END
    x (right edge) is not reliably available — only ``y`` gaps, indentation and
    font size are trustworthy geometric signals at this layer.
    """

    text: str
    left_x: Optional[float]
    y: Optional[float]
    font_size: Optional[float]


INVALID_PDF_MESSAGE = "The uploaded file is not a valid PDF document."
ENCRYPTED_PDF_MESSAGE = "The PDF is encrypted or password-protected. Remove the password before reviewing."
PDF_SUPPORT_NOT_INSTALLED_MESSAGE = "PDF support is not installed. Install the pypdf dependency before reviewing PDF files."
MAX_PDF_PAGES = 100
MAX_PDF_EXTRACTED_CHARACTERS = 500_000

# Decompression-bomb guard for the TEXT EXTRACTION path. ``page.extract_text()``
# (pypdf, in ``_extract_geo_lines``) and the fitz visual profile both trigger a
# FULL decode of every embedded image stream — and the decoded pixel buffer, not
# the on-disk compressed bytes, is what costs RAM. A single 6000x6000 image in a
# 108 KB file decodes to ~144 MB (4 bytes/pixel) and drove peak RSS to ~274 MB in
# the adversarial probe; a 12000x12000 image (~576 MB decoded, ~4 GB with decode
# transients) would OOM the 2 GB worker. The byte cap (10 MB), page cap (100) and
# char cap (500K) ALL pass such a file, so we add an explicit DECODED-PIXEL-AREA
# budget that is checked from the CHEAP ``get_image_info()`` (image dimensions
# WITHOUT decoding pixels) BEFORE any extract_text/visual-profile decode runs.
#
# BUDGET: 24 Mpix total embedded-image area across the (page-capped) pages.
#   * 24_000_000 pixels x 4 bytes/pixel (RGBA) = ~96 MB of decoded pixels — the
#     SAME ceiling as document_rendering.MAX_PAGE_PIXMAP_BYTES (96 MB), so the
#     extraction path and the rasterize path bound decoded RSS to one shared
#     number's worth of reasoning. Even at ~2-3x pypdf/Pillow decode transients
#     the worst-case transient peak (~200-290 MB) sits far under the 2 GB worker,
#     leaving headroom for the rest of the review pipeline.
#   * The 6000x6000 (36 Mpix, ~274 MB observed peak) bomb is rejected; a normal
#     image PDF — a small logo, or a single 300 DPI US-Letter page scan
#     (2550x3300 ~= 8.4 Mpix) — passes comfortably.
MAX_PDF_IMAGE_PIXELS = 24_000_000
PDF_IMAGE_BOMB_MESSAGE = (
    "The PDF embeds images far larger than the review limit (a likely "
    "decompression bomb). Reduce the embedded image resolution before reviewing."
)
# Belt-and-suspenders byte ceiling re-asserted INSIDE extract_pdf_document itself,
# rather than trusting every caller to have run upstream ensure_document_size. Bound
# directly to document_limits.MAX_DOCUMENT_BYTES (10 MB) so the two never drift.
MAX_PDF_DOCUMENT_BYTES = MAX_DOCUMENT_BYTES
# Cap on the number of vector paths a single page (and the whole document) may
# contribute to the visual profile. ``page.get_drawings()`` materializes one dict
# per vector path; a "drawings bomb" (a PDF with millions of trivial path ops) is
# otherwise unbounded and can exhaust memory inside the profiler. We stop counting
# once the cap is hit — the profile only needs drawing PRESENCE, not an exact count.
MAX_PDF_DRAWINGS_PER_PAGE = 50_000
MAX_PDF_DRAWINGS_TOTAL = 200_000

# Memory guard for the fitz visual profile. ``page.get_text("dict")`` with the
# default flags MATERIALIZES every embedded image's decoded bytes into the per-page
# dict — on an image-heavy PDF that single transient dominates the whole review's
# peak RSS (~50MB on a 3.8MB media-rich PDF, vs ~0.2MB without). The visual profile
# only needs per-span colours + image/drawing *presence*, never the pixels, so we
# strip ``TEXT_PRESERVE_IMAGES`` from the text flags and count images separately via
# the lightweight ``get_image_info()``. Same signal, ~250x less peak memory.
_FITZ_VISUAL_TEXT_FLAGS_NO_IMAGES: int | None = None


def _fitz_visual_text_flags(fitz_module: Any) -> int | None:
    """Text-extraction flags for the visual profile with image bytes suppressed.

    Returns ``None`` (use the library default) if the running PyMuPDF lacks the
    expected flag constants, so the profile degrades to the default behaviour
    rather than crashing on an unexpected build.
    """

    global _FITZ_VISUAL_TEXT_FLAGS_NO_IMAGES
    if _FITZ_VISUAL_TEXT_FLAGS_NO_IMAGES is not None:
        return _FITZ_VISUAL_TEXT_FLAGS_NO_IMAGES
    try:
        flags = int(fitz_module.TEXTFLAGS_DICT) & ~int(fitz_module.TEXT_PRESERVE_IMAGES)
    except Exception:  # pragma: no cover - exotic/old PyMuPDF build
        return None
    _FITZ_VISUAL_TEXT_FLAGS_NO_IMAGES = flags
    return flags

# Two chunks whose baselines differ by less than this many points belong to the
# same visual line (sub/superscript jitter, split runs on one line).
_SAME_LINE_Y_TOLERANCE = 3.0
# GLYPH-FRAGMENTED page detection: this many CONSECUTIVE single-character text
# operations (in drawing order) marks the page as rendered glyph-by-glyph — the
# signature-block/overlay style where every character is its own positioned Tj.
# Word-processor output draws whole runs per operation, so a run of 6 lone glyphs
# essentially never occurs outside per-glyph rendering; the shortest signature-
# block labels ("Signed") already reach it. Kept deliberately high so normal
# documents can never trip it (a stray superscript or standalone clause number is
# a run of 1).
_GLYPH_FRAGMENT_RUN_MIN = 6
# A heading-sized font jump relative to the body font.
_HEADING_FONT_FACTOR = 1.15
# Expected wrap pitch as a multiple of the body font size. Single-spaced text
# wraps at roughly 1.0–1.2x the font size; we anchor the wrap-pitch estimate here
# so that a uniform single-line page (no real wraps) cannot mistake its own clause
# gaps for the wrap pitch. The smallest-gap cluster may only REFINE this downward.
_WRAP_PITCH_FONT_FACTOR = 1.2
# Fixed wrap-pitch floor (points) used when no font size is available. Sized for a
# typical ~11pt body so the pitch never collapses to a clause gap.
_WRAP_PITCH_FLOOR_POINTS = 13.2
# Floating-point slack so an exact body*factor gap is not lost to representation
# error in the gap >= threshold comparisons.
_GAP_EPSILON = 0.5

# --- REGION / COLUMN reading-order reconstruction (defects 1, 2 & 3) --------
# The reviewer historically saw text bucketed by BASELINE y ONLY, then joined
# with " ". On a two-column page that interleaves clauses across the gutter
# ("1. Confidential Information ... 3. The obligations of ...") and on a stamped
# overlay it splices the stamp into the sentence — SILENTLY. We instead partition
# each page into regions (columns / body / overlay) and read each region in full
# before the next, keeping the LEGACY per-region grouping so a single-column page
# is byte-identical to today.
#
# There are NO true glyph widths at the pypdf visitor layer, so a chunk's right
# edge is ESTIMATED as ``char_count * font_size * _MEAN_ADVANCE_EM``. The estimate
# is used ONLY to locate a wide empty vertical corridor (a gutter); it never adds,
# drops or reorders text WITHIN a line, so a modest error is absorbed by the wide-
# gutter requirement. ~0.5em is the empirical average advance of proportional
# Latin body text (Times/Helvetica digest ~0.5).
_MEAN_ADVANCE_EM = 0.5
# A designed inter-column gutter is typically 18-36pt (>=2em at 11pt). 2em exceeds
# any inter-word space (~0.25em) or justified word space (rarely >1.5em) and the
# usual list indent (2-4em, but only at a line start — never a full-height
# corridor). Below 2em a gutter cannot be told apart from ordinary spacing.
_GUTTER_MIN_WIDTH_EM = 2.0
_GUTTER_MIN_WIDTH_PAGE_FRAC = 0.03
# The corridor must stay empty across >= this fraction of the baselines it spans.
# In a real two-column region nearly every body row respects the gutter; the slack
# tolerates the occasional full-width heading/figure that bridges it.
_GUTTER_COVERAGE_FRAC = 0.85
# Both sides of a split must carry real prose. Signature labels ("Name:" ~6),
# page numbers (~10) and short table codes fall below; sentences clear it. This is
# THE gate that spares a borderless term|definition or party|address table (whose
# short cells never reach 15) while passing genuine two-column body text.
_SIDE_MIN_MEDIAN_CHARS = 15
# The whole multi-column region needs at least this many baselines, and each column
# at least ``_COLUMN_MIN_BASELINES``. Below 6 the coverage fraction has too few
# samples and the false-positive population (2-cell address block, Name/Date sig
# line) dominates.
_MIN_REGION_BASELINES = 6
_COLUMN_MIN_BASELINES = 3
# The gutter's vertical extent must cover >= this fraction of the page's TEXT
# height (not the physical page — a two-column body can be short). A top-of-page
# two-cell block or a bottom signature block occupies only a small slice.
_REGION_HEIGHT_FRAC = 0.5
# Recursion caps for 3+/unbalanced columns. A band that resolves into more than
# _MAX_COLUMNS candidate columns is treated as tabular and NOT linearized.
_MAX_COLUMNS = 3
_MAX_COLUMN_DEPTH = 3
# A STAMPED OVERLAY (an "EXECUTED" stamp at a larger font, far to the right of the
# body sentence it sits on) must not splice into that sentence. We pull a chunk out
# of its baseline ONLY when it is BOTH markedly larger than the page body font AND
# separated by a wide horizontal gap from the body text on that baseline — the
# unmistakable overlay signature, never a same-font two-cell table row.
_OVERLAY_FONT_FACTOR = 1.5
_OVERLAY_MIN_GAP_EM = 2.0
# WIDTH-ESTIMATE ceiling: a borderless column reorder relies on ESTIMATED widths,
# so even a crisp split is capped here — the honest "I am fairly, not perfectly,
# sure I read this in the right order".
_WIDTH_ESTIMATE_CEILING = 0.8

# --- HORIZONTAL ADJACENCY: the garble PRODUCER fix (defect 4, join side) ------
# ``_merge_line_bucket`` joins the same-baseline chunks of one visual line. The old
# code forced a space between EVERY pair, so a word the PDF producer split into
# edge-to-edge chunks ("(", "COMPANY", ")" / "NON", "-", "DISCLOSURE") came back as
# "( COMPANY )" / "NON - DISCLOSURE". The join now DEFAULTS to a space (preserving
# the legacy behavior for everything ambiguous — column reorders, tables, newline-
# separated cells) and SUPPRESSES it only when two chunks are PROVABLY adjacent:
#   1. neither boundary carries the whitespace pypdf itself inserted for a real word
#      gap (surfaced as leading/trailing space on the raw chunk text), AND
#   2. the next chunk's left edge sits right where this chunk's estimated right edge
#      is — no real horizontal gap.
# Right edges are estimated per character from a proportional-Latin advance digest
# (Helvetica AFM / 1000). The estimate only ever DECIDES A SPACE vs NO-SPACE within
# a line; it never adds, drops or reorders a character. Per-character advance widths
# (fraction of em); default 0.5em for anything unlisted.
_GLYPH_ADVANCE_EM = {
    "!": 0.278, '"': 0.355, "#": 0.556, "$": 0.556, "%": 0.889, "&": 0.667,
    "'": 0.191, "(": 0.333, ")": 0.333, "*": 0.389, "+": 0.584, ",": 0.278,
    "-": 0.333, ".": 0.278, "/": 0.278, ":": 0.278, ";": 0.278, "<": 0.584,
    "=": 0.584, ">": 0.584, "?": 0.556, "@": 1.015, "[": 0.278, "\\": 0.278,
    "]": 0.278, "^": 0.469, "_": 0.556, "`": 0.333, "{": 0.334, "|": 0.260,
    "}": 0.334, "~": 0.584,
    "0": 0.556, "1": 0.556, "2": 0.556, "3": 0.556, "4": 0.556, "5": 0.556,
    "6": 0.556, "7": 0.556, "8": 0.556, "9": 0.556,
    "A": 0.667, "B": 0.667, "C": 0.722, "D": 0.722, "E": 0.667, "F": 0.611,
    "G": 0.778, "H": 0.722, "I": 0.278, "J": 0.500, "K": 0.667, "L": 0.556,
    "M": 0.833, "N": 0.722, "O": 0.778, "P": 0.667, "Q": 0.778, "R": 0.722,
    "S": 0.667, "T": 0.611, "U": 0.722, "V": 0.667, "W": 0.944, "X": 0.667,
    "Y": 0.667, "Z": 0.611,
    "a": 0.556, "b": 0.556, "c": 0.500, "d": 0.556, "e": 0.556, "f": 0.278,
    "g": 0.556, "h": 0.556, "i": 0.222, "j": 0.222, "k": 0.500, "l": 0.222,
    "m": 0.833, "n": 0.556, "o": 0.556, "p": 0.556, "q": 0.556, "r": 0.333,
    "s": 0.500, "t": 0.278, "u": 0.556, "v": 0.500, "w": 0.722, "x": 0.500,
    "y": 0.500, "z": 0.500,
}
_DEFAULT_GLYPH_ADVANCE_EM = 0.5
# Font size assumed when a chunk carries none, so the point gap and the point
# tolerance are computed in one unit.
_ADJACENCY_FALLBACK_FONT_PT = 11.0
# The next chunk is "adjacent" when its left edge is within this many em of the
# estimated right edge. Kept BELOW a real word space (~0.28em) so a genuine gap is
# never swallowed, and comfortably ABOVE the ~0 gap of true edge-to-edge chunks;
# the DEFAULT-to-space policy means any width-estimate error only ever KEEPS a space
# (the pre-fix behavior), never merges two words.
_ADJACENCY_TOLERANCE_EM = 0.2

# --- LETTER-SPACED / FRAGMENTED garble detection (defect 4, gaps a & b) -----
# ``_garbled_text_ratio`` counts only NON-alphanumeric symbols, so a letter-spaced
# run ("I N W I T N E S S W H E R E O F") or a kern/ligature-fragmented run
# ("IN WI TN ES S WH ER EO F") scores 0.0 and never trips it. We add a run test on
# the EXTRACTED text: the longest run of consecutive SHORT alphabetic tokens (<= 2
# chars). Real prose never chains many such tokens (English 2-letter words scatter
# among longer ones); a fragmented run is almost entirely short tokens. A legitimate
# spaced "N D A" title is only a run of 3, so the threshold spares it.
_GARBLE_SHORT_TOKEN_MAX_LEN = 2
_GARBLE_RUN_MIN = 6


@dataclass(frozen=True)
class PdfExtraction:
    paragraphs: List[Paragraph]
    quality: dict[str, object]


def extract_pdf_text(data: bytes) -> str:
    paragraphs = extract_pdf_paragraphs(data)
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)


def extract_pdf_paragraphs(data: bytes) -> List[Paragraph]:
    return extract_pdf_document(data).paragraphs


def extract_pdf_document(data: bytes, *, include_visual_profile: bool = True) -> PdfExtraction:
    """Extract review paragraphs + a quality report from a PDF.

    ``include_visual_profile`` defaults to ``True`` to preserve every existing
    interactive caller's behavior (the visual profile feeds source-fidelity preview
    signals in ``source_fidelity.py``). Pass ``False`` on the cost-sensitive Gmail
    poll import path: that path only needs paragraphs to create a 'Not Reviewed'
    matter, and computing the visual profile would pay a *second*, completely
    independent full PyMuPDF parse (``fitz.open`` + per-page ``get_text('dict')``
    span walk + ``get_drawings`` on up to ``MAX_PDF_PAGES`` pages) of C-level,
    GIL-held CPU stacked on the earlier pypdf ``_extract_geo_lines`` pass. When
    ``False`` the profile is not computed and a 'deferred' marker is recorded that
    downstream (``_pdf_quality_report`` / ``source_fidelity``) treats exactly like
    the existing 'unavailable' (not-yet-computed) case, so the data contract is
    unchanged — the interactive fidelity request recomputes it later on demand.
    """

    try:
        from io import BytesIO
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExtractionError(PDF_SUPPORT_NOT_INSTALLED_MESSAGE) from exc

    # Belt-and-suspenders byte ceiling. Every production caller is expected to have
    # run ensure_document_size already, but this module is also reachable directly
    # (tests, future callers); re-asserting the cap here means the extraction path can
    # never be handed an arbitrarily large blob just because one caller skipped the
    # upstream gate. A bomb's danger is decoded pixels, not file bytes (guarded
    # below), but this keeps the on-disk size bounded too.
    if len(data) > MAX_PDF_DOCUMENT_BYTES:
        raise PdfExtractionError(
            f"The PDF is {len(data):,} bytes, which exceeds the "
            f"{MAX_PDF_DOCUMENT_BYTES:,} byte review limit."
        )

    if not data.lstrip().startswith(b"%PDF-"):
        raise PdfExtractionError(INVALID_PDF_MESSAGE)

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfExtractionError(INVALID_PDF_MESSAGE) from exc

    if reader.is_encrypted:
        # Empty-password-encrypted PDFs decrypt and remain reviewable; truly
        # locked PDFs must be reported as encrypted rather than "scanned/invalid".
        try:
            decrypted = reader.decrypt("")
        except Exception as exc:
            raise PdfExtractionError(ENCRYPTED_PDF_MESSAGE) from exc
        if not decrypted:
            raise PdfExtractionError(ENCRYPTED_PDF_MESSAGE)

    page_geo_lines: list[list[GeoLine]] = []
    page_count = len(reader.pages)
    if page_count > MAX_PDF_PAGES:
        raise PdfExtractionError(f"The PDF has {page_count} pages, which exceeds the {MAX_PDF_PAGES} page review limit.")

    # IMAGE-PIXEL-AREA BUDGET — runs BEFORE the page loop below calls
    # _extract_geo_lines -> page.extract_text(), which is what decodes the embedded
    # image streams into RAM. Summing the embedded-image pixel area via the cheap
    # get_image_info() (dimensions WITHOUT decoding pixels) lets us reject a
    # decompression bomb with NO decode having happened. Fails OPEN (no rejection)
    # when PyMuPDF is unavailable or the probe errors — the guard never blocks a
    # reviewable PDF on its own infrastructure failure.
    _guard_pdf_image_pixel_area(data, page_count)

    pages_without_text = 0
    pages_with_text = 0
    extracted_character_count = 0
    repeated_margins: set[str] = set()
    page_signals: list[dict[str, object]] = []
    for page in reader.pages:
        try:
            geo_lines, page_signal = _extract_geo_lines(page)
        except Exception as exc:
            raise PdfExtractionError("The PDF text could not be extracted.") from exc
        page_signals.append(page_signal)
        extracted_character_count += sum(len(geo_line.text) for geo_line in geo_lines)
        if extracted_character_count > MAX_PDF_EXTRACTED_CHARACTERS:
            raise PdfExtractionError(
                f"The PDF produced more than the {MAX_PDF_EXTRACTED_CHARACTERS:,} character extraction limit."
            )
        page_geo_lines.append(geo_lines)
        if geo_lines:
            pages_with_text += 1
        else:
            pages_without_text += 1

    if page_count > 1:
        repeated_margins = _repeated_margin_lines([[g.text for g in lines] for lines in page_geo_lines])

    # PER-PAGE OCR rescue for a MIXED text+image PDF (defect B2). The whole-document
    # scanned fallback below only fires when the ENTIRE document has no text layer
    # (``if not paragraphs``), so a scanned signature/annex page sitting AMONG
    # text-layer pages was previously dropped SILENTLY. Here we identify the pages
    # that produced no text (0-based) and, for a MIXED document only (some pages DO
    # have text), OCR just those pages so their content is not lost. Any page that
    # cannot be rescued (OCR off/failed/too large) is named in a degraded-quality
    # warning below -- nothing is silently dropped. A single-column all-text PDF has
    # no image-only pages, so this whole block is inert and the output is unchanged.
    image_only_indices = [i for i, lines in enumerate(page_geo_lines) if not lines]  # 0-based
    rescued_pages: dict[int, list[str]] = {}
    if pages_with_text and image_only_indices:
        for idx, ocr_text in _try_ocr_pages(data, image_only_indices).items():
            texts = [t for t in _split_pdf_paragraphs(_normalized_lines(ocr_text)) if t.strip()]
            if texts:
                rescued_pages[idx] = texts

    paragraphs: List[Paragraph] = []
    for page_index, geo_lines in enumerate(page_geo_lines, start=1):
        filtered_lines = _filtered_geo_lines(geo_lines, repeated_margins)
        # Page-wide body font reference for the geometry trust tier: a heading is
        # only geometrically corroborated when its font is meaningfully larger than
        # the page's dominant (body) font. Computed once per page from the same
        # filtered lines the splitter sees, so it is not skewed by margins/headers.
        page_body_font = _dominant_font_size(_as_geo_lines(filtered_lines))
        for block in _split_pdf_paragraph_blocks(filtered_lines):
            paragraph_text = " ".join(item.text for item in block)
            paragraph: Paragraph = {
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_index,
                "text": paragraph_text,
            }
            geometry = _pdf_paragraph_geometry(block, page_body_font)
            if geometry is not None:
                paragraph["pdf_geometry"] = geometry
            paragraphs.append(paragraph)
        # OCR-rescued paragraphs for an image-only page, inserted at the page's
        # natural reading position (the loop is already in page order). Flagged
        # ``ocr`` so downstream never mistakes them for a clean text layer.
        for paragraph_text in rescued_pages.get(page_index - 1, []):
            paragraphs.append({
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_index,
                "text": paragraph_text,
                "ocr": True,
            })

    # Image-only pages that remained un-rescued (OCR off/failed/too large) in a
    # mixed document: their content is missing from the review and MUST be surfaced.
    image_only_page_numbers = [i + 1 for i in image_only_indices]
    dropped_image_pages = (
        [p for p in image_only_page_numbers if (p - 1) not in rescued_pages]
        if pages_with_text else []
    )
    ocr_pages_recovered = sorted(idx + 1 for idx in rescued_pages)

    # AcroForm (fillable-PDF) field VALUES (defect B1). A fillable NDA carries party
    # names/dates/amounts in form-field /V values that the text-layer extraction
    # above never reads, so the reviewer sees a BLANK template. Append the field
    # values as an explicit, clearly-labelled 'form field values' section (we cannot
    # honestly derive their on-page position from the text layer) and flag the doc as
    # a form below. A normal PDF has no AcroForm, so this is inert and byte-identical.
    form_field_entries = _extract_acroform_field_values(reader)
    if form_field_entries:
        paragraphs.append({
            "id": f"p{len(paragraphs) + 1}",
            "source_index": len(paragraphs) + 1,
            "source_part": "pdf",
            "page_number": page_count,
            "text": "Form field values (fillable PDF):",
            "form_field": True,
        })
        for label, value in form_field_entries:
            entry_text = f"{label}: {value}" if label else value
            paragraphs.append({
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_count,
                "text": entry_text,
                "form_field": True,
            })

    if not paragraphs:
        # No text layer (scanned / image-only PDF). Try the OCR fallback FIRST. It
        # is DEFAULT-OFF and self-gating: when OCR is disabled/unconfigured/fails it
        # returns None and we re-raise the SAME clear error below -- never an empty
        # or garbage "review". When it recovers text, that text is fed through the
        # existing TEXT-ONLY splitter so the never-merge-safe heuristics stay in
        # force (OCR yields flat text, exactly like the visitor-unsupported path).
        ocr_extraction = _try_ocr_fallback(data, page_count=page_count)
        if ocr_extraction is not None:
            return ocr_extraction
        raise PdfExtractionError("No readable text was found in the PDF. Scanned PDFs need OCR before review.")
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
    if include_visual_profile:
        visual_profile = _pdf_visual_profile(data)
    else:
        # LAZY skip: the visual profile is the second full PyMuPDF parse and is only
        # needed for interactive source-fidelity preview signals, not for the poll
        # import's 'Not Reviewed' matter. Record the SAME 'unavailable' contract
        # shape that _pdf_visual_profile emits on its error branch (status +
        # requires_source_preview) so _pdf_quality_report and source_fidelity treat
        # this as 'not-yet-computed' — fail-open, and recomputed on demand later.
        visual_profile = _deferred_visual_profile()
    reading_order = _reading_order_signal(page_signals, extracted_text)
    quality = _pdf_quality_report(
        page_count=page_count,
        pages_with_text=pages_with_text,
        pages_without_text=pages_without_text,
        extracted_text=extracted_text,
        paragraph_count=len(paragraphs),
        repeated_margin_count=len(repeated_margins),
        visual_profile=visual_profile,
        reading_order=reading_order,
        form_fields_present=bool(form_field_entries),
        image_only_pages_dropped=dropped_image_pages,
        ocr_pages_recovered=ocr_pages_recovered,
    )
    if ocr_pages_recovered:
        # Some image-only pages were rescued by per-page OCR -- flag the recovery so
        # downstream never treats the OCR'd text as a clean text layer.
        quality["ocr_recovered"] = True
    # ADDITIVE, default-OFF table recovery. When NDA_TABLE_AUGMENTATION_ENABLED is
    # unset/false this is a strict no-op (the quality block is returned unchanged
    # and the prose paragraphs above are never touched). When ON it attaches
    # recovered 2-column table cells under quality["visual_profile"], which the
    # one-dimensional prose splitter flattens. It NEVER raises.
    #
    # NOTE on include_visual_profile=False: the lazy skip above avoids the visual
    # profile's second PyMuPDF parse. In the COMMON case (table-aug env flag OFF,
    # the production default) this is a genuine net saving of that entire second
    # parse. When the table-aug flag is ON, augment_quality_with_tables re-parses
    # the PDF anyway, so the skip does not remove that separate parse — behavior of
    # augment_quality_with_tables is unchanged either way.
    quality = augment_quality_with_tables(quality, data)
    return PdfExtraction(paragraphs=paragraphs, quality=quality)


def _try_ocr_pages(data: bytes, page_indices: list[int]) -> dict[int, str]:
    """Per-PAGE OCR rescue for image-only pages in a MIXED document (defect B2).

    Returns ``{0-based page index: recovered text}`` for pages the DEFAULT-OFF OCR
    fallback could transcribe; an empty dict when OCR is off/unconfigured/unavailable
    or nothing was recovered. NEVER raises. Partial results are intentional: the
    caller names any un-rescued page in a degraded-quality warning, so nothing is
    silently lost.
    """

    if not page_indices:
        return {}
    try:
        from .pdf_ocr import ocr_pdf_pages
    except Exception:
        return {}
    try:
        result = ocr_pdf_pages(data, page_indices)
    except Exception:
        # ocr_pdf_pages is fail-safe by contract; belt-and-suspenders.
        return {}
    return result if isinstance(result, dict) else {}


def _extract_acroform_field_values(reader: Any) -> list[tuple[str, str]]:
    """Read fillable-PDF (AcroForm) field label/value pairs (defect B1).

    Returns an ordered list of ``(label, value)`` for every form field that carries
    a NON-EMPTY value. Returns ``[]`` when the PDF has no AcroForm (the normal case
    -- so extraction stays byte-identical for ordinary PDFs) or when the fields
    cannot be read. NEVER raises.

    The text-layer extraction never reads AcroForm ``/V`` values, so a fillable NDA
    (party names, dates, amounts entered into form fields) reaches the reviewer as a
    blank template. We surface those values so nothing is silently dropped. Checkbox
    off-states (``/Off``) and empty values are skipped; a checkbox on-state exports
    as its value name with the leading ``/`` removed.
    """

    try:
        fields = reader.get_fields()
    except Exception:
        return []
    if not fields:
        return []

    entries: list[tuple[str, str]] = []
    try:
        items = list(fields.items())
    except Exception:
        return []
    for name, field in items:
        raw_value = None
        try:
            if hasattr(field, "get"):
                raw_value = field.get("/V")
            if raw_value is None:
                raw_value = getattr(field, "value", None)
        except Exception:
            raw_value = None
        value = _normalize_form_field_value(raw_value)
        if not value:
            continue
        label = _normalize_form_field_label(name, field)
        entries.append((label, value))
    return entries


def _normalize_form_field_value(value: Any) -> str:
    """Coerce a form field ``/V`` into clean review text; '' when empty/off."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    text = " ".join(str(value).split())
    if not text:
        return ""
    # A checkbox/radio off-state carries no information for the reviewer.
    if text in {"/Off", "Off"}:
        return ""
    # A checkbox on-state (``/Yes``, ``/1``) exports as a name; drop the leading /.
    if text.startswith("/"):
        text = text[1:].strip()
    return text


def _normalize_form_field_label(name: Any, field: Any) -> str:
    """Human-readable label for a form field: /T name (or fully-qualified key)."""

    label: Any = None
    try:
        if hasattr(field, "get"):
            label = field.get("/T")
    except Exception:
        label = None
    if not label:
        label = name
    return " ".join(str(label or "").split())


def _try_ocr_fallback(data: bytes, *, page_count: int) -> PdfExtraction | None:
    """Recover text from a no-text-layer (scanned) PDF via the OCR fallback.

    Returns a ``PdfExtraction`` built from OCR'd text when the DEFAULT-OFF OCR
    fallback is enabled, configured AND recovered real text; otherwise ``None``
    so the caller re-raises the original clear scanned-reject error.

    The OCR text is FLAT (no geometry), so it is split with the TEXT-ONLY
    ``_split_pdf_paragraphs`` -- the same never-merge-safe path the
    visitor-unsupported flat-text case already uses. Each recovered paragraph is
    flagged ``{"ocr": True}`` and the quality report carries an ``ocr_recovered``
    flag plus a warning so downstream never mistakes OCR'd text for a clean text
    layer. NEVER raises and NEVER returns an empty extraction.
    """

    try:
        from .pdf_ocr import ocr_pdf_text
    except Exception:
        return None

    try:
        ocr_text = ocr_pdf_text(data)
    except Exception:
        # ocr_pdf_text is fail-safe by contract, but belt-and-suspenders: any
        # surprise degrades to the unchanged scanned-reject.
        return None
    if not ocr_text or not ocr_text.strip():
        return None

    lines = _normalized_lines(ocr_text)
    paragraph_texts = [text for text in _split_pdf_paragraphs(lines) if text.strip()]
    if not paragraph_texts:
        return None

    paragraphs: List[Paragraph] = []
    for paragraph_text in paragraph_texts:
        paragraph: Paragraph = {
            "id": f"p{len(paragraphs) + 1}",
            "source_index": len(paragraphs) + 1,
            "source_part": "pdf",
            "page_number": 1,
            "text": paragraph_text,
            "ocr": True,
        }
        paragraphs.append(paragraph)

    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
    quality = _pdf_quality_report(
        page_count=page_count,
        pages_with_text=0,
        pages_without_text=page_count,
        extracted_text=extracted_text,
        paragraph_count=len(paragraphs),
        repeated_margin_count=0,
        visual_profile=None,
    )
    quality["ocr_recovered"] = True
    warnings = quality.get("warnings")
    if isinstance(warnings, list):
        warnings.append({
            "type": "pdf_ocr_recovered",
            "message": (
                "This PDF had no text layer; the text was recovered by OCR and may "
                "contain transcription errors. Verify against the original document."
            ),
        })
    return PdfExtraction(paragraphs=paragraphs, quality=quality)


def _guard_pdf_image_pixel_area(data: bytes, page_count: int) -> None:
    """Reject a PDF whose embedded images would decode to too many pixels.

    This is the PRE-DECODE decompression-bomb guard for the extraction path. It
    uses PyMuPDF's ``get_image_info()``, which reports each embedded image's
    pixel WIDTH/HEIGHT from the stream dictionary WITHOUT decoding the pixels, so
    the dangerous full-image decode (``page.extract_text()`` in ``_extract_geo_lines``,
    and the fitz visual profile) never runs on a bomb.

    Only the page-capped prefix (``MAX_PDF_PAGES``) is inspected — the pages whose
    text we will actually extract — matching the page cap the rest of the pipeline
    honours. Total embedded-image pixel area across those pages is summed and
    compared to ``MAX_PDF_IMAGE_PIXELS``; exceeding it raises ``PdfExtractionError``
    BEFORE any decode.

    FAILS OPEN: if PyMuPDF is missing or the probe raises for any reason, we do NOT
    reject — the guard must never block a legitimately reviewable PDF because of its
    own infrastructure gap. (A bomb still cannot get FAR on a no-fitz box: the visual
    profile is the only other decoder and it independently degrades to "unavailable".)
    A genuine ``PdfExtractionError`` raised here is re-raised, not swallowed.
    """

    try:
        import fitz
    except ImportError:
        return

    document = None
    try:
        document = fitz.open(stream=data, filetype="pdf")
        inspected_pages = min(document.page_count, page_count, MAX_PDF_PAGES)
        total_pixels = 0
        for page_index in range(inspected_pages):
            try:
                image_infos = document[page_index].get_image_info()
            except Exception:
                # One unreadable page must not blind the whole guard; skip it.
                continue
            for info in image_infos or []:
                if not isinstance(info, dict):
                    continue
                width = _safe_int(info.get("width"))
                height = _safe_int(info.get("height"))
                if not width or not height or width < 0 or height < 0:
                    continue
                total_pixels += width * height
                if total_pixels > MAX_PDF_IMAGE_PIXELS:
                    raise PdfExtractionError(PDF_IMAGE_BOMB_MESSAGE)
    except PdfExtractionError:
        raise
    except Exception:
        # Probe failed for a non-rejection reason (corrupt build, unexpected API):
        # fail OPEN rather than block a reviewable PDF.
        return
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass


def _compose_translation(tm: Any, cm: Any) -> tuple[float, float, bool]:
    """Device baseline origin (x, y) of a text chunk, composing tm THROUGH cm.

    pypdf's ``visitor_text`` hands us BOTH the text matrix ``tm`` and the current
    transformation matrix ``cm`` (the graphics-state ``q .. cm .. Q`` transform).
    The old code read ``tm[4]/tm[5]`` and DROPPED ``cm`` entirely, so any block
    drawn under its own ``cm`` translation/scale (a stamped overlay, a watermark,
    a shifted content group) collapsed onto the wrong baseline.

    The device translation is the translation row of the composed matrix
    ``tm x cm`` under pypdf's row-vector convention (a point row-vector is
    multiplied ON THE LEFT). ORDER MATTERS: ``tm x cm`` (text placed, THEN the
    graphics transform applied) is correct; ``cm x tm`` is wrong. For a
    translation-only ``cm`` both orders give the same answer, which is exactly why
    a wrong order stays hidden until a SCALING/ROTATING ``cm`` — precisely the
    stamped-overlay/watermark inputs this fix targets (see the regression test).

    Returns ``(x, y, rotated)`` where ``rotated`` is True when either matrix
    carries a non-trivial rotation/skew (off-diagonal terms), so the caller can
    refuse to trust horizontal column geometry for that chunk.
    """

    def _six(m: Any) -> tuple[float, float, float, float, float, float]:
        return (
            float(m[0]), float(m[1]), float(m[2]),
            float(m[3]), float(m[4]), float(m[5]),
        )

    ta, tb, tc, td, te, tf = _six(tm)
    if cm is None:
        ca, cb, cc, cd, ce, cf = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    else:
        ca, cb, cc, cd, ce, cf = _six(cm)
    # Translation row of (tm x cm): (te, tf, 1) transformed by cm.
    x = te * ca + tf * cc + ce
    y = te * cb + tf * cd + cf
    rotated = (
        abs(tb) > 1e-6 or abs(tc) > 1e-6 or abs(cb) > 1e-6 or abs(cc) > 1e-6
    )
    return x, y, rotated


def _page_width(page: Any) -> Optional[float]:
    try:
        box = page.mediabox
        return float(box.width)
    except Exception:
        return None


def _extract_geo_lines(page: Any) -> tuple[list[GeoLine], dict[str, object]]:
    """Extract per-line text plus a per-page reading-order confidence signal.

    pypdf's ``visitor_text`` callback fires once per drawn text chunk with the
    text matrix ``tm``, the current transformation matrix ``cm`` and the font
    size. We compose ``tm x cm`` for the true device baseline (defect 3), then
    partition the page's chunks into REGIONS (columns / body / stamped overlay)
    and read each region fully before the next (defects 1 & 2). A single-column
    page yields exactly ONE region and is byte-identical to the legacy path.

    Returns ``(geo_lines, page_signal)``. ``page_signal`` is a small serializable
    dict carrying the per-page reading-order confidence and reasons; the caller
    folds every page's signal into the document quality report.
    """

    chunks: list[tuple[float, float, Optional[float], str]] = []
    rotated_flag = {"seen": False}

    def _visitor(text: str, cm: Any, tm: Any, _font_dict: Any, font_size: Any) -> None:
        raw = str(text)
        collapsed = " ".join(raw.split())
        if not collapsed:
            return
        # Preserve the space/no-space decision pypdf already made at this chunk's
        # boundaries (encoded as leading/trailing whitespace) so _merge_line_bucket
        # can reproduce it and NOT force a space between edge-to-edge chunks (the
        # garble PRODUCER fix, defect 4). Internal whitespace is still collapsed. All
        # OTHER consumers measure the TRIMMED text (see _chunk_width /
        # _chunks_are_glyph_fragmented), so this boundary space changes nothing else.
        lead = " " if raw[:1].isspace() else ""
        trail = " " if raw[-1:].isspace() else ""
        cleaned = f"{lead}{collapsed}{trail}"
        try:
            x, y, rotated = _compose_translation(tm, cm)
        except (TypeError, ValueError, IndexError):
            # Fall back to the raw text-matrix translation if composition fails,
            # never worse than the pre-fix behavior.
            try:
                x = float(tm[4])
                y = float(tm[5])
                rotated = False
            except (TypeError, ValueError, IndexError):
                return
        if rotated:
            rotated_flag["seen"] = True
        size = _safe_float(font_size)
        chunks.append((x, y, size, cleaned))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        chunks = []

    if chunks and not _chunks_are_glyph_fragmented(chunks):
        regions, page_signal = _partition_page_regions(
            chunks, _page_width(page), rotated_flag["seen"]
        )
        geo_lines: list[GeoLine] = []
        for region in regions:
            geo_lines.extend(_group_chunks_into_lines(region))
        geo_lines = [line for line in geo_lines if line.text]
        if geo_lines:
            return geo_lines, page_signal

    # Fallback: flat text with no coordinates so the splitter relies purely on
    # never-merge-safe text rules. Taken in two cases:
    #
    # 1. No usable geometry at all (e.g. visitor unsupported) — the original case.
    # 2. The page is GLYPH-FRAGMENTED: its text is drawn one character per
    #    positioned operation (per-glyph Tj/Tm — typical of signature-block
    #    overlays and some DOCX->PDF converters). The visitor layer CANNOT
    #    faithfully reassemble such a page: pypdf's ``visitor_text`` exposes no
    #    glyph widths and reports stale/duplicated ``tm`` translations for
    #    back-to-back single-glyph operations, so baseline-bucketing the chunks
    #    stacks same-line glyphs into one-character vertical "lines" ("C"/"E"/"O"
    #    as three paragraphs) and space-joins the rest into scrambled fragments
    #    ("B r i n e" + an orphan "a"). pypdf's own ``extract_text`` walks the
    #    content stream WITH real font metrics, so it reassembles the same page
    #    into coherent lines ("CEO", "Moorwand Limited") without inventing or
    #    dropping characters — strictly better text than any join we could do
    #    from the width-less visitor geometry. The cost is losing geometry for
    #    the page, which only fail-safes the splitter into per-line paragraphs
    #    (fragmenting, never merging) and keeps the ``pdf_confident`` trust tier
    #    closed — the documented safe degradation.
    # The flat-text fallback carries NO reliable geometry, so no reorder is
    # attempted (single implicit region) and the reading-order confidence for the
    # page stays clean; a letter-spaced/fragmented page is caught separately by the
    # document-level garble detector on the joined extracted text.
    fallback_signal = _default_page_signal()
    try:
        page_text = page.extract_text() or ""
    except Exception:
        page_text = ""
    flat_lines = [GeoLine(text=line, left_x=None, y=None, font_size=None) for line in _normalized_lines(page_text)]
    if flat_lines:
        return flat_lines, fallback_signal
    # A glyph-fragmented page whose flat extraction came back empty: mangled
    # grouped text still beats silently dropping the page's text entirely.
    return _group_chunks_into_lines(chunks), fallback_signal


Chunk = tuple  # (x, y, size, text) — device baseline origin, font size, text


def _default_page_signal() -> dict[str, object]:
    """A clean single-column page: full confidence, nothing reordered."""
    return {
        "columns": 1,
        "reorder": False,
        "overlay": False,
        "rotated": False,
        "confidence": 1.0,
        "reasons": [],
    }


def _chunk_dominant_font(chunks: list[Chunk]) -> float:
    sizes = [c[2] for c in chunks if c[2]]
    if not sizes:
        return 11.0
    # Mode-ish: the most common rounded size is the body font; ties -> smaller.
    counts: dict[float, int] = {}
    for s in sizes:
        key = round(float(s), 1)
        counts[key] = counts.get(key, 0) + 1
    best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    return best[0]


def _distinct_baselines(chunks: list[Chunk]) -> list[float]:
    ys = sorted({round(float(c[1]), 1) for c in chunks}, reverse=True)
    merged: list[float] = []
    for y in ys:
        if merged and abs(merged[-1] - y) <= _SAME_LINE_Y_TOLERANCE:
            continue
        merged.append(y)
    return merged


def _chunk_width(chunk: Chunk, body_font: float) -> float:
    size = chunk[2] or body_font
    # Trim boundary whitespace (the visitor now preserves it for the line-join) so
    # the width estimate counts only rendered glyphs, as before.
    return max(1.0, len(chunk[3].strip())) * float(size) * _MEAN_ADVANCE_EM


def _partition_page_regions(
    chunks: list[Chunk],
    page_width: Optional[float],
    rotated: bool,
) -> tuple[list[list[Chunk]], dict[str, object]]:
    """Partition a page's chunks into reading-order regions + a confidence signal.

    Order of operations, each strictly conservative (a false split newly corrupts a
    document that reads correctly today, so every destructive step has a HIGH bar
    while the confidence FLAG has a LOW bar):

      1. Rotation/skew -> refuse column geometry, one region, low confidence.
      2. Pull stamped OVERLAY chunks (markedly larger font + wide gap on a body
         baseline) into their own trailing region so they never splice into a
         sentence.
      3. Recursively split the remaining body into COLUMNS on a wide, mostly-empty
         vertical gutter, gated so no single-column layout can ever be split.

    When nothing fires, returns ``[chunks]`` unchanged -> byte-identical output.
    """

    signal = _default_page_signal()
    if rotated:
        signal["rotated"] = True
        signal["confidence"] = 0.4
        signal["reasons"] = ["cm_rotation_or_skew"]
        return [chunks], signal

    body_font = _chunk_dominant_font(chunks)
    body_chunks, overlay_chunks = _pull_overlay_chunks(chunks, body_font)

    regions, col_conf, col_reasons, near_miss = _recursive_column_split(
        body_chunks, page_width, body_font, _text_height(body_chunks), depth=0
    )
    if len(regions) > 1:
        signal["reorder"] = True
        signal["columns"] = len(regions)
        signal["confidence"] = min(signal["confidence"], col_conf)
        signal["reasons"].extend(col_reasons)
    elif near_miss:
        # A candidate gutter cleared the flag bar but missed the reorder bar: we do
        # NOT reorder (status-quo text, byte-safe) but we say so, loudly.
        signal["confidence"] = min(signal["confidence"], 0.5)
        signal["reasons"].extend(near_miss)

    if overlay_chunks:
        regions = list(regions) + [overlay_chunks]
        signal["overlay"] = True
        signal["confidence"] = min(signal["confidence"], 0.6)
        signal["reasons"].append("stamped_overlay_order_unknown")

    # Dedupe reasons preserving order.
    seen: set = set()
    signal["reasons"] = [r for r in signal["reasons"] if not (r in seen or seen.add(r))]
    return regions, signal


def _text_height(chunks: list[Chunk]) -> float:
    ys = [float(c[1]) for c in chunks]
    if not ys:
        return 0.0
    return max(ys) - min(ys)


def _pull_overlay_chunks(
    chunks: list[Chunk], body_font: float
) -> tuple[list[Chunk], list[Chunk]]:
    """Separate stamped-overlay chunks from body chunks.

    An overlay chunk is markedly LARGER than the body font AND sits a wide
    horizontal gap away from smaller (body) text on the SAME baseline. Both
    conditions are required, so a centered title alone on its own baseline (no
    body text beside it) and a same-font two-cell table row (no font jump) are
    NEVER pulled — they stay in the body, byte-identical.
    """

    # Bucket by baseline.
    buckets: dict[float, list[Chunk]] = {}
    order = sorted(chunks, key=lambda c: (-float(c[1]), float(c[0])))
    for c in order:
        placed = False
        for by in buckets:
            if abs(by - float(c[1])) <= _SAME_LINE_Y_TOLERANCE:
                buckets[by].append(c)
                placed = True
                break
        if not placed:
            buckets[float(c[1])] = [c]

    overlays: list[Chunk] = []
    overlay_ids: set[int] = set()
    for _by, bucket in buckets.items():
        smaller = [c for c in bucket if c[2] and c[2] < _OVERLAY_FONT_FACTOR * body_font]
        if not smaller:
            continue
        for c in bucket:
            if not c[2] or c[2] < _OVERLAY_FONT_FACTOR * body_font:
                continue
            cx = float(c[0])
            cx_end = cx + _chunk_width(c, body_font)
            # Horizontal gap to the nearest smaller (body) chunk on this baseline.
            gap = None
            for s in smaller:
                sx = float(s[0])
                sx_end = sx + _chunk_width(s, body_font)
                if cx >= sx_end:
                    d = cx - sx_end
                elif sx >= cx_end:
                    d = sx - cx_end
                else:
                    d = 0.0  # overlapping -> not a side-by-side overlay gap
                gap = d if gap is None else min(gap, d)
            if gap is not None and gap >= _OVERLAY_MIN_GAP_EM * (c[2] or body_font):
                overlays.append(c)
                overlay_ids.add(id(c))

    if not overlays:
        return chunks, []
    body = [c for c in chunks if id(c) not in overlay_ids]
    return body, overlays


def _recursive_column_split(
    chunks: list[Chunk],
    page_width: Optional[float],
    body_font: float,
    total_text_height: float,
    depth: int,
) -> tuple[list[list[Chunk]], float, list[str], list[str]]:
    """Recursively split ``chunks`` into left-to-right column regions.

    Returns ``(regions, confidence, reasons, near_miss_reasons)``. ``regions`` is
    ``[chunks]`` (a single region) when no confident split is found; the caller
    treats ``len(regions) == 1`` as "no reorder". Recursion resolves 3+/unbalanced
    columns; a band that fans into more than ``_MAX_COLUMNS`` columns is treated as
    tabular and left unsplit (row-major).
    """

    if depth >= _MAX_COLUMN_DEPTH:
        return [chunks], 1.0, [], []
    kind, payload = _find_one_gutter(chunks, page_width, body_font, total_text_height)
    if kind == "near_miss":
        return [chunks], 1.0, [], list(payload)
    if kind != "split":
        return [chunks], 1.0, [], []
    left, right, conf = payload
    left_regions, lc, lr, lnm = _recursive_column_split(
        left, page_width, body_font, total_text_height, depth + 1
    )
    right_regions, rc, rr, rnm = _recursive_column_split(
        right, page_width, body_font, total_text_height, depth + 1
    )
    regions = list(left_regions) + list(right_regions)
    if len(regions) > _MAX_COLUMNS:
        # Too many columns for prose: treat as tabular, do not linearize.
        return [chunks], 1.0, [], []
    confidence = min(conf, lc, rc)
    reasons = ["column_reconstructed"] + lr + rr
    return regions, confidence, reasons, lnm + rnm


def _find_one_gutter(
    chunks: list[Chunk],
    page_width: Optional[float],
    body_font: float,
    total_text_height: float,
) -> tuple[str, Any]:
    """Find ONE vertical gutter splitting ``chunks`` into a left and right column.

    Returns one of:
      ``("split", (left, right, confidence))``  -> a confident, gated split.
      ``("near_miss", [reason, ...])``          -> a candidate gutter that cleared
                                                   the low FLAG bar but missed the
                                                   high REORDER bar; do NOT reorder.
      ``("none", None)``                        -> no candidate at all.

    ALL of these gates must pass for a split, each killing a distinct false-positive
    class:
      * gutter width  >= max(2em, 3% page)  -> justified inter-word gaps, list
        indents, wide margins.
      * both sides median line length >= 15 -> Name:/Date: sig lines, page-number
        columns, term|definition and party|address 2-cell tables.
      * each side >= 3 baselines, region >= 6 baselines -> 2-cell blocks.
      * corridor empty across >= 85% of baselines -> centered titles, right-aligned
        folios (empty on only one row).
      * gutter vertical extent >= 50% of page text height -> top/bottom short blocks.
    """

    baselines = _distinct_baselines(chunks)
    if len(baselines) < _MIN_REGION_BASELINES:
        return ("none", None)

    intervals = [
        (float(c[0]), float(c[0]) + _chunk_width(c, body_font)) for c in chunks
    ]
    # Union the x-intervals and find the widest empty corridor between covered runs.
    covered = sorted(intervals)
    merged: list[list[float]] = []
    for a, b in covered:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if len(merged) < 2:
        return ("none", None)
    gaps = [(merged[i][1], merged[i + 1][0]) for i in range(len(merged) - 1)]
    g0, g1 = max(gaps, key=lambda g: g[1] - g[0])
    width = g1 - g0

    page_w = page_width or 612.0
    min_width = max(_GUTTER_MIN_WIDTH_EM * body_font, _GUTTER_MIN_WIDTH_PAGE_FRAC * page_w)
    if width < min_width:
        if width >= 0.7 * min_width:
            return ("near_miss", ["possible_multi_column_narrow_gutter"])
        return ("none", None)
    g_mid = (g0 + g1) / 2.0
    left = [c for c in chunks if float(c[0]) < g_mid]
    right = [c for c in chunks if float(c[0]) >= g_mid]
    if not left or not right:
        return ("none", None)

    left_baselines = _distinct_baselines(left)
    right_baselines = _distinct_baselines(right)
    if len(left_baselines) < _COLUMN_MIN_BASELINES or len(right_baselines) < _COLUMN_MIN_BASELINES:
        return ("none", None)

    left_lines = _group_chunks_into_lines(left)
    right_lines = _group_chunks_into_lines(right)
    left_med = median([len(l.text) for l in left_lines]) if left_lines else 0
    right_med = median([len(l.text) for l in right_lines]) if right_lines else 0
    if left_med < _SIDE_MIN_MEDIAN_CHARS or right_med < _SIDE_MIN_MEDIAN_CHARS:
        # Short cells on a side -> a table/sig block, not two-column prose.
        if min(left_med, right_med) >= 0.7 * _SIDE_MIN_MEDIAN_CHARS:
            return ("near_miss", ["possible_multi_column_short_side"])
        return ("none", None)

    # Coverage: a baseline "violates" the gutter when a chunk spans across it.
    spanning = 0
    for by in baselines:
        row = [c for c in chunks if abs(float(c[1]) - by) <= _SAME_LINE_Y_TOLERANCE]
        crosses = any(
            float(c[0]) < g0 and (float(c[0]) + _chunk_width(c, body_font)) > g1
            for c in row
        )
        if crosses:
            spanning += 1
    coverage = 1.0 - spanning / max(1, len(baselines))
    if coverage < _GUTTER_COVERAGE_FRAC:
        if coverage >= _GUTTER_COVERAGE_FRAC - 0.1:
            return ("near_miss", ["possible_multi_column_low_coverage"])
        return ("none", None)

    # Vertical extent of the gutter vs the page's total text height.
    gutter_extent = _text_height(left + right)
    if total_text_height > 0 and gutter_extent / total_text_height < _REGION_HEIGHT_FRAC:
        return ("none", None)

    # Confidence: crisp, wide, fully-clear gutter approaches the width-estimate
    # ceiling; a gutter exactly at threshold scores 0 and fires the loud banner.
    gutter_sharpness = _clamp((width - min_width) / min_width, 0.0, 1.0)
    coverage_margin = _clamp(
        (coverage - _GUTTER_COVERAGE_FRAC) / (1.0 - _GUTTER_COVERAGE_FRAC), 0.0, 1.0
    )
    confidence = _WIDTH_ESTIMATE_CEILING * (0.5 + 0.5 * min(gutter_sharpness, coverage_margin))
    return ("split", (left, right, confidence))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _chunks_are_glyph_fragmented(
    chunks: list[tuple[float, float, Optional[float], str]],
) -> bool:
    """True when the page draws its text one glyph per positioned operation.

    Detection is a run test in DRAWING ORDER: ``_GLYPH_FRAGMENT_RUN_MIN``
    consecutive single-character chunks. Per-glyph writers emit long runs of
    lone characters (a six-letter word is already six), while word-processor
    output draws whole runs/lines per operation, so ordinary pages never reach
    the threshold — an isolated superscript, page number or standalone clause
    number is a run of one. Space glyphs never break a run because the visitor
    drops whitespace-only chunks before they are recorded.

    When this fires, ``_extract_geo_lines`` abandons the visitor-geometry path
    for the page: the width-less, stale-``tm`` visitor chunks cannot be grouped
    back into faithful lines (see the fallback comment there), and pypdf's own
    metrics-aware ``extract_text`` must supply the page text instead.
    """

    run = 0
    for _x, _y, _size, text in chunks:
        # Measure the TRIMMED glyph (the visitor may carry a boundary space for the
        # line-join): a letter-spaced per-glyph run reports chunks like " I" whose
        # single rendered glyph must still count as a length-1 fragment.
        if len(text.strip()) == 1:
            run += 1
            if run >= _GLYPH_FRAGMENT_RUN_MIN:
                return True
        else:
            run = 0
    return False


def _group_chunks_into_lines(
    chunks: list[tuple[float, float, Optional[float], str]],
) -> list[GeoLine]:
    """Group visitor chunks that share a baseline into single visual lines."""

    if not chunks:
        return []
    # Preserve reading order: top-to-bottom (descending y), then left-to-right.
    ordered = sorted(chunks, key=lambda chunk: (-chunk[1], chunk[0]))
    lines: list[GeoLine] = []
    bucket: list[tuple[float, float, Optional[float], str]] = []
    bucket_y: Optional[float] = None
    for x, y, size, text in ordered:
        if bucket_y is None or abs(y - bucket_y) <= _SAME_LINE_Y_TOLERANCE:
            bucket.append((x, y, size, text))
            bucket_y = y if bucket_y is None else bucket_y
        else:
            lines.append(_merge_line_bucket(bucket))
            bucket = [(x, y, size, text)]
            bucket_y = y
    if bucket:
        lines.append(_merge_line_bucket(bucket))
    return [line for line in lines if line.text]


def _merge_line_bucket(bucket: list[tuple[float, float, Optional[float], str]]) -> GeoLine:
    bucket = sorted(bucket, key=lambda chunk: chunk[0])
    text = _join_line_chunks(bucket)
    left_x = min((chunk[0] for chunk in bucket), default=None)
    y = bucket[0][1] if bucket else None
    sizes = [chunk[2] for chunk in bucket if chunk[2] is not None]
    font_size = max(sizes) if sizes else None
    return GeoLine(text=text, left_x=left_x, y=y, font_size=font_size)


def _estimate_text_width(text: str, font_size: Optional[float]) -> float:
    """Rendered width of ``text`` in points from proportional-Latin advances."""
    size = font_size if (font_size and font_size > 0) else _ADJACENCY_FALLBACK_FONT_PT
    em = sum(_GLYPH_ADVANCE_EM.get(char, _DEFAULT_GLYPH_ADVANCE_EM) for char in text)
    return em * float(size)


def _join_line_chunks(bucket: list[tuple[float, float, Optional[float], str]]) -> str:
    """Join a visual line's x-sorted chunks (GARBLE PRODUCER fix, defect 4).

    DEFAULTS to a space between chunks (the legacy behavior, so column reorders,
    tables and newline-separated cells stay byte-identical) and SUPPRESSES the space
    only for a pair that is provably edge-to-edge: neither side carries pypdf's own
    word-gap whitespace AND the next chunk begins within ``_ADJACENCY_TOLERANCE_EM``
    of this chunk's estimated right edge. Internal whitespace is collapsed; the
    result is stripped.
    """

    pieces: list[str] = []
    prev_x: Optional[float] = None
    prev_size: Optional[float] = None
    prev_trimmed: str = ""
    prev_had_trailing_space = False
    for x, _y, size, raw in bucket:
        collapsed = " ".join(raw.split())
        if not collapsed:
            continue
        has_leading = raw[:1].isspace()
        has_trailing = raw[-1:].isspace()
        if pieces:
            prev_right = (prev_x or 0.0) + _estimate_text_width(prev_trimmed, prev_size)
            unit = prev_size if (prev_size and prev_size > 0) else _ADJACENCY_FALLBACK_FONT_PT
            adjacent = (
                not prev_had_trailing_space
                and not has_leading
                and (x - prev_right) <= _ADJACENCY_TOLERANCE_EM * float(unit)
            )
            if not adjacent:
                pieces.append(" ")
        pieces.append(collapsed)
        prev_x = x
        prev_size = size
        prev_trimmed = collapsed
        prev_had_trailing_space = has_trailing
    return "".join(pieces).strip()


def _safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _normalized_lines(text: str) -> list[str]:
    return [
        " ".join(raw_line.split())
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if " ".join(raw_line.split())
    ]


def _split_pdf_paragraphs(lines: Any) -> list[str]:
    """Split a page's lines into clause blocks (text only).

    Thin wrapper over ``_split_pdf_paragraph_blocks`` that flattens each block of
    ``GeoLine`` back into its joined text, preserving the legacy ``list[str]``
    contract for existing callers/tests.
    """

    return [" ".join(item.text for item in block) for block in _split_pdf_paragraph_blocks(lines)]


def _split_pdf_paragraph_blocks(lines: Any) -> list[list[GeoLine]]:
    """Split a page's lines into clause blocks, KEEPING per-line geometry.

    Accepts either ``list[GeoLine]`` (geometry-aware path) or a plain
    ``list[str]`` (geometry absent / legacy callers). The cardinal invariant is
    NEVER MERGE two genuinely separate clauses: geometry (vertical gaps, font
    jumps) only ever *adds* boundaries the text heuristics miss, and the only
    boundary the geometry path *removes* is a mid-sentence text split between two
    lines that are vertically adjacent (a single wrapped clause), which by
    definition cannot be two separate clauses.

    Returns each block as its list of ``GeoLine`` so a caller can recover the
    block's geometry (heading font size, indentation) AND the page-wide body
    font for the source-backed-PDF trust tier. The text-only ``_split_pdf_paragraphs``
    flattens these.
    """

    geo_lines = _as_geo_lines(lines)
    if not geo_lines:
        return []

    body_font = _dominant_font_size(geo_lines)
    line_height = _dominant_line_height(geo_lines, body_font)
    # NO page-global wrap signal is computed any more. The round-3 build derived a
    # page-wide "this page has at least one proven wrap" flag and fed it into the
    # sub-pitch JOIN decision; because the flag was page-wide it LEAKED — two
    # genuinely-separate finished clauses one wrap apart merged whenever a real wrap
    # appeared anywhere else on the page. The split/join decision is now made purely
    # from the LOCAL line pair, so never-merge holds by construction.

    blocks: list[list[GeoLine]] = []
    current: list[GeoLine] = []
    for geo_line in geo_lines:
        if _starts_new_pdf_paragraph(geo_line, current, line_height, body_font):
            blocks.append(current)
            current = []
        current.append(geo_line)
    if current:
        blocks.append(current)
    return blocks


def _pdf_paragraph_geometry(
    block: list[GeoLine], page_body_font: Optional[float]
) -> dict[str, object] | None:
    """Promote the per-line geometry of a block's HEADING line into metadata.

    The heading line is the block's FIRST line (where a numbered/heading marker
    sits). We surface its ``font_size`` and ``left_x``, the page-wide body font,
    and a derived ``heading_font_ratio`` so a downstream consumer (the structure
    builder's ``pdf_confident`` trust tier) can decide whether a regex heading
    match is CORROBORATED by geometry — a heading set in a larger font — rather
    than admitting a phantom (e.g. an address digit) on a bare regex match alone.

    Returns ``None`` when the block carries no usable geometry at all (flat-text
    fallback path), so a paragraph without geometry simply never becomes
    ``pdf_confident`` and the source-backed gate stays closed for it.
    """

    if not block:
        return None
    head = block[0]
    geometry: dict[str, object] = {}
    if head.font_size is not None:
        geometry["font_size"] = head.font_size
    if head.left_x is not None:
        geometry["left_x"] = head.left_x
    if head.y is not None:
        geometry["y"] = head.y
    if page_body_font is not None and page_body_font > 0:
        geometry["body_font"] = page_body_font
        if head.font_size is not None:
            geometry["heading_font_ratio"] = head.font_size / page_body_font
    if not geometry:
        return None
    return geometry


def _as_geo_lines(lines: Any) -> list[GeoLine]:
    result: list[GeoLine] = []
    for line in lines:
        if isinstance(line, GeoLine):
            if line.text:
                result.append(line)
        else:
            text = " ".join(str(line).split())
            if text:
                result.append(GeoLine(text=text, left_x=None, y=None, font_size=None))
    return result


def _dominant_line_height(geo_lines: list[GeoLine], body_font: Optional[float] = None) -> Optional[float]:
    """Estimate the body wrap pitch (single-line wrap distance) in points.

    The pitch is ANCHORED to the font size (``~1.2 * body_font``) or a fixed
    floor — NOT to the smallest observed baseline gap. This is the fix for the
    single-line-clause merge bug: on a page where every clause is one line there
    is no wrapped line to sample, so the previous "smallest-gap cluster" estimate
    took a CLAUSE gap as the pitch and the boundary threshold became unreachable,
    merging separate clauses.

    The smallest-gap cluster is used ONLY as a downward refinement, and only when
    wrapped lines actually exist: a wrap sits strictly below the font-derived
    pitch, so we refine using sub-font gaps alone. The result is clamped so it can
    never exceed the font/floor anchor — under-estimating the pitch biases toward
    SPLITTING (never-merge-safe), never toward merging.
    """

    anchor = _font_wrap_pitch(body_font)

    deltas: list[float] = []
    previous_y: Optional[float] = None
    for geo_line in geo_lines:
        if geo_line.y is None:
            previous_y = None
            continue
        if previous_y is not None:
            delta = previous_y - geo_line.y
            if delta > _SAME_LINE_Y_TOLERANCE:
                deltas.append(delta)
        previous_y = geo_line.y
    if not deltas:
        # No geometry to sample: fall back to the font/floor anchor so callers on
        # the geometry path still have a font-anchored pitch (never a clause gap).
        return anchor

    # Only gaps strictly below the font-derived pitch can be genuine wraps; gaps at
    # or above it are clause/paragraph spacing and must not lower the wrap pitch.
    wrap_gaps = sorted(delta for delta in deltas if delta < anchor - _GAP_EPSILON)
    if not wrap_gaps:
        # No sub-font gaps -> no observable wraps -> keep the font/floor anchor.
        return anchor
    smallest = wrap_gaps[0]
    pitch_cluster = [delta for delta in wrap_gaps if delta <= smallest * 1.25]
    refined = median(pitch_cluster)
    # DEFENSE-IN-DEPTH PITCH CAP: refinement may only ever LOWER the pitch, never raise
    # it above the font-anchored value. Observed baseline gaps are allowed to refine the
    # wrap pitch DOWN toward a tighter true wrap, but the font anchor (~1.2 * body_font)
    # is a hard ceiling. This bounds the uniform-spacing inflation that fed the round-5
    # bypass: even when every gap on the page is identical clause spacing, the pitch can
    # never be reported ABOVE the font anchor. (It can still settle AT the clause spacing
    # when that spacing happens to sit below the anchor — distinguishing a clause gap from
    # a wrap gap is impossible when they are identical — but the continuation gate in
    # _starts_new_pdf_paragraph makes that residual inflation un-exploitable: no JOIN can
    # absorb a capitalized next clause regardless of how low the pitch reads.)
    return min(refined, anchor)


def _font_wrap_pitch(body_font: Optional[float]) -> float:
    """Font-anchored expected wrap pitch in points (or a fixed floor)."""

    if body_font and body_font > 0:
        return body_font * _WRAP_PITCH_FONT_FACTOR
    return _WRAP_PITCH_FLOOR_POINTS


def _has_geometry(previous: GeoLine, line: GeoLine, line_height: Optional[float]) -> bool:
    """True iff both lines carry a baseline and we have a usable pitch threshold.

    When this is true the vertical gap is a trustworthy signal and drives the
    split/join decision directly. When it is false (visitor never fired, or one of
    the lines lacks a baseline) we have no geometric way to tell a wrap from a
    boundary and must fail safe by fragmenting (never merging).
    """

    return (
        previous.y is not None
        and line.y is not None
        and line_height is not None
        and line_height > 0
    )


def _dominant_font_size(geo_lines: list[GeoLine]) -> Optional[float]:
    sizes = [geo_line.font_size for geo_line in geo_lines if geo_line.font_size]
    if not sizes:
        return None
    return median(sizes)


def _starts_new_pdf_paragraph(
    line: GeoLine,
    current: list[GeoLine],
    line_height: Optional[float] = None,
    body_font: Optional[float] = None,
) -> bool:
    if not current:
        return False
    previous = current[-1]

    # --- THE GEOMETRY GAP CHECK IS THE FIRST GATE. ---
    # When per-line geometry is present, the vertical gap is the most trustworthy
    # signal we have, and a gap exceeding the font-anchored line pitch can NEVER be an
    # ordinary line wrap — it is paragraph spacing, i.e. a clause boundary. So when
    # the gap is wider than the pitch we SPLIT here UNCONDITIONALLY, before ANY
    # text-marker join guard runs. This is what makes never-merge STRUCTURAL: a
    # standalone-number previous line, a marker-led-open block, and a mid-sentence
    # wrap join are all evaluated ONLY below, AFTER this gate, and ONLY when the gap
    # is sub-pitch. No join guard can fire across a >pitch gap because control never
    # reaches one: a >pitch gap returns True right here. Two clauses separated by any
    # paragraph spacing therefore always split, regardless of markers.
    if _has_geometry(previous, line, line_height):
        if not _lines_are_adjacent(previous, line, line_height):
            # gap > pitch -> clause boundary, ALWAYS. No marker-led absorb, no
            # standalone-number join, no mid-sentence-wrap join may override it.
            return True

        # gap <= pitch (TRUE sub-pitch adjacency). Only now may a join guard apply —
        # the lines are genuinely one wrap apart, so a JOIN cannot bridge a paragraph
        # gap. We still split on structural heading/font cues; the only JOINs are the
        # three sub-pitch cases below.

        # A jump up in font size starts a new (heading) clause even at sub-pitch.
        if _has_heading_font_jump(previous, line, body_font):
            return True
        # Structural text markers split (numbered/lettered clause start, heading),
        # EXCEPT when the next line is the body of a standalone-number/marker pairing
        # handled by the JOIN guards below.
        if _is_heading(line.text):
            return True
        if _is_clause_start(line.text):
            return True
        if _is_standalone_clause_number(line.text) and not _is_standalone_clause_number(previous.text):
            if _block_has_prose(current):
                return True

        # The CONTINUATION GATE governs the body-absorbing JOINs (1, 2 and 3). A line a
        # marker-led block, a lone clause number, or an unfinished sentence may absorb
        # must be an UNAMBIGUOUS CONTINUATION: its first non-whitespace character is a
        # LOWERCASE LETTER. A genuine mid-sentence wrap ALWAYS continues with a lowercase
        # letter; a NEW clause/sentence NEVER opens with a bare lowercase letter — it
        # starts with a capital, a digit, a marker, a quote of any kind, a bullet, a
        # dash, a currency or section symbol, an open paren/bracket, etc. The gate is a
        # POSITIVE lowercase-only test (round-8): it is COMPLETE BY CONSTRUCTION — there
        # is no enumeration of clause-start markers to keep exhaustive, so no clause-start
        # character can be omitted and mis-read as a continuation. Anything that is not a
        # lowercase-letter lead falls through to SPLIT. This subsumes the round-5 fix
        # (capitalized next line cannot be absorbed even under an inflated sub-pitch) and
        # closes the round-7 hole where a non-enumerated start (e.g. a curly single quote
        # U+2018 opening a defined-term clause) slipped through as a continuation.
        next_is_continuation = _is_lowercase_continuation(line.text)

        # JOIN 1 — the standalone-number + its adjacent body. A bare clause number
        # ("2") immediately above (sub-pitch) a LOWERCASE continuation line absorbs
        # only that body — and ONLY when the next line is an unambiguous lowercase
        # continuation. A CAPITALIZED next line (e.g. a title "Confidentiality") is a
        # fresh clause/sentence start and falls through to SPLIT, so a lone number can
        # never bridge into a separate capitalized clause even under an inflated
        # (clause-spacing) pitch. The number fragmenting from its capitalized title is
        # the accepted safe failure under never-merge-absolute. Cannot bridge a real
        # paragraph gap: the geometry gap gate above has already kept us sub-pitch.
        if _is_standalone_clause_number(previous.text) and next_is_continuation:
            return False
        # JOIN 2 — the marker-led-open block absorbs its OWN immediately-adjacent body
        # line. A block opened by a clause number/heading whose last line has not yet
        # finished a sentence is a heading still reading its body; the sub-pitch next
        # line is that body — but ONLY when that next line is an unambiguous lowercase
        # continuation. A capitalized sentence-start next line is a fresh clause and
        # falls through to SPLIT, so the marker-led-open guard can never swallow a
        # separate clause even under an inflated (clause-spacing) pitch.
        if _block_is_marker_led_open(current) and next_is_continuation:
            return False
        # JOIN 3 — the one unambiguous mid-sentence wrap: previous line UNFINISHED (no
        # terminal punctuation) AND next line a lowercase continuation (not a fresh
        # sentence start). This is the unmistakable signature of a sentence that ran
        # out of horizontal space; it cannot be two clauses.
        previous_finished = _ends_sentence(previous.text)
        if not previous_finished and next_is_continuation:
            return False
        # EVERYTHING ELSE at sub-pitch SPLITS. In particular a FINISHED sentence
        # followed by a sentence-start one wrap apart always splits — there is no
        # page-global "page has wraps" escape hatch (that signal was page-wide and
        # leaked, merging two genuinely-separate finished clauses). The accepted cost
        # is that a single multi-sentence clause whose sentence boundary lands exactly
        # on a line break fragments into two blocks — the chosen safe failure.
        return True

    # --- No geometry for this line pair (visitor never fired, or one line lacks a
    # baseline): we cannot tell a wrap from a clause boundary from spacing, so we
    # fail SAFE (fragment, never merge) EXCEPT for the pairings that are structurally
    # a single clause. Fragmenting one clause into several is acceptable; merging two
    # is not. ---
    # Keep the bare standalone clause number joined to the title/body it introduces.
    if _is_standalone_clause_number(previous.text):
        return False
    # OVER-FRAGMENTATION fix (defect 6): a flat-fallback page split at EVERY line
    # break, blowing one wrapped clause into many one-line paragraphs (~3x the DOCX
    # count). Merge the one pairing that is UNAMBIGUOUSLY a single wrapped sentence —
    # the SAME conservative rule the geometry path's JOIN 3 and the DOCX clause-merge
    # use: the previous line did NOT finish a sentence (no terminal punctuation) AND
    # this line is a lowercase continuation. A new clause never opens with a bare
    # lowercase letter and a finished sentence always splits, so this can never merge
    # two distinct clauses; every other line break still splits.
    if not _ends_sentence(previous.text) and _is_lowercase_continuation(line.text):
        return False
    return True


def _block_has_prose(current: list[GeoLine]) -> bool:
    """True when the accumulated block already contains a completed clause body.

    Used to decide whether a following standalone clause number opens a new
    clause. The block counts as prose once it ends a sentence, which marks the
    prior clause as complete — a standalone number after that is a new boundary.
    """

    if not current:
        return False
    return _ends_sentence(current[-1].text)


def _block_is_marker_led_open(current: list[GeoLine]) -> bool:
    """True when the block was opened by a structural marker and is still open.

    A block is "marker-led" when its FIRST line is a clause number, a numbered/
    lettered clause start, or a short heading/title. It is "open" while its last
    line has not completed a sentence — i.e. the clause body is still being read.
    A line that follows such a block one wrap apart is that clause's body (the
    title/number absorbs its prose), so we JOIN rather than fragment the heading
    from its body. A markerless block (e.g. a definition list) is never marker-led,
    so its entries split — preserving the never-merge invariant.
    """

    if not current:
        return False
    first = current[0].text
    marker_led = (
        _is_standalone_clause_number(first)
        or _is_clause_start(first)
        or _is_heading(first)
    )
    return marker_led and not _ends_sentence(current[-1].text)


def _has_heading_font_jump(previous: GeoLine, line: GeoLine, body_font: Optional[float]) -> bool:
    if line.font_size is None or previous.font_size is None or body_font is None:
        return False
    # New line is meaningfully larger than the body AND larger than what precedes
    # it -> a heading begins a new clause.
    return line.font_size >= body_font * _HEADING_FONT_FACTOR and line.font_size > previous.font_size + 0.5


def _lines_are_adjacent(previous: GeoLine, line: GeoLine, line_height: Optional[float]) -> bool:
    """True only when geometry confirms the lines are one wrap apart (no gap).

    Adjacency is measured against the WRAP PITCH (``line_height``), not the larger
    paragraph-gap threshold: a gap meaningfully exceeding the wrap pitch is spacing,
    not a wrap, and must never be read as adjacent (which could merge two clauses).
    """

    if previous.y is None or line.y is None or line_height is None or line_height <= 0:
        return False
    gap = previous.y - line.y
    if gap <= _SAME_LINE_Y_TOLERANCE:
        return True
    return gap <= line_height + _GAP_EPSILON


def _is_clause_start(line: str) -> bool:
    return bool(re.match(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.|\([a-z0-9ivx]+\))\s+\S", line))


def _is_standalone_clause_number(line: str) -> bool:
    return bool(re.match(r"^\d+(?:\.\d+)*\.?$", line))


def _is_heading(line: str) -> bool:
    words = re.findall(r"[A-Za-z]+", line)
    if len(words) < 3 or len(words) > 14 or len(line) > 120:
        return False
    if words[0].lower() in {"this", "the", "each", "either", "any", "such"}:
        return False
    uppercase_words = [word for word in words if word.isupper() or word[:1].isupper()]
    return len(uppercase_words) >= max(1, int(len(words) * 0.75)) and not _ends_sentence(line)


def _ends_sentence(line: str) -> bool:
    return bool(re.search(r"[.;:!?)](?:[\"”’])?$", line))


def _is_lowercase_continuation(line: str) -> bool:
    """True IFF ``line`` begins a lowercase WORD (the start of a mid-sentence wrap).

    This is the continuation gate for all three body-absorbing JOINs. It is a
    POSITIVE test for "the next line continues a sentence", made COMPLETE BY
    CONSTRUCTION: a genuine mid-sentence wrap always continues with a real
    lowercase WORD, while a new clause/sentence/list-item never does — it opens
    with a capital, a digit, a marker, a quote of any kind, a bullet, a dash, a
    currency or section symbol, an open paren/bracket, OR a list enumerator. Every
    such lead falls through to SPLIT, so no clause-start can be mis-absorbed.

    Two precise exclusions matter here:

    1. ``str.islower()`` / the Unicode LOWERCASE PROPERTY is WRONG for the first
       char: it is True for non-letter MARKER glyphs that lead auto-numbered list
       items — small Roman numerals U+2170+ (category Nl), circled latin small
       letters U+24D0+ (So), ordinal indicators U+00AA/U+00BA (Lo), modifier
       letters (Lm), script small l U+2113 (Ll-but-symbol-like). A list item that
       text-extracts as such a single glyph must SPLIT, not JOIN. So we require the
       first char to be a lowercase LETTER specifically: ASCII a-z OR
       ``unicodedata.category(first) == "Ll"`` (which EXCLUDES Nl/So/Lo/Lm/No).

    2. A SHORT list enumerator — single OR multi-letter — begins with a lowercase
       letter but is a MARKER, not a word, and must SPLIT. Round-9 only excluded the
       LONE single-letter case ("i.", "a)") by testing the SECOND char, so a
       multi-letter ASCII roman/alpha enumerator ("ii.", "iii.", "iv.", "viii.",
       "ix.", "ab)") — whose second char is itself a LETTER — slipped through and
       was read as a continuation, MERGING a carve-out sub-list (e.g. a "Permitted
       Disclosures" list of items i. ii. iii. iv.) into its lead-in line. So we
       take the leading MAXIMAL run of ASCII lowercase letters (after stripping an
       optional opening "(" or "["), and if that run is SHORT (length 1..6) and is
       IMMEDIATELY followed by a list separator (".", ")", ":", "]"), the line is an
       enumerator, NOT a continuation. A genuine mid-sentence wrap word is never a
       short run immediately followed by a separator: it either is longer than 6
       letters or is followed by whitespace ("to its advisers" -> "to" then a SPACE;
       "information already ..." -> a long word then a space), so it still JOINs.
       Over-splitting a rare short-word-then-separator line is the accepted safe
       failure; we never merge.
    """

    stripped = line.lstrip()
    if not stripped:
        return False
    first = stripped[0]
    # The first char must be a lowercase LETTER (ASCII a-z or Unicode category Ll),
    # NOT merely a glyph with the lowercase property. This EXCLUDES Nl small Roman
    # numerals, So circled small letters, Lo ordinals, Lm modifiers, No, digits,
    # punctuation, symbols, bullets, and whitespace -> those all SPLIT.
    is_lowercase_letter = "a" <= first <= "z" or (
        unicodedata.category(first) == "Ll"
        # ...but EXCLUDE category-Ll letterlike SYMBOL glyphs (e.g. script small l
        # U+2113 ℓ, script small e U+212F): these are presentation/marker glyphs, not
        # word-starting letters. They carry a tagged compatibility decomposition
        # ("<font> 006C"), whereas genuine accented letters (é, ß, à) decompose
        # canonically or not at all. A tagged decomposition -> SPLIT.
        and not unicodedata.decomposition(first).startswith("<")
    )
    if not is_lowercase_letter:
        return False
    # Exclude a SHORT list enumerator — single OR multi-letter. ``first`` is already a
    # lowercase ASCII/Ll letter, so take the leading MAXIMAL run of ASCII lowercase
    # letters; if that run is short (length 1..6) and is IMMEDIATELY followed by a list
    # separator (".", ")", ":", "]"), the line is a list MARKER, not a word ("i.", "a)",
    # "b:", "c]", "ii.", "iii.", "iv.", "viii.", "ix.", "ab)"). A genuine mid-sentence
    # wrap word is never a short run immediately followed by a separator — it is either
    # longer than 6 letters or followed by whitespace ("to" -> SPACE) — so it still
    # JOINs. (A bracketed "(iii)" never reaches here: "(" is not a lowercase letter, so
    # it already SPLIT at the first-char gate above.) Over-splitting a rare
    # short-word-then-separator line is the accepted safe failure; never merge. -> SPLIT.
    run = re.match(r"[a-z]+", stripped)
    if run is not None:
        token = run.group()
        sep = stripped[len(token) : len(token) + 1]
        if 1 <= len(token) <= 6 and sep and sep in ".):]":
            return False
    return True


def _is_page_number(line: str) -> bool:
    return bool(re.match(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", line, flags=re.IGNORECASE))


def _filtered_geo_lines(geo_lines: list[GeoLine], repeated_margins: set[str]) -> list[GeoLine]:
    texts = [geo_line.text for geo_line in geo_lines]
    filtered: list[GeoLine] = []
    for index, geo_line in enumerate(geo_lines):
        if geo_line.text in repeated_margins:
            continue
        if _is_disposable_page_number(geo_line.text, index, texts):
            continue
        filtered.append(geo_line)
    return filtered


def _filtered_pdf_lines(lines: list[str], repeated_margins: set[str]) -> list[str]:
    filtered: list[str] = []
    for index, line in enumerate(lines):
        if line in repeated_margins:
            continue
        if _is_disposable_page_number(line, index, lines):
            continue
        filtered.append(line)
    return filtered


def _is_disposable_page_number(line: str, index: int, lines: list[str]) -> bool:
    if not _is_page_number(line):
        return False
    if _looks_like_standalone_clause_marker(line, index, lines):
        return False
    return True


def _looks_like_standalone_clause_marker(line: str, index: int, lines: list[str]) -> bool:
    if not _is_standalone_clause_number(line):
        return False
    if index >= len(lines) - 1:
        return False
    next_line = lines[index + 1]
    if _is_page_number(next_line):
        return False
    return bool(re.search(r"[A-Za-z]", next_line)) and len(next_line) <= 120


def _repeated_margin_lines(page_lines: list[list[str]]) -> set[str]:
    candidates: dict[str, int] = {}
    for lines in page_lines:
        for line in set([*lines[:2], *lines[-2:]]):
            if len(line) < 4 or _is_page_number(line) or not _is_non_substantive_margin_line(line):
                continue
            candidates[line] = candidates.get(line, 0) + 1
    minimum_repeats = max(2, int(len(page_lines) * 0.5))
    return {line for line, count in candidates.items() if count >= minimum_repeats}


def _is_non_substantive_margin_line(line: str) -> bool:
    normalized = str(line or "").strip()
    if not normalized or len(normalized) > 80 or _ends_sentence(normalized):
        return False
    words = re.findall(r"[A-Za-z]+", normalized)
    if len(words) > 2:
        return False
    return not re.search(
        r"\b(?:"
        r"agreement|clause|confidential|confidentiality|definition|disclos(?:e|ing|ure)|"
        r"information|mutual|nda|non-disclosure|nondisclosure|obligation|party|parties|"
        r"recipient|schedule|term|undertaking"
        r")\b",
        normalized,
        flags=re.IGNORECASE,
    )


def _letterspaced_garble_run(text: str) -> int:
    """Longest run of consecutive SHORT alphabetic tokens in the extracted text.

    This is the signal ``_garbled_text_ratio`` is structurally blind to: a page
    whose text is letter-spaced ("I N W I T N E S S W H E R E O F") or fragmented
    into kern/ligature pairs ("IN WI TN ES S WH ER EO F") is ALL short alphabetic
    tokens, yet contains zero unusual SYMBOLS, so the symbol-ratio scores 0.0.

    A token counts as SHORT when, stripped of surrounding punctuation, it is purely
    alphabetic and <= ``_GARBLE_SHORT_TOKEN_MAX_LEN`` characters. Any longer token
    (or a digit/marker token) breaks the run. Real prose scatters long words among
    its 1-2 letter words, so its runs stay tiny; a legitimate spaced "N D A" title
    is only a run of 3 — below ``_GARBLE_RUN_MIN`` — so it is never flagged.
    """

    best = 0
    run = 0
    for raw in text.split():
        stripped = raw.strip(".,;:!?()[]{}'\"“”‘’/-–—")
        if stripped.isalpha() and len(stripped) <= _GARBLE_SHORT_TOKEN_MAX_LEN:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _reading_order_signal(
    page_signals: list[dict[str, object]], extracted_text: str
) -> dict[str, object]:
    """Fold per-page reading-order signals + garble detection into ONE contract.

    This is the first-class confidence value the extractor returns (under
    ``quality["reading_order"]``) for the visibility layer to surface to the human
    reviewer. Document confidence is the MIN over pages (the worst page governs).
    See the module docstring / return for the precise, stable contract.
    """

    confidence = 1.0
    columns = 1
    reorder = False
    reasons: list[str] = []
    for sig in page_signals:
        try:
            confidence = min(confidence, float(sig.get("confidence", 1.0)))
            columns = max(columns, int(sig.get("columns", 1)))
        except (TypeError, ValueError):
            pass
        if sig.get("reorder") or sig.get("overlay"):
            reorder = True
        for reason in sig.get("reasons", []) or []:
            reasons.append(str(reason))

    garble_run = _letterspaced_garble_run(extracted_text)
    garbled = garble_run >= _GARBLE_RUN_MIN
    if garbled:
        confidence = min(confidence, 0.3)
        reasons.append("fragmented_or_letterspaced_text")

    seen: set = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return {
        "reading_order_confidence": round(confidence, 3),
        "columns_detected": columns,
        "reorder_applied": reorder,
        "garbled": garbled,
        "degraded": confidence < 0.8,
        "reasons": reasons,
    }


def _mark_reading_order_degraded(
    reading_order: dict[str, object] | None, reason: str
) -> None:
    """Flag the reading-order signal degraded and record a reason (idempotent)."""

    if not isinstance(reading_order, dict):
        return
    reading_order["degraded"] = True
    reasons = reading_order.get("reasons")
    if isinstance(reasons, list):
        if reason not in reasons:
            reasons.append(reason)
    else:
        reading_order["reasons"] = [reason]


def _pdf_quality_report(
    *,
    page_count: int,
    pages_with_text: int,
    pages_without_text: int,
    extracted_text: str,
    paragraph_count: int,
    repeated_margin_count: int,
    visual_profile: dict[str, object] | None = None,
    reading_order: dict[str, object] | None = None,
    form_fields_present: bool = False,
    image_only_pages_dropped: list[int] | None = None,
    ocr_pages_recovered: list[int] | None = None,
) -> dict[str, object]:
    extracted_characters = len(extracted_text)
    warnings: list[dict[str, object]] = []
    if pages_without_text:
        warnings.append({
            "type": "pdf_pages_without_text",
            "message": f"{pages_without_text} PDF page(s) produced no extractable text.",
        })
    if extracted_characters < 500:
        warnings.append({
            "type": "pdf_sparse_text",
            "message": "The PDF produced a small amount of extractable text; review the source carefully.",
        })
    if paragraph_count <= max(1, page_count // 2) and extracted_characters > 1000:
        warnings.append({
            "type": "pdf_low_paragraph_count",
            "message": "The PDF text may have been extracted as overly large paragraphs.",
        })
    if _garbled_text_ratio(extracted_text) > 0.25:
        warnings.append({
            "type": "pdf_garbled_text",
            "message": "The PDF extraction contains an unusual amount of symbols or spacing.",
        })
    if _visual_profile_requires_source_preview(visual_profile):
        warnings.append({
            "type": "pdf_visual_fidelity_requires_source_preview",
            "message": (
                "The PDF contains visual layout, color, image, or line-art signals that plain text extraction "
                "cannot preserve. Use the original PDF/page preview for layout review."
            ),
        })
    # Reading-order / layout confidence warning. Appended LAST and ONLY when the
    # signal is degraded, so a clean single-column page (confidence 1.0) adds no
    # warning and stays byte-identical to the pre-fix report.
    if reading_order and reading_order.get("degraded"):
        if reading_order.get("garbled"):
            warnings.append({
                "type": "pdf_fragmented_text",
                "message": (
                    "The PDF text appears letter-spaced or fragmented; the reviewer may be reading "
                    "scrambled words. Check the original source."
                ),
            })
        else:
            warnings.append({
                "type": "pdf_reading_order_uncertain",
                "message": (
                    "The PDF layout could not be fully verified (possible multi-column or overlay); "
                    "confirm the reading order against the original source."
                ),
            })
    # PER-PAGE OCR recovery notice (defect B2). Image-only pages amid text pages
    # that OCR rescued -- flag so the OCR'd text is not mistaken for a clean layer.
    if ocr_pages_recovered:
        pages_str = ", ".join(str(p) for p in ocr_pages_recovered)
        warnings.append({
            "type": "pdf_ocr_page_recovered",
            "message": (
                f"PDF page(s) {pages_str} had no text layer and were recovered by OCR; "
                "the text may contain transcription errors. Verify against the original document."
            ),
        })
    # DROPPED image-only pages (defect B2). A mixed text+image PDF whose scanned
    # page(s) could NOT be rescued -- name them explicitly and mark the extraction
    # degraded so nothing is silently lost.
    if image_only_pages_dropped:
        pages_str = ", ".join(str(p) for p in image_only_pages_dropped)
        warnings.append({
            "type": "pdf_image_only_pages_dropped",
            "message": (
                f"PDF page(s) {pages_str} contain no text layer (likely scanned or image-only) and "
                "their content is NOT included in this review. Review those pages against the original PDF."
            ),
        })
        _mark_reading_order_degraded(reading_order, "image_only_pages_dropped")
    # FILLABLE-FORM notice (defect B1). The form field values were appended in a
    # dedicated section rather than at their on-page position -- say so, and mark
    # the extraction degraded so the reviewer verifies placement.
    if form_fields_present:
        warnings.append({
            "type": "pdf_form_fields",
            "message": (
                "This PDF is a fillable form; its form field values were appended in a 'Form field values' "
                "section (their on-page position cannot be reliably derived). Verify against the original document."
            ),
        })
        _mark_reading_order_degraded(reading_order, "fillable_form_fields")
    quality: dict[str, object] = {
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "pages_without_text": pages_without_text,
        "extracted_characters": extracted_characters,
        "extracted_paragraphs": paragraph_count,
        "repeated_margin_lines_removed": repeated_margin_count,
        "warnings": warnings,
    }
    if visual_profile:
        quality["visual_profile"] = visual_profile
    if reading_order is not None:
        quality["reading_order"] = reading_order
    return quality


def _deferred_visual_profile() -> dict[str, object]:
    """Marker for a visual profile that was intentionally NOT computed (lazy path).

    Mirrors the ``status: "unavailable"`` contract that ``_pdf_visual_profile``
    returns on its own failure branches, so every downstream consumer
    (``_pdf_quality_report`` and ``source_fidelity``) treats a deferred profile as
    'not-yet-computed' rather than as 'no visual signals present' — the fail-open
    posture. ``requires_source_preview`` stays ``True`` so the source PDF/page
    preview is offered until the profile is recomputed on demand. The distinct
    ``reason`` lets callers tell a deferred profile apart from a runtime failure.
    """

    return {
        "status": "unavailable",
        "reason": "deferred",
        "requires_source_preview": True,
    }


def _pdf_visual_profile(data: bytes) -> dict[str, object]:
    """Return best-effort PDF visual signals that text extraction cannot preserve."""

    try:
        import fitz
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "pymupdf_not_installed",
            "requires_source_preview": True,
        }

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return {
            "status": "unavailable",
            "reason": "visual_profile_failed",
            "requires_source_preview": True,
        }

    try:
        text_span_count = 0
        non_black_text_span_count = 0
        image_count = 0
        drawing_count = 0
        drawings_cap_hit = False
        pages_with_non_black_text = 0
        pages_with_images = 0
        pages_with_drawings = 0
        unique_text_colors: set[int] = set()
        page_count = document.page_count
        profiled_pages = min(page_count, MAX_PDF_PAGES)
        # Suppress image-byte materialization in the per-page text dict (the peak-RSS
        # hog); count images via the lightweight get_image_info() instead. Falls back
        # to the default text dict on a PyMuPDF build without the flag constants.
        text_flags = _fitz_visual_text_flags(fitz)
        for page_index in range(profiled_pages):
            page = document[page_index]
            page_has_non_black_text = False
            page_has_images = False
            page_has_drawings = False
            if text_flags is not None:
                # Images are suppressed from the text dict below, so they never appear
                # as type==1 blocks; count them here via the lightweight image-info API.
                try:
                    page_images = page.get_image_info()
                except Exception:
                    page_images = []
                if page_images:
                    image_count += len(page_images)
                    page_has_images = True
            try:
                if text_flags is None:
                    blocks = page.get_text("dict").get("blocks", [])
                else:
                    blocks = page.get_text("dict", flags=text_flags).get("blocks", [])
            except Exception:
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == 1:
                    # Only reached on the default-flags fallback (text_flags is None),
                    # where images are NOT counted via get_image_info above.
                    image_count += 1
                    page_has_images = True
                    continue
                if block.get("type") != 0:
                    continue
                for line in block.get("lines") or []:
                    if not isinstance(line, dict):
                        continue
                    for span in line.get("spans") or []:
                        if not isinstance(span, dict):
                            continue
                        text_span_count += 1
                        color = _safe_int(span.get("color"))
                        if color is None:
                            continue
                        unique_text_colors.add(color)
                        if color != 0:
                            non_black_text_span_count += 1
                            page_has_non_black_text = True
            # DRAWINGS-BOMB CAP. ``page.get_drawings()`` returns one dict per vector
            # path; a PDF stuffed with millions of trivial path ops is otherwise an
            # unbounded memory sink inside the profiler. We need only drawing PRESENCE,
            # not an exact count, so once the document-wide cap is reached we stop
            # calling get_drawings() on further pages entirely, and we clamp any single
            # page's contribution to the per-page cap. The exact count is reported as
            # capped (drawings_count_capped) so downstream never mistakes the clamp for
            # a true total. This stays inside the existing fail-to-"unavailable" wrapper.
            if drawing_count < MAX_PDF_DRAWINGS_TOTAL:
                try:
                    page_drawings = page.get_drawings()
                except Exception:
                    page_drawings = []
                page_drawing_count = len(page_drawings) if page_drawings else 0
                # Drop the materialized list immediately; we only keep the count.
                page_drawings = None
                if page_drawing_count:
                    if page_drawing_count > MAX_PDF_DRAWINGS_PER_PAGE:
                        page_drawing_count = MAX_PDF_DRAWINGS_PER_PAGE
                        drawings_cap_hit = True
                    drawing_count += page_drawing_count
                    page_has_drawings = True
                    if drawing_count >= MAX_PDF_DRAWINGS_TOTAL:
                        drawing_count = MAX_PDF_DRAWINGS_TOTAL
                        drawings_cap_hit = True
            else:
                # Cap already hit on a prior page: skip the (potentially huge)
                # get_drawings() materialization but preserve the presence signal.
                page_has_drawings = True
                drawings_cap_hit = True
            if page_has_non_black_text:
                pages_with_non_black_text += 1
            if page_has_images:
                pages_with_images += 1
            if page_has_drawings:
                pages_with_drawings += 1
    except Exception:
        return {
            "status": "unavailable",
            "reason": "visual_profile_failed",
            "requires_source_preview": True,
        }
    finally:
        document.close()

    visual_features: list[str] = []
    if non_black_text_span_count:
        visual_features.append("colored_text")
    if drawing_count:
        visual_features.append("drawings_or_borders")
    if image_count:
        visual_features.append("images")
    requires_source_preview = bool(visual_features)
    return {
        "status": "ready",
        "page_count": page_count,
        "profiled_pages": profiled_pages,
        "text_span_count": text_span_count,
        "non_black_text_span_count": non_black_text_span_count,
        "unique_text_color_count": len(unique_text_colors),
        "drawing_count": drawing_count,
        "drawings_count_capped": drawings_cap_hit,
        "image_count": image_count,
        "pages_with_non_black_text": pages_with_non_black_text,
        "pages_with_drawings": pages_with_drawings,
        "pages_with_images": pages_with_images,
        "visual_features": visual_features,
        "requires_source_preview": requires_source_preview,
    }


def _visual_profile_requires_source_preview(visual_profile: dict[str, object] | None) -> bool:
    if not isinstance(visual_profile, dict):
        return False
    return bool(visual_profile.get("requires_source_preview"))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _garbled_text_ratio(text: str) -> float:
    if not text:
        return 1.0
    allowed_punctuation = set(".,;:!?()[]{}'\"“”‘’/@&%$#*+-–—\\")
    suspicious = sum(
        1
        for character in text
        if not character.isalnum() and not character.isspace() and character not in allowed_punctuation
    )
    return suspicious / max(1, len(text))
