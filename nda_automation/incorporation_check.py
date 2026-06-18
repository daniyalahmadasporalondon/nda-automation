"""Deterministic incorporation-by-reference / "shall prevail" detector.

WHY THIS EXISTS
---------------
An NDA can quietly subordinate itself to a SEPARATE, UNSEEN agreement: it both
*references* an external document ("subject to the MSA", "incorporated by
reference", "governed by the terms of the Master Services Agreement", "pursuant to
the SOW") AND *grants that external document overriding authority* ("which shall
prevail in any conflict", "takes precedence over this Agreement", "in the event of
conflict the MSA controls", "this NDA is subordinate to the MSA"). When both halves
are present, the confidentiality protections we believe we are signing can be
silently overridden by terms we have never read. That warrants human review.

This is a deterministic, ADDITIVE, REVIEW-ONLY signal. It mirrors the design of
``nda_automation/law_forum_check.py``:

* It reads the document via the SAME accessor that module uses -- the matter's
  ``extracted_text`` field.
* It NEVER force-FAILs and never overrides an AI verdict. The caller (wiring
  teammate) decides how to surface the finding; this module only DETECTS.
* It is FAIL-SAFE: any error returns ``None`` (it can never crash the board poll).

DIRECTION IS THE WHOLE GAME
---------------------------
The hard constraint is POLARITY: flag only when the OTHER document prevails, never
when THIS NDA prevails. The same tokens ("prevail", "supersede", "conflict",
"Master Services Agreement") appear in BOTH a dangerous subordination and a benign
self-asserting merger clause -- so the detector cannot key on tokens. It resolves,
for every precedence clause, the GRAMMATICAL SUBJECT (which document wins):

  * Subject == this NDA (incl. self-names "this Non-Disclosure / Confidentiality /
    Mutual Agreement") -> SAFE. e.g. "this NDA shall prevail over the MSA",
    "This Agreement ... supersedes ... any Master Services Agreement". SILENT.
  * Subject == an external agreement -> the other doc wins -> FLAG.
  * "this NDA is/remains subordinate to [external]" / "subject to [external] which
    shall prevail" -> this NDA is the loser -> FLAG.

PRECISION CONTRACT
------------------
FLAG only when an external/unseen agreement is given OVERRIDING authority over THIS
NDA. Stay SILENT for:
  * a benign reference with no precedence ("Confidential Information as defined in
    the MSA", "as further described in the SOW"),
  * a mere recital / historical mention ("WHEREAS the parties entered into an MSA"),
  * the REVERSE polarity, where THIS NDA prevails / supersedes (entire-agreement
    merger clauses included).

A precedence clause and the external-agreement reference need NOT sit in the same
sentence: a document-level incorporation-by-reference earlier in the text combined
with a generic prevail clause later (whose winning subject is not this NDA) is a
genuine subordination and is flagged (the distance trap).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

REASON_CODE = "incorporation_by_reference_override"

# ---------------------------------------------------------------------------
# External-agreement vocabulary.
#
# Names/abbreviations of the kinds of SEPARATE agreement an NDA might subordinate
# itself to. "this Agreement" / "this NDA" are deliberately NOT here -- a precedence
# clause whose subject is THIS document is the benign reverse polarity.
# ---------------------------------------------------------------------------
_EXTERNAL_AGREEMENT = (
    r"master\s+(?:services|service|subscription|supply)\s+agreement"
    r"|master\s+agreement"
    r"|statement\s+of\s+work"
    r"|strategic\s+partnership\s+agreement"
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
    r"|\bmsa\b"
    r"|\bsow\b"
    r"|\bsla\b"
)
_EXTERNAL_AGREEMENT_RE = re.compile(_EXTERNAL_AGREEMENT, re.IGNORECASE)

# "this Agreement" and its self-name variants -- the document UNDER review. A
# precedence subject matching this is OUR document (the benign / safe direction).
# Covers "this Non-Disclosure Agreement", "this Confidentiality Agreement", "this
# Mutual NDA", "the terms of this Agreement", "the provisions of this NDA", etc.
_THIS_DOC_NOUN = (
    r"this\s+(?:mutual\s+)?"
    r"(?:non-?disclosure\s+|confidentiality\s+|mutual\s+|present\s+)?"
    r"(?:agreement|nda)"
)
_THIS_DOC_RE = re.compile(_THIS_DOC_NOUN, re.IGNORECASE)

# A precedence-clause SUBJECT phrase: "the terms of <doc>", "the provisions of
# <doc>", or "<doc>" bare. We capture the doc noun so we can ask whose side wins.
_SUBJECT_PREFIX = r"(?:the\s+(?:terms?|provisions?|obligations?)\s+of\s+)?"

# Precedence VERBS that, when the subject is the OTHER document, mean the other doc
# wins. ("subordinate" is handled separately -- it flips the subject/object.)
_PRECEDENCE_VERB = (
    r"shall\s+(?:prevail|control|govern|take\s+precedence|supersede)"
    r"|(?:shall\s+)?supersede\s+and\s+govern"
    r"|takes?\s+precedence"
    r"|will\s+(?:prevail|control|govern|supersede)"
    r"|(?:prevails?|controls?|governs?|supersedes?)"
)

# Reference phrasing that, on its own, INCORPORATES / subordinates structurally
# (used both as the document-level reference for the distance trap and -- for
# "subject to [external]" -- as a half of a subordination when paired with a
# prevail verb).
_INCORP_REFERENCE = re.compile(
    r"incorporat\w*\s+(?:herein\s+)?by\s+reference"
    r"|incorporat\w*\s+(?:into|in)\s+this\s+(?:agreement|nda)\s+by\s+reference",
    re.IGNORECASE,
)

# Anaphoric reference to a PREVIOUSLY-named external agreement -- "that agreement",
# "such agreement", "the said agreement", "the other agreement". Used only on the
# distance-trap path (after a genuine incorporation-by-reference), to resolve a
# prevail clause whose subject points back to the incorporated doc. Deliberately
# excludes "this agreement/nda" (our doc) and "the entire agreement" (merger).
_ANAPHORIC_EXTERNAL = re.compile(
    r"\b(?:that|such|the\s+said|the\s+other)\s+agreement\b",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    """Split into sentence / clause segments (period, semicolon, newline)."""
    return [seg for seg in re.split(r"(?<=[.;])\s+|\n+", text or "") if seg.strip()]


# ---------------------------------------------------------------------------
# Direction resolution: for a sentence containing a precedence verb, decide which
# document is the WINNER (the grammatical subject of the prevail verb), and which
# is the LOSER. Returns the matched external-agreement name when the OTHER doc wins
# (or this NDA is explicitly subordinated), else None.
# ---------------------------------------------------------------------------

# (A) "[subject] <verb>": the noun phrase IMMEDIATELY before a precedence verb is
# the winner. We scan every precedence-verb occurrence and look back a bounded
# window for the nearest document noun (this-doc vs external). The nearest one is
# the subject.
_VERB_SCAN = re.compile(
    r"(?P<verb>" + _PRECEDENCE_VERB + r")",
    re.IGNORECASE,
)

# (B) Explicit subordination of THIS doc: "this NDA is/shall remain subordinate to
# [external]". Subject is this NDA but the verb 'subordinate to' makes the OBJECT
# the winner.
_SUBORDINATE_TO = re.compile(
    _THIS_DOC_NOUN
    + r"\s+(?:is\s+and\s+(?:shall|will)\s+remain|is|shall\s+(?:be|remain)|will\s+(?:be|remain)|are)\s+"
    + r"subordinate\s+to\s+(?:the\s+|that\s+certain\s+|any\s+)?(?P<ext>[^.;\n]*?)(?=[.;\n]|$)",
    re.IGNORECASE,
)

# (C) "subject to [external] ... which shall prevail/supersede/control": this NDA is
# made subject to an external doc that then gets a precedence verb. The "subject to
# [external]" frames the NDA as subordinate; the prevail verb confirms direction.
_SUBJECT_TO_EXTERNAL = re.compile(
    r"subject\s+to\s+(?:the\s+(?:terms?\s+(?:and\s+conditions?\s+)?of\s+)?)?"
    r"(?:the\s+|that\s+certain\s+|any\s+)?[^.;\n]*?(?:" + _EXTERNAL_AGREEMENT + r")",
    re.IGNORECASE,
)


def _nearest_subject_is_external(sentence: str, verb_start: int) -> tuple[bool, str]:
    """Resolve the subject of a precedence verb at ``verb_start``.

    Look back over the text preceding the verb and find the LAST (nearest) document
    noun -- either ``this <...> Agreement`` or an external-agreement name. That
    nearest noun is the grammatical subject (the party that wins).

    Returns ``(external_wins, external_name)`` where ``external_wins`` is True only
    when the nearest preceding subject is an EXTERNAL agreement. ``external_name``
    is the matched external phrase (best-effort) for the finding text.
    """
    before = sentence[:verb_start]

    this_doc_pos = -1
    for m in _THIS_DOC_RE.finditer(before):
        this_doc_pos = m.start()

    ext_pos = -1
    ext_name = ""
    for m in _EXTERNAL_AGREEMENT_RE.finditer(before):
        ext_pos = m.start()
        ext_name = m.group(0)

    if ext_pos < 0:
        # No external doc named before the verb -> subject is not an external doc.
        return (False, "")
    if this_doc_pos > ext_pos:
        # "this Agreement" sits closer to the verb -> OUR doc is the subject.
        return (False, "")
    # The nearest preceding subject is the external agreement -> it wins.
    return (True, ext_name)


def _external_wins_in_sentence(sentence: str) -> tuple[bool, str]:
    """Does this sentence grant an EXTERNAL agreement precedence over THIS NDA?

    Returns ``(flag, external_name)``. Resolves direction by subject, so a clause
    where THIS NDA is the prevailing subject (or where an entire-agreement merger
    clause runs in our favour) stays SILENT.
    """
    # (B) Explicit "this NDA is subordinate to [external]".
    m = _SUBORDINATE_TO.search(sentence)
    if m:
        ext = m.group("ext") or ""
        em = _EXTERNAL_AGREEMENT_RE.search(ext)
        if em:
            return (True, em.group(0))

    # (C) "subject to [external] ... <precedence verb>": NDA made subject to an
    # external doc, with a precedence verb confirming the external doc controls.
    sub = _SUBJECT_TO_EXTERNAL.search(sentence)
    if sub and re.search(_PRECEDENCE_VERB, sentence[sub.end():], re.IGNORECASE):
        # Make sure the prevailing subject after "subject to ..." is not THIS doc.
        for vm in _VERB_SCAN.finditer(sentence):
            if vm.start() < sub.end():
                continue
            ext_wins, ext_name = _nearest_subject_is_external(sentence, vm.start())
            # When the prevail verb has no nearer this-doc subject, the external doc
            # framed by "subject to" is the winner.
            if ext_wins:
                return (True, ext_name)
            if not _THIS_DOC_RE.search(sentence[sub.end():vm.start()]):
                em = _EXTERNAL_AGREEMENT_RE.search(sub.group(0))
                if em:
                    return (True, em.group(0))
        # Fall through: subject-to + a precedence verb with the external doc framed
        # as the controlling instrument.
        em = _EXTERNAL_AGREEMENT_RE.search(sub.group(0))
        if em:
            return (True, em.group(0))

    # (A) General subject resolution for every precedence verb in the sentence.
    for vm in _VERB_SCAN.finditer(sentence):
        ext_wins, ext_name = _nearest_subject_is_external(sentence, vm.start())
        if ext_wins:
            return (True, ext_name)

    return (False, "")


def _document_incorporates_external(text: str) -> str:
    """Return an external-agreement name that is INCORPORATED by reference document-
    wide (for the distance trap), or "" if none.

    Only true incorporation phrasing counts ("incorporated by reference",
    "incorporated into this Agreement by reference") -- a benign "as defined in the
    MSA" / "as described in the SOW" / a WHEREAS recital does NOT.
    """
    for sentence in _sentences(text):
        if not _INCORP_REFERENCE.search(sentence):
            continue
        em = _EXTERNAL_AGREEMENT_RE.search(sentence)
        if em:
            return em.group(0)
    return ""


def _detect_in_text(text: str) -> dict | None:
    """Core detector. Returns a finding dict or None.

    Two paths, both DIRECTION-resolved:

    1. SENTENCE-LOCAL: a single sentence grants an external agreement precedence
       over this NDA (subject resolution picks the external doc as winner, or this
       NDA is explicitly subordinated).

    2. DISTANCE TRAP: the document incorporates an external agreement by reference
       somewhere, AND a (possibly distant) generic prevail clause names an external
       agreement as the winner without this NDA being the prevailing subject.
    """
    sentences = _sentences(text)

    # Path 1 -- sentence-local, fully direction-resolved.
    for sentence in sentences:
        flag, ext_name = _external_wins_in_sentence(sentence)
        if flag:
            return _finding(sentence, ext_name)

    # Path 2 -- distance trap. Requires a real incorporation-by-reference of an
    # external agreement, plus a (possibly distant) prevail clause whose winner is
    # the incorporated external agreement -- named explicitly OR referred to
    # anaphorically ("that agreement" / "such agreement" / "the said agreement").
    # _external_wins_in_sentence already excludes the this-NDA-prevails direction.
    # Gating on a GENUINE incorporation-by-reference (not a benign "as defined in"
    # / WHEREAS recital) keeps fp05/fp06 silent.
    incorporated = _document_incorporates_external(text)
    if incorporated:
        for sentence in sentences:
            for vm in _VERB_SCAN.finditer(sentence):
                # (a) The external doc is named as the subject in this sentence.
                ext_wins, ext_name = _nearest_subject_is_external(sentence, vm.start())
                if ext_wins:
                    return _finding(sentence, ext_name or incorporated)
                # (b) Anaphoric subject ("that/such/the said agreement") that is NOT
                # this NDA -- resolves to the incorporated external agreement across
                # distance. Only when this NDA is not the nearer prevailing subject.
                before = sentence[: vm.start()]
                if not _ANAPHORIC_EXTERNAL.search(before):
                    continue
                ana_pos = max(
                    (m.start() for m in _ANAPHORIC_EXTERNAL.finditer(before)),
                    default=-1,
                )
                this_pos = max(
                    (m.start() for m in _THIS_DOC_RE.finditer(before)),
                    default=-1,
                )
                if ana_pos > this_pos:
                    return _finding(sentence, incorporated)
    return None


def _finding(sentence: str, ext_name: str) -> dict:
    snippet = " ".join(sentence.split())
    if len(snippet) > 240:
        snippet = snippet[:237].rstrip() + "..."
    named = ext_name.strip() if ext_name else "a separate agreement"
    message = (
        "This NDA appears to be subordinated to a separate, external agreement "
        f"({named}) that is given overriding authority -- the document references "
        "that agreement and states it prevails/controls in the event of conflict. "
        "The confidentiality terms may be silently overridden by an unseen "
        f'document. Human review recommended. Clause: "{snippet}"'
    )
    return {"reason_code": REASON_CODE, "message": message}


def _matter_text(matter: Mapping[str, Any]) -> str:
    """Document text accessor -- mirrors ``law_forum_check._matter_text``."""
    return str(matter.get("extracted_text") or "")


def detect_incorporation_override(matter: Mapping[str, Any]) -> dict | None:
    """Detect incorporation-by-reference subordination on a stored matter.

    Returns ``{"reason_code", "message"}`` when the matter's document subordinates
    itself to an external/unseen agreement that is given overriding authority OVER
    THIS NDA; otherwise ``None``. ADDITIVE, REVIEW-ONLY, FAIL-SAFE: any error
    returns None so it can never crash the board poll.
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
