"""Deterministic law<->forum mismatch detector (ADDITIVE review signal).

WHY THIS EXISTS
---------------
The ``governing_law`` review clause is contractually scoped to the *operative
governing-law sentence* only: the Playbook ``evidence_guidance`` explicitly tells
the model to IGNORE the dispute-resolution / court-venue clause ("do not use an
approved jurisdiction appearing only in ... court venue ..."). That is correct for
judging the governing law in isolation -- but it means a document whose governing
law and whose forum/venue name DIFFERENT jurisdictions (e.g. "governed by the laws
of England and Wales" + "exclusive jurisdiction of the courts of the Cayman
Islands") produces NO signal today. The eval (/tmp/judg-lawforum) confirmed the
production reviewer dismisses the foreign forum on purpose and passes 4/4 such
mismatches.

This module closes that LIVE gap with a deterministic check that mirrors the
GENERATION-side law<->court pairing (``nda_generation._COURT_FOR_OPTION_ID``,
surfaced as the shared ``governing_law_forum.canonical_forum_for_law`` helper):
each approved governing-law option has exactly ONE proper forum jurisdiction. We
invert that to score a parsed inbound document -- if the forum the document names
is a DIFFERENT jurisdiction from the one paired with its governing law, we raise a
mismatch finding.

ANTI-GHOST DESIGN RULE
----------------------
This detector is an ADDITIVE gap-filler, never an override:

* It may ELEVATE a matter to REVIEW (state "review"), with a clear reason.
* It NEVER force-FAILs (never writes state "check"), never downgrades an AI fail,
  and never weakens an AI verdict that is already >= review. The overlay only ever
  upgrades a clean PASS to REVIEW; every other input state is returned unchanged.
* It is FAIL-SAFE: any exception is swallowed (it can never crash the board poll),
  and when the governing-law option or the document forum cannot be resolved it
  stays SILENT (no false flag). Detection requires BOTH a resolved approved law and
  a recognizable foreign forum.

It is a deterministic PRIMARY signal that stands on its own; it does NOT depend on
removing the Playbook "ignore venue" guidance (that is left to the
foundation/integration step). When both name the SAME jurisdiction it is silent.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Canonical jurisdiction "buckets".
#
# Each bucket carries:
#   law   -> phrases that, in a GOVERNING-LAW sentence, name this jurisdiction.
#   forum -> phrases that, in a FORUM/VENUE/ARBITRATION sentence, name this
#            jurisdiction's courts/seat.
#
# This mirrors the generation-side approved options + ``_COURT_FOR_OPTION_ID``
# pairing. The approved-law buckets are keyed by the SAME option ids the Playbook
# uses (england_and_wales / delaware / india / difc / ontario_canada), so the
# governing-law option id resolved by ``governing_law_view.derive_governing_law``
# is itself a bucket key. The extra buckets (cayman_islands / new_york /
# singapore) are forum-only jurisdictions that are NOT approved laws -- present so
# a foreign forum can still be NAMED in the finding.
# ---------------------------------------------------------------------------
JURISDICTIONS: dict[str, dict[str, list[str]]] = {
    "england_and_wales": {
        "law": [r"laws?\s+of\s+england(?:\s+and\s+wales)?", r"english\s+law"],
        "forum": [r"courts?\s+of\s+england(?:\s+and\s+wales)?", r"english\s+courts?"],
    },
    "delaware": {
        "law": [r"laws?\s+of\s+(?:the\s+state\s+of\s+)?delaware", r"delaware\s+law"],
        "forum": [
            r"courts?\s+(?:located\s+)?in\s+(?:the\s+state\s+of\s+)?delaware",
            r"courts?\s+of\s+(?:the\s+state\s+of\s+)?delaware",
            r"delaware\s+courts?",
        ],
    },
    # India forum recognition is RULE-BASED, not a hand-maintained city list. The
    # bucket fires when a forum/venue/arbitration sentence names India by any of the
    # robust signals below, so it does not silently miss whichever Indian city a
    # given NDA happens to pick (Gandhinagar, Chennai, Kolkata, Hyderabad, ...):
    #   * "India" / "Indian" / "courts of India" / "Indian courts";
    #   * any Indian STATE/UT (`_INDIA_STATE`), e.g. "courts of Gujarat";
    #   * "[City], India" -- any city named together with India;
    #   * the common metros by name (kept explicit so a bare "courts of Mumbai" with
    #     no trailing ", India" still resolves), now ALSO covering Chennai / Kolkata
    #     / Hyderabad / Gandhinagar / Pune / Ahmedabad alongside the originals.
    # Precision is preserved by the `_FORUM_SENTENCE` gate (only dispute/venue
    # sentences are inspected) and by the mismatch comparison (an India-law NDA whose
    # forum is also India produces bucket `india` on both sides -> no foreign -> silent).
    "india": {
        "law": [r"laws?\s+of\s+india", r"indian\s+law"],
        "forum": [],  # built below from _INDIA_FORUM_PATTERNS (rule-based, not a city list)
    },
    "difc": {
        "law": [
            r"laws?\s+of\s+the\s+difc",
            r"difc\s+law",
            r"dubai\s+international\s+financial\s+cent(?:re|er)\s+law",
        ],
        "forum": [
            r"difc\s+courts?",
            r"courts?\s+of\s+the\s+dubai\s+international\s+financial\s+cent(?:re|er)",
            r"dubai\s+international\s+financial\s+cent(?:re|er)\s+courts?",
        ],
    },
    "ontario_canada": {
        "law": [r"laws?\s+of\s+(?:the\s+province\s+of\s+)?ontario", r"ontario\s+law"],
        "forum": [r"courts?\s+of\s+(?:the\s+province\s+of\s+)?ontario", r"ontario\s+courts?"],
    },
    # ---- forum-only jurisdictions (NOT approved laws) -------------------------
    "cayman_islands": {
        "law": [r"laws?\s+of\s+the\s+cayman\s+islands"],
        "forum": [r"courts?\s+of\s+the\s+cayman\s+islands", r"cayman\s+islands?\s+courts?"],
    },
    "new_york": {
        "law": [r"laws?\s+of\s+(?:the\s+state\s+of\s+)?new\s+york", r"new\s+york\s+law"],
        "forum": [
            r"courts?\s+(?:located\s+)?in\s+(?:the\s+state\s+of\s+)?new\s+york",
            r"courts?\s+of\s+(?:the\s+state\s+of\s+)?new\s+york",
            r"new\s+york\s+courts?",
        ],
    },
    "singapore": {
        "law": [r"laws?\s+of\s+singapore", r"singapore\s+law"],
        "forum": [
            r"courts?\s+of\s+singapore",
            r"singapore\s+courts?",
            r"(?:arbitration|seat(?:ed)?)\s+[^.;\n]*?singapore",
            r"\bsiac\b",
        ],
    },
    # ---- onshore Dubai / UAE (distinct from the difc bucket) -------------------
    # The user has ruled DIFC distinct from onshore UAE: a DIFC-law NDA that names
    # the ONSHORE Dubai/UAE courts (the Emirate's civil-law courts, NOT the DIFC
    # Courts) is a genuine law<->forum split. This is a FORUM-ONLY bucket (onshore
    # UAE is not an approved governing law). CRITICAL PRECISION: these patterns must
    # NOT fire when "DIFC" is present in the forum phrase -- "DIFC Courts, Dubai" is
    # the DIFC forum name, not onshore Dubai. That exclusion is enforced in
    # ``_match_buckets`` via ``_DIFC_PRESENT`` (DIFC is matched FIRST and suppresses
    # this bucket), so the stray "Dubai" token in a DIFC phrase can never leak here.
    "onshore_dubai": {
        "law": [],
        "forum": [
            r"courts?\s+of\s+(?:the\s+emirate\s+of\s+)?dubai",
            r"dubai\s+courts?",
            r"onshore\s+(?:dubai|uae)\s+courts?",
            r"(?:uae|u\.a\.e\.)\s+federal\s+courts?",
            r"federal\s+courts?\s+of\s+the\s+(?:uae|united\s+arab\s+emirates)",
            r"courts?\s+of\s+the\s+united\s+arab\s+emirates",
        ],
    },
}

# ---------------------------------------------------------------------------
# India forum SMART RULE (replaces the hand-maintained metro list).
#
# `_INDIA_STATE` is the closed set of Indian states + union territories. Naming
# any of them in a forum clause ("courts of Gujarat", "courts at Tamil Nadu") is
# an unambiguous India-forum signal, so we never have to enumerate every city.
# `_INDIA_METRO` keeps the common metros explicit so a bare "courts of Mumbai"
# (no trailing ", India") still resolves -- now expanded past the original three.
# ---------------------------------------------------------------------------
_INDIA_STATE = (
    r"andhra\s+pradesh|arunachal\s+pradesh|assam|bihar|chhattisgarh|goa|gujarat|"
    r"haryana|himachal\s+pradesh|jharkhand|karnataka|kerala|madhya\s+pradesh|"
    r"maharashtra|manipur|meghalaya|mizoram|nagaland|odisha|punjab|rajasthan|"
    r"sikkim|tamil\s+nadu|telangana|tripura|uttar\s+pradesh|uttarakhand|"
    r"west\s+bengal|delhi|puducherry|chandigarh|ladakh|jammu\s+and\s+kashmir"
)
_INDIA_METRO = (
    r"mumbai|bengaluru|bangalore|new\s+delhi|chennai|kolkata|hyderabad|"
    r"gandhinagar|pune|ahmedabad"
)
# Order: India/Indian first, then states, then metros, then "<City>, India", then
# arbitration seat. Each is anchored to a court/seat/jurisdiction context by the
# leading verb so an incidental "India" mention elsewhere never leaks in.
_INDIA_FORUM_PATTERNS = [
    r"courts?\s+(?:of|in|at)\s+india",
    r"indian\s+courts?",
    rf"courts?\s+(?:of|in|at)\s+(?:the\s+state\s+of\s+)?(?:{_INDIA_STATE})\b",
    rf"courts?\s+(?:of|in|at)\s+(?:{_INDIA_METRO})\b",
    # "[City], India" -- any city named together with India in a venue clause.
    r"courts?\s+(?:of|in|at)\s+[a-z][a-z .'-]*?,\s*india\b",
    # Arbitration seated anywhere in India (state, metro, or India itself).
    rf"(?:arbitration|seat(?:ed)?)\s+[^.;\n]*?(?:india|{_INDIA_STATE}|{_INDIA_METRO})\b",
]
JURISDICTIONS["india"]["forum"] = _INDIA_FORUM_PATTERNS

# When a forum phrase mentions the DIFC at all, the onshore-Dubai bucket must NOT
# fire: "DIFC Courts, Dubai International Financial Centre" is the DIFC forum name,
# and the bare "Dubai" token inside it is part of that name -- not the onshore
# Emirate of Dubai courts. DIFC wins; the onshore bucket is suppressed.
_DIFC_PRESENT = re.compile(r"difc|dubai\s+international\s+financial\s+cent(?:re|er)", re.IGNORECASE)

# Human-readable jurisdiction labels for the finding text.
JURISDICTION_LABELS: dict[str, str] = {
    "england_and_wales": "England and Wales",
    "delaware": "Delaware",
    "india": "India",
    "difc": "DIFC (Dubai International Financial Centre)",
    "ontario_canada": "Ontario, Canada",
    "cayman_islands": "Cayman Islands",
    "new_york": "New York",
    "singapore": "Singapore",
    "onshore_dubai": "onshore Dubai / UAE (courts outside the DIFC)",
}

# Sentence-role gates: a sentence is only inspected for a LAW jurisdiction when it
# reads like a governing-law sentence, and only for a FORUM jurisdiction when it
# reads like a dispute-resolution / venue / arbitration sentence. This keeps an
# incorporation recital ("organized under the laws of X") or a party address from
# being mistaken for an operative clause.
_LAW_SENTENCE = re.compile(
    r"governed\s+by|construed\s+in\s+accordance|choice\s+of\s+law|governing\s+law",
    re.IGNORECASE,
)
_FORUM_SENTENCE = re.compile(
    r"jurisdiction|venue|submit\s+to|courts?\s+of|courts?\s+(?:located\s+)?in|"
    r"arbitrat|forum|seat(?:ed)?|dispute",
    re.IGNORECASE,
)

# Recital phrasing that names a jurisdiction WITHOUT being an operative
# governing-law sentence -- excluded so "incorporated under the laws of India"
# never reads as the agreement's governing law.
_RECITAL_PREFIX = re.compile(
    r"incorporat|organi[sz]ed|formed|registered|domicil|existing\s+under|established",
    re.IGNORECASE,
)

REASON_CODE = "law_forum_jurisdiction_mismatch"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _sentences(text: str) -> list[str]:
    """Crude split on sentence / clause boundaries (period, semicolon, newline)."""
    return [seg for seg in re.split(r"(?<=[.;])\s+|\n+", text or "") if seg.strip()]


def _match_buckets(sentence: str, role: str) -> set[str]:
    hits: set[str] = set()
    # Precision guard: when the DIFC is named in the phrase, suppress the
    # onshore-Dubai bucket so the "Dubai" token inside "DIFC Courts, Dubai" is read
    # as part of the DIFC forum name, never as the onshore Emirate of Dubai courts.
    difc_present = bool(_DIFC_PRESENT.search(sentence))
    for jur, pats in JURISDICTIONS.items():
        if jur == "onshore_dubai" and difc_present:
            continue
        for pat in pats.get(role, ()):  # type: ignore[arg-type]
            if re.search(pat, sentence, re.IGNORECASE):
                hits.add(jur)
                break
    return hits


def extract_law_jurisdictions(text: str) -> set[str]:
    """Jurisdictions named in the document's GOVERNING-LAW sentence(s).

    Only sentences that read like an operative governing-law clause are inspected,
    and pure incorporation/registration recitals are skipped, so a party recital
    cannot leak in as a competing governing law.
    """
    found: set[str] = set()
    for sentence in _sentences(text):
        if not _LAW_SENTENCE.search(sentence):
            continue
        if _RECITAL_PREFIX.search(sentence) and not re.search(
            r"this\s+agreement|governed\s+by|governing\s+law", sentence, re.IGNORECASE
        ):
            continue
        found |= _match_buckets(sentence, "law")
    return found


def extract_forum_jurisdictions(text: str) -> set[str]:
    """Jurisdictions named in the document's FORUM/VENUE/ARBITRATION sentence(s)."""
    found: set[str] = set()
    for sentence in _sentences(text):
        if not _FORUM_SENTENCE.search(sentence):
            continue
        found |= _match_buckets(sentence, "forum")
    return found


