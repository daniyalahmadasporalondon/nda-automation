"""Deterministic email-subject -> counterparty-name normalizer.

Inbound NDA matters carry no real counterparty field, so the registry falls back
to the raw email *subject*. That subject becomes a Drive folder name + the
Repository counterparty column + corpus/dashboard grouping — which is why folders
end up literally named ``"Fwd: Air India <> Aspora"``. This module turns such a
subject into the best deterministic counterparty name.

It is the FAIL-OPEN fallback used only when no AI extraction exists.
:func:`normalize_counterparty` is pure, deterministic, idempotent, network-free,
regex/string only, and never raises. The full vetted ruleset (an ordered pipeline
adversarially designed against the real Drive folder corpus) is implemented here
step-for-step; see the numbered comments below, which map 1:1 to the spec.

Key invariants:
  * collapse whitespace FIRST (a ReDoS backstop), then normalize unicode dashes;
  * strip stacked leading prefixes (Fwd/Re/FW/CALL/Invitation/...) and a trailing
    ``@ <weekday> <date>`` calendar tail;
  * ONLY split on a connector and drop a side when a side contains a *first-party*
    token (whole-word match). With NO first-party token present the de-prefixed
    subject passes through UNCHANGED — names like ``Acme_Fintech``,
    ``03-REVIEW-mutual-nda``, ``Air India - Mutual NDA Template (...)`` are never
    mangled;
  * return ``""`` only for empty/lone-prefix input (the caller maps ``""`` ->
    ``"Unknown Counterparty"``).
"""
from __future__ import annotations

import re

from . import entity_registry

# --- length cap ------------------------------------------------------------
# Real subjects are <300 chars; the cap is purely a ReDoS backstop (step 0).
_MAX_SUBJECT_LENGTH = 4096

# --- unicode dash normalization (step 2) -----------------------------------
# A pure char-map (NOT a regex) cannot backtrack. Maps every unicode dash to an
# ASCII '-' so the weak-hyphen connector + calendar-tail rules see one separator.
_DASH_CHARS = "‐‑‒–—―−"  # ‐ ‑ ‒ – — ― −
_DASH_TRANSLATION = {ord(ch): "-" for ch in _DASH_CHARS}

# --- leading prefixes (step 3) ---------------------------------------------
# Longest-alternative-first so 'updated invitation' is not eaten as 'invitation'.
# No leading \s* because whitespace is collapsed first (step 1). Applied in a
# fixpoint .sub(count=1)+lstrip loop to peel stacked/interleaved/repeated prefixes.
_PREFIX_WORDS = (
    "updated invitation",
    "invitation",
    "cancelled",
    "canceled",
    "declined",
    "accepted",
    "call",
    "fwd",
    "fw",
    "re",
)
_PREFIX_ALT = "|".join(_PREFIX_WORDS)
_PREFIX_RE = re.compile(rf"^(?:{_PREFIX_ALT})\s*:\s*", re.IGNORECASE)
# Lone-prefix guard (step 6): a string that is ONLY a prefix word + optional ':'.
_LONE_PREFIX_RE = re.compile(rf"^(?:{_PREFIX_ALT})\s*:?\s*$", re.IGNORECASE)

# --- trailing calendar tail (step 4) ---------------------------------------
# Anchored on '@ <weekday>' so it cannot eat a bare '@' inside a real name. Drops
# '@ Wed May 13, 2026 7:30pm - 8:00pm' BEFORE any split, so the tail's internal
# ' - ' is never mistaken for a connector.
_CAL_TAIL_RE = re.compile(
    r"\s*@\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b[\s\S]*$",
    re.IGNORECASE,
)

# --- edge-junk trim (step 5) -----------------------------------------------
_EDGE_JUNK_RE = re.compile(r"^[\s\-|/:•·,]+|[\s\-|/:•·,]+$")

# --- strong connectors (step 9) --------------------------------------------
# All matched SIMULTANEOUSLY via one combined regex (NOT rank-ordered): this is
# what makes 'Zeta | Aspora Connect: Mehul<>Neha' -> ['Zeta','Aspora
# Connect: Mehul','Neha'] resolve to 'Zeta'. '<>','<->','|','/' split bare; ' x '
# and ' vs '/' vs. ' require surrounding spaces so an in-word/leading 'x' never
# splits.
_STRONG_RE = re.compile(
    r"\s*<->\s*"
    r"|\s*<>\s*"
    r"|\s*\|\s*"
    r"|\s*/\s*"
    r"|(?<=\s)x(?=\s)"
    r"|(?<=\s)vs\.?(?=\s)",
    re.IGNORECASE,
)

