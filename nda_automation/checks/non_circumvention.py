from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    _check,
    _clause_term_patterns,
    _not_present,
    _paragraph_matches,
)
from .context import attach_structure_context, merge_paragraphs, paragraphs_with_concepts

LEGAL_CIRCUMVENTION_OBJECT = (
    r"(?:(?:any|all|applicable|relevant|mandatory|its|their|the)\s+)*"
    r"(?:law|laws|legal\s+(?:requirements?|obligations?)|regulatory\s+(?:requirements?|obligations?)|"
    r"regulations?|statutes?|sanctions)"
)
LAWFUL_CIRCUMVENTION_PATTERN = (
    rf"\bcircumvent(?:ing)?\s+{LEGAL_CIRCUMVENTION_OBJECT}\b"
    rf"|\bcircumvention\s+of\s+{LEGAL_CIRCUMVENTION_OBJECT}\b"
)


def _check_non_circumvention(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    context_concepts = ["non_circumvention"]
    prohibited_patterns = _clause_term_patterns(clause, "search_terms")
    prohibited_paragraphs = [
        paragraph
        for paragraph in merge_paragraphs(
            _paragraph_matches(paragraphs, prohibited_patterns),
            paragraphs_with_concepts(paragraphs, review_context, context_concepts),
        )
        if _has_prohibited_non_circumvention(str(paragraph["text"]), prohibited_patterns)
    ]
    prohibited_language = [
        pattern
        for pattern in prohibited_patterns
        if any(
            re.search(pattern, _without_lawful_circumvention_context(str(paragraph["text"])), flags=re.IGNORECASE)
            for paragraph in prohibited_paragraphs
        )
    ]

    if not prohibited_language:
        return attach_structure_context(
            _not_present(clause, "No prohibited non-circumvention language detected.", []),
            review_context,
            context_concepts,
        )
    return attach_structure_context(_check(
        clause,
        "Prohibited non-circumvention or substitute-purpose language found.",
        prohibited_paragraphs,
        what_to_fix="Remove non-circumvention, introduced-party non-solicit, substitute-purpose, or exclusivity language.",
    ), review_context, context_concepts)


def _has_prohibited_non_circumvention(text: str, prohibited_patterns: List[str]) -> bool:
    searchable_text = _without_lawful_circumvention_context(text)
    return any(re.search(pattern, searchable_text, flags=re.IGNORECASE) for pattern in prohibited_patterns)


def _without_lawful_circumvention_context(text: str) -> str:
    return re.sub(LAWFUL_CIRCUMVENTION_PATTERN, " ", text, flags=re.IGNORECASE)
