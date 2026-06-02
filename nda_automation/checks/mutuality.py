from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    _check,
    _clause_term_patterns,
    _clause_terms,
    _literal_word_pattern,
    _match,
    _not_present,
    _paragraph_matches,
)

MUTUALITY_VARIANT_PATTERNS = {
    "mutual": r"\bmutual(?:ity|ly)?\b",
    "reciprocal": r"\breciprocal(?:ly)?\b",
}
NEGATED_MUTUALITY_PATTERN = (
    r"\b(?:not|no|without)\s+(?:a\s+|any\s+)?(?:mutual(?:ly)?|reciprocal(?:ly)?|reciprocity)\b"
    r"|\bnon[-\s]?mutual\b"
)


def _check_mutuality(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    _review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    search_patterns = _mutuality_search_patterns(clause)
    one_way_patterns = _clause_term_patterns(clause, "one_way_terms")
    mutual_paragraphs = _mutuality_paragraphs(paragraphs, search_patterns)
    separated_role_paragraphs = _mutual_role_paragraphs(normalized, clause, paragraphs)
    one_way_paragraphs = _paragraph_matches(paragraphs, one_way_patterns)

    if (mutual_paragraphs or separated_role_paragraphs) and not one_way_paragraphs:
        return _match(
            clause,
            "Mutual obligation language found.",
            mutual_paragraphs + separated_role_paragraphs,
        )
    if one_way_paragraphs:
        return _check(
            clause,
            "One-way or unilateral confidentiality language needs review.",
            one_way_paragraphs,
            what_to_fix="Revise the NDA so both parties are bound as both Disclosing Party and Receiving Party.",
        )
    return _not_present(
        clause,
        "The text does not clearly create mutual confidentiality obligations.",
        [],
        what_to_fix="Add mutual confidentiality language that binds both parties symmetrically.",
    )

def _mutual_role_paragraphs(
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
) -> List[Paragraph]:
    role_terms = _clause_terms(clause, "role_terms")
    reciprocity_patterns = _clause_term_patterns(clause, "role_reciprocity_terms")
    if len(role_terms) < 2 or not reciprocity_patterns:
        return []
    if not all(re.search(_literal_word_pattern(term), normalized, flags=re.IGNORECASE) for term in role_terms):
        return []

    evidence: List[Paragraph] = []
    for role_term in role_terms:
        role_pattern = _literal_word_pattern(role_term)
        role_evidence = [
            paragraph
            for paragraph in paragraphs
            if re.search(role_pattern, str(paragraph["text"]), flags=re.IGNORECASE)
            and any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in reciprocity_patterns)
        ]
        if not role_evidence:
            return []
        evidence.extend(role_evidence)

    return evidence


def _mutuality_paragraphs(paragraphs: List[Paragraph], search_patterns: List[str]) -> List[Paragraph]:
    matches: List[Paragraph] = []
    seen = set()
    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        if not _has_unnegated_mutuality_signal(paragraph_text, search_patterns):
            continue
        dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        if dedup_key in seen:
            continue
        matches.append(paragraph)
        seen.add(dedup_key)
    return matches


def _has_unnegated_mutuality_signal(text: str, search_patterns: List[str]) -> bool:
    negated_spans = [
        match.span()
        for match in re.finditer(NEGATED_MUTUALITY_PATTERN, text, flags=re.IGNORECASE)
    ]
    for pattern in search_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if not _match_inside_any_span(match.span(), negated_spans):
                return True
    return False


def _match_inside_any_span(match_span: tuple[int, int], spans: List[tuple[int, int]]) -> bool:
    match_start, match_end = match_span
    return any(start <= match_start and match_end <= end for start, end in spans)


def _mutuality_search_patterns(clause: Dict[str, object]) -> List[str]:
    return [
        MUTUALITY_VARIANT_PATTERNS.get(term, _literal_word_pattern(term))
        for term in _clause_terms(clause, "search_terms")
    ]
