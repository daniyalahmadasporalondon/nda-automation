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
    # Only a genuine string is the document. A non-string ``extracted_text`` (dict,
    # list, number, ...) is NOT str()-coerced -- its repr could otherwise trip a
    # finding -- it is treated as "no text" so the detector stays silent.
    value = matter.get("extracted_text")
    return value if isinstance(value, str) else ""


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
    r"\b(?:includ(?:e|es|ing)|shall\s+include|deemed\s+to\s+include|"
    r"encompass(?:es|ing)?|comprises?|covers?|extends?\s+to|also\s+means)\b",
    re.IGNORECASE,
)

# The standard exclusion categories -- the things a healthy CI definition EXCLUDES.
# If a definition affirmatively INCLUDES one of these, it is poisoned.
#
# CRITICAL: every "public" match is WORD-BOUNDARED (\bpublic) so the bare substring
# "public" inside "non-public" (a standard, narrow CI scope in a huge fraction of
# real NDAs) does NOT register as the excluded category. "non-public" is handled as
# a negation by ``_is_safe_polarity`` below.
_EXCLUDED_CATEGORY = re.compile(
    r"\bpublic(?:ly)?\b(?:\s+(?:available|known|disclosed|domain))?|"
    r"\bpublic\s+domain\b|"
    r"(?:generally|widely|already|publicly)\s+known|"
    r"(?:already|previously|lawfully|rightfully)\s+(?:in\s+(?:its|the\s+receiving\s+party.?s)\s+possession|"
    r"known|possessed)|"
    r"independently\s+(?:developed|created|conceived|derived)|"
    r"received\s+from\s+a\s+third\s+party|rightfully\s+(?:obtained|received)",
    re.IGNORECASE,
)

# Exclusion FRAMING -- the polarity flip that means the sentence is (correctly)
# carving these categories OUT, not pulling them in. Its presence makes the
# sentence safe regardless of the included categories named.
#
# NOTE: a bare "cease to be" / "no longer" is NOT exclusion framing -- the
# OBFUSCATED poison "shall NOT cease to be Confidential Information by reason of
# entering the public domain" (tp07) uses exactly that phrasing to ACHIEVE the
# poison. We only treat a POSITIVE "ceases to be / no longer ... confidential"
# (handled by ``_obfuscated_negation_poison``) -- not the negated form -- and we
# do NOT list it here, so it can never suppress the real finding.
_EXCLUSION_FRAMING = re.compile(
    r"shall\s+not\s+include|does\s+not\s+include|do\s+not\s+include|not\s+include|"
    r"\bexclud(?:e|es|ing|ed)\b|\bexception(?:s)?\b|\bother\s+than\b|\bexcept\b|"
    r"shall\s+not\s+apply|do(?:es)?\s+not\s+apply|obligations?\s+[^.;:]{0,40}?not\s+apply|"
    r"not\s+(?:be\s+)?(?:deemed|considered|treated|regarded)\s+(?:as\s+)?confidential",
    re.IGNORECASE,
)

# Negation / narrowing phrasing that makes a CI category SAFE (the UTSA polarity):
#   * "non-public" / "non public" -- a NARROW scope, the opposite of poison;
#   * "not generally known" / "not being generally known" -- UTSA secrecy test;
#   * "not ... publicly available / in the public domain / independently developed";
#   * "independent economic value" / "independent commercial value" -- the UTSA
#     trade-secret VALUE phrase (fp01/fp06), which is not an exclusion category.
_SAFE_POLARITY = re.compile(
    r"non[-\s]?public|"
    r"not\s+(?:being\s+|be\s+)?(?:generally|widely|publicly)\s+known|"
    r"not\s+(?:being\s+|be\s+)?(?:readily\s+)?ascertainable|"
    r"not\b[^.;:]{0,40}?(?:public|in\s+the\s+public\s+domain|"
    r"independently\s+(?:developed|created)|in\s+(?:its|the)\s+possession)|"
    r"independent\s+(?:economic|commercial)\s+value",
    re.IGNORECASE,
)


