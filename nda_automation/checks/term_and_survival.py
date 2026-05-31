from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    YEAR_TERM_EVIDENCE_PATTERN,
    _check,
    _clause_term_patterns,
    _extract_year_terms,
    _match,
    _max_term_years,
    _normalize,
    _not_present,
    _paragraph_matches,
    _term_context_patterns,
    _year_count_label,
)
def _check_term_and_survival(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    max_years = _max_term_years(clause)
    cap_label = _year_count_label(max_years)
    term_context_patterns = _term_context_patterns(clause)
    indefinite_patterns = _clause_term_patterns(clause, "indefinite_terms")
    term_paragraphs = _paragraph_matches(paragraphs, term_context_patterns)
    term_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in term_paragraphs))
    year_terms = _extract_year_terms(term_normalized)
    has_term_within_cap = any(1 <= years <= max_years for years in year_terms)
    has_term_over_cap = any(years > max_years for years in year_terms)
    ordinary_indefinite_term = any(re.search(pattern, term_normalized) for pattern in indefinite_patterns)

    if has_term_over_cap:
        return _check(
            clause,
            f"A term or survival period exceeds the cap of {cap_label}.",
            _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN]),
            what_to_fix=(
                "Reduce the ordinary confidentiality term or survival period "
                f"to a fixed period of {cap_label} or less."
            ),
        )
    if ordinary_indefinite_term:
        return _check(
            clause,
            f"Ordinary confidentiality appears indefinite rather than capped at {cap_label}.",
            _paragraph_matches(term_paragraphs, indefinite_patterns),
            what_to_fix=(
                "Replace indefinite ordinary confidentiality language "
                f"with a fixed period of {cap_label} or less."
            ),
        )
    if has_term_within_cap:
        return _match(
            clause,
            f"Term or survival period is within the cap of {cap_label}.",
            _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN]),
        )
    return _not_present(
        clause,
        f"No fixed term or survival period of up to {cap_label} was found.",
        term_paragraphs,
        what_to_fix=f"Add a fixed term or ordinary confidentiality survival period of {cap_label} or less.",
    )

