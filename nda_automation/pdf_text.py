from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from statistics import median
from typing import Any, List, Optional

from .review_document import Paragraph


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

# Image-decompression-bomb guard. A tiny PDF can embed enormous images (huge
# pixel dimensions stored in a few KB of compressed bytes); decoding them to a
# raster explodes RSS. A probe measured a single 6000x6000 image (36 MP) costing
# ~274 MB RSS to decode (~7.6 bytes per decoded pixel). To bound total decoded
# memory to a few hundred MB — comfortably under the 2 GB ceiling even with
# interpreter/library overhead — we cap the SUMMED pixel area across the
# page-capped pages at 50 megapixels: 50 MP * ~7.6 B/px ~= 380 MB worst-case
# decode. The area is read from each image's declared width x height via PyMuPDF
# ``get_image_info`` (metadata only — it does NOT decode pixels), so an oversized
# PDF is rejected BEFORE any raster spike.
MAX_PDF_TOTAL_IMAGE_PIXELS = 50_000_000
IMAGE_BOMB_PDF_MESSAGE = (
    "The PDF embeds images too large to process safely "
    "(possible decompression bomb). Reduce the image resolution before reviewing."
)

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


@dataclass(frozen=True)
class PdfExtraction:
    paragraphs: List[Paragraph]
    quality: dict[str, object]


def extract_pdf_text(data: bytes) -> str:
    paragraphs = extract_pdf_paragraphs(data)
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)


def extract_pdf_paragraphs(data: bytes) -> List[Paragraph]:
    return extract_pdf_document(data).paragraphs


def extract_pdf_document(data: bytes) -> PdfExtraction:
    try:
        from io import BytesIO
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExtractionError(PDF_SUPPORT_NOT_INSTALLED_MESSAGE) from exc

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
    # Reject decompression-bomb images BEFORE any text/visual decode spikes RSS.
    _guard_pdf_image_pixels(data)
    pages_without_text = 0
    pages_with_text = 0
    extracted_character_count = 0
    repeated_margins: set[str] = set()
    for page in reader.pages:
        try:
            geo_lines = _extract_geo_lines(page)
        except Exception as exc:
            raise PdfExtractionError("The PDF text could not be extracted.") from exc
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

    paragraphs: List[Paragraph] = []
    for page_index, geo_lines in enumerate(page_geo_lines, start=1):
        filtered_lines = _filtered_geo_lines(geo_lines, repeated_margins)
        for paragraph_text in _split_pdf_paragraphs(filtered_lines):
            paragraphs.append({
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_index,
                "text": paragraph_text,
            })

    if not paragraphs:
        raise PdfExtractionError("No readable text was found in the PDF. Scanned PDFs need OCR before review.")
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
    visual_profile = _pdf_visual_profile(data)
    return PdfExtraction(
        paragraphs=paragraphs,
        quality=_pdf_quality_report(
            page_count=page_count,
            pages_with_text=pages_with_text,
            pages_without_text=pages_without_text,
            extracted_text=extracted_text,
            paragraph_count=len(paragraphs),
            repeated_margin_count=len(repeated_margins),
            visual_profile=visual_profile,
        ),
    )