def _ci_sentence_is_poison(sentence: str) -> bool:
    """True iff this sentence is a CI definition that INCLUDES an excluded category.

    Precision gates (all must hold):
      * the sentence names the protected term AND reads like a definition;
      * an INCLUSION verb is present (so the category is pulled IN, not used);
      * it names a standard exclusion category (public / already-known / etc.);
      * NO exclusion framing is present ("shall not include", "shall not apply"...);
      * NO safe/UTSA polarity is present ("non-public", "not generally known",
        "independent economic value").
    """
    if not _CI_TERM.search(sentence):
        return False
    if not _CI_DEFINITION.search(sentence):
        return False
    if not _INCLUSION_VERB.search(sentence):
        return False
    if not _EXCLUDED_CATEGORY.search(sentence):
        return False
    # Polarity guards: exclusion framing, or UTSA/narrowing polarity, means the
    # sentence is doing the NORMAL thing (carving out / secrecy test / narrow scope).
    if _EXCLUSION_FRAMING.search(sentence):
        return False
    if _SAFE_POLARITY.search(sentence):
        return False
    return True


# Obfuscated negation poison (tp07): a "Confidential Information" definition that
# DEEMS public / generally-known / independently-developed information to STAY
# confidential -- "information shall NOT CEASE to be Confidential Information by
# reason of entering the public domain ... and NO EXCLUSIONS shall apply". The
# effect is identical to "includes public info" but the phrasing is negated, so the
# plain inclusion path misses it. We require the protected term, a "no exclusions"
# / "shall not cease to be confidential" frame, AND an excluded category named.
_NO_EXCLUSIONS_FRAME = re.compile(
    r"no\s+exclusions?\s+(?:of\s+any\s+kind\s+)?shall\s+apply|"
    r"shall\s+not\s+cease\s+to\s+be\s+confidential|"
    r"not\s+cease\s+to\s+be\s+confidential\s+information|"
    r"remain(?:s)?\s+confidential\s+(?:information\s+)?(?:notwithstanding|even\s+(?:if|though)|"
    r"regardless|despite)|"
    r"none\s+of\s+the\s+exclusions\s+(?:set\s+out\s+)?(?:below\s+)?shall\s+apply",
    re.IGNORECASE,
)


def _obfuscated_negation_poison(text: str) -> bool:
    """True iff the document DEEMS otherwise-excludable info to stay confidential.

    Catches the negated-form poison (tp07): a frame that overrides the exclusions
    ("no exclusions shall apply" / "shall not cease to be Confidential Information")
    co-located with at least one named exclusion category (public / generally known
    / independently developed). The frame must appear in a sentence that references
    the protected term, so a generic "no exceptions" elsewhere cannot trip it.
    """
    for sentence in _sentences(text):
        if not _NO_EXCLUSIONS_FRAME.search(sentence):
            continue
        if not _CI_TERM.search(sentence):
            continue
        if _EXCLUDED_CATEGORY.search(sentence):
            return True
    return False


