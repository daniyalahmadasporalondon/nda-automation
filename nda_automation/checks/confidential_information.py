from __future__ import annotations

import re
from typing import Dict, Iterable, List

from .common import (
    ClauseResult,
    INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW,
    Paragraph,
    _check,
    _clause_term_patterns,
    _clause_terms,
    _confidential_categories_label,
    _dedupe_terms,
    _literal_word_pattern,
    _match,
    _normalize,
    _not_present,
    _paragraph_matches,
)
def _check_confidential_information(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    definition_name_terms, definition_coverage_terms = _confidential_definition_search_terms(clause)
    categories = _clause_terms(clause, "definition_categories")
    category_label = _confidential_categories_label(categories)
    definition_name_patterns = [_literal_word_pattern(term) for term in definition_name_terms]
    definition_paragraphs = _paragraph_matches(paragraphs, definition_name_patterns)
    definition_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in definition_paragraphs))
    coverage_terms = _dedupe_terms(definition_coverage_terms + categories)
    coverage_hits = [term for term in coverage_terms if term in definition_normalized]
    broad_definition = bool(definition_paragraphs) and len(coverage_hits) >= 4
    exclusion_paragraphs = _paragraph_matches(paragraphs, _clause_term_patterns(clause, "exclusion_context_terms"))
    problematic_exclusion_paragraphs = _problematic_confidential_exclusion_paragraphs(
        exclusion_paragraphs,
        _clause_terms(clause, "problematic_exclusion_terms"),
        _clause_terms(clause, "independent_development_terms"),
        _clause_terms(clause, "independent_development_qualification_terms"),
    )

    if broad_definition and not problematic_exclusion_paragraphs:
        return _match(
            clause,
            "Broad confidential information definition found with no extra exclusions detected.",
            definition_paragraphs,
        )

    if not broad_definition:
        if not definition_paragraphs:
            return _not_present(
                clause,
                "No Confidential Information definition was found.",
                [],
                what_to_fix=(
                    "Add a broad Confidential Information definition "
                    f"covering non-public {category_label or 'required'} information."
                ),
            )
        return _check(
            clause,
            "The definition of Confidential Information is missing or too narrow.",
            definition_paragraphs,
            what_to_fix=(
                "Broaden the Confidential Information definition "
                f"to cover the required {category_label or 'playbook'} categories."
            ),
        )
    else:
        return _check(
            clause,
            "The exclusions appear broader than the allowed standard carve-outs.",
            problematic_exclusion_paragraphs,
            what_to_fix=(
                "Remove residual knowledge, reverse-engineering, or unqualified independent-development exclusions "
                "from Confidential Information."
            ),
        )

def _confidential_definition_search_terms(clause: Dict[str, object]) -> tuple[List[str], List[str]]:
    """Split confidential-information search terms by their playbook contract.

    For the `confidential_information` clause, `search_terms[0]` is the
    definition paragraph anchor. The remaining `search_terms[1:]` entries are
    coverage signals checked inside the anchored definition paragraphs.
    """
    search_terms = _clause_terms(clause, "search_terms")
    definition_aliases = ["proprietary information"]
    definition_terms = _dedupe_terms(search_terms[:1] + definition_aliases)
    return definition_terms, search_terms[1:]

def _problematic_confidential_exclusion_paragraphs(
    exclusion_paragraphs: Iterable[Paragraph],
    problematic_terms: Iterable[str],
    independent_development_terms: Iterable[str],
    independent_development_qualification_terms: Iterable[str],
) -> List[Paragraph]:
    problematic_patterns = [_literal_word_pattern(term) for term in problematic_terms]
    independent_development_patterns = [_literal_word_pattern(term) for term in independent_development_terms]
    qualification_patterns = [_literal_word_pattern(term) for term in independent_development_qualification_terms]
    matches: List[Paragraph] = []

    for paragraph in exclusion_paragraphs:
        paragraph_text = str(paragraph["text"])
        paragraph_normalized = _normalize(paragraph_text)
        has_problematic_term = any(re.search(pattern, paragraph_normalized) for pattern in problematic_patterns)
        has_unqualified_independent_development = _has_unqualified_independent_development(
            paragraph_normalized,
            independent_development_patterns,
            qualification_patterns,
        )

        if not has_problematic_term and not has_unqualified_independent_development:
            continue

        matches.append(paragraph)

    return matches

def _has_unqualified_independent_development(
    normalized_text: str,
    independent_development_patterns: Iterable[str],
    qualification_patterns: Iterable[str],
) -> bool:
    qualification_patterns = list(qualification_patterns)
    for pattern in independent_development_patterns:
        for match in re.finditer(pattern, normalized_text):
            window_start = max(0, match.start() - INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
            window_end = min(len(normalized_text), match.end() + INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
            context = normalized_text[window_start:window_end]
            if not any(re.search(qualification_pattern, context) for qualification_pattern in qualification_patterns):
                return True
    return False
