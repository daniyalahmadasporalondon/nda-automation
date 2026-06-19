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

Precision is the whole game here, because the surface words are extremely common:

    * "Notwithstanding termination, the confidentiality obligations survive."
    * "Notwithstanding the foregoing, either party may disclose as required by law."
      (this is itself a carve-out, not a negation of one)
    * "Notwithstanding anything to the contrary, the exclusions SHALL APPLY."
      (an affirmation -- the opposite of a negation)

The structural signal that separates a real negation from all of these benign
uses is the one thing they never share: a **named exclusions noun** (exclusions /
exceptions / carve-outs / excluded information) that is *governed by a
disapplication verb* (shall not apply, are void, are inapplicable, of no force or
effect, superseded / overridden, deemed deleted / struck, no exception shall be
available). We work SENTENCE-BY-SENTENCE so the negation verb can only attach to a
noun in its own sentence -- a "notwithstanding" lead-in is sufficient context but
NOT required (a bare "the exceptions in Section 4 are inapplicable" is just as
dangerous and carries no lead-in).

We never treat a negation that governs "the obligations" / "the restrictions" as a
hit -- "the obligations of confidentiality shall not apply to information that is
public" IS the normal exclusions clause working correctly, not a negation of it.
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

# The thing being negated must be the confidentiality EXCLUSIONS, named as such.
# Crucially this does NOT include "obligations"/"restrictions"/"duties": a negation
# that governs those is the normal exclusions clause ("the obligations shall not
# apply to public information"), not a negation of the exclusions.
_EXCLUSION_NOUN = (
    r"(?:exclusions?"
    r"|exceptions?"
    r"|carve[\s\-]?outs?"
    r"|excluded\s+information"
    r"|exempt(?:ions?|\s+information)?)"
)

# A disapplication verb-phrase: the exclusions are switched off / overridden / removed.
# Extended (vs. the lead-in-anchored v1) to cover the indirect phrasings the
# adversarial panel surfaced: supersede / override / yield / struck / deemed deleted /
# of no force or effect / shall not be available.
_NEGATION = (
    r"(?:"
    r"(?:shall|will|do(?:es)?|may|are|is|be|been|being)?\s*"
    r"not\s+(?:apply|be\s+(?:applicable|available|effective|of\s+(?:any\s+)?(?:effect|force)))"
    r"|(?:shall|will|are|is|be|been|being)?\s*"
    r"(?:void|inapplicable|disregarded|deleted|struck|stricken|waived|nullified|"
    r"superseded|overridden|yield(?:s|ed)?)"
    r"|(?:be\s+|been\s+)?(?:deemed|treated\s+as)\s+"
    r"(?:void|inapplicable|deleted|struck|removed|of\s+no\s+(?:effect|force))"
    r"|of\s+no\s+(?:force|effect)(?:\s+(?:or|and|nor)\s+(?:force|effect))?"
    r"|(?:supersede|override)s?"
    r")"
)

# A window between the exclusion noun and its negation -- they must sit close together
# in the same sentence so the verb genuinely governs the exclusions noun (not some
# other noun later in a long sentence).
_NEAR = r"[^.;\n]{0,90}?"

# Sentence-splitter: a real negation verb only attaches to a noun in its own sentence,
# so we scope every match to a single sentence. This is what lets us drop the mandatory
# lead-in without stitching an exclusions noun in one sentence to a verb in the next.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;\n])")


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Two orderings within a sentence, because either part can come first:
#   "the exclusions in Section 2 shall not apply"      (noun -> negation)
#   "no exception ... shall be available"              (negation/quantifier -> noun)
_NOUN_THEN_NEGATION = _compile(rf"{_EXCLUSION_NOUN}{_NEAR}{_NEGATION}\b")
_NEGATION_THEN_NOUN = _compile(rf"{_NEGATION}\b{_NEAR}{_EXCLUSION_NOUN}")

# "no exception / exclusion / carve-out shall be available / shall apply" -- a universal
# quantifier disapplication (tp08). The quantifier "no" is what makes "shall be
# available" a negation here, so it must directly precede the exclusion noun.
_NO_EXCLUSION_AVAILABLE = _compile(
    rf"\bno\s+(?:[a-z]+,?\s+){{0,4}}?{_EXCLUSION_NOUN}{_NEAR}"
    r"shall\s+(?:be\s+available|apply)"
)