def detect_ci_poison(text: str) -> dict | None:
    """Flag a "Confidential Information" definition that swallows the exclusions."""
    poisoned = any(_ci_sentence_is_poison(s) for s in _sentences(text))
    if not poisoned:
        poisoned = _obfuscated_negation_poison(text)
    if poisoned:
        return {
            "reason_code": REASON_CODE_CI_POISON,
            "message": (
                "The 'Confidential Information' definition affirmatively "
                "INCLUDES (or refuses to exclude) information that the standard "
                "carve-outs normally exclude (public / already-known / "
                "independently-developed information), which guts the exclusions "
                "through the definition. Recommend human review."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# CI-poison SEVERITY (fail vs review).
#
# The overlay path can only ever ELEVATE a clean PASS to REVIEW; it never FAILs. But
# a base CI clause check already lands an affirmatively-poisoned definition on REVIEW
# for unrelated reasons (e.g. "broad definition does not clearly cover enough
# categories"), so the overlay is a no-op and the genuinely defective definition is
# presented as merely "needs a look". A reviewer skimming a review-heavy board may
# wave it through. The AI-path eval fixtures already expect ``fail`` for these poison
# cases; the deterministic/overlay path under-classifies.
#
# This severity function lets the deterministic path reach a FAIL-tier verdict, but
# ONLY for the unambiguous AFFIRMATIVE-INCLUSION-WITHOUT-CARVE-OUT shape:
#
#   * AFFIRMATIVE poison is present (the definition expressly INCLUDES, or refuses to
#     cease treating as confidential, a standard excluded category), AND
#   * NO recognized carve-out block survives anywhere in the document.
#
# A document that affirmatively poisons the definition but STILL carries a real
# exclusion block (a self-contradiction worth a human's eyes, not a clear defect)
# stays REVIEW. A definition that is merely narrow / simply lacks a carve-out without
# affirmatively pulling public info IN never reaches this function at all (it is not
# "poisoned" by ``detect_ci_poison``), so it can never be FAILed here -- the
# conservative, no-over-failing contract the spec requires.
# ---------------------------------------------------------------------------

# A recognized standard carve-out: a sentence that EXCLUDES (exclusion framing) a
# named excluded category. This is the healthy "shall not include / does not apply to
# information that is publicly available ..." block. Its presence anywhere means the
# document still grants the standard exclusions, so an affirmative-inclusion sentence
# elsewhere is a self-contradiction (REVIEW), not a clean defect (FAIL).
def _has_recognized_carveout(text: str) -> bool:
    for sentence in _sentences(text):
        if not _CI_TERM.search(sentence) and not _EXCLUDED_CATEGORY.search(sentence):
            # A carve-out names an excluded category; if neither the protected term
            # nor a category appears, this sentence cannot be a CI carve-out.
            continue
        if _EXCLUSION_FRAMING.search(sentence) and _EXCLUDED_CATEGORY.search(sentence):
            return True
    return False


def ci_poison_severity(text: str) -> str | None:
    """Severity of any CI-definition poison: ``"fail"`` | ``"review"`` | ``None``.

    * ``None``  -- no affirmative poison detected (silent; never over-fails a merely
      narrow definition, which is not poison in the first place).
    * ``"fail"`` -- AFFIRMATIVE poison AND no recognized carve-out survives anywhere:
      the definition expressly swallows the exclusions and nothing grants them back.
    * ``"review"`` -- affirmative poison co-located with a surviving carve-out block
      (an internal contradiction): kept at REVIEW for human judgment, never FAILed.

    Fail-safe: any error returns ``None`` so the detector can never crash a caller.
    """
    try:
        if detect_ci_poison(text) is None:
            return None
        return "review" if _has_recognized_carveout(text) else "fail"
    except Exception:  # noqa: BLE001 -- fail-safe.
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

# Language that reaches BEYOND the corporate-control test into arbitrary scope --
# i.e. sweeps in NON-parties / competitors / arbitrary designees. These are the
# phrasings that make a group definition a weapon when a restraint relies on it.
_OVERBROAD_SCOPE = re.compile(
    r"any\s+(?:person|entity|party|third\s+party)\s+(?:that|whom|which)?\s*"
    r"(?:a\s+party|the\s+disclosing\s+party|we|it|such\s+party)\s+"
    r"(?:may\s+|has\s+)?(?:designate|nominate|specif(?:y|ies)|choose|select|deem|done\s+business\s+with)|"
    r"whether\s+or\s+not\s+(?:affiliated|related|a\s+party|under\s+common\s+control|employed)|"
    r"any\s+(?:other\s+)?(?:person|entity|party|third\s+part(?:y|ies))\s+"
    r"(?:whatsoever|of\s+any\s+kind)|"
    r"any\s+(?:actual\s+or\s+potential\s+)?competitor|"
    r"any\s+entity\s+in\s+the\s+same\s+industry|"
    r"(?:affiliated|associated|or\s+otherwise\s+connected)\s*,?\s*"
    r"(?:associated|or\s+otherwise\s+connected|including)|"
    r"otherwise\s+connected|"
    r"any\s+company\s+in\s+which\s+it\s+holds\s+any\s+interest|"
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

# A restraint / obligation verb -- the thing that, when it RELIES on the broadened
# group term, weaponizes the definition. Note: a restraint counts only when its
# sentence ALSO names the group term (see ``_restraint_relies_on_group``), so a
# generic "shall not disclose" or a NEGATED "there are NO non-compete obligations"
# (fp09) that does not target the group term never trips the detector.
_RESTRAINT = re.compile(
    r"shall\s+not\s+(?:solicit|hire|engage|employ|compete|do\s+business|deal|"
    r"circumvent|approach|poach|interfere)|"
    r"(?:may|will|must)\s+not\s+(?:solicit|hire|engage|compete|do\s+business)|"
    r"agree(?:s)?\s+not\s+to\s+(?:solicit|hire|compete|do\s+business)|"
    r"\bnon[-\s]?(?:solicit|compete|circumvent|dealing)\b|"
    r"shall\s+(?:not\s+)?(?:procure|ensure|cause)\s+that\s+(?:none|no)\b|"
    r"competes?\s+with|solicit(?:s|ing)?\s+or\s+(?:hire|do\s+business)",
    re.IGNORECASE,
)

# A NEGATED restraint -- a sentence that DISCLAIMS any restraint ("there are NO
# non-compete or non-solicitation obligations", fp09). Such a sentence must never
# count as a restraint relying on the group term.
_NEGATED_RESTRAINT = re.compile(
    r"(?:there\s+are\s+|are\s+|is\s+)?no\s+non[-\s]?(?:compete|solicit)|"
    r"no\s+(?:non[-\s]?compete\s+or\s+non[-\s]?solicitation|restraint|restriction)",
    re.IGNORECASE,
)


def _affiliate_definition_is_overbroad(sentence: str) -> bool:
    """True iff this sentence DEFINES Affiliate/Representative/Group over-broadly.

    Over-broad == reaches beyond the corporate-control test (arbitrary designation
    / unaffiliated third parties / competitors). The standard pure-control
    definition is NOT over-broad even though it is, by design, capacious.
    """
    if not _GROUP_TERM.search(sentence):
        return False
    if not _GROUP_DEFINITION.search(sentence):
        return False
    if not _OVERBROAD_SCOPE.search(sentence):
        return False
    return True


def _restraint_relies_on_group(sentence: str) -> bool:
    """True iff this sentence is a RESTRAINT that targets the group/Affiliate term.

    The restraint must (a) read like a genuine restraint verb, (b) name the
    group/Affiliate/Representative term, and (c) NOT be a sentence that disclaims
    restraints. This is what makes the breadth dangerous -- a restraint sweeping in
    everyone the over-broad definition reaches.
    """
    if _NEGATED_RESTRAINT.search(sentence):
        return False
    if not _RESTRAINT.search(sentence):
        return False
    if not _GROUP_TERM.search(sentence):
        return False
    return True


def detect_affiliate_poison(text: str) -> dict | None:
    """Flag an over-broad Affiliate/Representative/Group def feeding a restraint.

    Requires BOTH (a) an over-broad group definition AND (b) a restraint/obligation
    that RELIES on the group term (names it). A broad definition used only to scope
    permitted disclosure, or with no restraint targeting it, stays silent.
    """
    sentences = _sentences(text)
    overbroad = any(_affiliate_definition_is_overbroad(s) for s in sentences)
    if not overbroad:
        return None
    has_restraint = any(_restraint_relies_on_group(s) for s in sentences)
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
