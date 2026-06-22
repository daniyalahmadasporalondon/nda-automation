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
from .docx_xml import _normalize_paragraph_text, _paragraph_text

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
    """
    reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(
        pdf_bytes, source_filename, converter=converter
    )
    indexed = reconstructed_body_index(reconstructed.data)
    mapped, mapped_count, unmapped_count = map_paragraphs_to_reconstruction(
        pypdf_paragraphs, indexed
    )
    return PdfWorkingDocument(
        docx_bytes=reconstructed.data,
        docx_filename=reconstructed.filename,
        paragraphs=mapped,
        headers=reconstructed.headers,
        mapped_count=mapped_count,
        unmapped_count=unmapped_count,
    )


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

    Alignment is a MONOTONIC forward scan: each pypdf paragraph is matched against the
    reconstructed paragraphs at or after the running cursor, so repeated/duplicate
    clause text aligns one-to-one in document order (twin-safe) instead of all matching
    the first occurrence. A pypdf fragment that the reconstruction merged into a larger
    paragraph still resolves -- its normalized text is a confident token-subset of that
    paragraph. A paragraph that cannot be confidently placed KEEPS its ``source_part``
    marker so the legacy fail-closed text-anchor path still guards it (never a silent
    drop).
    """
    mapped: list[dict[str, Any]] = []
    mapped_count = 0
    unmapped_count = 0
    # Reconstructed paragraphs eligible for the next match, paired with their canonical
    # source_index. Consumed monotonically so duplicates align in order.
    cursor = 0
    non_empty = [(idx, norm) for (idx, _text, norm) in reconstructed_index if norm]

    for paragraph in pypdf_paragraphs:
        updated = dict(paragraph)
        target = _normalize_paragraph_text(paragraph.get("text"))
        match_position, source_index = _best_forward_match(target, non_empty, cursor)
        if source_index is not None and match_position is not None:
            updated["source_index"] = source_index
            # Drop the PDF marker: this review paragraph now anchors by index into the
            # reconstructed DOCX body, exactly like a native-DOCX review paragraph.
            updated.pop("source_part", None)
            mapped_count += 1
            cursor = match_position + 1
        else:
            unmapped_count += 1
        mapped.append(updated)
    return mapped, mapped_count, unmapped_count


def _best_forward_match(
    target: str,
    non_empty: Sequence[tuple[int, str]],
    cursor: int,
) -> tuple[int | None, int | None]:
    """Best reconstructed paragraph for ``target`` at or after ``cursor``.

    Returns ``(position_in_non_empty, source_index)`` or ``(None, None)``. Prefers a
    normalized-equality / token-subset match closest to the cursor (keeping alignment
    monotonic), then the highest token-set ratio above the threshold.
    """
    if not target:
        return None, None
    best_position: int | None = None
    best_index: int | None = None
    best_score = 0.0
    for position in range(cursor, len(non_empty)):
        source_index, candidate = non_empty[position]
        if candidate == target or _is_token_subset(target, candidate):
            return position, source_index
        score = _token_set_ratio(target, candidate)
        if score >= PDF_INGEST_MATCH_RATIO and score > best_score:
            best_score = score
            best_position = position
            best_index = source_index
    return best_position, best_index


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