# A back-reference disapplication of the immediately-preceding exclusions sentence:
#   "the preceding sentence shall not apply" (tp06, where the preceding sentence was a
#   required-by-law carve-out). Only a hit when the PREVIOUS sentence actually stated an
#   exclusions/carve-out (checked in code), so a generic "the preceding sentence shall
#   not apply" attached to a non-exclusions clause never fires.
_BACKREF_NEGATION = _compile(
    r"the\s+(?:preceding|foregoing|above|previous)\s+"
    r"(?:sentence|paragraph|provision|clause)\s+"
    r"shall\s+not\s+apply"
)

# An affirmation guard: a sentence that AFFIRMS the exclusions ("the exclusions ...
# shall apply in full", "nothing limits ... right to rely on them") must stay silent
# even though it pairs an exclusions noun with "apply" (fp06).
_AFFIRMS_EXCLUSIONS = _compile(
    rf"{_EXCLUSION_NOUN}{_NEAR}shall\s+apply(?:\s+in\s+full)?\b"
)

# A guard against a benign reading: a clause that disapplies exclusions only "to the
# extent required by law" / "as permitted by applicable law" is restoring a *lawful*
# carve-out, not gutting the protection. (tp06 is NOT this -- it removes the lawful
# carve-out outright, which is why it lacks the "to the extent ... by law" scope.)
_BENIGN_REQUIRED_BY_LAW = _compile(
    r"to\s+the\s+extent\s+(?:required|permitted)\s+by\s+(?:applicable\s+)?law"
)

# Markers that a sentence describes a confidentiality exclusion / carve-out (used to
# validate the _BACKREF_NEGATION's target sentence).
_EXCLUSION_CONCEPT = _compile(
    r"(?:not\s+apply|do(?:es)?\s+not\s+(?:apply|include|extend)|"
    r"shall\s+not\s+apply|is\s+not\s+(?:deemed\s+)?confidential|"
    r"(?:publicly|public\s+domain)|independently\s+developed|"
    r"required\s+(?:to\s+be\s+disclosed\s+)?by\s+law|rightfully\s+(?:known|received)|"
    rf"{_EXCLUSION_NOUN})"
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


def _sentences(text: str) -> list[str]:
    """Split into clause-level sentences (on ``.``/``;``/newline)."""
    return [seg for seg in _SENTENCE_SPLIT.split(text) if seg.strip()]


def _tidy(sentence: str) -> str:
    """Collapse whitespace and truncate a sentence for the human-readable message."""
    snippet = re.sub(r"\s+", " ", sentence).strip()
    if len(snippet) > 240:
        snippet = snippet[:237].rstrip() + "..."
    return snippet


def _sentence_negates_exclusions(sentence: str) -> bool:
    """True when this single sentence disapplies a NAMED confidentiality exclusion."""
    # Affirmation / lawful-disclosure carve-out -- explicitly benign, never a hit.
    if _AFFIRMS_EXCLUSIONS.search(sentence):
        return False
    if _BENIGN_REQUIRED_BY_LAW.search(sentence):
        return False
    if _NO_EXCLUSION_AVAILABLE.search(sentence):
        return True
    if _NOUN_THEN_NEGATION.search(sentence):
        return True
    if _NEGATION_THEN_NOUN.search(sentence):
        return True
    return False


def detect_carveout_negation(matter: Any) -> dict | None:
    """Flag a clause that negates / disapplies the confidentiality exclusions.

    Returns ``{"reason_code", "message"}`` when some sentence disapplies a named
    confidentiality exclusion (directly, or via a back-reference to a preceding
    exclusions sentence), otherwise ``None``. Review-only and fail-safe: any error
    returns ``None``.
    """
    try:
        text = _document_text(matter)
        if not text:
            return None

        sentences = _sentences(text)
        for index, sentence in enumerate(sentences):
            # Direct disapplication of a named exclusions noun in this sentence.
            if _sentence_negates_exclusions(sentence):
                return _finding(sentence)

            # Back-reference disapplication ("the preceding sentence shall not apply"),
            # but only when the actual preceding sentence stated an exclusion/carve-out
            # and this disapplication is not itself scoped to lawful disclosure.
            if _BACKREF_NEGATION.search(sentence) and not _BENIGN_REQUIRED_BY_LAW.search(sentence):
                prev = sentences[index - 1] if index > 0 else ""
                if prev and _EXCLUSION_CONCEPT.search(prev):
                    return _finding(sentence)

        return None
    except Exception:
        # Review-only detector: never let a detection error affect the matter.
        return None


def _finding(sentence: str) -> dict:
    return {
        "reason_code": REASON_CODE,
        "message": (
            "A clause appears to cancel or disapply the confidentiality "
            "exclusions/carve-outs, which would gut the standard protections. "
            f'Needs review: "{_tidy(sentence)}"'
        ),
    }
