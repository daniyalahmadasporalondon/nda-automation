"""Structure-aware insertion anchor for MISSING clauses (roadmap item #6).

When a required clause is missing and a redline inserts it, the *position* of that
insertion matters in a near-signed document. The legacy placement
(``clause_outcomes._logical_missing_clause_anchor``) uses per-clause regex tiers that
scan paragraph text for keywords and stop short of the signature block. This module
offers a structure-aware alternative: place the new clause in real section order, after
the section that should logically precede it, derived from the document's parsed
:func:`contract_structure`.

This is attempt #4. The previous three were dropped because they could place a freshly
inserted MISSING clause INSIDE or AFTER the signature/execution block of a near-signed
NDA. Those attempts leaned on a single premise -- "refuse the final source-backed
section, where signatures always live" -- which is FALSE whenever a source-backed
Schedule / Exhibit / Annex / notarial block follows the signatures (the signatures then
sit in a NON-final section and were left unguarded), and they under-detected signature
lines with non-standard wording (``Per:``, ``Authorised signatory``, deeds).

This version drops that fragile premise and relies on TWO independent invariants, each
sufficient on its own to keep the anchor out of the signature region:

INVARIANT 1 -- SIGNATURE-REGION GUARD (primary, value-preserving).
    Scan the document paragraphs and compute the SIGNATURE REGION: the start index of
    the execution block, found with a BLOCK-AWARE, BROAD-VOCABULARY, CONSERVATIVE
    detector (see :func:`signature_region_start_index`). REFUSE any candidate anchor at
    or AFTER the region start, REGARDLESS of which section it belongs to. This fixes the
    schedules-after-signatures hole structurally: it does not matter whether signatures
    are in the final section -- they are in the region, and the region is refused.
    Conservative by construction: when unsure whether a run of lines is a signature
    region, we TREAT IT AS ONE. Over-refusal merely falls back to the legacy tiers
    (safe); under-detection is the dangerous failure we must avoid.

INVARIANT 2 -- NEVER-WORSE-THAN-LEGACY (hard safety net, in ``clause_outcomes``).
    The caller (``clause_outcomes._insertion_anchor_paragraph``) computes what the
    legacy regex tiers would return and NEVER accepts a structure anchor at a LATER
    paragraph index than legacy. If the structure pick is at or beyond the legacy pick,
    legacy wins. This makes #6 provably "at worst equal to legacy, never worse" --
    independent of region-detection precision. Even if Invariant 1 ever missed a region
    that the legacy tiers caught, Invariant 2 would still refuse to place later than
    legacy did.

Additional guardrails carried over from the safe parts of the prior design:

* SOURCE-BACKED gate: only real Word numbering/heading metadata is trusted as a
  section. A section scraped from flat text (e.g. an address digit read as a clause
  number, or a PDF with no structure) yields ``None`` -> legacy tiers run unchanged.
* Fully defensive: the public entry points are wrapped so any unexpected shape returns
  ``None`` (legacy fallback) rather than raising. A malformed structure can never break
  redline building.
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

# Legacy merged-block detector vocabulary: the single-paragraph shape where multiple
# markers (and/or a "For <party>" line) live in ONE paragraph. Kept byte-identical to
# clause_outcomes.SIGNATURE_MARKER_LINE_PATTERN / _is_signature_anchor_paragraph so the
# region detector subsumes the legacy detector exactly.
_LEGACY_SIGNATURE_MARKER_LINE_PATTERN = r"^\s*(?:by|title|date)\s*:"

# ---------------------------------------------------------------------------
# BROAD signature-line vocabulary (Invariant 1). Each pattern matches a SINGLE line of
# a signature/execution block. All are whole-line or line-anchored and length-bounded so
# body prose mentioning "name", "date", or "representative" mid-sentence does not trip
# them. We err toward INCLUSION: a false positive only refuses an anchor (safe); a false
# negative could place inside signatures (dangerous).
# ---------------------------------------------------------------------------

# Labelled markers that take a colon/paren: "By:", "Per:", "Its:", "Title:", "Date:",
# "Name:", "Signature:", "Signed:", "Print Name:", "Name (print):", "Witness:",
# "Witnessed by: ___", "Accepted and agreed:", "Authorised signatory:",
# "Signed for ABC Ltd:", "On behalf of X:". The phrase markers may carry a party or
# preposition before the colon.
_SIGNATURE_LINE_MARKER_PATTERN = (
    r"^\s*(?:"
    r"(?:by|title|date|name|signature|sign|signed|per|its|signatory|"
    r"print(?:ed)?\s*name|name\s*\(print\)|witness(?:ed)?|"
    r"accepted\s+and\s+agreed|authoris(?:ed|zed)\s+signatory|on\s+behalf\s+of)"
    r"\b[A-Za-z0-9 .,'/&()-]{0,80}[:(]"
    r")"
)
# A trailing-role line whose words name a signer role even WITHOUT a colon:
# "Authorised signatory", "Duly authorized representative", "Authorised signatory of
# the Recipient", "Its: Director", a lone "Director". Whole-line so a mid-sentence
# "as a representative of" in body prose is ignored.
_SIGNATURE_ROLE_LINE_PATTERN = (
    r"^\s*(?:[A-Za-z][A-Za-z .,'/&()-]{0,80}?\b\s*)?"
    r"(?:duly\s+)?(?:authoris(?:ed|zed)\s+)?"
    r"(?:signatory|representative|director)"
    r"(?:\s+of\s+(?:the\s+)?[A-Za-z][A-Za-z .,'/&()-]{0,40})?\s*$"
)
# A bare no-colon signature label standing ALONE on its own line: "Signature",
# "Print Name", "Printed Name", "Name", "Date", "Witness", "Signed". Whole-line +
# length-bounded so body prose is never caught.
_SIGNATURE_BARE_LABEL_PATTERN = (
    r"^\s*(?:signature|print(?:ed)?\s*name|printed\s+name|name|date|witness|signed)\s*$"
)
# A no-colon execution line naming the signing party: "Signed for and on behalf of
# Aspora Limited", "Signed for ABC Ltd", "For and on behalf of X". Requires the strong
# "signed" prefix or an explicit "on behalf of", so a bare clause-prose "For the
# purposes of ..." is NOT caught here.
_SIGNATURE_PARTY_LINE_PATTERN = (
    r"^\s*(?:"
    r"signed\s+(?:for|by)\b[A-Za-z0-9 .,'/&()-]{1,80}"
    r"|"
    r"(?:signed\s+)?(?:for\s+and\s+)?on\s+behalf\s+of\s+[A-Za-z0-9][A-Za-z0-9 .,'/&()-]{1,80}"
    r")\s*$"
)
# A signature underscore / blank fill / "/s/" line: "____", "/s/", "x_______".
_SIGNATURE_FILL_LINE_PATTERN = r"^\s*(?:_{3,}|/s/|x_{2,})[A-Za-z0-9 .,'/&()_-]*$"
# Execution / attestation preamble lines: "IN WITNESS WHEREOF ...", "EXECUTED as a
# DEED ...", "Executed and delivered as a deed ...", "AS WITNESS the hands ...".
_SIGNATURE_EXECUTION_PREAMBLE_PATTERN = (
    r"^\s*(?:"
    r"in\s+witness\s+whereof"
    r"|as\s+witness"
    r"|executed\s+(?:and\s+delivered\s+)?as\s+a\s+deed"
    r"|signed(?:,)?\s+sealed\s+and\s+delivered"
    r")\b"
)
# Notary / acknowledgment headings that introduce a notarial block: "Notarial
# Acknowledgment", "Notary Public", "Acknowledgment", "State of ...", "County of ...",
# "Sworn (to and subscribed) before me", "Jurat". These follow the signatures.
_SIGNATURE_NOTARY_PATTERN = (
    r"^\s*(?:"
    r"notar(?:ial|y)\b"
    r"|acknowled?ge?ment\b"
    r"|jurat\b"
    r"|sworn\s+(?:to\s+)?(?:and\s+subscribed\s+)?before\s+me"
    r"|state\s+of\s+[A-Za-z]"
    r"|county\s+of\s+[A-Za-z]"
    r")"
)

_SIGNATURE_LINE_PATTERNS = (
    SIGNATURE_FOR_LINE_PATTERN,
    _SIGNATURE_LINE_MARKER_PATTERN,
    _SIGNATURE_ROLE_LINE_PATTERN,
    _SIGNATURE_BARE_LABEL_PATTERN,
    _SIGNATURE_PARTY_LINE_PATTERN,
    _SIGNATURE_FILL_LINE_PATTERN,
    _SIGNATURE_EXECUTION_PREAMBLE_PATTERN,
    _SIGNATURE_NOTARY_PATTERN,
)


def structure_aware_insertion_anchor(
    clause_id: str,
    paragraphs_by_id: Mapping[str, Paragraph],
    contract_structure: Mapping[str, Any] | None,
) -> Paragraph | None:
    """Return the paragraph AFTER which a missing ``clause_id`` should be inserted.

    Returns ``None`` (caller falls back to the regex tiers) when there is no usable
    structure, the clause has no canonical position, no preceding section maps, the
    chosen anchor is not source-backed, or the anchor would land at/after the signature
    region (Invariant 1). The caller additionally enforces Invariant 2
    (never-worse-than-legacy) on top of whatever this returns.
    """
    try:
        return _structure_aware_insertion_anchor(clause_id, paragraphs_by_id, contract_structure)
    except Exception:
        # Defensive: a structure-aware anchor must NEVER break redline building. Any
        # surprise falls back to the legacy regex tiers.
        return None


def signature_region_start_index(paragraphs_by_id: Mapping[str, Paragraph]) -> int | None:
    """Public, defensive wrapper around :func:`_signature_region_start_index`.

    Returns the document ``index`` (1-based) of the FIRST paragraph of the signature /
    execution region, or ``None`` when no region is detected. Never raises.
    """
    try:
        ordered = _ordered_paragraphs(paragraphs_by_id)
        return _signature_region_start_index(ordered)
    except Exception:
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

    # INVARIANT 1: compute the signature-region start. Any candidate at/after this index
    # is refused regardless of section membership. None means "no region detected".
    signature_region_index = _signature_region_start_index(ordered_paragraphs)

    target_rank = _CANONICAL_CLAUSE_ORDER.index(clause_id)

    # Find the last source-backed section that maps to a clause ranked strictly BEFORE
    # the target, whose chosen anchor paragraph sits before the signature region.
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
            section, paragraphs_by_id, signature_region_index
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

    # INVARIANT 1 (final assertion): never return an anchor at/after the signature
    # region, even if some section bookkeeping disagreed with the per-paragraph scan.
    if signature_region_index is not None:
        anchor_index = _paragraph_index(best_anchor)
        if anchor_index is None or anchor_index >= signature_region_index:
            return None
    # Defense in depth: never return a paragraph that is itself a signature line.
    if _is_signature_line_paragraph(best_anchor):
        return None
    return best_anchor


# ---------------------------------------------------------------------------
# Signature-region detection (Invariant 1)
# ---------------------------------------------------------------------------


def _signature_region_start_index(ordered_paragraphs: Sequence[Paragraph]) -> int | None:
    """Index of the FIRST paragraph of the document's signature/execution region.

    BLOCK-AWARE: the region can arrive as

    * a MERGED single paragraph (multiple markers, or a "For <party>" line plus a
      marker) -- caught by :func:`_is_legacy_merged_signature_paragraph`; OR
    * the ONE-MARKER-PER-PARAGRAPH DOCX default, where "For Aspora Limited", "By: ___",
      "Title: ___", "Date: ___" are each their own paragraph -- a RUN of consecutive
      signature-ish lines; OR
    * an execution/attestation PREAMBLE ("IN WITNESS WHEREOF", "EXECUTED as a DEED") or
      a NOTARY heading, which begin the region even before the marker lines appear.

    CONSERVATIVE: we return the EARLIEST index that begins the region. A single stray
    signature-ish line in body prose (e.g. one "Date:" in a notices clause) is NOT a
    region on its own -- a marker run requires length >= 2. But a strong execution
    preamble or notary heading is a region opener by itself (these never appear in body
    prose). When in doubt we treat a run as a region: over-refusal is safe.

    Returns the paragraph ``index`` (1-based) of the region start, or ``None``.
    """
    paragraphs = list(ordered_paragraphs)
    for position, paragraph in enumerate(paragraphs):
        # (a) Merged single-paragraph block -- subsumes the legacy detector exactly.
        if _is_legacy_merged_signature_paragraph(paragraph):
            return _paragraph_index(paragraph)
        # (b) Strong region opener: an execution preamble or notary heading begins the
        #     region on its own (these wordings do not occur in operative body prose).
        if _is_strong_region_opener(paragraph):
            return _paragraph_index(paragraph)
        # (c) One-marker-per-paragraph block: this line is signature-ish AND it starts a
        #     run of >= 2 consecutive signature-ish lines.
        if _is_signature_line_paragraph(paragraph) and _starts_signature_run(paragraphs, position):
            return _paragraph_index(paragraph)
    return None


def _starts_signature_run(paragraphs: Sequence[Paragraph], position: int) -> bool:
    """True when ``position`` is the FIRST signature-ish line of a run of >= 2 such lines.

    Requiring length >= 2 avoids treating a single stray "Date:" / "Name:" line in body
    prose as a whole signature region, while the standard DOCX block (a "For <party>"
    line plus per-marker lines, repeated per party) easily clears the threshold. Blank
    paragraphs between marker lines do NOT break the run -- DOCX commonly interleaves
    empty paragraphs in an execution block.
    """
    # Must be the START of the run: the previous NON-BLANK paragraph is not signature-ish.
    previous_index = position - 1
    while previous_index >= 0 and _is_blank_paragraph(paragraphs[previous_index]):
        previous_index -= 1
    if previous_index >= 0 and _is_signature_line_paragraph(paragraphs[previous_index]):
        return False

    run_length = 0
    for paragraph in paragraphs[position:]:
        if _is_blank_paragraph(paragraph):
            continue
        if not _is_signature_line_paragraph(paragraph):
            break
        run_length += 1
        if run_length >= 2:
            return True
    return run_length >= 2


def _is_legacy_merged_signature_paragraph(paragraph: Mapping[str, Any]) -> bool:
    """Merged-block detector kept byte-identical to the legacy clause_outcomes logic: a
    single paragraph carrying >= 2 ``by:/title:/date:`` markers, or a "For <party>" line
    plus >= 1 such marker."""
    text = str(paragraph.get("text") or "")
    marker_count = len(
        re.findall(_LEGACY_SIGNATURE_MARKER_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE)
    )
    has_for_line = bool(re.search(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    return marker_count >= 2 or (has_for_line and marker_count >= 1)


def _is_strong_region_opener(paragraph: Mapping[str, Any]) -> bool:
    """True for a line that opens the execution region on its own: an execution /
    attestation preamble ("IN WITNESS WHEREOF", "EXECUTED as a DEED") or a notary /
    acknowledgment heading. These wordings do not occur in operative body prose, so they
    safely mark the region start without needing a run."""
    text = str(paragraph.get("text") or "")
    return any(
        re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        for pattern in (_SIGNATURE_EXECUTION_PREAMBLE_PATTERN, _SIGNATURE_NOTARY_PATTERN)
    )


def _is_signature_line_paragraph(paragraph: Mapping[str, Any]) -> bool:
    """True for a SINGLE line of a signature/execution block, using the broad vocabulary.

    Recognizes a "For/Signed for <party>" line, any labelled marker
    (By:/Title:/Date:/Name:/Per:/Its:/Signed:/Print Name:/Witness:/Accepted and
    agreed:/Authorised signatory:/On behalf of:), a trailing-role line ("Authorised
    signatory", "Duly authorized representative", lone "Director"), a bare no-colon label
    ("Signature"/"Print Name"/"Date"), a fill/underscore/"/s/" line, an execution
    preamble, or a notary heading -- plus the merged multi-marker shape.

    Used both to detect the one-marker-per-paragraph DOCX layout and as defense-in-depth
    when validating a chosen anchor.
    """
    if _is_legacy_merged_signature_paragraph(paragraph):
        return True
    text = str(paragraph.get("text") or "")
    if not text.strip():
        return False
    return any(
        re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        for pattern in _SIGNATURE_LINE_PATTERNS
    )


def _is_blank_paragraph(paragraph: Mapping[str, Any]) -> bool:
    return not str(paragraph.get("text") or "").strip()


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


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
    signature_region_index: int | None,
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
        # Invariant 1: never anchor at/after the signature region.
        if signature_region_index is not None and index >= signature_region_index:
            continue
        # Defense in depth: never anchor on a signature-ish line even if the region
        # scan somehow missed it (e.g. a stray block before the main one).
        if _is_signature_line_paragraph(paragraph):
            continue
        # Skip blank paragraphs as anchors so the insert lands on real text.
        if _is_blank_paragraph(paragraph):
            continue
        candidates.append(paragraph)
    if not candidates:
        return None
    return max(candidates, key=lambda paragraph: _paragraph_index(paragraph) or 0)


def _ordered_paragraphs(paragraphs_by_id: Mapping[str, Paragraph]) -> list[Paragraph]:
    return sorted(paragraphs_by_id.values(), key=lambda paragraph: _paragraph_index(paragraph) or 0)


def _paragraph_index(paragraph: Mapping[str, Any]) -> int | None:
    index = paragraph.get("index")
    return index if isinstance(index, int) else None


def _normalize(text: str) -> str:
    lowered = str(text or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()
