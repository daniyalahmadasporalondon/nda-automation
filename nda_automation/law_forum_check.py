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

# ===========================================================================
# Canonical jurisdiction "buckets" -- DERIVED FROM THE PLAYBOOK (north star).
#
# Each bucket carries:
#   law   -> phrases that, in a GOVERNING-LAW sentence, name this jurisdiction.
#   forum -> phrases that, in a FORUM/VENUE/ARBITRATION sentence, name this
#            jurisdiction's courts/seat.
#
# THE APPROVED-LAW BUCKETS ARE NO LONGER HARDCODED. They are built at module
# load (and re-buildable) from the Playbook's
# ``governing_law.rules.approved_options`` -- the SAME single source
# ``governing_law_view`` reads -- so adding a 6th approved law to playbook.json
# makes this law<->forum mismatch detector recognize it automatically (no code
# change), instead of going blind to it. For each approved option we:
#   * seed the LAW recognition phrases from its ``value`` + ``aliases``;
#   * seed the FORUM recognition from its ``forum_jurisdiction``;
#   * MERGE IN the hand-tuned matcher fragments below (`_APPROVED_LAW_MATCHER_AUGMENTS`)
#     keyed by option id -- the rich, precision-tuned regexes (India's smart rule,
#     Delaware's "courts of the State of", DIFC's multi-form recognition, ...) that
#     free-text aliases alone cannot express. The augments are pure RECOGNITION
#     HEURISTICS, never rule DATA: the set of approved laws, their names, and their
#     paired forum jurisdiction all come from the Playbook. An option with no augment
#     still gets robust default matchers built from its free-text name.
#
# The forum-only jurisdictions (cayman_islands / new_york / singapore /
# onshore_dubai) are NOT approved laws -- they are foreign venues the detector
# watches for so a foreign forum can still be NAMED in the finding. They remain
# code constants (`_FOREIGN_FORUM_BUCKETS`).
# ===========================================================================

# ---------------------------------------------------------------------------
# India forum SMART RULE (replaces a hand-maintained metro list).
#
# `_INDIA_STATE` is the closed set of Indian states + union territories. Naming
# any of them in a forum clause ("courts of Gujarat", "courts at Tamil Nadu") is
# an unambiguous India-forum signal, so we never have to enumerate every city.
# `_INDIA_METRO` keeps the common metros explicit so a bare "courts of Mumbai"
# (no trailing ", India") still resolves.
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

