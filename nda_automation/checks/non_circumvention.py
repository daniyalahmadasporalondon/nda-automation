from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping

from .common import (
    ClauseResult,
    ISSUE_TYPE_UNCLEAR,
    Paragraph,
    _check,
    _clause_term_patterns,
    _not_present,
    _paragraph_matches,
    is_circumvention_freedom_preserving,
)
from .context import attach_structure_context, merge_paragraphs, paragraphs_with_concepts
from ..review_state import _semantic_review_code, _has_ids, _generic_reason_code, CLAUSE_DECISION_PASS

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
NON_CIRCUMVENTION_REFERENCE_SCOPE_PATTERN = (
    r"\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose|introduced\s+contacts?)\b"
    r".{0,180}\b(?:clause|clauses|article|articles|section|sections|schedule|schedules|"
    r"annex|annexes|annexure|annexures|appendix|appendices)\b"
    r"|\b(?:clause|clauses|article|articles|section|sections|schedule|schedules|"
    r"annex|annexes|annexure|annexures|appendix|appendices)\b"
    r".{0,180}\b(?:non[-\s]?circumvention|non[-\s]?solicitation|non[-\s]?solicit|"
    r"exclusivity|exclusive\s+dealing|direct\s+dealing|substitute\s+purpose|introduced\s+contacts?)\b"
)
REFERENCE_REVIEW_STATUSES = {"partial", "unresolved", "review", "no_non_circumvention_signal"}


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
    reference_analysis = _non_circumvention_reference_analysis(
        paragraphs,
        candidate_patterns,
        review_context or {},
    )
    _merge_non_circumvention_reference_analysis(analysis, reference_analysis, paragraphs)
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
        record = _non_circumvention_paragraph_record(paragraph, prohibited_patterns)
        classification = str(record.get("classification") or "")
        if record.get("lawful_circumvention"):
            lawful_circumvention_paragraphs.append(paragraph)
        if classification == "no_signal":
            continue

        if classification == "negated_reference":
            negated_reference_paragraphs.append(paragraph)
        elif classification == "prohibited":
            prohibited_paragraphs.append(paragraph)
        else:
            review_paragraphs.append(paragraph)
        signal_records.append(record)

    return {
        "prohibited_paragraphs": prohibited_paragraphs,
        "review_paragraphs": review_paragraphs,
        "lawful_circumvention_paragraphs": lawful_circumvention_paragraphs,
        "negated_reference_paragraphs": negated_reference_paragraphs,
        "signal_records": signal_records,
        "references": [],
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


def _non_circumvention_paragraph_record(
    paragraph: Paragraph,
    prohibited_patterns: List[str],
) -> Dict[str, object]:
    paragraph_text = str(paragraph.get("text") or "")
    searchable_text = _without_lawful_circumvention_context(paragraph_text)
    matched_patterns = [
        pattern
        for pattern in prohibited_patterns
        if re.search(pattern, searchable_text, flags=re.IGNORECASE)
    ]
    if not matched_patterns:
        return {
            "paragraph_id": str(paragraph.get("id") or ""),
            "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
            "matched_pattern_count": 0,
            "classification": "no_signal",
            "lawful_circumvention": searchable_text != paragraph_text,
        }
    if is_circumvention_freedom_preserving(searchable_text):
        # Freedom-preserving carve-out ("shall not be restricted from dealing with
        # introduced contacts") -- the literal opposite of a restriction, not a signal.
        return {
            "paragraph_id": str(paragraph.get("id") or ""),
            "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
            "matched_pattern_count": len(matched_patterns),
            "classification": "no_signal",
            "lawful_circumvention": searchable_text != paragraph_text,
        }
    if _is_negated_non_circumvention_reference(searchable_text):
        classification = "negated_reference"
    elif _has_hard_prohibited_non_circumvention(searchable_text):
        classification = "prohibited"
    elif _is_non_circumvention_heading_only(searchable_text):
        classification = "heading_only"
    else:
        classification = "review"
    return {
        "paragraph_id": str(paragraph.get("id") or ""),
        "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
        "matched_pattern_count": len(matched_patterns),
        "classification": classification,
        "lawful_circumvention": searchable_text != paragraph_text,
    }


def _non_circumvention_reference_analysis(
    all_paragraphs: List[Paragraph],
    prohibited_patterns: List[str],
    review_context: Dict[str, object],
) -> Dict[str, object]:
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    concepts_by_section_id = _concepts_by_section_id(review_context)
    reference_resolver = review_context.get("reference_resolver")
    references = reference_resolver.get("references", []) if isinstance(reference_resolver, dict) else []

    records: List[Dict[str, object]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        paragraph_id = str(reference.get("paragraph_id") or "")
        paragraph = paragraph_lookup.get(paragraph_id)
        if not paragraph:
            continue
        source_record = _non_circumvention_paragraph_record(paragraph, prohibited_patterns)
        source_in_scope = _is_non_circumvention_reference_scope(str(paragraph.get("text") or ""))
        target_records = _non_circumvention_reference_target_records(
            reference,
            paragraph_lookup,
            concepts_by_section_id,
            prohibited_patterns,
        )
        target_in_scope = any(bool(target.get("non_circumvention_scope")) for target in target_records)
        if not source_in_scope and not target_in_scope:
            continue
        records.append({
            "paragraph_id": paragraph_id,
            "paragraph_index": reference.get("paragraph_index") if isinstance(reference.get("paragraph_index"), int) else None,
            "reference_text": str(reference.get("reference_text") or ""),
            "kind": str(reference.get("kind") or ""),
            "status": _non_circumvention_reference_status(reference, source_record, target_records),
            "resolver_status": str(reference.get("status") or ""),
            "source_classification": str(source_record.get("classification") or ""),
            "unresolved_numbers": [
                str(number)
                for number in reference.get("unresolved_numbers", [])
                if str(number)
            ],
            "targets": target_records,
        })
    return {
        "references": records,
        "reference_count": len(records),
    }


def _non_circumvention_reference_target_records(
    reference: Dict[str, object],
    paragraph_lookup: Dict[str, Paragraph],
    concepts_by_section_id: Dict[str, List[str]],
    prohibited_patterns: List[str],
) -> List[Dict[str, object]]:
    target_records: List[Dict[str, object]] = []
    for target in reference.get("targets", []):
        if not isinstance(target, dict):
            continue
        section_id = str(target.get("id") or "")
        concepts = [
            str(concept)
            for concept in concepts_by_section_id.get(section_id, [])
            if str(concept)
        ]
        paragraph_records = [
            _non_circumvention_paragraph_record(paragraph_lookup[str(paragraph_id)], prohibited_patterns)
            for paragraph_id in target.get("paragraph_ids", [])
            if str(paragraph_id) in paragraph_lookup
        ]
        target_status = _non_circumvention_target_status(paragraph_records)
        target_records.append({
            "section_id": section_id,
            "label": str(target.get("label") or ""),
            "paragraph_ids": [
                str(paragraph_id)
                for paragraph_id in target.get("paragraph_ids", [])
                if str(paragraph_id)
            ],
            "concepts": concepts,
            "non_circumvention_scope": "non_circumvention" in concepts or target_status != "no_signal",
            "status": target_status,
            "paragraphs": paragraph_records,
        })
    return target_records


def _non_circumvention_target_status(paragraph_records: List[Dict[str, object]]) -> str:
    classifications = [
        str(record.get("classification") or "")
        for record in paragraph_records
        if str(record.get("classification") or "") and str(record.get("classification") or "") != "no_signal"
    ]
    if "prohibited" in classifications:
        return "prohibited"
    if "review" in classifications:
        return "review"
    if "negated_reference" in classifications:
        return "negated"
    if "heading_only" in classifications:
        return "review"
    return "no_signal"


def _non_circumvention_reference_status(
    reference: Dict[str, object],
    source_record: Dict[str, object],
    target_records: List[Dict[str, object]],
) -> str:
    target_statuses = [
        str(target.get("status") or "")
        for target in target_records
        if str(target.get("status") or "")
    ]
    if "prohibited" in target_statuses:
        return "prohibited"
    resolver_status = str(reference.get("status") or "")
    if resolver_status == "unresolved":
        return "unresolved"
    if resolver_status == "partial" or reference.get("unresolved_numbers"):
        return "partial"
    if "review" in target_statuses:
        return "review"
    source_classification = str(source_record.get("classification") or "")
    if "negated" in target_statuses:
        return "negated" if source_classification == "negated_reference" else "review"
    if not target_statuses or all(status == "no_signal" for status in target_statuses):
        return "no_non_circumvention_signal"
    return "review"


def _merge_non_circumvention_reference_analysis(
    analysis: Dict[str, object],
    reference_analysis: Dict[str, object],
    all_paragraphs: List[Paragraph],
) -> None:
    references = [
        reference
        for reference in reference_analysis.get("references", [])
        if isinstance(reference, dict)
    ]
    analysis["references"] = references
    if not references:
        return
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    for reference in references:
        status = str(reference.get("status") or "")
        if status == "prohibited":
            _append_reference_target_paragraphs(
                analysis["prohibited_paragraphs"],
                paragraph_lookup,
                reference,
                {"prohibited"},
            )
        elif status in REFERENCE_REVIEW_STATUSES:
            _append_paragraph_by_id(analysis["review_paragraphs"], paragraph_lookup, str(reference.get("paragraph_id") or ""))
            _append_reference_target_paragraphs(
                analysis["review_paragraphs"],
                paragraph_lookup,
                reference,
                {"review", "heading_only", "no_signal"},
            )
        elif status == "negated":
            _remove_reference_target_paragraphs(
                analysis["review_paragraphs"],
                reference,
                {"heading_only"},
            )
            _append_paragraph_by_id(
                analysis["negated_reference_paragraphs"],
                paragraph_lookup,
                str(reference.get("paragraph_id") or ""),
            )
            _append_reference_target_paragraphs(
                analysis["negated_reference_paragraphs"],
                paragraph_lookup,
                reference,
                {"negated_reference"},
            )


def _remove_reference_target_paragraphs(
    paragraphs: List[Paragraph],
    reference: Dict[str, object],
    classifications: set[str],
) -> None:
    paragraph_ids_to_remove = {
        str(record.get("paragraph_id") or "")
        for target in reference.get("targets", [])
        if isinstance(target, dict)
        for record in target.get("paragraphs", [])
        if isinstance(record, dict) and str(record.get("classification") or "") in classifications
    }
    if not paragraph_ids_to_remove:
        return
    paragraphs[:] = [
        paragraph
        for paragraph in paragraphs
        if str(paragraph.get("id") or "") not in paragraph_ids_to_remove
    ]


def _append_reference_target_paragraphs(
    paragraphs: List[Paragraph],
    paragraph_lookup: Dict[str, Paragraph],
    reference: Dict[str, object],
    classifications: set[str],
) -> None:
    for target in reference.get("targets", []):
        if not isinstance(target, dict):
            continue
        for record in target.get("paragraphs", []):
            if not isinstance(record, dict):
                continue
            if str(record.get("classification") or "") not in classifications:
                continue
            _append_paragraph_by_id(paragraphs, paragraph_lookup, str(record.get("paragraph_id") or ""))


def _append_paragraph_by_id(
    paragraphs: List[Paragraph],
    paragraph_lookup: Dict[str, Paragraph],
    paragraph_id: str,
) -> None:
    paragraph = paragraph_lookup.get(paragraph_id)
    if not paragraph:
        return
    existing_ids = {str(existing.get("id") or "") for existing in paragraphs}
    if paragraph_id not in existing_ids:
        paragraphs.append(paragraph)


def _concepts_by_section_id(review_context: Dict[str, object]) -> Dict[str, List[str]]:
    classifier = review_context.get("concept_classifier")
    if not isinstance(classifier, dict) or not isinstance(classifier.get("concepts_by_section_id"), dict):
        return {}
    return {
        str(section_id): [
            str(concept)
            for concept in concepts
            if str(concept)
        ]
        for section_id, concepts in classifier["concepts_by_section_id"].items()
        if isinstance(concepts, list)
    }


def _is_non_circumvention_reference_scope(paragraph_text: str) -> bool:
    return bool(re.search(NON_CIRCUMVENTION_REFERENCE_SCOPE_PATTERN, paragraph_text, flags=re.IGNORECASE))


def _is_non_circumvention_heading_only(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:(?:article|clause|section|schedule)\s+[A-Za-z0-9IVXLCivxlc.() -]+\s*:?\s*)?"
            r"(?:non[-\s]?circumvention|non[-\s]?solicitation|introduced\s+contacts?|"
            r"exclusive\s+dealing|exclusivity|substitute\s+purpose)\s*[:.-]?\s*$",
            text,
            flags=re.IGNORECASE,
        )
    )