# --- weak connectors (step 11) ---------------------------------------------
# Spaced hyphen ' - ' or spaced ' and ', split ONLY when an exact standalone
# first-party segment (is_fp_anchor) is present.
_WEAK_RE = re.compile(r"\s+-\s+|\s+and\s+", re.IGNORECASE)

# --- trailing-noise words (step 12) ----------------------------------------
# Agenda cruft trimmed off an EXTRACTED counterparty on the split path ONLY (never
# the fallback — running this on the no-FP fallback was the original bug that
# turned '03-REVIEW-mutual-nda' into '03-REVIEW-mutual'). Longest-first.
_TRAILING_NOISE_WORDS = (
    "mutual nda",
    "follow up",
    "follow-up",
    "followup",
    "kick off",
    "kickoff",
    "catch up",
    "catchup",
    "introduction",
    "intro",
    "connect",
    "sync",
    "call",
    "discussion",
    "meeting",
    "updates",
    "update",
    "notes",
    "note",
    "ndas",
    "nda",
    "mnda",
    "template",
)
# Sort longest-first so multi-word noise wins over its prefixes.
_TRAILING_NOISE_ALT = "|".join(
    re.escape(word)
    for word in sorted(_TRAILING_NOISE_WORDS, key=len, reverse=True)
)
_TRAILING_NOISE_RE = re.compile(
    rf"(?:[\s:\-|/]+(?:{_TRAILING_NOISE_ALT}))+\s*$",
    re.IGNORECASE,
)
# A part whose ENTIRE content is a single noise word is pure noise -> dropped.
_NOISE_EXACT = frozenset(word.casefold() for word in _TRAILING_NOISE_WORDS)


def _default_first_party_tokens() -> tuple[str, ...]:
    """First-party tokens derived from the entity registry short_names.

    'Aspora' is the pinned primary anchor; the secondary tokens come from the
    registry so there is a single source of truth. We NEVER add a bare
    'Vance'/'Nesse'/'Real' token — a real counterparty could legitimately carry
    those surnames. Sorted longest-first so a multi-word token ('Aspora Financial
    Services') is tested before its prefix ('Aspora').
    """
    tokens: list[str] = ["Aspora"]
    try:
        for entity in entity_registry.list_entities():
            short = str(entity.get("short_name") or "").strip()
            if short and short not in tokens:
                tokens.append(short)
    except Exception:
        # Fail-open: never let registry inspection break the normalizer.
        pass
    # Dedupe (preserve first occurrence) then sort longest-first.
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    unique.sort(key=len, reverse=True)
    return tuple(unique)


# Cached at import time: one source of truth, one compile of the per-token matchers.
_DEFAULT_FIRST_PARTY_TOKENS = _default_first_party_tokens()


