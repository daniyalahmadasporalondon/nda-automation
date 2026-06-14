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
# A single signature-block line carrying a labelled marker -- the DOCX-default shape
# where each label is its own Word paragraph. Anchored to line start so body prose
# mentioning "name" or "date" mid-sentence does not trip it. Covers the standard
# vocabulary plus the non-standard wordings the adversary surfaced: Per:, Its: Director,
# Signed:, Print Name:, Name (print):, Witness/Witnessed by:, Accepted and agreed:.
# DEFENSE-IN-DEPTH ONLY -- the final-source-backed-section guard is what guarantees
# signatures are never anchored in; this just improves the floor's precision.
_SIGNATURE_LINE_MARKER_PATTERN = (
    r"^\s*(?:"
    # Plain labels that take a colon/paren directly: "By:", "Per:", "Its:", "Date:".
    r"(?:by|title|date|name|signature|sign|per|its|signatory|"
    r"print(?:ed)?\s*name|name\s*\(print\))\b\s*[:(]"
    r"|"
    # Phrase markers that may carry a party/preposition before the colon:
    # "Signed:", "Signed for ABC Ltd:", "Signed for and on behalf of X:",
    # "Witnessed by: ___", "Accepted and agreed:", "Authorised signatory:".
    r"(?:signed|witness(?:ed)?|accepted\s+and\s+agreed|authoris(?:ed|zed)\s+signatory|"
    r"on\s+behalf\s+of)\b[A-Za-z .,'/&()-]{0,80}[:(]"
    r")"
)
# A trailing-role line whose LAST words name a signer role, even without a colon:
# "Authorised signatory", "Duly authorized representative", "Authorised signatory of
# the Recipient". These name who signs, so a paragraph ending in them is a signature
# line. Whole-line so a mid-sentence "as a representative of" in body prose is ignored.
_SIGNATURE_ROLE_LINE_PATTERN = (
    r"^\s*(?:[A-Za-z][A-Za-z .,'/&()-]{0,80}\b)?"
    r"(?:authoris(?:ed|zed)\s+signatory|duly\s+authoris(?:ed|zed)\s+representative|"
    r"authoris(?:ed|zed)\s+representative|signatory|representative|director|"
    r"its\s+director)\b[A-Za-z .,'/&()-]{0,40}\s*$"
)
# A bare no-colon signature label standing ALONE on its own line ("Signature",
# "Print Name", "Date", "Name", "Witness"). Whole-line + length-bounded so body prose
# is never caught.
_SIGNATURE_BARE_LABEL_PATTERN = (
    r"^\s*(?:signature|print(?:ed)?\s*name|printed\s+name|name|date|witness|"
    r"authoris(?:ed|zed)\s+signatory|signatory)\s*$"
)
# A no-colon execution line naming the signing party: "Signed for and on behalf of
# Aspora Limited", "Signed for ABC Ltd", "For and on behalf of X". Requires the strong
# "signed" prefix or an explicit "on behalf of", so a bare clause-prose "For the
# purposes of ..." is NOT caught (that path is the checker's SIGNATURE_FOR_LINE_PATTERN,
# which has its own exclusions). Whole-line + length-bounded.
_SIGNATURE_PARTY_LINE_PATTERN = (
    r"^\s*(?:"
    r"signed\s+(?:for|by)\b[A-Za-z0-9 .,'/&()-]{1,80}"
    r"|"
    r"(?:signed\s+)?(?:for\s+and\s+)?on\s+behalf\s+of\s+[A-Za-z0-9][A-Za-z0-9 .,'/&()-]{1,80}"
    r")\s*$"
)
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

    # PRIMARY STRUCTURAL GUARD (vocabulary-INDEPENDENT). build_contract_structure always
    # extends the LAST detected heading's section to EOF, so the signature block is
    # ALWAYS swallowed into the document's FINAL source-backed section -- regardless of
    # how the signature lines are worded (Per:, Its: Director, Authorised signatory,
    # Duly authorized representative, Signed:, bare no-colon labels, ...). Therefore an
    # anchor may NEVER fall in that final source-backed section. We compute its paragraph
    # ids up front; any candidate inside it is refused -> the legacy regex tiers run.
    # This makes anchoring in/after signatures structurally impossible while preserving
    # #6's value for every NON-final section.
    final_source_backed_paragraph_ids = _final_source_backed_section_paragraph_ids(sections)

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

    if best_anchor is None:
        return None
    # Hard guard: never return an anchor that belongs to the final source-backed section
    # (where signatures always live). Refuse -> None -> legacy regex tiers.
    if str(best_anchor.get("id") or "") in final_source_backed_paragraph_ids:
        return None
    return best_anchor


