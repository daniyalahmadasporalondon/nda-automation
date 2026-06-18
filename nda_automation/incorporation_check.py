"""Deterministic incorporation-by-reference / "shall prevail" detector.

WHY THIS EXISTS
---------------
An NDA can quietly subordinate itself to a SEPARATE, UNSEEN agreement: it both
*references* an external document ("subject to the MSA", "incorporated by
reference", "governed by the terms of the Master Services Agreement", "pursuant to
the SOW") AND *grants that external document overriding authority* ("which shall
prevail in any conflict", "takes precedence over this Agreement", "in the event of
conflict the MSA controls", "supersedes this Agreement"). When both halves are
present, the confidentiality protections we believe we are signing can be silently
overridden by terms we have never read. That warrants human review.

This is a deterministic, ADDITIVE, REVIEW-ONLY signal. It mirrors the design of
``nda_automation/law_forum_check.py``:

* It reads the document via the SAME accessor that module uses -- the matter's
  ``extracted_text`` field.
* It NEVER force-FAILs and never overrides an AI verdict. The caller (wiring
  teammate) decides how to surface the finding; this module only DETECTS.
* It is FAIL-SAFE: any error returns ``None`` (it can never crash the board poll),
  and it stays SILENT unless BOTH a reference to an external agreement AND a
  precedence/override grant for that external agreement are present.

PRECISION CONTRACT
------------------
FLAG only when an external/unseen agreement is given OVERRIDING authority:
  * a REFERENCE to an external agreement ("subject to", "incorporated by
    reference", "governed by the terms of [other]", "pursuant to the [MSA]"), AND
  * a PRECEDENCE grant in that external agreement's favour ("shall prevail",
    "takes precedence", "in the event of conflict [other] controls", "supersedes
    this Agreement").

Stay SILENT (no flag) for:
  * a benign reference with no precedence ("Confidential Information as defined in
    the MSA", "as described in the SOW"), and
  * the REVERSE polarity, where THIS NDA prevails over other terms ("this Agreement
    shall prevail over any conflicting terms", "this NDA supersedes all prior
    agreements"). A self-asserting NDA is not a subordination risk.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

REASON_CODE = "incorporation_by_reference_override"

# ---------------------------------------------------------------------------
# External-agreement vocabulary.
#
# Names/abbreviations of the kinds of SEPARATE agreement an NDA might subordinate
# itself to. Used both to recognise a "reference to an external agreement" and to
# recognise WHICH party a precedence clause favours (the external doc, not "this
# Agreement"). "this Agreement" / "this NDA" are deliberately NOT here -- a
# precedence clause favouring THIS document is the benign reverse polarity.
# ---------------------------------------------------------------------------
_EXTERNAL_AGREEMENT = (
    r"master\s+(?:services|service|agreement|subscription)\s+agreement"
    r"|master\s+agreement"
    r"|\bm\.?s\.?a\.?\b"
    r"|statement\s+of\s+work"
    r"|\bs\.?o\.?w\.?\b"
    r"|services\s+agreement"
    r"|subscription\s+agreement"
    r"|purchase\s+agreement"
    r"|framework\s+agreement"
    r"|consulting\s+agreement"
    r"|license\s+agreement|licence\s+agreement"
    r"|partnership\s+agreement"
    r"|reseller\s+agreement"
    r"|distribution\s+agreement"
    r"|main\s+agreement"
    r"|primary\s+agreement"
    r"|principal\s+agreement"
    r"|underlying\s+agreement"
    r"|other\s+agreement"
    r"|prior\s+agreement"
    r"|separate\s+agreement"
    r"|\bmsa\b"
    r"|\bsow\b"
    r"|\bsla\b"
)

# Phrasing that REFERENCES an external agreement -- on its own this is benign; it
# only matters when paired with a precedence grant in the SAME sentence.
_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "subject to the MSA", "subject to the terms of the Master Services Agreement"
    re.compile(
        r"subject\s+to\s+(?:the\s+(?:terms?\s+(?:and\s+conditions?\s+)?of\s+)?)?"
        r"(?:the\s+|that\s+certain\s+|any\s+)?[^.;\n]*?(?:" + _EXTERNAL_AGREEMENT + r")",
        re.IGNORECASE,
    ),
    # "incorporated (herein) by reference" -- the classic incorporation phrase.
    re.compile(r"incorporat\w*\s+(?:herein\s+)?by\s+reference", re.IGNORECASE),
    # "governed by the terms of the MSA"
    re.compile(
        r"governed\s+by\s+(?:the\s+)?terms?\s+(?:and\s+conditions?\s+)?of\s+"
        r"[^.;\n]*?(?:" + _EXTERNAL_AGREEMENT + r")",
        re.IGNORECASE,
    ),
    # "pursuant to the MSA", "in accordance with the Master Services Agreement"
    re.compile(
        r"(?:pursuant\s+to|in\s+accordance\s+with|under\s+the\s+terms\s+of)\s+"
        r"(?:the\s+|that\s+certain\s+|any\s+)?[^.;\n]*?(?:" + _EXTERNAL_AGREEMENT + r")",
        re.IGNORECASE,
    ),
    # "forms part of / is part of the MSA"
    re.compile(
        r"(?:forms?|is|are)\s+(?:a\s+)?part\s+of\s+[^.;\n]*?(?:" + _EXTERNAL_AGREEMENT + r")",
        re.IGNORECASE,
    ),
    # Bare mention of a named external agreement. On its own this is benign; it
    # only flags when the SAME sentence also grants that agreement precedence
    # (enforced by the caller pairing reference AND precedence per sentence). This
    # catches "the Master Services Agreement controls" / "the SOW supersedes this
    # Agreement", where the external doc is named directly as the precedence subject
    # with no "subject to"/"pursuant to" preamble.
    re.compile(r"(?:" + _EXTERNAL_AGREEMENT + r")", re.IGNORECASE),
)

# Precedence / override phrasing. Captured in two flavours:
#   * _PRECEDENCE_GENERIC -- "shall prevail", "takes precedence", "controls",
#     "shall govern" with no explicit subject. In a sentence that ALSO references
#     an external agreement (and does NOT assert "this Agreement" as the winner),
#     the external doc is the implied winner.
#   * _PRECEDENCE_OVER_THIS -- explicitly overrides THIS agreement
#     ("supersedes this Agreement", "prevails over this Agreement", "this
#     Agreement is subordinate to ...") -- unambiguous subordination.
_PRECEDENCE_GENERIC = re.compile(
    r"shall\s+(?:prevail|control|govern|take\s+precedence|supersede)"
    r"|takes?\s+precedence"
    r"|will\s+(?:prevail|control|govern|supersede)"
    # bare present-tense verb ("the MSA controls", "the SOW prevails/supersedes")
    r"|\b(?:controls?|prevails?|supersedes?)\b"
    r"|in\s+(?:the\s+event|case)\s+of\s+(?:any\s+)?(?:conflict|inconsistency)"
    r"|to\s+the\s+extent\s+of\s+(?:any\s+)?(?:conflict|inconsistency)",
    re.IGNORECASE,
)
_PRECEDENCE_OVER_THIS = re.compile(
    r"(?:prevail|control|govern|take\s+precedence|supersede)s?\s+over\s+this\s+"
    r"(?:agreement|nda)"
    r"|supersedes?\s+this\s+(?:agreement|nda)"
    r"|this\s+(?:agreement|nda)\s+(?:is|shall\s+be|will\s+be)\s+subordinate\s+to"
    r"|this\s+(?:agreement|nda)\s+(?:is|shall\s+be|will\s+be)\s+subject\s+to",
    re.IGNORECASE,
)

# REVERSE polarity guard: a precedence clause where THIS document is the SUBJECT
# (the winner) is benign and must NOT flag. e.g. "this Agreement shall prevail
# over ...", "this NDA supersedes all prior agreements", "this Agreement controls".
# CRITICAL: "this Agreement" must be the SUBJECT (immediately before the precedence
# verb), so "supersedes this Agreement" (this doc is the OBJECT/loser) is NOT a
# match here -- that is genuine subordination. A short adverbial gap
# ("shall"/"will"/"hereby"/"expressly") between subject and verb is allowed.
_THIS_DOC_PREVAILS = re.compile(
    r"this\s+(?:agreement|nda)\s+"
    r"(?:(?:shall|will|hereby|expressly|does)\s+){0,3}"
    r"(?:prevail|control|govern|take\s+precedence|supersede)",
    re.IGNORECASE,
)

_THIS_DOC = re.compile(r"this\s+(?:agreement|nda)", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    """Split into sentence / clause segments (period, semicolon, newline)."""
    return [seg for seg in re.split(r"(?<=[.;])\s+|\n+", text or "") if seg.strip()]


def _references_external(sentence: str) -> bool:
    return any(p.search(sentence) for p in _REFERENCE_PATTERNS)


def _grants_external_precedence(sentence: str) -> bool:
    """True when the sentence grants an EXTERNAL agreement overriding authority.

    Handles polarity: a clause where THIS document is the declared winner is NOT a
    subordination and returns False even if precedence words appear.
    """
    # Unambiguous: explicitly subordinates THIS agreement to something else.
    if _PRECEDENCE_OVER_THIS.search(sentence):
        # ...unless the very same clause says THIS doc prevails (mixed wording);
        # the explicit "over this agreement" wins only if this doc isn't the victor.
        if not _THIS_DOC_PREVAILS.search(sentence):
            return True

    # Generic precedence ("shall prevail", "in the event of conflict ... controls").
    if _PRECEDENCE_GENERIC.search(sentence):
        # Reverse-polarity guard: if THIS document is the one prevailing, it's benign.
        if _THIS_DOC_PREVAILS.search(sentence):
            return False
        return True

    return False


def _detect_in_text(text: str) -> dict | None:
    """Core detector over raw document text. Returns a finding dict or None.

    Flags only when a SINGLE sentence both references an external agreement and
    grants that external agreement overriding precedence (and does not merely
    assert that THIS document prevails).
    """
    for sentence in _sentences(text):
        if not _grants_external_precedence(sentence):
            continue
        if not _references_external(sentence):
            continue
        snippet = " ".join(sentence.split())
        if len(snippet) > 240:
            snippet = snippet[:237].rstrip() + "..."
        message = (
            "This NDA appears to subordinate itself to a separate, external "
            "agreement that is given overriding authority -- it both references "
            "another agreement and states that agreement prevails in the event of "
            "conflict. The confidentiality terms may be silently overridden by an "
            f'unseen document. Human review recommended. Clause: "{snippet}"'
        )
        return {"reason_code": REASON_CODE, "message": message}
    return None


def _matter_text(matter: Mapping[str, Any]) -> str:
    """Document text accessor -- mirrors ``law_forum_check._matter_text``."""
    return str(matter.get("extracted_text") or "")


def detect_incorporation_override(matter: Mapping[str, Any]) -> dict | None:
    """Detect incorporation-by-reference subordination on a stored matter.

    Returns ``{"reason_code", "message"}`` when the matter's document subordinates
    itself to an external/unseen agreement that is given overriding authority;
    otherwise ``None``. ADDITIVE, REVIEW-ONLY, FAIL-SAFE: any error returns None so
    it can never crash the board poll.
    """
    try:
        if not isinstance(matter, Mapping):
            return None
        text = _matter_text(matter)
        if not text:
            return None
        return _detect_in_text(text)
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the poll.
        return None