def _normalize_to_bucket(value: object) -> str:
    """Map a free label / canonical id to one of our jurisdiction bucket keys.

    Used to canonicalize the EXPECTED forum the shared helper returns
    (``forum_jurisdiction`` / ``court_name``) into the same bucket vocabulary the
    document extractor produces, so the two sides compare apples-to-apples
    regardless of whether the helper emits an option id, a jurisdiction label, or a
    court name. Returns "" when no bucket matches.
    """
    token = str(value or "").strip().lower()
    if not token:
        return ""
    # Direct bucket id (e.g. the helper already returns "england_and_wales").
    if token in JURISDICTIONS:
        return token
    # Otherwise treat the text as a court/forum phrase and match it against every
    # bucket's forum AND law patterns (a court_name like "DIFC Courts" hits the
    # forum patterns; a bare "England and Wales" hits the law/forum phrasing).
    for jur, pats in JURISDICTIONS.items():
        for role in ("forum", "law"):
            for pat in pats.get(role, ()):  # type: ignore[arg-type]
                if re.search(pat, token, re.IGNORECASE):
                    return jur
    # Last resort: a bare label match against the human labels.
    for jur, label in JURISDICTION_LABELS.items():
        if label.lower() in token or token in label.lower():
            return jur
    return ""


def _label(bucket: str) -> str:
    return JURISDICTION_LABELS.get(bucket, bucket.replace("_", " ").title() if bucket else "")