def _final_source_backed_section_paragraph_ids(sections: Sequence[Any]) -> set[str]:
    """Paragraph ids of the document's FINAL source-backed section.

    The final source-backed section is the source-backed section whose paragraph range
    reaches furthest in document order (contains the highest paragraph index). Because
    build_contract_structure runs the last heading's section to EOF, the signature block
    is always part of this section, so refusing any anchor inside it is what guarantees
    #6 can never anchor in/after signatures -- independent of signature vocabulary."""
    final_section: Mapping[str, Any] | None = None
    final_reach = -1
    for section in sections:
        if not isinstance(section, Mapping) or not _section_is_source_backed(section):
            continue
        reach = _section_max_paragraph_index(section)
        if reach is None:
            continue
        if reach > final_reach:
            final_reach = reach
            final_section = section
    if final_section is None:
        return set()
    return {
        str(paragraph_id)
        for paragraph_id in (final_section.get("paragraph_ids") or [])
        if isinstance(paragraph_id, str) and paragraph_id
    }


def _section_max_paragraph_index(section: Mapping[str, Any]) -> int | None:
    end_index = section.get("end_index")
    if isinstance(end_index, int):
        return end_index
    # Fall back to scanning paragraph ids of the review-id form ``p{index}``.
    max_index: int | None = None
    for paragraph_id in section.get("paragraph_ids") or []:
        index = _paragraph_id_index(paragraph_id)
        if index is not None and (max_index is None or index > max_index):
            max_index = index
    return max_index


def _paragraph_id_index(paragraph_id: Any) -> int | None:
    match = re.fullmatch(r"p(\d+)", str(paragraph_id or ""), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


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
    """True for a single line of a signature block. Recognizes a "For/Signed for <party>"
    line, a labelled marker (By:/Title:/Date:/Name:/Per:/Its:/Signed:/Print Name:/
    Witness:/Accepted and agreed:/Authorised signatory:/On behalf of:), a trailing-role
    line ("Authorised signatory", "Duly authorized representative", "Its: Director"), a
    bare no-colon label ("Signature"/"Print Name"/"Date"), a fill/underscore line, or the
    merged multi-marker shape. Used to detect the one-marker-per-paragraph DOCX layout.

    DEFENSE-IN-DEPTH: this vocabulary only improves the floor's precision. Safety is
    guaranteed by the final-source-backed-section guard regardless of wording."""
    if _is_signature_anchor_paragraph(paragraph):
        return True
    text = str(paragraph.get("text") or "")
    patterns = (
        SIGNATURE_FOR_LINE_PATTERN,
        _SIGNATURE_LINE_MARKER_PATTERN,
        _SIGNATURE_ROLE_LINE_PATTERN,
        _SIGNATURE_BARE_LABEL_PATTERN,
        _SIGNATURE_PARTY_LINE_PATTERN,
        _SIGNATURE_FILL_LINE_PATTERN,
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def _paragraph_index(paragraph: Mapping[str, Any]) -> int | None:
    index = paragraph.get("index")
    return index if isinstance(index, int) else None


def _normalize(text: str) -> str:
    lowered = str(text or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()
