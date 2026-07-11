"""Approach C: convert a PDF matter to a working DOCX ONCE at ingest, and map each
pypdf review paragraph to its reconstructed-DOCX body paragraph so the export anchors
by an exact INDEX (identical to a native-DOCX matter), never a two-engine fuzzy match.

The old PDF redline path sat on two different text engines:

* review extracted clauses with ``pypdf`` (over-splitting multi-sentence clauses into
  mid-sentence FRAGMENTS), and
* export rebuilt the body with ``pdf2docx`` (a DIFFERENT chunker),

then anchored a redline by fuzzy-matching the fragment's ``original_text`` against one
reconstructed paragraph. They diverged on every multi-sentence clause, the anchor
failed, and the export was blocked (the 0/29 class).

This module collapses that to ONE robust path. At ingest we:

1. Reconstruct the PDF to a DOCX once (the same ``pdf2docx`` engine the export used).
2. Number the reconstructed body paragraphs with the canonical, twin-safe walker
   (``iter_indexed_body_paragraphs``) -- the SAME numbering ``docx_export`` anchors
   into.
3. Align each pypdf review paragraph to exactly one reconstructed-DOCX paragraph and
   stamp that paragraph's ``source_index`` onto the review paragraph, DROPPING the
   ``source_part:"pdf"`` marker so it is thereafter treated as DOCX body content.

We keep the pypdf review TEXT (the AI reviewer + geometry-based clause detection
depend on it; reviewing the lossy reconstruction would degrade prose/clause quality).
Only the anchor identity is borrowed from the reconstruction. From then on a converted
PDF matter behaves IDENTICALLY to a native DOCX matter: its working document is the
reconstructed DOCX and its redlines anchor by index.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, Sequence
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from . import pdf_docx_reconstruction
from .docx_text import iter_indexed_body_paragraphs
from .docx_xml import _normalize_paragraph_text, _paragraph_text, fold_ligatures

# A pypdf fragment maps to a reconstructed paragraph only when their texts agree at or
# above this confidence: normalized equality OR a token-set similarity ratio. This is
# the SAME family of match the legacy fuzzy anchor used, but it runs ONCE at ingest
# (over the whole document, with a monotonic forward cursor) rather than per-redline at
# export time, so a multi-sentence clause's fragments resolve deterministically.
PDF_INGEST_MATCH_RATIO = 0.6


@dataclass(frozen=True)
class PdfWorkingDocument:
    """The reconstructed DOCX a converted PDF matter stores as its working document,
    plus the review paragraphs re-keyed to anchor by index into that DOCX."""

    docx_bytes: bytes
    docx_filename: str
    paragraphs: list[dict[str, Any]]
    headers: dict[str, str] | None
    mapped_count: int
    unmapped_count: int


def convert_pdf_matter_to_docx(
    pdf_bytes: bytes,
    source_filename: str,
    pypdf_paragraphs: Sequence[dict[str, Any]],
    *,
    converter: pdf_docx_reconstruction.PdfToDocxConverter | None = None,
) -> PdfWorkingDocument:
    """Reconstruct the PDF, then re-key its pypdf review paragraphs to anchor by index.

    Raises the same ``PdfDocxReconstructionError`` subclasses ``reconstruct_pdf_to_docx``
    raises when the engine is unavailable or fails -- the caller decides whether to fall
    back to the legacy (un-converted) PDF matter so ingest is never hard-blocked.

    Also raises ``PdfDocxReconstructionFailedError`` when the reconstructed DOCX has NO
    anchorable body text (a scanned / image-only / text-empty PDF), so an empty working
    DOCX is never registered. The caller's fail-open path keeps the PDF page-image view.
    """
    # De-rotate BEFORE reconstruction. pdf2docx mishandles a page-level /Rotate
    # (90/180/270): it reads text blocks in the unrotated coordinate space, so a
    # rotated scan reconstructs to an EMPTY DOCX -- and the matter permanently loses
    # its working DOCX / faithful render / index anchors. Baking the rotation into
    # upright page geometry (rotation 0) first makes pdf2docx see normal, upright
    # content. Fail-open: if PyMuPDF is missing or the PDF has no rotation, the bytes
    # pass through unchanged (byte-identical, so caching/behavior is unaffected).
    conversion_bytes = _normalize_pdf_rotation(pdf_bytes)
    reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(
        conversion_bytes, source_filename, converter=converter
    )
    indexed = reconstructed_body_index(reconstructed.data)
    mapped, mapped_count, unmapped_count = map_paragraphs_to_reconstruction(
        pypdf_paragraphs, indexed
    )
    # EMPTY-BODY GUARD (fail-open, shared by ingest AND retro-conversion). A
    # scanned / image-only / text-empty PDF reconstructs to a structurally-valid
    # DOCX (the 4 required zip parts) that has NO anchorable body text. Registering
    # that as the role="working" artifact would flip ``matter_has_working_docx`` to
    # True (presence-only), light up the faithful DOCX render + a "Reconstructed
    # Word" download of an EMPTY document, and leave every redline/anchor with
    # nothing to bind to. Refuse the conversion when the reconstructed body has no
    # non-empty paragraph OR not a single pypdf review paragraph mapped onto it, so
    # the caller's fail-open path keeps the matter on the PDF page-image view. This
    # is raised as the standard reconstruction-failed error precisely because both
    # call sites already treat that as "keep the legacy un-converted PDF matter".
    has_body_text = any(norm for (_index, _text, norm) in indexed)
    if not has_body_text or mapped_count <= 0:
        raise pdf_docx_reconstruction.PdfDocxReconstructionFailedError(
            "PDF reconstruction produced no anchorable body text "
            f"(body_paragraphs_with_text={'yes' if has_body_text else 'no'}, "
            f"mapped_paragraphs={mapped_count}); keeping the PDF source."
        )
    return PdfWorkingDocument(
        docx_bytes=reconstructed.data,
        docx_filename=reconstructed.filename,
        paragraphs=mapped,
        headers=reconstructed.headers,
        mapped_count=mapped_count,
        unmapped_count=unmapped_count,
    )


def _normalize_pdf_rotation(pdf_bytes: bytes) -> bytes:
    """Return ``pdf_bytes`` with every page's /Rotate baked into upright geometry.

    A page carrying a non-zero /Rotate (90/180/270 -- a photographed or scanner-fed
    page, or a landscape page a tool marked rotated) makes pdf2docx emit an EMPTY
    DOCX: it lays text out in the pre-rotation coordinate space, so the blocks fall
    outside the page and are dropped. Here we re-draw each rotated page onto a fresh
    upright page (swapping width/height for 90/270) via PyMuPDF's ``show_pdf_page``,
    which honors the source rotation while embedding the content as a vector form
    XObject -- so TEXT STAYS SELECTABLE (pdf2docx can still parse it) and the result
    has rotation 0.

    Fail-open and byte-preserving: returns the ORIGINAL bytes unchanged when PyMuPDF
    is unavailable, no page is rotated, or anything goes wrong. Only a genuinely
    rotated PDF is rewritten, so an ordinary upright PDF is byte-identical (its cache
    key and every downstream behavior are unaffected)."""
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return pdf_bytes

    source = None
    output = None
    try:
        source = fitz.open(stream=pdf_bytes, filetype="pdf")
        if not any(int(getattr(page, "rotation", 0) or 0) % 360 for page in source):
            # No page is rotated -- leave the bytes untouched (byte-identical).
            return pdf_bytes
        output = fitz.open()
        for page in source:
            rotation = int(getattr(page, "rotation", 0) or 0) % 360
            rect = page.rect
            if rotation in (90, 270):
                width, height = rect.height, rect.width
            else:
                width, height = rect.width, rect.height
            new_page = output.new_page(width=width, height=height)
            # show_pdf_page honors the source page's own rotation, drawing it
            # upright onto the (rotation-0) target page.
            new_page.show_pdf_page(new_page.rect, source, page.number)
        normalized = output.tobytes()
    except Exception:
        # Any failure -> fall back to the original bytes; reconstruction then runs on
        # the un-normalized PDF exactly as it did before this guard existed.
        return pdf_bytes
    finally:
        for handle in (source, output):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
    return normalized or pdf_bytes


def reconstructed_body_index(docx_bytes: bytes) -> list[tuple[int, str, str]]:
    """Return ``(source_index, text, normalized_text)`` for each reconstructed body
    paragraph, numbered by the canonical twin-safe walker the export anchors into."""
    with ZipFile(BytesIO(docx_bytes)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    index: list[tuple[int, str, str]] = []
    for indexed in iter_indexed_body_paragraphs(root):
        text = _paragraph_text(indexed.paragraph)
        index.append((indexed.source_index, text, _normalize_paragraph_text(text)))
    return index


def map_paragraphs_to_reconstruction(
    pypdf_paragraphs: Sequence[dict[str, Any]],
    reconstructed_index: Sequence[tuple[int, str, str]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Stamp each pypdf review paragraph with the ``source_index`` of the reconstructed
    body paragraph it best matches, dropping the ``source_part:"pdf"`` marker on success.

    Alignment is a forward scan with a running cursor, so repeated/duplicate clause text
    aligns one-to-one in document order (twin-safe) instead of all matching the first
    occurrence. Two MATCH KINDS advance the cursor differently:

    * A one-to-one match (normalized equality, or a fuzzy token-set ratio) CONSUMES the
      reconstructed paragraph: the cursor advances PAST it so the next pypdf paragraph
      looks further on.
    * A token-SUBSET match means pdf2docx MERGED several pypdf fragments of one
      multi-sentence clause into a single reconstructed paragraph. The cursor stays AT
      that paragraph so every consecutive fragment of the merged clause re-matches it
      and shares its ``source_index`` (rather than the 2nd fragment falling through to
      the NEXT clause and stealing its index -- the collision that strict export would
      then reject). The cursor only advances off a merged paragraph once a later
      fragment matches a paragraph beyond it.

    A paragraph that cannot be confidently placed KEEPS its ``source_part`` marker so the
    legacy fail-closed text-anchor path still guards it (never a silent drop).
    """
    mapped: list[dict[str, Any]] = []
    mapped_count = 0
    unmapped_count = 0
    # Reconstructed paragraphs eligible for the next match, paired with their canonical
    # source_index. Consumed forward so duplicates align in order.
    cursor = 0
    non_empty = [(idx, norm) for (idx, _text, norm) in reconstructed_index if norm]

    for paragraph in pypdf_paragraphs:
        updated = dict(paragraph)
        # Fold ligatures in the RETAINED review text so a converted PDF's paragraphs
        # read "Confidential" not "Conﬁdential" -- keeping the working-DOCX review
        # text consistent with the folded normalization the anchor mapping and the
        # deterministic clause regexes use. A paragraph with no ligature is unchanged.
        raw_text = paragraph.get("text")
        if isinstance(raw_text, str):
            folded_text = fold_ligatures(raw_text)
            if folded_text != raw_text:
                updated["text"] = folded_text
        target = _normalize_paragraph_text(paragraph.get("text"))
        match_position, source_index, is_subset = _best_forward_match(target, non_empty, cursor)
        if source_index is not None and match_position is not None:
            updated["source_index"] = source_index
            # Drop the PDF marker: this review paragraph now anchors by index into the
            # reconstructed DOCX body, exactly like a native-DOCX review paragraph.
            updated.pop("source_part", None)
            mapped_count += 1
            # A merged-paragraph (subset) match does NOT consume the paragraph -- leave
            # the cursor on it so the next consecutive fragment of the same clause can
            # also map to it. A one-to-one match consumes it (advance past).
            cursor = match_position if is_subset else match_position + 1
        else:
            unmapped_count += 1
        mapped.append(updated)
    return mapped, mapped_count, unmapped_count


