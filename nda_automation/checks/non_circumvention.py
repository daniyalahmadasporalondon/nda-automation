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

LAWFUL_CIRCUMVENTION_PATTERN = r"\bcircumvent(?:ing|ion)?\b.{0,50}\b(?:applicable\s+law|law|laws|legal|regulatory|regulation|statute|sanctions)\b"


def _check_non_circumvention(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    prohibited_patterns = _clause_term_patterns(clause, "search_terms")
    prohibited_paragraphs = [
        paragraph
        for paragraph in _paragraph_matches(paragraphs, prohibited_patterns)
        if not _is_lawful_circumvention_context(str(paragraph["text"]))
    ]
    prohibited_language = [
        pattern
        for pattern in prohibited_patterns
        if any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for paragraph in prohibited_paragraphs)
    ]

    if not prohibited_language:
        return _not_present(clause, "No prohibited non-circumvention language detected.", [])
    return _check(
        clause,
        "Prohibited non-circumvention or substitute-purpose language found.",
        prohibited_paragraphs,
        what_to_fix="Remove non-circumvention, introduced-party non-solicit, substitute-purpose, or exclusivity language.",
    )


def _is_lawful_circumvention_context(text: str) -> bool:
    return bool(re.search(LAWFUL_CIRCUMVENTION_PATTERN, text, flags=re.IGNORECASE))