def _attach_non_circumvention_analysis(result: ClauseResult, analysis: Dict[str, object]) -> None:
    result["non_circumvention_analysis"] = {
        "prohibited_paragraph_ids": _paragraph_ids(analysis["prohibited_paragraphs"]),
        "review_paragraph_ids": _paragraph_ids(analysis["review_paragraphs"]),
        "lawful_circumvention_paragraph_ids": _paragraph_ids(analysis["lawful_circumvention_paragraphs"]),
        "negated_reference_paragraph_ids": _paragraph_ids(analysis["negated_reference_paragraphs"]),
        "signal_records": analysis["signal_records"],
        "reference_count": len(analysis.get("references", [])),
        "references": analysis.get("references", []),
    }


def _paragraph_ids(paragraphs: Iterable[Paragraph]) -> List[str]:
    return [str(paragraph.get("id") or "") for paragraph in paragraphs if paragraph.get("id")]


def reason_code(clause: Mapping[str, Any], decision: str) -> str:
    semantic_code = _semantic_review_code(clause, decision)
    if semantic_code:
        return semantic_code
    if _has_ids(clause, "non_circumvention_analysis", "prohibited_paragraph_ids"):
        return "prohibited_non_circumvention_restriction"
    if _has_non_circumvention_reference_status(
        clause,
        {"partial", "unresolved", "review", "no_non_circumvention_signal"},
    ):
        return "unclear_non_circumvention_reference"
    if _has_ids(clause, "non_circumvention_analysis", "review_paragraph_ids"):
        return "possible_non_circumvention_restriction"
    if _has_ids(clause, "non_circumvention_analysis", "negated_reference_paragraph_ids"):
        return "negated_non_circumvention_reference"
    if _has_ids(clause, "non_circumvention_analysis", "lawful_circumvention_paragraph_ids"):
        return "lawful_circumvention_reference_ignored"
    if decision == CLAUSE_DECISION_PASS:
        return "no_non_circumvention_restriction"
    return _generic_reason_code(clause, decision)


def _has_non_circumvention_reference_status(clause: Dict[str, object], statuses: set[str]) -> bool:
    analysis = clause.get("non_circumvention_analysis")
    if not isinstance(analysis, dict):
        return False
    references = analysis.get("references", [])
    if not isinstance(references, list):
        return False
    return any(
        isinstance(reference, dict) and str(reference.get("status") or "") in statuses
        for reference in references
    )
