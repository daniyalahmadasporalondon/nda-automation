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

SIGNATURE_FOR_LINE_PATTERN = (
    r"^\s*for\s+"
    r"(?!(?:a\s+period|the\s+avoidance|avoidance|purposes?|the\s+purposes?|"
    r"clarity|example|information|convenience|the\s+foregoing|any|each|either|both|all|this)\b)"
    r"[a-z0-9&.,' -]{2,80}\s*$"
)


def _check_signatures(
    text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    _review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    signature_patterns = _signature_evidence_patterns(clause)
    by_marker_patterns = _signature_marker_patterns(clause, "party")
    title_marker_patterns = _signature_marker_patterns(clause, "title")
    date_marker_patterns = _signature_marker_patterns(clause, "date")
    partial_matches = _signature_evidence_paragraphs(paragraphs, signature_patterns)
    signature_text = "\n".join(str(paragraph["text"]) for paragraph in partial_matches)
    signature_normalized = " ".join(signature_text.lower().split())
    party_markers = len(_signature_for_lines(signature_text))
    party_markers += _count_pattern_matches(by_marker_patterns, signature_normalized)
    title_markers = _count_pattern_matches(title_marker_patterns, signature_normalized)
    date_markers = _count_pattern_matches(date_marker_patterns, signature_normalized) + len(
        re.findall(r"\b\d{1,2}\s+[a-z]{3,9}\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", signature_normalized)
    )

    if party_markers >= 2 and title_markers >= 2 and date_markers >= 1:
        return _match(clause, "Execution block appears to include both parties, titles, and a date.", partial_matches)
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


def _signature_evidence_paragraphs(paragraphs: List[Paragraph], signature_patterns: List[str]) -> List[Paragraph]:
    matches = _paragraph_matches(paragraphs, signature_patterns)
    for_line_matches = [
        paragraph
        for paragraph in paragraphs
        if _signature_for_lines(str(paragraph["text"]))
        and any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in signature_patterns)
    ]
    seen = set()
    evidence: List[Paragraph] = []
    for paragraph in matches + for_line_matches:
        paragraph_id = paragraph.get("id")
        if paragraph_id in seen:
            continue
        seen.add(paragraph_id)
        evidence.append(paragraph)
    return evidence


def _signature_for_lines(text: str) -> List[str]:
    return re.findall(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE)