def _best_forward_match(
    target: str,
    non_empty: Sequence[tuple[int, str]],
    cursor: int,
) -> tuple[int | None, int | None, bool]:
    """Best reconstructed paragraph for ``target`` at or after ``cursor``.

    Returns ``(position_in_non_empty, source_index, is_subset)`` or ``(None, None,
    False)``. Prefers a normalized-equality / token-subset match closest to the cursor
    (keeping alignment monotonic), then the highest token-set ratio above the threshold.
    ``is_subset`` is True only for a token-subset (merged-paragraph) match, which the
    caller treats as a non-consuming match so a merged paragraph can absorb several
    consecutive fragments.
    """
    if not target:
        return None, None, False
    best_position: int | None = None
    best_index: int | None = None
    best_score = 0.0
    for position in range(cursor, len(non_empty)):
        source_index, candidate = non_empty[position]
        if candidate == target:
            return position, source_index, False
        if _is_token_subset(target, candidate):
            return position, source_index, True
        score = _token_set_ratio(target, candidate)
        if score >= PDF_INGEST_MATCH_RATIO and score > best_score:
            best_score = score
            best_position = position
            best_index = source_index
    return best_position, best_index, False


def _is_token_subset(fragment: str, paragraph: str) -> bool:
    """True when ``fragment``'s tokens are a (near-)subset of ``paragraph``'s.

    pdf2docx routinely MERGES the pypdf fragments of a multi-sentence clause into one
    paragraph; the fragment's tokens are then contained in that paragraph. Require the
    fragment to be non-trivial so a stray word does not match an unrelated paragraph.
    """
    fragment_tokens = fragment.split()
    if len(fragment_tokens) < 3:
        return False
    paragraph_tokens = set(paragraph.split())
    if not paragraph_tokens:
        return False
    contained = sum(1 for token in fragment_tokens if token in paragraph_tokens)
    return contained / len(fragment_tokens) >= 0.9


def _token_set_ratio(left: str, right: str) -> float:
    left_tokens = sorted(left.split())
    right_tokens = sorted(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return SequenceMatcher(None, left_tokens, right_tokens).ratio()
