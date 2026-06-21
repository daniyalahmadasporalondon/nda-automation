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

# Sentence reads like it is DEFINING the term (not merely using it). Kept in sync
# with ``_INCLUSION_VERB`` so any inclusion-polarity verb also reads as definitional
# (e.g. "extends to", "is deemed to include", "also means").
_CI_DEFINITION = re.compile(
    r"\b(?:means|shall\s+mean|is\s+defined\s+as|refers?\s+to|includes?|"
    r"shall\s+include|including|deemed\s+to\s+include|encompass(?:es|ing)?|"
    r"comprises?|covers?|extends?\s+to|also\s+means)\b",
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
#
# PRECISION: "public" must carry an availability/knowledge/domain qualifier
# ("publicly available", "public domain", "public knowledge", "publicly known",
# "public information/data") to count as the excluded category. A bare "public"
# (e.g. "public interest", "public company", "publicly traded") is NOT the category
# and must not trigger the poison path -- over-failing a clean NDA is the dangerous
# direction.
_EXCLUDED_CATEGORY = re.compile(
    r"\bpublicly\s+(?:available|known|disclosed|accessible)\b|"
    r"\bpublic\s+(?:domain|knowledge|information|record|records|data)\b|"
    r"\bin\s+the\s+public\s+domain\b|"
    r"(?:generally|widely|already|publicly)\s+known|"
    r"(?:already|previously|lawfully|rightfully)\s+(?:in\s+(?:its|the\s+receiving\s+party.?s)\s+possession|"
    r"known|possessed)|"
    r"independently\s+(?:developed|created|conceived|derived)|"
    r"received\s+from\s+a\s+third\s+party|rightfully\s+(?:obtained|received)",
    re.IGNORECASE,
)

# A NEGATED inclusion verb -- "does not include", "shall not include", "not
# including", "no longer includes" -- means the sentence is CARVING the category OUT,
# not pulling it in. This is the ONLY suppression baked into affirmative-inclusion
# detection: it distinguishes "includes publicly available" (poison polarity) from
# "does not include publicly available" (carve-out polarity). Broader carve-out
# CONNECTIVES ("save", "except", "other than", ...) are handled structurally by the
# whole-text exclusion-signal scan in ``_has_any_exclusion_signal`` -- NOT by a finite
# allowlist here -- so the polarity is "innocent until a negated inclusion is shown",
# never "guilty unless a vocabulary word appears".
_NEGATED_INCLUSION = re.compile(
    r"\b(?:do(?:es)?|shall|will|would|may|must|can|could)\s+not\s+"
    r"(?:be\s+)?(?:deemed\s+to\s+)?includ|"
    r"\bnot\s+includ(?:e|es|ing|ed)\b|"
    r"\bno\s+longer\s+(?:be\s+)?includ|"
    r"\bnever\s+includ",
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
    """True iff this sentence AFFIRMATIVELY INCLUDES an excluded category.

    This is the AFFIRMATIVE-INCLUSION signal ONLY -- the polarity-correct question
    "does the definition pull a normally-excluded category IN?". It deliberately does
    NOT consult the carve-out vocabulary: whether a carve-out exists ANYWHERE is a
    separate, generous whole-text question answered by ``_has_any_exclusion_signal``,
    and it controls fail-vs-review (never silence). Here we only require:

      * the sentence names the protected term AND reads like a definition;
      * an INCLUSION verb is present that is NOT negated ("includes" -- not "does not
        include" / "not including"); a negated inclusion is a carve-out, not poison;
      * it names a standard exclusion category (public / already-known / etc.);
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
    # A NEGATED inclusion ("does not include publicly available") is a carve-out, not
    # affirmative inclusion -- the only suppression at the affirmative-inclusion layer.
    if _NEGATED_INCLUSION.search(sentence):
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

# ANY-exclusion-signal scan (the polarity-correct, generous gate).
#
# The ``fail`` tier is reserved for affirmative inclusion of an excluded category with
# GENUINELY ZERO exclusion signal anywhere in the definition. Rather than prove a
# carve-out via a finite allowlist of connectives (whack-a-mole, biased toward
# guilty), we scan BROADLY for any plausible exclusion signal and, if ANY is present,
# cap at ``review`` -- never ``fail``. Over-failing a clean NDA is far worse than
# under-failing a poison that the base CI check already REVIEWs, so this list is meant
# to be wide and is backed by a catch-all (a negation token near an excluded
# category). A "fake" carve-out word that does not truly carve out the public category
# will therefore cap at ``review`` instead of ``fail`` -- the SAFE direction.
_EXCLUSION_SIGNAL = re.compile(
    r"\bexclud\w*|"                       # exclude / excludes / excluding / excluded / exclusive
    r"\bexcept\w*|\bexcepting\b|"         # except / except for/that / excepting / excepted / exception(s)
    r"\bsave\b|"                          # save / save for / save where / save that / save only / save and excepting
    r"\bnot\s+includ\w*|"                 # not include / not including
    r"\bother\s+than\b|"
    r"\bunless\b|"
    r"\bprovided\b|"                      # provided that / provided however that
    r"\bminus\b|"
    r"\bless\s+any\b|"
    r"\bbarring\b|"
    r"\bsetting\s+aside\b|"
    r"\baside\s+from\b|\bapart\s+from\b|"
    r"\bto\s+the\s+exclusion\s+of\b|"
    r"\bwith\s+the\s+carve[-\s]?out\b|\bcarve[-\s]?out\b|"
    r"\bsubject\s+to\b|"
    r"\bexclusive\s+of\b|"
    r"\bto\s+the\s+extent\s+not\b|"
    r"\bbut\s+not\b|"
    r"--|—|–|"                  # em-dash / en-dash carve-out
    r"\(",                                 # parenthetical carve-out
    re.IGNORECASE,
)

# Catch-all: a negation token within ~60 chars (either side) of an excluded-category
# mention. Generous net for carve-out phrasings the vocabulary above misses
# ("...shall not be Confidential Information", "no longer protected", ...).
_NEGATION_TOKEN = re.compile(r"\b(?:not|no|never|excl)\w*", re.IGNORECASE)


def _has_any_exclusion_signal(text: str) -> bool:
    """True iff ANY plausible exclusion/carve-out signal appears in the definition.

    Polarity-correct and intentionally GENEROUS: presence of any signal caps the
    verdict at ``review`` (never ``fail``). Two layers:
      1. a broad vocabulary OR (``_EXCLUSION_SIGNAL``), plus
      2. a catch-all: a negation token within ~60 chars of an excluded category.
    """
    if _EXCLUSION_SIGNAL.search(text or ""):
        return True
    # Catch-all proximity check: negation token near an excluded-category mention.
    for cat in _EXCLUDED_CATEGORY.finditer(text or ""):
        window = (text or "")[max(0, cat.start() - 60): cat.end() + 60]
        if _NEGATION_TOKEN.search(window):
            return True
    return False


def ci_poison_severity(text: str) -> str | None:
    """Severity of any CI-definition poison: ``"fail"`` | ``"review"`` | ``None``.

    Polarity-correct decision (biased HARD toward NOT failing):

    * ``None``  -- NO affirmative inclusion of an excluded category. A merely-narrow
      definition, or a pure carve-out block, is not poison and is never failed.
    * ``"fail"`` -- affirmative inclusion of an excluded category AND GENUINELY ZERO
      exclusion signal of any kind anywhere in the definition (the rare, high-
      confidence path: "Confidential Information includes publicly available info"
      with no exclusion anywhere).
    * ``"review"`` -- affirmative inclusion BUT some plausible exclusion signal is
      present anywhere (an inline carve-out, a separate carve-out block, or even a
      "fake" carve-out word): capped at REVIEW for a human, NEVER failed.

    Fail-safe: any error returns ``None`` so the detector can never crash a caller.
    """
    try:
        if detect_ci_poison(text) is None:
            return None
        # The OBFUSCATED-NEGATION poison (tp07) is a frame that explicitly OVERRIDES
        # the carve-outs ("no exclusions of any kind shall apply" / "shall not cease
        # to be Confidential Information"). That frame is the INVERSE of a carve-out,
        # so the generous exclusion-signal scan (which would see its "no exclusions"
        # negation tokens) must NOT demote it: it is a high-confidence FAIL on its own.
        if _obfuscated_negation_poison(text):
            return "fail"
        # Plain affirmative inclusion: FAIL only when GENUINELY ZERO exclusion signal
        # exists anywhere; any plausible carve-out signal caps at REVIEW.
        return "review" if _has_any_exclusion_signal(text) else "fail"
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
