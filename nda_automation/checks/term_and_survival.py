from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    YEAR_TERM_EVIDENCE_PATTERN,
    YEAR_TERM_PATTERN,
    YEAR_WORDS,
    _check,
    _clause_term_patterns,
    _match,
    _max_term_years,
    _normalize,
    _not_present,
    _paragraph_matches,
    _term_context_patterns,
    _year_count_label,
)

CARVE_OUT_MARKER_PATTERN = r"\b(?:except|excluding|other\s+than|save\s+for|provided\s+that)\b"


def _check_term_and_survival(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    max_years = _max_term_years(clause)
    cap_label = _year_count_label(max_years)
    term_context_patterns = _term_context_patterns(clause)
    indefinite_patterns = _clause_term_patterns(clause, "indefinite_terms")
    term_paragraphs = _paragraph_matches(paragraphs, term_context_patterns)
    term_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in term_paragraphs))
    year_terms = _extract_year_terms_with_context(term_normalized)
    has_term_within_cap = any(0 < term["years"] <= max_years for term in year_terms)
    ordinary_over_cap_terms = [
        term
        for term in year_terms
        if term["years"] > max_years and not _is_allowed_carve_out_year(term_normalized, term, clause)
    ]
    has_term_over_cap = bool(ordinary_over_cap_terms)
    ordinary_indefinite_matches = [
        match
        for pattern in indefinite_patterns
        for match in re.finditer(pattern, term_normalized)
        if not _is_allowed_carve_out_fragment(term_normalized, match.start(), match.end(), clause)
    ]

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
    if ordinary_indefinite_matches:
        return _check(
            clause,
            f"Survival language appears indefinite or perpetual rather than capped at {cap_label}.",
            _paragraph_matches(term_paragraphs, indefinite_patterns),
            what_to_fix=(
                "Replace indefinite or perpetual ordinary confidentiality language "
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


def _extract_year_terms_with_context(normalized: str) -> List[Dict[str, int]]:
    terms: List[Dict[str, int]] = []
    for match in re.finditer(YEAR_TERM_PATTERN, normalized):
        word_value, digit_value, parenthetical_value, unit = match.groups()
        if parenthetical_value:
            value = int(parenthetical_value)
        elif digit_value:
            value = int(digit_value)
        elif word_value:
            value = YEAR_WORDS[word_value]
        else:
            continue
        years = value / 12 if unit.startswith("month") else value
        terms.append({"years": years, "start": match.start(), "end": match.end()})
    return terms


def _is_allowed_carve_out_year(normalized: str, term: Dict[str, int], clause: Dict[str, object]) -> bool:
    return _is_allowed_carve_out_fragment(normalized, term["start"], term["end"], clause)


def _is_allowed_carve_out_fragment(normalized: str, start: int, end: int, clause: Dict[str, object]) -> bool:
    fragment = _term_fragment(normalized, start, end)
    carve_out_patterns = _carve_out_context_patterns(clause)
    if not any(re.search(pattern, fragment) for pattern in carve_out_patterns):
        return False
    return bool(
        re.search(CARVE_OUT_MARKER_PATTERN, fragment)
        or any(re.match(pattern, fragment.lstrip()) for pattern in carve_out_patterns)
        or any(_term_scoped_to_carve_out(fragment, pattern) for pattern in carve_out_patterns)
    )


def _term_scoped_to_carve_out(fragment: str, carve_out_pattern: str) -> bool:
    return bool(
        re.search(
            rf"\b(?:for|as\s+to|with\s+respect\s+to|in\s+respect\s+of|solely\s+for|limited\s+to)\s+(?:the\s+)?{carve_out_pattern}\b",
            fragment,
        )
    )


def _carve_out_context_patterns(clause: Dict[str, object]) -> List[str]:
    configured_terms = clause.get("longer_survival_carve_out_terms")
    if isinstance(configured_terms, list):
        terms = configured_terms
    else:
        terms = [
            "trade secret",
            "trade secrets",
            "legal obligation",
            "legal obligations",
            "required by law",
            "applicable law",
        ]
    return [
        re.escape(str(term).lower().strip()).replace(r"\ ", r"\s+")
        for term in terms
        if str(term).strip()
    ]


def _term_fragment(normalized: str, start: int, end: int) -> str:
    left_candidates = [
        normalized.rfind(separator, 0, start)
        for separator in (".", ";", ",")
    ]
    right_candidates = [
        position
        for position in (normalized.find(separator, end) for separator in (".", ";", ","))
        if position != -1
    ]
    left = max(left_candidates) + 1
    right = min(right_candidates) if right_candidates else len(normalized)
    return normalized[left:right].strip()