# ---------------------------------------------------------------------------
# Shared-helper resolution (foundation teammate owns the helper).
# ---------------------------------------------------------------------------
def _canonical_forum_for_law(playbook: Mapping[str, Any], law_option_id: str) -> dict | None:
    """Resolve the canonical forum for an approved governing-law option.

    Delegates to the shared ``governing_law_forum.canonical_forum_for_law`` helper
    when it is available (the foundation teammate owns/implements it). Until that
    module is merged the import fails and we return None, which keeps the detector
    silent rather than guessing -- the deterministic pairing oracle is owned by the
    foundation track, not duplicated here.
    """
    try:
        from . import governing_law_forum  # noqa: PLC0415 -- optional dependency.
    except Exception:  # noqa: BLE001 -- helper not merged yet / import error.
        return None
    helper = getattr(governing_law_forum, "canonical_forum_for_law", None)
    if helper is None:
        return None
    try:
        result = helper(playbook, law_option_id)
    except Exception:  # noqa: BLE001 -- never let a helper bug crash the poll.
        return None
    return result if isinstance(result, dict) else None


def expected_forum_bucket(playbook: Mapping[str, Any], law_option_id: str) -> str:
    """The canonical forum-jurisdiction bucket paired with an approved law option.

    Prefers the shared helper's ``forum_jurisdiction`` (then ``court_name``), each
    normalized into our bucket vocabulary. Falls back to the law option id itself
    when the helper is unavailable but the law option is one whose forum is, by
    construction, the SAME jurisdiction (e.g. england_and_wales law -> E&W courts).
    Returns "" when nothing resolves.
    """
    info = _canonical_forum_for_law(playbook, law_option_id)
    if isinstance(info, dict):
        for key in ("forum_jurisdiction", "court_name", "law_label", "option_id"):
            bucket = _normalize_to_bucket(info.get(key))
            if bucket:
                return bucket
    # Helper unavailable: for an approved law option the expected forum is the same
    # jurisdiction as the law (each approved option's proper forum sits in its own
    # jurisdiction), so the option id is itself the expected forum bucket.
    if str(law_option_id or "").strip().lower() in JURISDICTIONS:
        return str(law_option_id).strip().lower()
    return ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect_mismatch(
    text: str,
    law_option_id: str,
    playbook: Mapping[str, Any] | None = None,
) -> dict | None:
    """Deterministically flag a governing-law <-> forum jurisdiction mismatch.

    Returns a finding dict ``{reason_code, reason, law_option_id, law_jurisdiction,
    expected_forum, document_forum}`` when the document names a forum jurisdiction
    that DIFFERS from the one paired with its (approved) governing law; otherwise
    returns None.

    Silent (None) -- never a false flag -- when:
      * ``law_option_id`` is empty / not an approved option, OR
      * the expected forum cannot be resolved, OR
      * the document names no recognizable forum jurisdiction, OR
      * the document forum matches the expected forum (aligned control).
    """
    law_option_id = str(law_option_id or "").strip().lower()
    if not law_option_id:
        return None

    expected = expected_forum_bucket(playbook or {}, law_option_id)
    if not expected:
        return None

    document_forums = extract_forum_jurisdictions(text)
    if not document_forums:
        # No recognizable forum at all -> nothing to compare -> stay silent.
        return None

    # A mismatch exists when the document names a forum jurisdiction OTHER than the
    # expected one. (The expected jurisdiction legitimately appearing alongside is
    # fine; only a FOREIGN forum is a problem.)
    foreign = {bucket for bucket in document_forums if bucket and bucket != expected}
    if not foreign:
        return None

    foreign_sorted = sorted(foreign)
    foreign_labels = ", ".join(_label(b) for b in foreign_sorted)
    expected_label = _label(expected)
    reason = (
        f"Governing law and forum name different jurisdictions: the agreement is "
        f"governed by the law of {expected_label} but submits disputes to "
        f"{foreign_labels}. A law/forum jurisdiction mismatch warrants human review."
    )
    return {
        "reason_code": REASON_CODE,
        "reason": reason,
        "law_option_id": law_option_id,
        "law_jurisdiction": expected,
        "expected_forum": expected,
        "document_forum": foreign_sorted[0],
        "document_forums": foreign_sorted,
    }


