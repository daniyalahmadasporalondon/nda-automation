"""Structure-aware insertion anchor for MISSING clauses (roadmap item #6).

When a required clause is missing and a redline inserts it, the *position* of that
insertion matters in a near-signed document. The legacy placement
(``clause_outcomes._logical_missing_clause_anchor``) uses per-clause regex tiers that
scan paragraph text for keywords. This module offers a structure-aware alternative:
place the new clause in real section order, after the section that should logically
precede it, derived from the document's parsed :func:`contract_structure`.

SAFETY CONTRACT (item #6 is HIGH RISK because it MUTATES output):

* This is a *try-first* layer. ``structure_aware_insertion_anchor`` returns ``None``
  whenever it cannot produce a confident, source-backed, pre-signature anchor. The
  caller (``clause_outcomes._insertion_anchor_paragraph``) then falls back to the
  existing regex tiers UNCHANGED.
* The whole entry point is defensive: any unexpected shape returns ``None`` rather
  than raising, so a malformed structure can never break redline building.
* It never anchors at or after a signature block (the inserted clause must precede
  signatures), mirroring the legacy tiers' invariant.
* It only trusts SOURCE-BACKED sections (real Word numbering/heading metadata), so a
  section scraped from flat text (e.g. an address digit read as a clause number) is
  never used as an anchor.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .checks.signatures import SIGNATURE_FOR_LINE_PATTERN
from .review_document import Paragraph

# Canonical operative order of the standard NDA clauses. A missing clause is placed
# after the last present section that maps to an EARLIER clause in this order. Clauses
# not in the list (or that map to no section) simply yield no structure anchor, so the
# regex tiers handle them.
_CANONICAL_CLAUSE_ORDER = (
    "mutuality",
    "confidential_information",
    "term_and_survival",
    "non_circumvention",
    "governing_law",
    "signatures",
)

# Heading cues per clause (mirrors clause_localization, kept local so the two can
# evolve independently). A section whose normalized heading contains any cue maps to
# that clause concept.
_CLAUSE_SECTION_CUES: dict[str, tuple[str, ...]] = {
    "mutuality": ("mutual", "reciprocal"),
    "confidential_information": (
        "confidential information",
        "confidentiality",
        "proprietary information",
        "definition",
    ),
    "term_and_survival": ("term", "survival", "duration", "termination", "survive"),
    "non_circumvention": ("circumvention", "non solicit", "non-solicit", "exclusivity"),
    "governing_law": ("governing law", "applicable law", "choice of law", "jurisdiction"),
}

_SIGNATURE_MARKER_LINE_PATTERN = r"^\s*(?:by|title|date)\s*:"
# A single signature-block line: a lone marker label (By:/Title:/Date:/Name:/
# Signature[:]) on its own paragraph -- the DOCX-default shape where each label is its
# own Word paragraph. Anchored to line start so body prose mentioning "name" or "date"
# mid-sentence does not trip it.
_SIGNATURE_LINE_MARKER_PATTERN = r"^\s*(?:by|title|date|name|signature|signed)\s*[:_]"
# A signature underscore/blank fill line ("____", "/s/", "___________").
_SIGNATURE_FILL_LINE_PATTERN = r"^\s*(?:_{3,}|/s/|x_{2,})\s*$"


def structure_aware_insertion_anchor(
    clause_id: str,
    paragraphs_by_id: Mapping[str, Paragraph],
    contract_structure: Mapping[str, Any] | None,
) -> Paragraph | None:
    """Return the paragraph AFTER which a missing ``clause_id`` should be inserted.

    Returns ``None`` (caller falls back to the regex tiers) when there is no usable
    structure, the clause has no canonical position, no preceding section maps, the
    chosen anchor is not source-backed, or the anchor would land at/after signatures.
    """
    try:
        return _structure_aware_insertion_anchor(clause_id, paragraphs_by_id, contract_structure)
    except Exception:
        # Defensive: a structure-aware anchor must NEVER break redline building. Any
        # surprise falls back to the legacy regex tiers.
        return None


def _structure_aware_insertion_anchor(
    clause_id: str,
    paragraphs_by_id: Mapping[str, Paragraph],
    contract_structure: Mapping[str, Any] | None,
) -> Paragraph | None:
    clause_id = str(clause_id or "")
    if clause_id not in _CANONICAL_CLAUSE_ORDER:
        return None
    if not isinstance(contract_structure, Mapping):
        return None
    sections = contract_structure.get("sections")
    if not isinstance(sections, Sequence):
        return None

    ordered_paragraphs = _ordered_paragraphs(paragraphs_by_id)
    if not ordered_paragraphs:
        return None
    signature_floor_index = _first_signature_index(ordered_paragraphs)

    target_rank = _CANONICAL_CLAUSE_ORDER.index(clause_id)

    # Find the last source-backed section that maps to a clause ranked strictly BEFORE
    # the target, whose last paragraph sits before the signature block.
    best_anchor: Paragraph | None = None
    best_end_index = -1
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        if not _section_is_source_backed(section):
            continue
        mapped_clause = _section_clause_concept(section)
        if mapped_clause is None:
            continue
        rank = _CANONICAL_CLAUSE_ORDER.index(mapped_clause)
        if rank >= target_rank:
            continue
        anchor = _section_last_paragraph_before_signatures(
            section, paragraphs_by_id, signature_floor_index
        )
        if anchor is None:
            continue
        anchor_index = _paragraph_index(anchor)
        if anchor_index is None:
            continue
        if anchor_index > best_end_index:
            best_end_index = anchor_index
            best_anchor = anchor
    return best_anchor


def _section_clause_concept(section: Mapping[str, Any]) -> str | None:
    heading = _normalize(str(section.get("heading") or ""))
    label = _normalize(str(section.get("label") or ""))
    haystack = f"{heading} {label}".strip()
    if not haystack:
        return None
    for clause_id in _CANONICAL_CLAUSE_ORDER:
        cues = _CLAUSE_SECTION_CUES.get(clause_id)
        if not cues:
            continue
        if any(cue in haystack for cue in cues):
            return clause_id
    return None


def _section_is_source_backed(section: Mapping[str, Any]) -> bool:
    source = section.get("source")
    return isinstance(source, Mapping) and bool(source)


def _section_last_paragraph_before_signatures(
    section: Mapping[str, Any],
    paragraphs_by_id: Mapping[str, Paragraph],
    signature_floor_index: int | None,
) -> Paragraph | None:
    paragraph_ids = section.get("paragraph_ids")
    if not isinstance(paragraph_ids, Sequence):
        return None
    candidates: list[Paragraph] = []
    for paragraph_id in paragraph_ids:
        paragraph = paragraphs_by_id.get(str(paragraph_id)) if isinstance(paragraph_id, str) else None
        if paragraph is None:
            continue
        index = _paragraph_index(paragraph)
        if index is None:
            continue
        if signature_floor_index is not None and index >= signature_floor_index:
            continue
        # Defense in depth: never anchor on a signature-ish line even if the floor
        # somehow missed it (e.g. a stray block before the main one).
        if _is_signature_line_paragraph(paragraph):
            continue
        candidates.append(paragraph)
    if not candidates:
        return None
    return max(candidates, key=lambda paragraph: _paragraph_index(paragraph) or 0)


def _ordered_paragraphs(paragraphs_by_id: Mapping[str, Paragraph]) -> list[Paragraph]:
    return sorted(paragraphs_by_id.values(), key=lambda paragraph: _paragraph_index(paragraph) or 0)


def _first_signature_index(ordered_paragraphs: Sequence[Paragraph]) -> int | None:
    """Index of the FIRST paragraph of the document's signature block, block-aware.

    The signature block can arrive in two shapes:

    * MERGED -- the whole block sits in one paragraph (multiple markers, or a "For X"
      line plus a marker). The legacy ``_is_signature_anchor_paragraph`` detects this.
    * ONE-MARKER-PER-PARAGRAPH (the DOCX default) -- "For Aspora Limited", "By: ___",
      "Title: ___", "Date: ___" each become their OWN paragraph, so no single paragraph
      trips the merged test. Here the block is a RUN of consecutive signature-ish lines.

    We return the index of the earliest paragraph that either is a merged signature
    paragraph OR begins a run of >=2 consecutive signature-ish lines. Conservative by
    design: anything that looks like the start of a signature run is treated as the
    block, so an anchor can only ever be refused (never wrongly admitted past it),
    keeping #6 at least as safe as the legacy regex tiers.
    """
    paragraphs = list(ordered_paragraphs)
    for position, paragraph in enumerate(paragraphs):
        # Merged single-paragraph block: matches the legacy detector directly.
        if _is_signature_anchor_paragraph(paragraph):
            return _paragraph_index(paragraph)
        # One-marker-per-paragraph block: this paragraph is a signature-ish line AND it
        # begins/continues a run of >=2 consecutive signature-ish lines.
        if _is_signature_line_paragraph(paragraph) and _starts_signature_run(paragraphs, position):
            return _paragraph_index(paragraph)
    return None


def _starts_signature_run(paragraphs: Sequence[Paragraph], position: int) -> bool:
    """True when ``position`` is the FIRST signature-ish line of a run of >=2 such lines.

    A run is the maximal block of consecutive signature-ish paragraphs ending at this
    position's block. Requiring length >=2 avoids treating a single stray "Date:" / "Name:"
    line in body prose as a whole signature block, while the standard DOCX block (a
    "For <party>" line plus per-marker lines) easily clears the threshold.
    """
    # Must be the START of the run: the previous paragraph is NOT signature-ish.
    if position > 0 and _is_signature_line_paragraph(paragraphs[position - 1]):
        return False
    run_length = 0
    for paragraph in paragraphs[position:]:
        if not _is_signature_line_paragraph(paragraph):
            break
        run_length += 1
        if run_length >= 2:
            return True
    return run_length >= 2


def _is_signature_anchor_paragraph(paragraph: Mapping[str, Any]) -> bool:
    """Merged-block detector (kept identical to the legacy clause_outcomes logic): a
    single paragraph carrying >=2 markers, or a "For <party>" line plus a marker."""
    text = str(paragraph.get("text") or "")
    marker_count = len(re.findall(_SIGNATURE_MARKER_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    has_for_line = bool(re.search(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    return marker_count >= 2 or (has_for_line and marker_count >= 1)


def _is_signature_line_paragraph(paragraph: Mapping[str, Any]) -> bool:
    """True for a single line of a signature block: a "For <party>" line, a lone marker
    label (By:/Title:/Date:/Name:/Signature), a fill/underscore line, or the merged
    multi-marker shape. Used to detect the one-marker-per-paragraph DOCX layout."""
    if _is_signature_anchor_paragraph(paragraph):
        return True
    text = str(paragraph.get("text") or "")
    if re.search(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE):
        return True
    if re.search(_SIGNATURE_LINE_MARKER_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE):
        return True
    if re.search(_SIGNATURE_FILL_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE):
        return True
    return False


def _paragraph_index(paragraph: Mapping[str, Any]) -> int | None:
    index = paragraph.get("index")
    return index if isinstance(index, int) else None


def _normalize(text: str) -> str:
    lowered = str(text or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()