def _compile_first_party_matchers(tokens: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    """Whole-word matchers for each token, longest-first.

    The alnum boundaries ``(?<![A-Za-z0-9])TOKEN(?![A-Za-z0-9])`` are what stop
    'Aspora' matching inside 'Asporable'/'MyAspora' while still matching 'Aspora
    Users'.
    """
    return tuple(
        re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        for token in tokens
    )


_DEFAULT_FIRST_PARTY_MATCHERS = _compile_first_party_matchers(_DEFAULT_FIRST_PARTY_TOKENS)


def _resolve_tokens(
    first_party_tokens: list[str] | None,
) -> tuple[tuple[str, ...], tuple[re.Pattern, ...]]:
    """Resolve the token list + matchers, using the cached default when None."""
    if first_party_tokens is None:
        return _DEFAULT_FIRST_PARTY_TOKENS, _DEFAULT_FIRST_PARTY_MATCHERS
    cleaned = [str(token).strip() for token in first_party_tokens if str(token).strip()]
    if not cleaned:
        return _DEFAULT_FIRST_PARTY_TOKENS, _DEFAULT_FIRST_PARTY_MATCHERS
    cleaned.sort(key=len, reverse=True)
    tokens = tuple(cleaned)
    return tokens, _compile_first_party_matchers(tokens)


def _clean_edges(value: str) -> str:
    """Strip a leading/trailing run of edge junk, then collapse internal whitespace."""
    stripped = _EDGE_JUNK_RE.sub("", value)
    return " ".join(stripped.split()).strip()


def _strip_prefixes(value: str) -> str:
    """Peel stacked/interleaved/repeated leading prefixes to a fixpoint (step 3)."""
    current = value
    while True:
        nxt = _PREFIX_RE.sub("", current, count=1).lstrip()
        if nxt == current:
            return current
        current = nxt


def _trailing_noise_trim(value: str) -> str:
    """Trim agenda cruft off an extracted counterparty, looped to a fixpoint (step 12)."""
    current = value
    while True:
        nxt = _TRAILING_NOISE_RE.sub("", current).strip()
        if nxt == current or not nxt:
            # Never empty a segment via trimming; the caller treats an emptied
            # segment as pure noise and drops it.
            return nxt if nxt else ""
        current = nxt


def _make_has_fp(matchers: tuple[re.Pattern, ...]):
    def has_fp(side: str) -> bool:
        return any(matcher.search(side) for matcher in matchers)

    return has_fp


def _make_is_fp_anchor(tokens: tuple[str, ...]):
    folded = frozenset(token.casefold() for token in tokens)

    def is_fp_anchor(seg: str) -> bool:
        return seg.strip().casefold() in folded

    return is_fp_anchor


def normalize_counterparty(
    subject: str,
    first_party_tokens: list[str] | None = None,
) -> str:
    """Best deterministic counterparty name for an email ``subject``.

    Pure, deterministic, idempotent, never raises. Returns a non-empty cleaned
    name, or ``""`` for empty/lone-prefix input (the caller maps ``""`` ->
    ``"Unknown Counterparty"``). ``first_party_tokens`` defaults to the
    registry-derived set with 'Aspora' pinned.
    """
    # --- step 0: GUARD + CAP --------------------------------------------------
    raw = str(subject if subject is not None else "")
    tokens, matchers = _resolve_tokens(first_party_tokens)
    has_fp = _make_has_fp(matchers)
    is_fp_anchor = _make_is_fp_anchor(tokens)

    # --- step 1: COLLAPSE WHITESPACE FIRST (ReDoS guard, precedence-critical) --
    # ``str.split()`` is a linear, non-backtracking C-level scan, so it tames even
    # 200k leading spaces before any regex sees them. The 4096 cap is applied to
    # the COLLAPSED string (a pure ReDoS backstop): capping the raw bytes first
    # would let a run of leading spaces fill the cap and truncate the real subject
    # away (the spec's own '(200000 leading spaces) + Aspora <> Z' -> 'Z' example
    # only holds if collapse precedes the cap). Real subjects are <300 chars.
    s = " ".join(raw.split()).strip()[:_MAX_SUBJECT_LENGTH]

    # --- step 2: NORMALIZE UNICODE DASHES (char-map, cannot backtrack) --------
    s = s.translate(_DASH_TRANSLATION)

    # --- step 3: STRIP STACKED LEADING PREFIXES (fixpoint) --------------------
    s = _strip_prefixes(s)
    s = " ".join(s.split()).strip()

    # --- step 4: STRIP TRAILING CALENDAR TAIL ---------------------------------
    s = _CAL_TAIL_RE.sub("", s)
    s = " ".join(s.split()).strip()

    # --- step 5: SNAPSHOT the guaranteed-non-empty safe fallback --------------
    deprefixed = _clean_edges(s)

    # --- step 6: EMPTY / LONE-PREFIX GUARD ------------------------------------
    if not deprefixed or _LONE_PREFIX_RE.match(deprefixed):
        return ""

    # --- step 8: NO-ANCHOR PASS-THROUGH (anti-mangling firewall) --------------
    # (step 7 = the has_fp / is_fp_anchor predicates, defined above.)
    if not has_fp(s):
        return deprefixed

    # --- step 9 + 10: STRONG-CONNECTOR SPLIT + SELECTION ----------------------
    strong_parts = [part.strip() for part in _STRONG_RE.split(s)]
    strong_parts = [part for part in strong_parts if part]
    if len(strong_parts) >= 2:
        fp_flags = [has_fp(part) for part in strong_parts]
        if any(fp_flags) and not all(fp_flags):
            survivors = []
            for part in strong_parts:
                if has_fp(part):
                    continue
                if part.casefold() in _NOISE_EXACT:
                    continue
                trimmed = _trailing_noise_trim(part)
                if trimmed:
                    survivors.append(trimmed)
            if survivors:
                return survivors[0]

    # --- step 11: WEAK-CONNECTOR SPLIT (only if step 10 produced nothing) -----
    weak_parts = [part.strip() for part in _WEAK_RE.split(s)]
    weak_parts = [part for part in weak_parts if part]
    anchor_indices = [i for i, part in enumerate(weak_parts) if is_fp_anchor(part)]
    if anchor_indices:
        survivors: list[str] = []
        for i, part in enumerate(weak_parts):
            if is_fp_anchor(part):
                continue
            # Only take a segment ADJACENT to an exact FP anchor.
            if not any(abs(i - a) == 1 for a in anchor_indices):
                continue
            if has_fp(part):
                continue
            if part.casefold() in _NOISE_EXACT:
                continue
            trimmed = _trailing_noise_trim(part)
            if trimmed and not has_fp(trimmed):
                survivors.append(trimmed)
        if survivors:
            return survivors[0]

    # --- step 13: FP-PRESENT-BUT-UNRESOLVED FALLBACK --------------------------
    return deprefixed
