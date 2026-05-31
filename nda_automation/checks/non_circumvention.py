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
def _check_non_circumvention(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    prohibited_patterns = _clause_term_patterns(clause, "search_terms")
    prohibited_language = [pattern for pattern in prohibited_patterns if re.search(pattern, normalized)]

    if not prohibited_language:
        return _not_present(clause, "No prohibited non-circumvention language detected.", [])
    return _check(
        clause,
        "Prohibited non-circumvention or substitute-purpose language found.",
        _paragraph_matches(paragraphs, prohibited_patterns),
        what_to_fix="Remove non-circumvention, introduced-party non-solicit, substitute-purpose, or exclusivity language.",
    )

