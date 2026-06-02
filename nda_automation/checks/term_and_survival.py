from __future__ import annotations

import re
from typing import Dict, List

from ..concept_classifier import ORDINARY_CONFIDENTIALITY_CONCEPTS
from .common import (
    ClauseResult,
    Paragraph,
    YEAR_TERM_EVIDENCE_PATTERN,
    YEAR_TERM_PATTERN,
    YEAR_WORDS,
    _check,
    _clause_term_patterns,
    _match,
    _max_term_years,
    _normalize,
    _not_present,
    _paragraph_matches,
    _term_context_patterns,
    _year_count_label,
)

CARVE_OUT_SURVIVAL_PATTERN = (
    r"\b(?:surviv(?:e|es|ed|ing|al)|remain(?:s|ed|ing)?|continu(?:e|es|ed|ing)|"
    r"last(?:s|ed|ing)?|in\s+effect|binding|required?|requires?)\b"
)
ORDINARY_SURVIVAL_SUBJECT_PATTERN = (
    r"\b(?:(?:all|the|such|ordinary|confidentiality|confidential|parties'?|party)\s+)"
    r"(?:confidentiality\s+)?(?:obligations?|undertakings?|provisions?|duties?)\b"
    r"|\bconfidentiality\s+(?:surviv(?:e|es)|continu(?:e|es)|remain(?:s)?)\b"
)


def _check_term_and_survival(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    max_years = _max_term_years(clause)
    cap_label = _year_count_label(max_years)
    term_context_patterns = _term_context_patterns(clause)
    indefinite_patterns = _clause_term_patterns(clause, "indefinite_terms")
    term_paragraphs = _paragraph_matches(paragraphs, term_context_patterns)
    term_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in term_paragraphs))
    reference_analysis = _survival_reference_analysis(term_paragraphs, paragraphs, review_context or {})
    year_terms = _extract_year_terms_with_context(term_normalized)
    has_term_within_cap = any(0 < term["years"] <= max_years for term in year_terms)
    ordinary_over_cap_terms = [
        term
        for term in year_terms
        if term["years"] > max_years and not _is_allowed_carve_out_year(term_normalized, term, clause)
    ]
    has_term_over_cap = bool(ordinary_over_cap_terms)
    ordinary_indefinite_matches = [
        match
        for pattern in indefinite_patterns
        for match in re.finditer(pattern, term_normalized)
        if not _is_allowed_carve_out_fragment(term_normalized, match.start(), match.end(), clause)
    ]

    if has_term_over_cap:
        result = _check(
            clause,
            f"A term or survival period exceeds the cap of {cap_label}.",
            _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN]),
            what_to_fix=(
                "Reduce the ordinary confidentiality term or survival period "
                f"to a fixed period of {cap_label} or less."
            ),
        )
        _attach_survival_analysis(result, reference_analysis)
        return result
    if ordinary_indefinite_matches:
        result = _check(
            clause,
            f"Survival language appears indefinite or perpetual rather than capped at {cap_label}.",
            _paragraph_matches(term_paragraphs, indefinite_patterns),
            what_to_fix=(
                "Replace indefinite or perpetual ordinary confidentiality language "
                f"with a fixed period of {cap_label} or less."
            ),
        )
        _attach_survival_analysis(result, reference_analysis)
        return result
    if has_term_within_cap:
        evidence_paragraphs = _term_evidence_paragraphs(term_paragraphs, paragraphs, reference_analysis)
        if reference_analysis["confidentiality_reference_count"]:
            reason = (
                "Referenced confidentiality provisions survive within "
                f"the cap of {cap_label}."
            )
        else:
            reason = f"Term or survival period is within the cap of {cap_label}."
        result = _match(clause, reason, evidence_paragraphs)
        _attach_survival_analysis(result, reference_analysis)
        return result
    result = _not_present(
        clause,
        f"No fixed term or survival period of up to {cap_label} was found.",
        term_paragraphs,
        what_to_fix=f"Add a fixed term or ordinary confidentiality survival period of {cap_label} or less.",
    )
    _attach_survival_analysis(result, reference_analysis)
    return result


def _extract_year_terms_with_context(normalized: str) -> List[Dict[str, int]]:
    terms: List[Dict[str, int]] = []
    for match in re.finditer(YEAR_TERM_PATTERN, normalized):
        word_value, digit_value, parenthetical_digit, parenthetical_word, unit = match.groups()
        if digit_value:
            value = int(digit_value)
        elif parenthetical_digit:
            value = int(parenthetical_digit)
        elif word_value:
            value = YEAR_WORDS[word_value]
        elif parenthetical_word:
            value = YEAR_WORDS[parenthetical_word]
        else:
            continue
        years = value / 12 if unit.startswith("month") else value
        terms.append({"years": years, "start": match.start(), "end": match.end()})
    return terms