# ---------------------------------------------------------------------------
# Per-approved-option recognition AUGMENTS (heuristics, NOT rule data).
#
# These are the precision-tuned matcher fragments the bare free-text name +
# aliases cannot express. They are MERGED on top of the playbook-derived seeds
# (deduped) when an approved option has a matching id. An approved option WITHOUT
# an augment still gets robust default matchers (see `_default_law_matchers` /
# `_default_forum_matchers`) built from its name, so a brand-new 6th approved law
# is recognized out of the box -- the augment only sharpens the established five.
# ---------------------------------------------------------------------------
_APPROVED_LAW_MATCHER_AUGMENTS: dict[str, dict[str, list[str]]] = {
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
    "india": {
        "law": [r"laws?\s+of\s+india", r"indian\s+law"],
        # Rule-based, not a city list (see _INDIA_FORUM_PATTERNS above).
        "forum": list(_INDIA_FORUM_PATTERNS),
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
}

# ---------------------------------------------------------------------------
# Foreign forum-only buckets (NOT approved laws) -- kept as code constants.
# These are jurisdictions the detector watches for as a FOREIGN forum so a
# law<->forum split can still NAME the venue. They are not in the Playbook's
# approved-law options, so they cannot be derived -- the detector owns them.
# ---------------------------------------------------------------------------
_FOREIGN_FORUM_BUCKETS: dict[str, dict[str, list[str]]] = {
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
# Playbook-sourced approved-law bucket derivation.
# ---------------------------------------------------------------------------
def _phrase_to_regex(phrase: str) -> str:
    r"""Turn a free-text jurisdiction name into a whitespace-tolerant regex fragment.

    "England and Wales" -> r"england\s+and\s+wales"; collapses internal runs of
    whitespace to ``\s+`` so a name spanning a line break or double space still
    matches, and escapes any regex metacharacters in the raw token.
    """
    token = " ".join(str(phrase or "").strip().split())
    if not token:
        return ""
    return re.escape(token).replace(r"\ ", r"\s+")


def _default_law_matchers(names: list[str]) -> list[str]:
    """Default GOVERNING-LAW recognition phrases built from an option's free text.

    For each name (value/label/alias) we recognize "laws of <name>" and
    "<name> law" -- the two operative governing-law phrasings -- so a brand-new
    approved law is recognized from its Playbook name with no hand-tuning.
    """
    out: list[str] = []
    for name in names:
        frag = _phrase_to_regex(name)
        if not frag:
            continue
        out.append(rf"laws?\s+of\s+(?:the\s+)?{frag}")
        out.append(rf"{frag}\s+law")
    return out


def _default_forum_matchers(names: list[str]) -> list[str]:
    """Default FORUM recognition phrases built from an option's free text.

    Recognizes "courts of/in/at <name>" and "<name> courts" for each name -- the
    common venue phrasings -- so a new approved law's own-jurisdiction forum (and a
    document that names it as a foreign forum) is recognized from the Playbook name.
    """
    out: list[str] = []
    for name in names:
        frag = _phrase_to_regex(name)
        if not frag:
            continue
        out.append(rf"courts?\s+(?:of|in|at)\s+(?:the\s+)?{frag}")
        out.append(rf"{frag}\s+courts?")
    return out


def _option_law_names(option: Mapping[str, Any]) -> list[str]:
    """Distinct free-text names for an approved option: value, label, aliases."""
    names: list[str] = []
    for key in ("value", "label"):
        token = str(option.get(key) or "").strip()
        if token:
            names.append(token)
    aliases = option.get("aliases")
    if isinstance(aliases, (list, tuple)):
        for alias in aliases:
            token = str(alias or "").strip()
            if token:
                names.append(token)
    # De-dup case-insensitively, preserving order.
    seen: set[str] = set()
    distinct: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            distinct.append(name)
    return distinct


def _dedup(patterns: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for pat in patterns:
        if pat and pat not in seen:
            seen.add(pat)
            out.append(pat)
    return out


def _approved_options() -> list[Mapping[str, Any]]:
    """The active Playbook's ``governing_law`` approved options (best-effort).

    Reuses ``governing_law_view``'s playbook resolution so this detector reads the
    SAME single source the dashboard/corpus governing-law dimension reads. Any
    failure yields an empty list (the approved-law buckets just won't derive, and
    the foreign-forum buckets still work) -- the detector never crashes on a missing
    Playbook.
    """
    try:
        from . import governing_law_view  # noqa: PLC0415 -- avoid load-time cycle.

        return governing_law_view._approved_governing_law_options()
    except Exception:  # noqa: BLE001 -- a missing/broken Playbook just disables derivation.
        return []


def _build_approved_law_buckets() -> dict[str, dict[str, list[str]]]:
    """Derive the approved-law jurisdiction buckets from the Playbook options.

    For each approved option (keyed by its id) build law + forum recognition from
    its free-text name(s) and forum_jurisdiction, then MERGE the hand-tuned augment
    (when present). The label cache is seeded alongside so the finding text shows
    the Playbook label for a derived law.
    """
    buckets: dict[str, dict[str, list[str]]] = {}
    for option in _approved_options():
        option_id = str(option.get("id") or "").strip().lower()
        if not option_id:
            continue
        law_names = _option_law_names(option)
        forum_jurisdiction = str(option.get("forum_jurisdiction") or "").strip()
        # Law recognition: from value/label/aliases.
        law_patterns = _default_law_matchers(law_names)
        # Forum recognition: from forum_jurisdiction, AND from the law names (the
        # law's own jurisdiction is also its proper forum, so "courts of <law>"
        # resolves to this bucket and an aligned NDA stays silent).
        forum_seed_names = list(law_names)
        if forum_jurisdiction:
            forum_seed_names.append(forum_jurisdiction)
        forum_patterns = _default_forum_matchers(forum_seed_names)
        # Merge the precision augment (heuristics) for this option, if any.
        augment = _APPROVED_LAW_MATCHER_AUGMENTS.get(option_id, {})
        law_patterns = _dedup(list(augment.get("law", [])) + law_patterns)
        forum_patterns = _dedup(list(augment.get("forum", [])) + forum_patterns)
        buckets[option_id] = {"law": law_patterns, "forum": forum_patterns}
    return buckets


# JURISDICTIONS is the merged bucket map: derived approved-law buckets +
# foreign-forum buckets. Built at import; re-derivable via ``reset_buckets`` after
# a Playbook republish (mirrors ``governing_law_view.reset_caches``).
# ``playbook_authoring`` now actually calls ``reset_buckets()`` on publish/save/
# restore, so a republish takes effect in-process without a restart.
JURISDICTIONS: dict[str, dict[str, list[str]]] = {}


def approved_law_buckets() -> dict[str, dict[str, list[str]]]:
    """The Playbook-derived approved-law buckets only (not the foreign-forum ones)."""
    return {k: v for k, v in JURISDICTIONS.items() if k not in _FOREIGN_FORUM_BUCKETS}


def reset_buckets() -> None:
    """Rebuild JURISDICTIONS from the current Playbook (tests / a Playbook republish).

    Approved-law buckets are re-derived from the Playbook options; the foreign-forum
    buckets are constant. Mutates JURISDICTIONS in place so existing references stay
    valid.
    """
    merged = _build_approved_law_buckets()
    for name, bucket in _FOREIGN_FORUM_BUCKETS.items():
        # A foreign-forum name must never shadow a derived approved-law bucket of the
        # same id (defensive -- the two id spaces are disjoint today).
        merged.setdefault(name, {"law": list(bucket["law"]), "forum": list(bucket["forum"])})
    # Concurrent readers (ThreadingHTTPServer requests) race this in-place swap:
    # building the merged map FIRST shrinks the empty/partial window from a Playbook
    # disk read under flock to just the clear()+update() below.
    JURISDICTIONS.clear()
    JURISDICTIONS.update(merged)


reset_buckets()

# When a forum phrase mentions the DIFC at all, the onshore-Dubai bucket must NOT
# fire: "DIFC Courts, Dubai International Financial Centre" is the DIFC forum name,
# and the bare "Dubai" token inside it is part of that name -- not the onshore
# Emirate of Dubai courts. DIFC wins; the onshore bucket is suppressed.
_DIFC_PRESENT = re.compile(r"difc|dubai\s+international\s+financial\s+cent(?:re|er)", re.IGNORECASE)

# Human-readable jurisdiction labels for the finding text.
#
# Foreign-forum buckets are constant (they are not Playbook options). The
# approved-law buckets prefer their Playbook label (via ``_label`` -> the
# ``governing_law_view`` label cache) so a 6th approved law shows its real label;
# the few entries kept here for approved laws are nicer OVERRIDES (e.g. DIFC's
# parenthetical) that read better than the bare Playbook value in a finding.
_FOREIGN_FORUM_LABELS: dict[str, str] = {
    "cayman_islands": "Cayman Islands",
    "new_york": "New York",
    "singapore": "Singapore",
    "onshore_dubai": "onshore Dubai / UAE (courts outside the DIFC)",
}
# Optional display overrides for approved-law buckets (nicer than the bare
# Playbook value). Any approved law WITHOUT an override falls back to its Playbook
# label, so a new approved law is labelled from the Playbook automatically.
_APPROVED_LAW_LABEL_OVERRIDES: dict[str, str] = {
    "difc": "DIFC (Dubai International Financial Centre)",
}
# Back-compat: the original flat label map other modules/tests may import. Kept as
# the union (foreign-forum labels + approved-law overrides); the live label
# resolution in ``_label`` additionally falls back to the Playbook label.
JURISDICTION_LABELS: dict[str, str] = {**_FOREIGN_FORUM_LABELS, **_APPROVED_LAW_LABEL_OVERRIDES}

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


# ===========================================================================
# GENERIC forum-jurisdiction detector (closes the "any forum outside the 4
# hardcoded foreign buckets is silently missed" gap).
#
# The bucket system above (5 approved laws + 4 foreign-forum constants) is a
# PRECISION layer: it recognises specific named jurisdictions with hand-tuned
# regexes. But a forum it has never heard of -- "courts of California", "courts
# of Texas", "courts of Hong Kong", "arbitration seated in Zurich" -- produces NO
# bucket hit, so a genuine law<->forum split ships as a clean PASS.
#
# This generic layer fills that gap WITHOUT new buckets:
#   1. _extract_forum_tokens   -- pull the raw <X> jurisdiction token out of any
#                                 "courts of/in/at <X>", "<X> courts",
#                                 "arbitration seated in <X>", "<X> arbitration"
#                                 forum phrase.
#   2. _token_family           -- normalise a token (or an approved-law id) to a
#                                 coarse jurisdiction FAMILY (country / legal
#                                 system). California/Texas/Delaware/New York ->
#                                 "us"; London/England -> "england_and_wales";
#                                 Paris -> "france"; Zurich -> "switzerland"; ...
#   3. detect_mismatch compares the law's family to each forum token's family and
#      FLAGS when they differ -- additive, elevate-to-REVIEW only, same contract.
#
# Precision rules baked in:
#   * A forum whose family MATCHES the law's family never flags (England law +
#     London courts, India law + Bengaluru courts, Delaware law + New York courts
#     are all "us"/"england_and_wales"/"india" within-family, so silent).
#   * A forum that is a SUB-REGION of the law's jurisdiction never flags (the
#     family map collapses sub-regions onto the parent family, so "courts of
#     California" under US law is within-family).
#   * Unknown tokens (a forum we cannot map to any family) stay SILENT -- never a
#     guess-flag. Detection still requires a resolvable law AND a recognisable
#     foreign forum family.
# ===========================================================================

# Coarse jurisdiction families. Each approved-law bucket id and each recognisable
# forum region/city is mapped to a FAMILY key (a country / unified legal system).
# A law and a forum that share a family are the SAME jurisdiction for the purposes
# of this check (so US-law + Delaware-courts, or England-law + London-courts, are
# aligned and silent). Reuse the existing India smart-rule + bucket vocabulary
# where possible; this map only adds the COARSE grouping the buckets don't carry.
#
# Keys are lowercased, whitespace-collapsed jurisdiction tokens; values are family
# ids. Approved-law bucket ids map to their own family so the law side normalises
# through the same table.
_FORUM_FAMILY_ALIASES: dict[str, str] = {
    # --- approved-law bucket ids (law side normalises through here too) ----------
    "england_and_wales": "england_and_wales",
    "india": "india",
    "delaware": "us",
    "difc": "difc",
    "ontario_canada": "canada",
    "new_york": "us",
    "cayman_islands": "cayman_islands",
    "singapore": "singapore",
    "onshore_dubai": "onshore_uae",
    # --- England & Wales ---------------------------------------------------------
    "england": "england_and_wales",
    "english": "england_and_wales",
    "england and wales": "england_and_wales",
    "london": "england_and_wales",
    "wales": "england_and_wales",
    "united kingdom": "england_and_wales",
    "uk": "england_and_wales",
    "great britain": "england_and_wales",
    # --- United States (a unified family: any US state/city is "us") -------------
    "united states": "us",
    "united states of america": "us",
    "usa": "us",
    "u.s.": "us",
    "u.s.a.": "us",
    "america": "us",
    "new york": "us",
    "california": "us",
    "texas": "us",
    "florida": "us",
    "illinois": "us",
    "washington": "us",
    "san francisco": "us",
    "los angeles": "us",
    "new york city": "us",
    "manhattan": "us",
    "boston": "us",
    "chicago": "us",
    "seattle": "us",
    "state of delaware": "us",
    "state of new york": "us",
    "state of california": "us",
    "state of texas": "us",
    # --- Canada ------------------------------------------------------------------
    "canada": "canada",
    "canadian": "canada",
    "ontario": "canada",
    "province of ontario": "canada",
    "toronto": "canada",
    "british columbia": "canada",
    "vancouver": "canada",
    "quebec": "canada",
    "montreal": "canada",
    # --- France ------------------------------------------------------------------
    "france": "france",
    "french": "france",
    "paris": "france",
    # --- Switzerland -------------------------------------------------------------
    "switzerland": "switzerland",
    "swiss": "switzerland",
    "zurich": "switzerland",
    "geneva": "switzerland",
    "zug": "switzerland",
    # --- Hong Kong ---------------------------------------------------------------
    "hong kong": "hong_kong",
    "hksar": "hong_kong",
    # --- Singapore (the bucket id "singapore" is mapped above) -------------------
    "siac": "singapore",
    # --- Germany -----------------------------------------------------------------
    "germany": "germany",
    "german": "germany",
    "frankfurt": "germany",
    "munich": "germany",
    "berlin": "germany",
    # --- Netherlands -------------------------------------------------------------
    "netherlands": "netherlands",
    "the netherlands": "netherlands",
    "dutch": "netherlands",
    "amsterdam": "netherlands",
    # --- Cayman ------------------------------------------------------------------
    "cayman islands": "cayman_islands",
    "cayman": "cayman_islands",
    # --- UAE / Dubai (onshore) ---------------------------------------------------
    "dubai": "onshore_uae",
    "abu dhabi": "onshore_uae",
    "united arab emirates": "onshore_uae",
    "uae": "onshore_uae",
    # --- Ireland -----------------------------------------------------------------
    "ireland": "ireland",
    "irish": "ireland",
    "dublin": "ireland",
}

# Human-readable family labels for the generic finding text (a generic token that
# does not resolve to a labelled bucket falls back to its raw extracted token).
_FAMILY_LABELS: dict[str, str] = {
    "us": "the United States",
    "england_and_wales": "England and Wales",
    "india": "India",
    "canada": "Canada",
    "france": "France",
    "switzerland": "Switzerland",
    "hong_kong": "Hong Kong",
    "singapore": "Singapore",
    "germany": "Germany",
    "netherlands": "the Netherlands",
    "cayman_islands": "the Cayman Islands",
    "onshore_uae": "onshore Dubai / UAE",
    "difc": "the DIFC",
    "ireland": "Ireland",
}

# India smart-rule sources (states + metros) all collapse to the India family, so
# "courts of Karnataka" / "courts of Bengaluru" under India law stay silent.
for _name in re.split(r"\|", _INDIA_STATE):
    _FORUM_FAMILY_ALIASES.setdefault(_name.replace(r"\s+", " ").strip(), "india")
for _name in re.split(r"\|", _INDIA_METRO):
    _FORUM_FAMILY_ALIASES.setdefault(_name.replace(r"\s+", " ").strip(), "india")
_FORUM_FAMILY_ALIASES.setdefault("india", "india")
_FORUM_FAMILY_ALIASES.setdefault("indian", "india")

# Forum-phrase patterns that capture the raw jurisdiction token <X>. Mirror the
# vocabulary used by _FORUM_SENTENCE / the bucket forum regexes. The capture group
# greedily takes the jurisdiction noun-phrase up to a clause boundary, and a
# trailing normaliser trims connective tails ("for any dispute", "located in", a
# trailing "court(s)").
_FORUM_TOKEN_PATTERNS = [
    re.compile(r"courts?\s+(?:located\s+)?(?:of|in|at)\s+(?:the\s+)?([a-z][\w .,'&-]*)", re.IGNORECASE),
    re.compile(r"(?:arbitration|seat(?:ed)?)\s+(?:of\s+arbitration\s+)?(?:in|at|of)\s+(?:the\s+)?([a-z][\w .,'&-]*)", re.IGNORECASE),
    re.compile(r"([a-z][\w .'&-]*?)\s+courts?\b", re.IGNORECASE),
    re.compile(r"([a-z][\w .'&-]*?)\s+arbitration\b", re.IGNORECASE),
]

# Connective words that precede the real jurisdiction in a "<X> courts" / forum
# phrase and must be stripped so the captured token is the jurisdiction itself, not
# "exclusive jurisdiction of the" etc. Also the role nouns the bucket vocabulary
# uses as context.
_FORUM_TOKEN_STOPWORDS = {
    "the", "a", "an", "of", "in", "at", "to", "and", "or", "for", "any", "all",
    "exclusive", "non-exclusive", "nonexclusive", "competent", "courts", "court",
    "jurisdiction", "venue", "forum", "seat", "seated", "located", "state",
    "province", "emirate", "city", "republic", "federal", "national", "local",
    "dispute", "disputes", "parties", "party", "submit", "submits", "irrevocably",
    "arbitration", "arbitral", "tribunal", "this", "agreement", "before", "shall",
    "by", "be", "resolved", "finally", "subject", "country",
}


def _clean_forum_token(raw: str) -> str:
    """Normalise a captured forum token to a lowercased, trimmed jurisdiction name.

    Splits on the first clause/connective boundary, drops leading/trailing stop
    words and role nouns, collapses whitespace. Returns "" when nothing meaningful
    remains. The result is looked up in ``_FORUM_FAMILY_ALIASES``.
    """
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    # Cut at the first clause-tail connective ("... for any dispute", "... in
    # respect of", trailing punctuation runs) so only the jurisdiction noun-phrase
    # survives.
    token = re.split(
        r"\b(?:for|in\s+respect|in\s+connection|arising|with\s+respect|to\s+the|"
        r"shall|over|relating|regarding|concerning|pursuant)\b",
        token,
        maxsplit=1,
    )[0]
    token = token.strip(" .,;:-'\"()")
    # Drop leading/trailing stop words ("exclusive jurisdiction of the State of
    # California" -> "state of california"); the alias table keeps "state of X"
    # forms directly, and _token_family also windows over the significant words.
    words = [w for w in re.split(r"\s+", token) if w]
    while words and words[0] in _FORUM_TOKEN_STOPWORDS:
        words.pop(0)
    while words and words[-1] in _FORUM_TOKEN_STOPWORDS:
        words.pop()
    return " ".join(words).strip()


def _token_family(token: str) -> str:
    """Map a cleaned jurisdiction token (or approved-law id) to a coarse family.

    Returns the family id from ``_FORUM_FAMILY_ALIASES`` (exact match first, then a
    progressive multi-word window so "state of california" and "california, usa"
    resolve), or "" when the token is unknown -- an unknown forum is never flagged.
    """
    tok = str(token or "").strip().lower()
    if not tok:
        return ""
    if tok in _FORUM_FAMILY_ALIASES:
        return _FORUM_FAMILY_ALIASES[tok]
    # Try progressively shorter contiguous windows over the significant words so
    # "<city>, <country>" or "state of <X>" still resolve to a family.
    parts = [p for p in re.split(r"[\s,]+", tok) if p and p not in _FORUM_TOKEN_STOPWORDS]
    n = len(parts)
    for size in range(n, 0, -1):
        for start in range(0, n - size + 1):
            phrase = " ".join(parts[start : start + size])
            if phrase in _FORUM_FAMILY_ALIASES:
                return _FORUM_FAMILY_ALIASES[phrase]
    return ""


def extract_forum_families(text: str) -> set[str]:
    """All coarse jurisdiction families named in the document's FORUM sentence(s).

    Generic counterpart to ``extract_forum_jurisdictions``: works for ANY forum the
    family map recognises, not only the hand-tuned buckets. Only forum-role
    sentences are inspected; unknown tokens are dropped (never a guess-flag).
    """
    families: set[str] = set()
    for sentence in _sentences(text):
        if not _FORUM_SENTENCE.search(sentence):
            continue
        for pattern in _FORUM_TOKEN_PATTERNS:
            for match in pattern.finditer(sentence):
                family = _token_family(_clean_forum_token(match.group(1)))
                if family:
                    families.add(family)
    return families


def _law_family(law_option_id: str) -> str:
    """The coarse jurisdiction family of an approved governing-law option id.

    Falls back to the option id itself as a singleton family when no explicit
    alias exists, so a foreign forum still differs from a brand-new approved law.
    """
    fam = _FORUM_FAMILY_ALIASES.get(str(law_option_id or "").strip().lower(), "")
    if fam:
        return fam
    return str(law_option_id or "").strip().lower()


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
    # Last resort: a bare label match against the human labels of every bucket
    # (foreign-forum constants + each approved-law bucket's Playbook label), so a
    # free expected-forum string like "England and Wales" still resolves to its
    # bucket even when no law/forum regex above happened to match it.
    for jur in JURISDICTIONS:
        label = _label(jur).strip().lower()
        if label and (label in token or token in label):
            return jur
    return ""


def _label(bucket: str) -> str:
    """Human-readable label for a jurisdiction bucket.

    Foreign-forum buckets + the approved-law display overrides use the constant
    label map; every other approved-law bucket falls back to its PLAYBOOK label
    (via ``governing_law_view.governing_law_label``) so a newly-added approved law
    is labelled from the Playbook automatically, never a bare title-cased id.
    """
    if not bucket:
        return ""
    override = JURISDICTION_LABELS.get(bucket)
    if override:
        return override
    if bucket not in _FOREIGN_FORUM_BUCKETS:
        try:
            from . import governing_law_view  # noqa: PLC0415 -- avoid load-time cycle.

            label = governing_law_view.governing_law_label(bucket)
            if label:
                return label
        except Exception:  # noqa: BLE001 -- label is cosmetic; fall back below.
            pass
    return bucket.replace("_", " ").title()


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

    # ---- PRECISION layer: the hand-tuned bucket vocabulary (4 foreign forums + 5
    # approved laws). When the document names a recognised foreign BUCKET, prefer
    # it -- the bucket label reads better in the finding ("Cayman Islands").
    document_forums = extract_forum_jurisdictions(text)
    foreign = {bucket for bucket in document_forums if bucket and bucket != expected}
    if foreign:
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

    # ---- GENERIC layer: catch any forum OUTSIDE the hardcoded buckets (the bug).
    # Compare the law's coarse jurisdiction FAMILY against every forum family the
    # document names. A forum family that differs from the law family is a foreign
    # forum the buckets were blind to (California / Texas / Hong Kong / Paris /
    # Zurich / ...). A forum whose family MATCHES the law (England law + London
    # courts) or is a SUB-REGION (US law + Delaware courts -> both "us") collapses
    # to the same family and never flags. Unknown forum tokens are dropped upstream
    # (never a guess-flag).
    law_fam = _law_family(law_option_id)
    if not law_fam:
        return None
    forum_families = extract_forum_families(text)
    if not forum_families:
        # No recognizable forum at all -> nothing to compare -> stay silent.
        return None
    foreign_families = sorted(f for f in forum_families if f and f != law_fam)
    if not foreign_families:
        return None

    expected_label = _label(expected)
    foreign_labels = ", ".join(_FAMILY_LABELS.get(f, f.replace("_", " ")) for f in foreign_families)
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
        "document_forum": foreign_families[0],
        "document_forums": foreign_families,
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
