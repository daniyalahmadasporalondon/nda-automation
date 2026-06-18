"""Detect a "notwithstanding carve-out negation" trick in an NDA.

A standard NDA defines the universal confidentiality EXCLUSIONS -- the categories
of information that are *not* protected (already public, independently developed,
rightfully received from a third party, required by law, etc.). A sneaky draft
keeps those exclusions intact for show, then adds a clause ELSEWHERE that quietly
cancels them:

    "Notwithstanding the foregoing, the exclusions in Section 2 shall not apply
     where the Discloser deems the information sensitive."

The exclusions are still on the page, so a clause-by-clause read passes them --
but they have been gutted, leaving the receiving party with no real carve-outs.

This module is an ADDITIVE, REVIEW-ONLY detector. It never fails a matter and
never mutates one; it returns a single review flag (or ``None``) that the wiring
layer surfaces alongside the deterministic review. It is fail-safe: any error
(or absent / unreadable document) yields ``None`` rather than raising.

Precision is the whole game here. "Notwithstanding" is one of the most common
words in a contract, and almost every use is benign:

    * "Notwithstanding termination, the confidentiality obligations survive."
    * "Notwithstanding the foregoing, either party may disclose as required by law."
      (this is itself a carve-out, not a negation of one)

So we flag ONLY when an override lead-in is co-located, in the same sentence, with
a phrase that DISAPPLIES the confidentiality exclusions/exceptions/carve-outs
("the exclusions ... shall not apply", "the foregoing exceptions are void",
"Section X carve-outs are inapplicable"). The negation verb must govern an
*exclusions* noun -- never the confidentiality obligation itself.
"""
from __future__ import annotations

import re
from typing import Any

REASON_CODE = "notwithstanding_carveout_negation"

# The matter-level raw-document accessor, mirroring ``matter_summary.build_summary_context``
# (``matter.get("extracted_text")``) -- the same field the AI review and corpus index read.
_DOCUMENT_FIELD = "extracted_text"

# Cap the text we scan so a pathological document can't make the regex sweep expensive.
_MAX_SCAN_CHARS = 200_000

# --- Building blocks -------------------------------------------------------

# An override lead-in: the clause announces it is overriding something stated earlier.
_OVERRIDE_LEAD_IN = (
    r"(?:notwithstanding\b[^.;]*?"
    r"|(?:anything|any\s+(?:provision|term|clause)[^.;]*?)\s+to\s+the\s+contrary"
    r"|(?:the\s+)?(?:foregoing|above|preceding)\b)"
)

# The thing being negated must be the confidentiality EXCLUSIONS, named as such.
# We accept the explicit exclusion nouns, OR a "Section/Clause/Paragraph N" reference
# that the same sentence then ties to exclusions/exceptions/carve-outs.
_EXCLUSION_NOUN = (
    r"(?:exclusions?"
    r"|exceptions?"
    r"|carve[\s\-]?outs?"
    r"|excluded\s+information"
    r"|exempt(?:ions?|\s+information)?)"
)

# The negation verb-phrase: the exclusions are switched off.
_NEGATION = (
    r"(?:shall|will|do(?:es)?|may|are|is|be)?\s*"
    r"(?:not\s+(?:apply|be\s+(?:applicable|available|effective))"
    r"|(?:be\s+)?(?:void|inapplicable|of\s+no\s+(?:effect|force))"
    r"|(?:be\s+)?(?:disregarded|deemed\s+(?:void|inapplicable|deleted)|deleted|waived))"
)

# A window between the exclusion noun and its negation -- they must sit close together
# in the same sentence, with no clause-ending punctuation between them, so we don't
# stitch an exclusion noun in one sentence to a negation in the next.
_NEAR = r"[^.;]{0,80}?"


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


# Two orderings, because either the exclusion noun or the negation can come first:
#   "the exclusions in Section 2 shall not apply"      (noun -> negation)
#   "shall not apply to the exclusions in Section 2"   (negation -> noun)
_NOUN_THEN_NEGATION = _compile(
    rf"{_OVERRIDE_LEAD_IN}[^.;]*?{_EXCLUSION_NOUN}{_NEAR}{_NEGATION}\b"
)
_NEGATION_THEN_NOUN = _compile(
    rf"{_OVERRIDE_LEAD_IN}[^.;]*?{_NEGATION}\b{_NEAR}{_EXCLUSION_NOUN}"
)

# A guard against a benign reading: a clause that disapplies exclusions only "to the
# extent required by law" / "as required by applicable law" is restoring a *lawful*
# carve-out, not gutting the protection. (Rare, but it keeps us conservative.)
_BENIGN_REQUIRED_BY_LAW = _compile(
    r"to\s+the\s+extent\s+(?:required|permitted)\s+by\s+(?:applicable\s+)?law"
)


def _document_text(matter: Any) -> str:
    """Read the raw document text off a matter, fail-safe.

    Mirrors the ``matter.get("extracted_text")`` accessor used across the codebase
    (matter_summary / corpus_index / ai review). Returns ``""`` for anything that
    isn't a mapping with usable text.
    """
    try:
        text = matter.get(_DOCUMENT_FIELD)  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return ""
    if not isinstance(text, str):
        return ""
    return text[:_MAX_SCAN_CHARS]


def _sentence_around(text: str, span: tuple[int, int]) -> str:
    """The clause/sentence containing ``span``, for the human-readable message."""
    start, end = span
    left = max(
        text.rfind(".", 0, start),
        text.rfind(";", 0, start),
        text.rfind("\n", 0, start),
    )
    right_candidates = [
        pos for pos in (text.find(".", end), text.find(";", end), text.find("\n", end)) if pos != -1
    ]
    right = min(right_candidates) if right_candidates else len(text)
    snippet = text[left + 1 : right].strip()
    snippet = re.sub(r"\s+", " ", snippet)
    if len(snippet) > 240:
        snippet = snippet[:237].rstrip() + "..."
    return snippet


def detect_carveout_negation(matter: Any) -> dict | None:
    """Flag an override clause that negates the confidentiality exclusions.

    Returns ``{"reason_code", "message"}`` when the document contains a
    "notwithstanding ... the exclusions shall not apply"-style negation of the
    confidentiality carve-outs, otherwise ``None``. Review-only and fail-safe:
    any error returns ``None``.
    """
    try:
        text = _document_text(matter)
        if not text:
            return None

        for pattern in (_NOUN_THEN_NEGATION, _NEGATION_THEN_NOUN):
            match = pattern.search(text)
            if match is None:
                continue
            sentence = _sentence_around(text, match.span())
            # Don't flag a clause that merely defers exclusions to a lawful disclosure.
            if sentence and _BENIGN_REQUIRED_BY_LAW.search(sentence):
                continue
            quoted = sentence or match.group(0).strip()
            return {
                "reason_code": REASON_CODE,
                "message": (
                    "An override clause appears to cancel the confidentiality "
                    "exclusions/carve-outs, which would gut the standard protections. "
                    f'Needs review: "{quoted}"'
                ),
            }
        return None
    except Exception:
        # Review-only detector: never let a detection error affect the matter.
        return None
