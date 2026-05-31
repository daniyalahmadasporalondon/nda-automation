from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    ISSUE_TYPE_UNCLEAR,
    Paragraph,
    _check,
    _count_pattern_matches,
    _match,
    _not_present,
    _paragraph_matches,
    _signature_evidence_patterns,
    _signature_marker_patterns,
)


def _check_signatures(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    signature_patterns = _signature_evidence_patterns(clause)
    by_marker_patterns = _signature_marker_patterns(clause, "party")
    title_marker_patterns = _signature_marker_patterns(clause, "title")
    date_marker_patterns = _signature_marker_patterns(clause, "date")
    party_markers = len(re.findall(r"^\s*for\s+[a-z0-9&.,' -]{2,80}", text, flags=re.IGNORECASE | re.MULTILINE))
    party_markers += _count_pattern_matches(by_marker_patterns, normalized)
    title_markers = _count_pattern_matches(title_marker_patterns, normalized)
    date_markers = _count_pattern_matches(date_marker_patterns, normalized) + len(
        re.findall(r"\b\d{1,2}\s+[a-z]{3,9}\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", normalized)
    )

    if party_markers >= 2 and title_markers >= 2 and date_markers >= 1:
        return _match(clause, "Execution block appears to include both parties, titles, and a date.", _paragraph_matches(paragraphs, signature_patterns))
    partial_matches = _paragraph_matches(paragraphs, signature_patterns)
    if partial_matches:
        return _check(
            clause,
            "The execution block is missing both-party signatures, titles, or a date.",
            partial_matches,
            issue_type=ISSUE_TYPE_UNCLEAR,
            what_to_fix="Complete both execution blocks with party name, signatory, title, and date.",
        )
    return _not_present(
        clause,
        "No execution block was found.",
        [],
        what_to_fix="Add execution blocks for both parties with legal entity name, authorised signatory, title, and date.",
    )
