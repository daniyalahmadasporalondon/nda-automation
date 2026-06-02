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

USAGE_RIGHT_ACTION_PATTERN = (
    r"(?:use|using|retain|retaining|disclose|disclosing|exploit|exploiting|"
    r"reverse\s+engineer(?:ing)?)"
)
USAGE_RIGHT_BEFORE_PATTERN = (
    rf"(?:\b(?:may|can)\s+{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\b(?:shall|will|is|are|be|remain(?:s)?)\s+(?:free|permitted|allowed|entitled)\s+to\s+"
    rf"{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\b(?:has|have)\s+(?:the\s+)?right\s+to\s+{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\bnothing\b[^.;]{{0,120}}\b(?:prohibits|prevents|restricts|limits)\b[^.;]{{0,80}}\bfrom\s+"
    rf"{USAGE_RIGHT_ACTION_PATTERN})(?:\s+\w+){{0,8}}\s*$"
)
REVERSE_ENGINEERING_RIGHT_BEFORE_PATTERN = (
    r"(?:\b(?:may|can)\s+"
    r"|\b(?:shall|will|is|are|be|remain(?:s)?)\s+(?:free|permitted|allowed|entitled)\s+to\s+"
    r"|\b(?:has|have)\s+(?:the\s+)?right\s+to\s+)$"
)
USAGE_RIGHT_AFTER_PATTERN = (
    r"^(?:\s+\w+){0,8}\s+\b(?:may|can|shall|will)\s+be\s+"
    r"(?:used|retained|disclosed|exploited|reverse\s+engineered)\b"
)
NEGATED_RIGHT_BEFORE_PATTERN = r"\b(?:must|shall|may|can|will)\s+not\b[^.;]{0,80}$|\b(?:not|never)\s+[^.;]{0,40}$"
def _check_confidential_information(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    _review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    definition_name_terms, definition_coverage_terms = _confidential_definition_search_terms(clause)
    categories = _clause_terms(clause, "definition_categories")
    category_label = _confidential_categories_label(categories)
    definition_name_patterns = [_literal_word_pattern(term) for term in definition_name_terms]
    definition_paragraphs = _paragraph_matches(paragraphs, definition_name_patterns)
    definition_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in definition_paragraphs))
    coverage_terms = _dedupe_terms(definition_coverage_terms + categories)
    coverage_hits = [term for term in coverage_terms if term in definition_normalized]
    broad_definition = bool(definition_paragraphs) and len(coverage_hits) >= 4
    problematic_exclusion_paragraphs = _problematic_confidential_exclusion_paragraphs(
        paragraphs,
        _clause_term_patterns(clause, "exclusion_context_terms"),
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
    paragraphs: Iterable[Paragraph],
    exclusion_context_patterns: Iterable[str],
    problematic_terms: Iterable[str],
    independent_development_terms: Iterable[str],
    independent_development_qualification_terms: Iterable[str],
) -> List[Paragraph]:
    exclusion_context_patterns = list(exclusion_context_patterns)
    problematic_patterns = [_literal_word_pattern(term) for term in problematic_terms]
    independent_development_patterns = [_literal_word_pattern(term) for term in independent_development_terms]
    qualification_patterns = [_literal_word_pattern(term) for term in independent_development_qualification_terms]
    matches: List[Paragraph] = []

    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        paragraph_normalized = _normalize(paragraph_text)
        has_exclusion_context = any(re.search(pattern, paragraph_normalized) for pattern in exclusion_context_patterns)
        has_usage_right_context = _has_problematic_usage_right(
            paragraph_normalized,
            [*problematic_patterns, *independent_development_patterns],
        )
        if not has_exclusion_context and not has_usage_right_context:
            continue

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

def _has_problematic_usage_right(normalized_text: str, problematic_patterns: Iterable[str]) -> bool:
    for pattern in problematic_patterns:
        for match in re.finditer(pattern, normalized_text):
            before = _current_clause_prefix(normalized_text, match.start())
            after = _current_clause_suffix(normalized_text, match.end())
            if re.search(NEGATED_RIGHT_BEFORE_PATTERN, before):
                continue
            if (
                re.search(USAGE_RIGHT_BEFORE_PATTERN, before)
                or (
                    _pattern_matches_reverse_engineering(pattern)
                    and re.search(REVERSE_ENGINEERING_RIGHT_BEFORE_PATTERN, before)
                )
                or re.search(USAGE_RIGHT_AFTER_PATTERN, after)
            ):
                return True
    return False

def _current_clause_prefix(text: str, end: int) -> str:
    left = max(text.rfind(separator, 0, end) for separator in (".", ";"))
    return text[left + 1:end]

def _current_clause_suffix(text: str, start: int) -> str:
    right_candidates = [
        position
        for position in (text.find(separator, start) for separator in (".", ";"))
        if position != -1
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return text[start:right]

def _pattern_matches_reverse_engineering(pattern: str) -> bool:
    return "reverse" in pattern and "engineer" in pattern

def _has_unqualified_independent_development(
    normalized_text: str,
    independent_development_patterns: Iterable[str],
    qualification_patterns: Iterable[str],
) -> bool:
    qualification_patterns = list(qualification_patterns)
    for pattern in independent_development_patterns:
        for match in re.finditer(pattern, normalized_text):
            context = _independent_development_qualification_context(normalized_text, match.end())
            if not any(re.search(qualification_pattern, context) for qualification_pattern in qualification_patterns):
                return True
    return False


def _independent_development_qualification_context(normalized_text: str, start: int) -> str:
    window_end = min(len(normalized_text), start + INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
    context = normalized_text[start:window_end]
    boundary = re.search(r"[.;]|,\s+(?:and|or)\b", context)
    if boundary:
        return context[:boundary.start()]
    return context
