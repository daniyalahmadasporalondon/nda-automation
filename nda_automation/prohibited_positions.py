"""Canonical prohibited-legal-position patterns — sourced from the Playbook.

These are the positions an NDA must never assert: the Playbook bans them, the
generator must never introduce one, and an AI clause adapter must never smuggle
one in. The SAME set is consumed in several places so the in-process guard, the
pre-save ship gate, and gen-verify's independent gate all agree on what is
off-position (otherwise a family the guard misses leaks past it and is caught
only by the external gate — exactly the drift this module exists to prevent):

* ``nda_generation_ai.GuardedClauseAdapter`` — rejects a drifted adapter clause
  (defence in depth; falls back to the deterministic Playbook wording).
* ``nda_generation._assert_generated_nda_is_on_position`` — the hard pre-save
  gate on the ship path; a hit means refuse to save.
* gen-verify's harness — the independent adversarial gate (imports this set so
  its meaning-based scan and the generator's guard never drift apart).

SINGLE SOURCE OF TRUTH: the patterns live in ``playbook.json`` on the
``non_circumvention`` dynamic clause under ``prohibited_position_patterns``
(``[{"label", "pattern"}, ...]``) — the Playbook is the canonical home for every
rule/threshold. This module reads them from there at import time and falls back
to the literal set below only if the Playbook is unreadable or omits the field,
so the guard never silently disappears.

Each entry is ``(label, regex)`` matched case-insensitively against normalised
text. The regexes are meaning-based (they target the POSITION, not one phrasing),
so paraphrase by an AI adapter is still caught. They — and slightly broaden —
the families gen-verify red-teams; notably ``non_solicit`` is loosened to catch
the un-hyphenated / interposed-words forms ("agrees not to solicit", "shall not,
during the term, solicit").
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Pattern

# Literal fallback — kept verbatim equal to the Playbook's
# non_circumvention.prohibited_position_patterns so the guard degrades gracefully
# (rather than vanishing) if the Playbook file is missing or malformed. The
# Playbook is authoritative; this only backstops a read failure.
_FALLBACK_PROHIBITED_POSITION_PATTERN_SOURCES: tuple[tuple[str, str], ...] = (
    ("non_compete", r"non-?compete|shall not (?:directly or indirectly )?(?:compete|engage in any business that competes)|competing business"),
    # Loosened per the AI-first safety review: catch the un-hyphenated forms and
    # any modal/verb + "not" + "solicit" with words interposed, plus "solicit or
    # hire" and a bare "shall/agree to solicit".
    ("non_solicit", r"non-?solicit|(?:shall|will|may|agree|agrees|undertake|undertakes)\b[^.]{0,25}\bnot\b[^.]{0,25}\bsolicit|(?:shall|will|may|agrees?)\b[^.]{0,15}\bsolicit|refrain from soliciting|solicit or hire"),
    ("non_circumvention", r"non-?circumvent|shall not circumvent|circumvent or bypass|bypass the disclosing party|\bdeal\s+directly\b|introduced\s+part"),
    # Tightened: the bare ``\bexclusiv(?:e|ity)\b`` over-blocked benign business
    # adjectives ("exclusive distribution partnership"). Require a positional /
    # obligational context so a real exclusivity RESTRAINT still trips, an adjective
    # does not. Kept byte-equal to the Playbook's authored pattern.
    ("exclusivity", r"sole and exclusive|deal exclusively|exclusive right to|exclusive dealing|exclusivity (?:obligation|arrangement|provision)"),
    ("ip_assignment", r"hereby assigns?\b|assignment of (?:all )?intellectual property|all (?:right,? )?title and interest in"),
    ("perpetual_confidentiality", r"in perpetuity|perpetual(?:ly)?\b|indefinitely\b|never expire|forever\b|for an unlimited (?:time|period)"),
    ("penalty", r"liquidated damages|penalty of|penalt(?:y|ies)\b|punitive damages"),
    ("auto_renew_lock", r"automatically renew|evergreen|may not (?:be )?terminat"),
)

_PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "playbook.json"
_PROHIBITED_CLAUSE_ID = "non_circumvention"
_PROHIBITED_FIELD = "prohibited_position_patterns"


def _load_prohibited_position_sources() -> tuple[tuple[str, str], ...]:
    """Read the prohibited-position pattern sources from the Playbook.

    Reads the raw ``playbook.json`` directly (no ``checker`` import) to avoid an
    import cycle — the ``prohibited_position_patterns`` field is preserved verbatim
    by playbook normalization, so the raw file value is authoritative. Falls back
    to the literal set on any read/parse error or a missing/empty field, so the
    prohibited-position guard is never silently dropped.
    """
    try:
        with _PLAYBOOK_PATH.open("r", encoding="utf-8") as handle:
            playbook = json.load(handle)
        clauses = playbook.get("clauses", []) if isinstance(playbook, dict) else []
        clause = next(
            (c for c in clauses if isinstance(c, dict) and c.get("id") == _PROHIBITED_CLAUSE_ID),
            None,
        )
        entries = clause.get(_PROHIBITED_FIELD) if isinstance(clause, dict) else None
        if isinstance(entries, list):
            sources: list[tuple[str, str]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label", "")).strip()
                pattern = entry.get("pattern")
                if label and isinstance(pattern, str) and pattern:
                    # Validate the regex compiles before trusting it.
                    re.compile(pattern, re.IGNORECASE)
                    sources.append((label, pattern))
            if sources:
                return tuple(sources)
    except (OSError, ValueError, re.error):
        pass
    return _FALLBACK_PROHIBITED_POSITION_PATTERN_SOURCES


# Source regexes (strings) — the shareable, language-level definition, sourced
# from the Playbook. Kept as strings (not compiled) so any consumer can
# compose/recompile them as needed.
PROHIBITED_POSITION_PATTERN_SOURCES: tuple[tuple[str, str], ...] = _load_prohibited_position_sources()

# Compiled (label -> Pattern), case-insensitive, for direct use.
PROHIBITED_POSITION_PATTERNS: tuple[tuple[str, "Pattern[str]"], ...] = tuple(
    (label, re.compile(source, re.IGNORECASE)) for label, source in PROHIBITED_POSITION_PATTERN_SOURCES
)

# A single combined pattern (any family) for a cheap "is any prohibited position
# present?" check, e.g. the per-clause adapter guard.
ANY_PROHIBITED_POSITION: "Pattern[str]" = re.compile(
    "|".join(source for _label, source in PROHIBITED_POSITION_PATTERN_SOURCES),
    re.IGNORECASE,
)


def first_prohibited_position(text: str) -> str:
    """Return the label of the first prohibited position found in ``text``, or "".

    Used by the ship gate to name the offending family in its error. Callers that
    must exempt the narrow permitted-survival carve-out (perpetual_confidentiality)
    handle that separately — this is a pure pattern scan."""
    for label, pattern in PROHIBITED_POSITION_PATTERNS:
        if pattern.search(text):
            return label
    return ""