def _extract_geo_lines(page: Any) -> list[GeoLine]:
    """Extract per-line text plus the geometry ``visitor_text`` exposes.

    pypdf's ``visitor_text`` callback fires once per drawn text chunk with the
    text-matrix translation (start x via ``tm[4]``, baseline y via ``tm[5]``)
    and the font size. We group those chunks into visual lines by baseline y so
    the splitter can use vertical gaps, indentation and font size — the only
    geometric signals reliably available at this layer. If the visitor yields
    nothing usable we fall back to flat ``extract_text`` lines with no geometry,
    which keeps the never-merge-safe text heuristics in force.
    """

    chunks: list[tuple[float, float, Optional[float], str]] = []

    def _visitor(text: str, _cm: Any, tm: Any, _font_dict: Any, font_size: Any) -> None:
        cleaned = " ".join(str(text).split())
        if not cleaned:
            return
        try:
            x = float(tm[4])
            y = float(tm[5])
        except (TypeError, ValueError, IndexError):
            return
        size = _safe_float(font_size)
        chunks.append((x, y, size, cleaned))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        chunks = []

    geo_lines = _group_chunks_into_lines(chunks)
    if geo_lines:
        return geo_lines

    # Fallback: no usable geometry (e.g. visitor unsupported). Use flat text with
    # no coordinates so the splitter relies purely on never-merge-safe text rules.
    try:
        page_text = page.extract_text() or ""
    except Exception:
        page_text = ""
    return [GeoLine(text=line, left_x=None, y=None, font_size=None) for line in _normalized_lines(page_text)]


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
    text = " ".join(" ".join(chunk[3].split()) for chunk in bucket if chunk[3].split())
    left_x = min((chunk[0] for chunk in bucket), default=None)
    y = bucket[0][1] if bucket else None
    sizes = [chunk[2] for chunk in bucket if chunk[2] is not None]
    font_size = max(sizes) if sizes else None
    return GeoLine(text=text, left_x=left_x, y=y, font_size=font_size)


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
    """Split a page's lines into clause blocks.

    Accepts either ``list[GeoLine]`` (geometry-aware path) or a plain
    ``list[str]`` (geometry absent / legacy callers). The cardinal invariant is
    NEVER MERGE two genuinely separate clauses: geometry (vertical gaps, font
    jumps) only ever *adds* boundaries the text heuristics miss, and the only
    boundary the geometry path *removes* is a mid-sentence text split between two
    lines that are vertically adjacent (a single wrapped clause), which by
    definition cannot be two separate clauses.
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

    blocks: list[str] = []
    current: list[GeoLine] = []
    for geo_line in geo_lines:
        if _starts_new_pdf_paragraph(geo_line, current, line_height, body_font):
            blocks.append(" ".join(item.text for item in current))
            current = []
        current.append(geo_line)
    if current:
        blocks.append(" ".join(item.text for item in current))
    return blocks


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
    # baseline): we cannot tell a wrap from a clause boundary, so we must fail SAFE
    # (fragment, never merge). Keep the one pairing that is structurally a single
    # marker — a bare standalone clause number immediately followed by its title —
    # joined; SPLIT every other line break. Fragmenting one clause into several is
    # acceptable; merging two is not. ---
    if _is_standalone_clause_number(previous.text):
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


def _pdf_quality_report(
    *,
    page_count: int,
    pages_with_text: int,
    pages_without_text: int,
    extracted_text: str,
    paragraph_count: int,
    repeated_margin_count: int,
    visual_profile: dict[str, object] | None = None,
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
    return quality


def _guard_pdf_image_pixels(data: bytes) -> None:
    """Reject a PDF whose embedded images would decode to too much memory.

    Sums the declared pixel area (width x height) of every embedded image across
    the page-capped pages using PyMuPDF ``page.get_image_info()`` — which reads
    the image dictionaries' metadata and does NOT decode pixels — and raises
    ``PdfExtractionError`` BEFORE the text/visual decode loop if the total exceeds
    ``MAX_PDF_TOTAL_IMAGE_PIXELS``. This is the decompression-bomb defence: a tiny
    compressed PDF can declare enormous raster dimensions, and the rejection here
    happens before any pixels are materialised, so no RSS spike occurs.

    Degrades SAFELY: if PyMuPDF is unavailable, the document fails to open, or
    ``get_image_info`` raises on any page, the guard does NOT block — it returns
    and lets normal extraction (which has its own char/page limits) proceed. Only
    a clear over-budget measurement rejects.
    """

    try:
        import fitz
    except ImportError:
        return

    document = None
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return

    try:
        total_pixels = 0
        profiled_pages = min(document.page_count, MAX_PDF_PAGES)
        for page_index in range(profiled_pages):
            try:
                images = document[page_index].get_image_info()
            except Exception:
                # No reliable pixel metadata for this page -> degrade safely.
                return
            for image in images:
                width = _safe_int(image.get("width"))
                height = _safe_int(image.get("height"))
                if width is None or height is None or width <= 0 or height <= 0:
                    continue
                total_pixels += width * height
                if total_pixels > MAX_PDF_TOTAL_IMAGE_PIXELS:
                    raise PdfExtractionError(IMAGE_BOMB_PDF_MESSAGE)
    finally:
        if document is not None:
            document.close()


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
            try:
                page_drawings = page.get_drawings()
            except Exception:
                page_drawings = []
            if page_drawings:
                drawing_count += len(page_drawings)
                page_has_drawings = True
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
