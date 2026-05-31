from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    _approved_laws,
    _check,
    _governing_anchor_patterns,
    _governing_law_change_fix,
    _governing_law_missing_fix,
    _literal_word_pattern,
    _match,
    _not_present,
    _paragraph_matches,
)
def _check_governing_law(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    governing_anchor_patterns = _governing_anchor_patterns(clause)
    approved_patterns = [_literal_word_pattern(law) for law in _approved_laws(clause)]
    governing_paragraphs = _paragraph_matches(paragraphs, governing_anchor_patterns)
    approved_governing_paragraphs = [
        paragraph
        for paragraph in governing_paragraphs
        if any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in approved_patterns)
    ]

    if approved_governing_paragraphs:
        return _match(clause, "Approved governing law found.", approved_governing_paragraphs)
    if governing_paragraphs:
        return _check(
            clause,
            "A governing law clause was found, but it does not use an approved law.",
            governing_paragraphs,
            what_to_fix=_governing_law_change_fix(clause),
        )
    return _not_present(
        clause,
        "No governing law clause was found.",
        [],
        what_to_fix=_governing_law_missing_fix(clause),
    )