# ---------------------------------------------------------------------------
# Matter-level helpers (read the resolved law + text off a matter dict).
# ---------------------------------------------------------------------------
def _matter_law_option_id(matter: Mapping[str, Any]) -> str:
    """The matter's resolved governing-law approved-option id, or "".

    Reuses ``governing_law_view.derive_governing_law`` -- the single source the
    dashboard/corpus already use -- so the detector can never drift from how the
    rest of the app resolves a matter's governing law.
    """
    try:
        from . import governing_law_view  # noqa: PLC0415 -- avoid load-time cycle.

        return str(governing_law_view.derive_governing_law(dict(matter)) or "")
    except Exception:  # noqa: BLE001 -- a resolution error must not crash the poll.
        return ""


def _matter_text(matter: Mapping[str, Any]) -> str:
    return str(matter.get("extracted_text") or "")


def _active_playbook() -> Mapping[str, Any]:
    """The active Playbook dict (best-effort; empty mapping on any failure)."""
    try:
        from . import playbook_runtime  # noqa: PLC0415 -- avoid import cycle at load.

        bundle = playbook_runtime.ensure_active_playbook_bundle()
        playbook = bundle.playbook if bundle is not None else {}
        return playbook if isinstance(playbook, Mapping) else {}
    except Exception:  # noqa: BLE001 -- a missing/broken Playbook just disables the dim.
        return {}


