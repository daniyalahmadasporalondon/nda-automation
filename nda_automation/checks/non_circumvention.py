from __future__ import annotations

import re
from typing import Dict, Iterable, List

from .common import (
    ClauseResult,
    ISSUE_TYPE_UNCLEAR,
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
FLEXIBLE_NON_CIRCUMVENTION_CANDIDATE_PATTERNS = [
    r"\bcircumvent\w*\b",
    r"\bbypass\w*\b",
    r"\bdeal(?:s|ing)?\s+(?:directly|exclusiv\w+)\b",
    r"\bexclusiv\w+\b",
    r"\bsolicit\w*\b",
    r"\bintroduced\b.{0,60}\b(?:part(?:y|ies)|contacts?|customers?|counterpart(?:y|ies))\b",
    r"\b(?:part(?:y|ies)|contacts?|customers?|counterpart(?:y|ies))\b.{0,60}\bintroduced\b",
]
NEGATED_NON_CIRCUMVENTION_REFERENCE_PATTERN = (
    r"\b(?:does|do|doesn't|don't|shall|will|may|can|must)\s+not\s+"
    r"(?:include|create|impose|establish|grant|constitute|amount\s+to)\b"
    r".{0,140}\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose)\b"
    r"|\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose)\b"
    r".{0,140}\b(?:does|do|doesn't|don't|shall|will|may|can|must)\s+not\s+"
    r"(?:apply|arise|exist|be\s+(?:created|imposed|established|granted|required))\b"
    r"|\bno\s+(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose)\s+"
    r"(?:obligations?|restrictions?|rights?|is|are|will|shall|may|can|appl(?:y|ies)|exists?|created|imposed)\b"
    r"|\bfor\s+the\s+avoidance\s+of\s+doubt\b.{0,80}\b(?:no|not)\b.{0,140}"
    r"\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose)\b"
)
RESTRICTIVE_NON_CIRCUMVENTION_PATTERN = (
    r"\b(?:must|shall|will|may|can)\s+not\b"
    r"|\b(?:agrees?|undertakes?|covenants?)\s+(?:not\s+|to\s+)?"
    r"|\b(?:is|are|be|remain(?:s)?)\s+(?:prohibited|restricted|barred|prevented|subject)\b"
    r"|\b(?:avoid|bypass|prohibit|restrict|prevent)\b"
    r"|\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit)\b"
    r".{0,100}\b(?:covenant|restriction|obligation|undertaking|clause|agreement|introduced|contacts?|customers?|parties)\b"
    r"|\b(?:introduced\s+(?:contacts?|customers?|parties)|(?:contacts?|customers?|parties)\s+introduced)\b"
    r".{0,100}\b(?:non[-\s]?solicitation|non[-\s]?solicit|exclusive\s+dealing|exclusivity|substitute\s+purpose)\b"
    r"|\b(?:exclusive\s+dealing|exclusivity|substitute\s+purpose)\b"
    r".{0,100}\b(?:introduced|contacts?|customers?|parties)\b"
    r"|\bdeal(?:s|ing)?\s+exclusiv\w+\s+with\b"
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
    candidate_patterns = prohibited_patterns + FLEXIBLE_NON_CIRCUMVENTION_CANDIDATE_PATTERNS
    candidate_paragraphs = merge_paragraphs(
        _paragraph_matches(paragraphs, candidate_patterns),
        paragraphs_with_concepts(paragraphs, review_context, context_concepts),
    )
    analysis = _non_circumvention_analysis(candidate_paragraphs, candidate_patterns)
    prohibited_paragraphs = analysis["prohibited_paragraphs"]
    review_paragraphs = analysis["review_paragraphs"]

    if prohibited_paragraphs:
        result = _check(
            clause,
            "Prohibited non-circumvention or substitute-purpose language found.",
            prohibited_paragraphs,
            what_to_fix=(
                "Remove non-circumvention, introduced-party non-solicit, substitute-purpose, "
                "or exclusivity language."
            ),
        )
        _attach_non_circumvention_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if review_paragraphs:
        result = _review(
            clause,
            (
                "Possible non-circumvention, introduced-party, substitute-purpose, or exclusivity "
                "language was found, but it is not clearly an operative restriction."
            ),
            review_paragraphs,
            what_to_verify=(
                "Confirm whether the language restricts direct dealings, introduced contacts, "
                "substitute transactions, or exclusivity beyond confidentiality."
            ),
        )
        _attach_non_circumvention_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    result = _not_present(clause, "No prohibited non-circumvention language detected.", [])
    _attach_non_circumvention_analysis(result, analysis)
    return attach_structure_context(result, review_context, context_concepts)


def _review(
    clause: Dict[str, object],
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    *,
    what_to_verify: str,
) -> ClauseResult:
    result = _check(
        clause,
        reason,
        matched_paragraphs,
        issue_type=ISSUE_TYPE_UNCLEAR,
        what_to_fix=what_to_verify,
    )
    result["decision"] = "review"
    result["needs_review"] = True
    result["review_reason"] = reason
    result["decision_reason"] = reason
    return result


def _non_circumvention_analysis(
    candidate_paragraphs: Iterable[Paragraph],
    prohibited_patterns: List[str],
) -> Dict[str, object]:
    prohibited_paragraphs: List[Paragraph] = []
    review_paragraphs: List[Paragraph] = []
    lawful_circumvention_paragraphs: List[Paragraph] = []
    negated_reference_paragraphs: List[Paragraph] = []
    signal_records: List[Dict[str, object]] = []

    for paragraph in candidate_paragraphs:
        paragraph_text = str(paragraph["text"])
        searchable_text = _without_lawful_circumvention_context(paragraph_text)
        if searchable_text != paragraph_text:
            lawful_circumvention_paragraphs.append(paragraph)

        matched_patterns = [
            pattern
            for pattern in prohibited_patterns
            if re.search(pattern, searchable_text, flags=re.IGNORECASE)
        ]
        if not matched_patterns:
            continue

        record = {
            "paragraph_id": str(paragraph.get("id") or ""),
            "matched_pattern_count": len(matched_patterns),
        }
        if _is_negated_non_circumvention_reference(searchable_text):
            negated_reference_paragraphs.append(paragraph)
            record["classification"] = "negated_reference"
        elif _has_hard_prohibited_non_circumvention(searchable_text):
            prohibited_paragraphs.append(paragraph)
            record["classification"] = "prohibited"
        else:
            review_paragraphs.append(paragraph)
            record["classification"] = "review"
        signal_records.append(record)

    return {
        "prohibited_paragraphs": prohibited_paragraphs,
        "review_paragraphs": review_paragraphs,
        "lawful_circumvention_paragraphs": lawful_circumvention_paragraphs,
        "negated_reference_paragraphs": negated_reference_paragraphs,
        "signal_records": signal_records,
    }


def _has_prohibited_non_circumvention(text: str, prohibited_patterns: List[str]) -> bool:
    searchable_text = _without_lawful_circumvention_context(text)
    return any(re.search(pattern, searchable_text, flags=re.IGNORECASE) for pattern in prohibited_patterns)


def _has_hard_prohibited_non_circumvention(searchable_text: str) -> bool:
    return bool(re.search(RESTRICTIVE_NON_CIRCUMVENTION_PATTERN, searchable_text, flags=re.IGNORECASE))


def _is_negated_non_circumvention_reference(searchable_text: str) -> bool:
    return bool(re.search(NEGATED_NON_CIRCUMVENTION_REFERENCE_PATTERN, searchable_text, flags=re.IGNORECASE))


def _without_lawful_circumvention_context(text: str) -> str:
    return re.sub(LAWFUL_CIRCUMVENTION_PATTERN, " ", text, flags=re.IGNORECASE)


def _attach_non_circumvention_analysis(result: ClauseResult, analysis: Dict[str, object]) -> None:
    result["non_circumvention_analysis"] = {
        "prohibited_paragraph_ids": _paragraph_ids(analysis["prohibited_paragraphs"]),
        "review_paragraph_ids": _paragraph_ids(analysis["review_paragraphs"]),
        "lawful_circumvention_paragraph_ids": _paragraph_ids(analysis["lawful_circumvention_paragraphs"]),
        "negated_reference_paragraph_ids": _paragraph_ids(analysis["negated_reference_paragraphs"]),
        "signal_records": analysis["signal_records"],
    }


def _paragraph_ids(paragraphs: Iterable[Paragraph]) -> List[str]:
    return [str(paragraph.get("id") or "") for paragraph in paragraphs if paragraph.get("id")]
