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


def _check_mutuality(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    search_patterns = _mutuality_search_patterns(clause)
    one_way_patterns = _clause_term_patterns(clause, "one_way_terms")
    mutual_paragraphs = [
        paragraph
        for paragraph in _paragraph_matches(paragraphs, search_patterns)
        if not _negates_mutuality(str(paragraph["text"]))
    ]
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


def _negates_mutuality(text: str) -> bool:
    return bool(re.search(NEGATED_MUTUALITY_PATTERN, text, flags=re.IGNORECASE))


def _mutuality_search_patterns(clause: Dict[str, object]) -> List[str]:
    return [
        MUTUALITY_VARIANT_PATTERNS.get(term, _literal_word_pattern(term))
        for term in _clause_terms(clause, "search_terms")
    ]