def detect_matter_mismatch(matter: Mapping[str, Any]) -> dict | None:
    """Run the mismatch detector over a stored matter (fail-safe).

    Resolves the matter's governing-law option + extracted text + active Playbook
    and delegates to :func:`detect_mismatch`. Returns the finding dict or None.
    Any error is swallowed (returns None) so it can never crash the board poll.
    """
    try:
        if not isinstance(matter, Mapping):
            return None
        law_option_id = _matter_law_option_id(matter)
        if not law_option_id:
            return None
        text = _matter_text(matter)
        if not text:
            return None
        return detect_mismatch(text, law_option_id, _active_playbook())
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the poll.
        return None


# ---------------------------------------------------------------------------
# Additive overlay (the anti-ghost seam).
# ---------------------------------------------------------------------------
def apply_lawforum_overlay(
    review_state: dict | None,
    matter: Mapping[str, Any],
) -> dict | None:
    """ELEVATE a clean review_state to REVIEW on a law/forum mismatch -- additive.

    THE ANTI-GHOST CONTRACT:
      * Only a state that is currently PASS ("pass") is ever upgraded -- to REVIEW.
      * Any state that is already REVIEW or CHECK (or anything other than a clean
        pass) is returned UNCHANGED: the detector never downgrades, never softens,
        and never overrides a stronger AI verdict. It is strictly a gap-filler that
        adds a review signal where the AI produced a clean pass.
      * Fail-safe: when there is no mismatch, or the input is not a clean pass, or
        anything raises, the original ``review_state`` is returned untouched.

    Returns the (possibly elevated) review_state. Pure: it builds a new dict and
    never mutates the input. Designed to be called in the read/projection path
    (e.g. ``matter_view.public_matter`` right after ``matter_review_state``), so it
    never persists over the stored review.
    """
    try:
        if not isinstance(review_state, dict):
            return review_state
        # Import locally to avoid any load-time cycle with review_state.
        from .review_state import (  # noqa: PLC0415
            REVIEW_STATE_PASS,
            REVIEW_STATE_REVIEW,
            _overall_status_for_state,
            _state_label,
            _state_tone,
        )

        current = str(review_state.get("state") or "").strip().lower()
        # Only a clean PASS is elevatable. Anything already needing attention
        # (review/check) is a STRONGER signal -- leave it exactly as the AI set it.
        if current != REVIEW_STATE_PASS:
            return review_state

        finding = detect_matter_mismatch(matter)
        if not finding:
            return review_state

        elevated = dict(review_state)
        elevated["state"] = REVIEW_STATE_REVIEW
        elevated["overall_status"] = _overall_status_for_state(REVIEW_STATE_REVIEW)
        elevated["label"] = _state_label(REVIEW_STATE_REVIEW)
        elevated["tone"] = _state_tone(REVIEW_STATE_REVIEW)
        elevated["requires_attention"] = True
        elevated["requires_human_review"] = True
        elevated["blocks_send"] = True
        elevated["blocks_auto_send"] = True
        elevated["law_forum_mismatch"] = True
        elevated["law_forum_mismatch_reason"] = finding.get("reason", "")
        # Surface the reason code alongside the existing ones (additive, deduped).
        existing_codes = elevated.get("reason_codes")
        codes = list(existing_codes) if isinstance(existing_codes, list) else []
        if REASON_CODE not in codes:
            codes.append(REASON_CODE)
        elevated["reason_codes"] = codes
        return elevated
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the poll; never alter on error.
        return review_state
