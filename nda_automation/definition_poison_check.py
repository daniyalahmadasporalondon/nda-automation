"""Deterministic "definition-poison" detector (ADDITIVE review signal).

WHY THIS EXISTS
---------------
A clause can look perfectly standard on its face while a DEFINITION elsewhere in
the same document quietly makes it harmful. Two classic shapes:

* "Confidential Information" defined to AFFIRMATIVELY INCLUDE information that the
  standard carve-outs exist to exclude -- public / publicly available / already in
  the recipient's possession / independently developed. The body obligations read
  normally, but the definition has gutted the exclusions, so genuinely public
  knowledge is contractually treated as a secret.
* "Affiliate" / "Representative" / "Group" defined broadly enough to sweep in
  NON-parties (any company under common ownership, "any person we designate",
  consultants, competitors). A restraint or obligation written against
  "Representatives" then binds far more people/entities than the clause appears to.

The deterministic clause checks reason about each clause in isolation; the AI
reviewer is contractually scoped to the operative sentence. Neither reliably
notices that a benign-looking restraint inherits its teeth from a poisoned
definition. This module closes that gap.

ANTI-GHOST DESIGN RULE
----------------------
This detector is an ADDITIVE gap-filler, never an override:

* It only ever raises a REVIEW signal (a finding dict); it NEVER force-FAILs,
  NEVER downgrades, and NEVER overrides an AI verdict. Wiring (owned by
  ``wiring-lead``) is expected to only ELEVATE a clean pass to review.
* It is FAIL-SAFE: ANY exception is swallowed and the function returns None, so it
  can never crash the board poll. A document it cannot parse stays SILENT.
* It is PRECISION-TUNED to be quiet on normal drafting. A standard UTSA-style CI
  definition ("information that derives independent economic value ... not
  generally known ... not readily ascertainable", followed by the usual
  exclusions) is SILENT -- only a definition that AFFIRMATIVELY pulls the excluded
  categories back IN is flagged. A standard corporate-control Affiliate definition
  ("any entity controlling, controlled by, or under common control with a Party")
  is SILENT -- only one broad enough to sweep in arbitrary non-parties that a
  restraint then leans on is flagged.

Returns ``{"reason_code", "message"}`` or ``None``. REVIEW-only, additive.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

REASON_CODE_CI_POISON = "definition_poison_confidential_information"
REASON_CODE_AFFILIATE_POISON = "definition_poison_overbroad_affiliate"


# ---------------------------------------------------------------------------
# Document accessor.
#
# Read the document the SAME way ``law_forum_check`` does: the resolved, extracted
# plain text lives on ``matter["extracted_text"]``. Nothing else is consulted, so
# the detector can never drift from how the rest of the app sees the document.
# ---------------------------------------------------------------------------
def _matter_text(matter: Mapping[str, Any]) -> str:
    return str(matter.get("extracted_text") or "")


# ---------------------------------------------------------------------------
# Sentence segmentation.
# ---------------------------------------------------------------------------
def _sentences(text: str) -> list[str]:
    """Crude split on sentence / clause boundaries (period, semicolon, newline)."""
    return [seg for seg in re.split(r"(?<=[.;:])\s+|\n+", text or "") if seg.strip()]


# ---------------------------------------------------------------------------
# Confidential-Information definition poison.
#
# The poison shape: a sentence that DEFINES "Confidential Information" and, in the
# SAME breath, affirmatively pulls one of the standard exclusion categories back
# IN -- e.g. "Confidential Information includes information that is publicly
# available" or "... shall include information already known to / independently
# developed by the Receiving Party".
#
# Calibration against the real slice-bank NDA (review_eval_cases fixture):
#   "'Confidential Information' means any data or information that is proprietary
#    to the Disclosing Party and not generally known to the public ..."
#   ... with a separate "shall not include information which: ... is generally
#   known by the public ... independently developed ..." exclusion block.
# That is the NORMAL shape and MUST stay silent. The distinguishing fact is the
# POLARITY: the normal definition NEGATES the public category ("NOT generally
# known"); the poison definition INCLUDES it. We therefore require the inclusion
# verb (include / shall include / means ... includes / encompasses) to govern the
# excluded category WITHOUT an intervening negation, and we explicitly skip any
# sentence carrying the exclusion framing ("shall not include", "does not
# include", "excludes", "other than", "except").
# ---------------------------------------------------------------------------

# Names the document might give the protected term.
_CI_TERM = re.compile(
    r"confidential\s+information|proprietary\s+information|\bconfidential\s+material",
    re.IGNORECASE,
)

# Sentence reads like it is DEFINING the term (not merely using it).
_CI_DEFINITION = re.compile(
    r"\b(?:means|shall\s+mean|is\s+defined\s+as|refers?\s+to|includes?|"
    r"shall\s+include|including|encompass(?:es|ing)?|comprises?|covers?)\b",
    re.IGNORECASE,
)

# An INCLUSION verb -- the polarity that makes a definition over-broad.
_INCLUSION_VERB = re.compile(
    r"\b(?:includ(?:e|es|ing)|shall\s+include|encompass(?:es|ing)?|comprises?|"
    r"covers?|extends?\s+to|also\s+means|means)\b",
    re.IGNORECASE,
)

# The standard exclusion categories -- the things a healthy CI definition EXCLUDES.
# If a definition affirmatively INCLUDES one of these, it is poisoned.
_EXCLUDED_CATEGORY = re.compile(
    r"public(?:ly)?(?:\s+(?:available|known|disclosed))?|"
    r"(?:generally|widely|already|publicly)\s+known|"
    r"in\s+the\s+public\s+domain|"
    r"(?:already|previously|lawfully|rightfully)\s+(?:in\s+(?:its|the\s+receiving\s+party.?s)\s+possession|"
    r"known|possessed)|"
    r"independently\s+(?:developed|created|conceived|derived)|"
    r"received\s+from\s+a\s+third\s+party",
    re.IGNORECASE,
)

# Exclusion FRAMING -- the polarity flip that means the sentence is (correctly)
# carving these categories OUT, not pulling them in. Its presence makes the
# sentence safe regardless of the included categories named.
_EXCLUSION_FRAMING = re.compile(
    r"shall\s+not\s+include|does\s+not\s+include|do\s+not\s+include|not\s+include|"
    r"\bexclud(?:e|es|ing|ed)\b|\bexception\b|\bother\s+than\b|\bexcept\b|"
    r"shall\s+not\s+(?:be|constitute|apply)|not\s+(?:be\s+)?(?:deemed|considered|"
    r"treated|regarded)|cease(?:s)?\s+to\s+be|no\s+longer",
    re.IGNORECASE,
)

# A negation directly attached to the excluded category ("not generally known",
# "which is not publicly available") -- the UTSA polarity. Treated as safe.
_NEGATED_CATEGORY = re.compile(
    r"\bnot\b[^.;:]{0,40}?(?:"
    r"public|generally\s+known|widely\s+known|already\s+known|"
    r"in\s+the\s+public\s+domain|independently\s+(?:developed|created)|"
    r"in\s+(?:its|the)\s+possession"
    r")",
    re.IGNORECASE,
)


def _ci_sentence_is_poison(sentence: str) -> bool:
    """True iff this sentence is a CI definition that INCLUDES an excluded category.

    Precision gates (all must hold):
      * the sentence names the protected term AND reads like a definition;
      * it names a standard exclusion category (public / already-known / etc.);
      * an INCLUSION verb is present (so the category is pulled IN, not used);
      * NO exclusion framing is present ("shall not include", "other than", ...);
      * the category is NOT directly negated ("not generally known" -> UTSA-safe).
    """
    if not _CI_TERM.search(sentence):
        return False
    if not _CI_DEFINITION.search(sentence):
        return False
    if not _EXCLUDED_CATEGORY.search(sentence):
        return False
    if not _INCLUSION_VERB.search(sentence):
        return False
    # Polarity guards: an exclusion frame, or a negation glued to the category,
    # means the sentence is doing the NORMAL thing (carving out / UTSA phrasing).
    if _EXCLUSION_FRAMING.search(sentence):
        return False
    if _NEGATED_CATEGORY.search(sentence):
        return False
    return True


def detect_ci_poison(text: str) -> dict | None:
    """Flag a "Confidential Information" definition that swallows the exclusions."""
    for sentence in _sentences(text):
        if _ci_sentence_is_poison(sentence):
            return {
                "reason_code": REASON_CODE_CI_POISON,
                "message": (
                    "The 'Confidential Information' definition affirmatively "
                    "INCLUDES information that the standard carve-outs normally "
                    "exclude (public / already-known / independently-developed "
                    "information), which guts the exclusions through the "
                    "definition. Recommend human review."
                ),
            }
    return None


# ---------------------------------------------------------------------------
# Over-broad Affiliate / Representative / Group definition poison.
#
# The poison shape: a definition of "Affiliate" / "Representative" / "Group" that
# is broad enough to sweep in NON-parties (any person/entity the party DESIGNATES,
# "any third party", competitors, "any person whether or not affiliated"), which a
# restraint/obligation then relies on -- so the restraint binds far more than it
# appears.
#
# The NORMAL shape (MUST stay silent) is the standard corporate-control test:
#   "Affiliate means any entity that controls, is controlled by, or is under common
#    control with a Party."
# That is bounded by ownership/control and is fine. We flag only when the
# definition reaches BEYOND control to arbitrary designation / unaffiliated third
# parties, AND a restraint/obligation actually leans on the broadened term.
# ---------------------------------------------------------------------------
_GROUP_TERM = re.compile(
    r'"?\b(?:affiliate|representative|group|associated\s+(?:company|entity|person))s?\b"?',
    re.IGNORECASE,
)

_GROUP_DEFINITION = re.compile(
    r"\b(?:means|shall\s+mean|is\s+defined\s+as|refers?\s+to|includes?|"
    r"shall\s+include|including|encompass(?:es|ing)?)\b",
    re.IGNORECASE,
)

# Language that reaches BEYOND the corporate-control test into arbitrary scope.
_OVERBROAD_SCOPE = re.compile(
    r"any\s+(?:person|entity|party|third\s+party)\s+(?:that|whom|which)?\s*"
    r"(?:a\s+party|the\s+disclosing\s+party|we|it)\s+"
    r"(?:may\s+)?(?:designate|nominate|specif(?:y|ies)|choose|select|deem)|"
    r"whether\s+or\s+not\s+(?:affiliated|related|a\s+party)|"
    r"any\s+(?:other\s+)?(?:person|entity|party|third\s+part(?:y|ies))\s+"
    r"(?:whatsoever|of\s+any\s+kind|the\s+disclosing\s+party\s+(?:may\s+)?designates?)|"
    r"includ(?:e|es|ing)\s+(?:but\s+not\s+limited\s+to\s+)?(?:any\s+)?competitor|"
    r"any\s+third\s+part(?:y|ies)\b|"
    r"regardless\s+of\s+(?:ownership|control|affiliation)",
    re.IGNORECASE,
)

# The standard corporate-control test -- bounded, and therefore SAFE.
_CONTROL_TEST = re.compile(
    r"control(?:s|led|ling)?(?:\s+by)?|under\s+common\s+control|"
    r"owns?|owned|ownership|holding\s+company|subsidiary|parent\s+(?:company|entity)|"
    r"voting\s+(?:securities|shares|power|interest)|directly\s+or\s+indirectly\s+control",
    re.IGNORECASE,
)

# A restraint / obligation that would LEAN ON the broadened defined term.
_RESTRAINT = re.compile(
    r"shall\s+not|may\s+not|will\s+not|must\s+not|agree(?:s)?\s+not\s+to|"
    r"refrain|prohibit(?:ed|s)?|restrain(?:ed|s|t)?|restrict(?:ed|s|ion)?|"
    r"non[-\s]?(?:solicit|compete|circumvent|dealing)|"
    r"shall\s+(?:be\s+)?(?:bound|liable|responsible)|"
    r"\bbound\s+by\b|cause\s+(?:its|each|all)\s+",
    re.IGNORECASE,
)


def _affiliate_definition_is_overbroad(sentence: str) -> bool:
    """True iff this sentence DEFINES Affiliate/Representative/Group over-broadly.

    Over-broad == reaches beyond the corporate-control test (arbitrary designation
    / unaffiliated third parties). The standard pure-control definition is NOT
    over-broad even though it is, by design, capacious.
    """
    if not _GROUP_TERM.search(sentence):
        return False
    if not _GROUP_DEFINITION.search(sentence):
        return False
    if not _OVERBROAD_SCOPE.search(sentence):
        return False
    return True


def detect_affiliate_poison(text: str) -> dict | None:
    """Flag an over-broad Affiliate/Representative/Group def feeding a restraint.

    Requires BOTH (a) an over-broad group definition AND (b) at least one
    restraint/obligation somewhere in the document that would inherit that breadth.
    A definition alone, with no restraint relying on it, stays silent (precision).
    """
    sentences = _sentences(text)
    overbroad = any(_affiliate_definition_is_overbroad(s) for s in sentences)
    if not overbroad:
        return None
    has_restraint = any(_RESTRAINT.search(s) for s in sentences)
    if not has_restraint:
        return None
    return {
        "reason_code": REASON_CODE_AFFILIATE_POISON,
        "message": (
            "An 'Affiliate' / 'Representative' / 'Group' definition is broad "
            "enough to sweep in non-parties (beyond the usual corporate-control "
            "test), and a restraint or obligation in the document relies on that "
            "defined term -- so the restraint binds far more parties than it "
            "appears to. Recommend human review."
        ),
    }


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def detect_definition_poison(matter: Mapping[str, Any]) -> dict | None:
    """Detect a poisoned definition on a stored matter (fail-safe, review-only).

    Returns ``{"reason_code", "message"}`` for the first poison shape found
    (CI-definition poison takes precedence over Affiliate poison), or ``None`` when
    nothing is flagged. ANY error is swallowed and returns ``None`` so the detector
    can never crash the board poll.
    """
    try:
        if not isinstance(matter, Mapping):
            return None
        text = _matter_text(matter)
        if not text:
            return None
        return detect_ci_poison(text) or detect_affiliate_poison(text)
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the poll.
        return None