def _survival_reference_analysis(
    term_paragraphs: List[Paragraph],
    all_paragraphs: List[Paragraph],
    review_context: Dict[str, object],
) -> Dict[str, object]:
    paragraph_ids = {str(paragraph.get("id")) for paragraph in term_paragraphs if paragraph.get("id")}
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    classifier = review_context.get("concept_classifier")
    concepts_by_section_id = {}
    if isinstance(classifier, dict) and isinstance(classifier.get("concepts_by_section_id"), dict):
        concepts_by_section_id = classifier["concepts_by_section_id"]
    reference_resolver = review_context.get("reference_resolver")
    references = reference_resolver.get("references", []) if isinstance(reference_resolver, dict) else []

    records: List[Dict[str, object]] = []
    confidentiality_reference_count = 0
    target_paragraph_ids: List[str] = []
    for reference in references:
        if not isinstance(reference, dict) or str(reference.get("paragraph_id")) not in paragraph_ids:
            continue
        target_records = []
        target_has_confidentiality = False
        for target in reference.get("targets", []):
            if not isinstance(target, dict):
                continue
            section_id = str(target.get("id") or "")
            raw_concepts = concepts_by_section_id.get(section_id, [])
            if not isinstance(raw_concepts, list):
                raw_concepts = []
            concepts = [
                str(concept)
                for concept in raw_concepts
                if str(concept)
            ]
            is_confidentiality = bool(ORDINARY_CONFIDENTIALITY_CONCEPTS.intersection(concepts))
            target_has_confidentiality = target_has_confidentiality or is_confidentiality
            for paragraph_id in target.get("paragraph_ids", []):
                paragraph_key = str(paragraph_id)
                if paragraph_key in paragraph_lookup and paragraph_key not in target_paragraph_ids:
                    target_paragraph_ids.append(paragraph_key)
            target_records.append({
                "section_id": section_id,
                "label": str(target.get("label") or ""),
                "concepts": concepts,
                "ordinary_confidentiality": is_confidentiality,
            })
        if target_has_confidentiality:
            confidentiality_reference_count += 1
        records.append({
            "reference_text": str(reference.get("reference_text") or ""),
            "paragraph_id": str(reference.get("paragraph_id") or ""),
            "status": str(reference.get("status") or ""),
            "targets": target_records,
            "ordinary_confidentiality": target_has_confidentiality,
        })

    return {
        "references": records,
        "reference_count": len(records),
        "confidentiality_reference_count": confidentiality_reference_count,
        "target_paragraph_ids": target_paragraph_ids,
    }


def _term_evidence_paragraphs(
    term_paragraphs: List[Paragraph],
    all_paragraphs: List[Paragraph],
    reference_analysis: Dict[str, object],
) -> List[Paragraph]:
    evidence = _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN])
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    for paragraph_id in reference_analysis.get("target_paragraph_ids", []):
        paragraph = paragraph_lookup.get(str(paragraph_id))
        if paragraph:
            evidence.append(paragraph)
    return evidence


def _attach_survival_analysis(result: ClauseResult, reference_analysis: Dict[str, object]) -> None:
    if not reference_analysis.get("reference_count"):
        return
    result["term_survival_analysis"] = {
        "reference_count": reference_analysis["reference_count"],
        "confidentiality_reference_count": reference_analysis["confidentiality_reference_count"],
        "references": reference_analysis["references"],
    }


def _is_allowed_carve_out_year(normalized: str, term: Dict[str, int], clause: Dict[str, object]) -> bool:
    return _is_allowed_carve_out_fragment(normalized, term["start"], term["end"], clause)


def _is_allowed_carve_out_fragment(normalized: str, start: int, end: int, clause: Dict[str, object]) -> bool:
    fragment, relative_start, relative_end = _term_fragment_bounds(normalized, start, end)
    carve_out_patterns = _carve_out_context_patterns(clause)
    if not any(re.search(pattern, fragment) for pattern in carve_out_patterns):
        return False
    return bool(
        any(_carve_out_scoped_before_term(fragment, relative_start, pattern) for pattern in carve_out_patterns)
        or any(_term_scoped_to_carve_out(fragment, relative_end, pattern) for pattern in carve_out_patterns)
    )


def _carve_out_scoped_before_term(fragment: str, term_start: int, carve_out_pattern: str) -> bool:
    before_term = fragment[:term_start]
    carve_out_matches = list(re.finditer(carve_out_pattern, before_term))
    if not carve_out_matches:
        return False

    last_carve_out = carve_out_matches[-1]
    scoped_text = before_term[last_carve_out.start():]
    if not re.search(CARVE_OUT_SURVIVAL_PATTERN, scoped_text):
        return False
    if re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, before_term[last_carve_out.end():]):
        return False
    return True


def _term_scoped_to_carve_out(fragment: str, term_end: int, carve_out_pattern: str) -> bool:
    after_term = _fragment_after_term_until_next_duration(fragment, term_end)
    return bool(
        re.search(
            rf"\b(?:for|as\s+to|with\s+respect\s+to|in\s+respect\s+of|solely\s+for|limited\s+to)\s+(?:the\s+)?{carve_out_pattern}\b",
            after_term,
        )
    )


def _fragment_after_term_until_next_duration(fragment: str, term_end: int) -> str:
    after_term = fragment[term_end:]
    next_duration = re.search(YEAR_TERM_PATTERN, after_term)
    if next_duration:
        return after_term[:next_duration.start()]
    return after_term


def _carve_out_context_patterns(clause: Dict[str, object]) -> List[str]:
    configured_terms = clause.get("longer_survival_carve_out_terms")
    if isinstance(configured_terms, list):
        terms = configured_terms
    else:
        terms = [
            "trade secret",
            "trade secrets",
            "legal obligation",
            "legal obligations",
            "required by law",
            "applicable law",
        ]
    return [
        re.escape(str(term).lower().strip()).replace(r"\ ", r"\s+")
        for term in terms
        if str(term).strip()
    ]


def _term_fragment_bounds(normalized: str, start: int, end: int) -> tuple[str, int, int]:
    left_candidates = [
        normalized.rfind(separator, 0, start)
        for separator in (".", ";", ",")
    ]
    right_candidates = [
        position
        for position in (normalized.find(separator, end) for separator in (".", ";", ","))
        if position != -1
    ]
    left = max(left_candidates) + 1
    right = min(right_candidates) if right_candidates else len(normalized)
    while left < right and normalized[left].isspace():
        left += 1
    while right > left and normalized[right - 1].isspace():
        right -= 1
    return normalized[left:right], start - left, end - left
