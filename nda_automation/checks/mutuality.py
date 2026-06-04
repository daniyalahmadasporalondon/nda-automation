from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping

from .common import (
    ClauseResult,
    Paragraph,
    _check,
    _clause_term_patterns,
    _clause_terms,
    _literal_word_pattern,
    _match,
    _not_present,
    _paragraph_matches,
)
from .context import attach_structure_context
from ..review_state import _semantic_review_code, _has_ids, CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW

MUTUALITY_VARIANT_PATTERNS = {
    "mutual": r"\bmutual(?:ity|ly)?\b",
    "reciprocal": r"\breciprocal(?:ly)?\b",
}
NEGATED_MUTUALITY_PATTERN = (
    r"\b(?:not|no|without)\s+(?:a\s+|any\s+)?(?:mutual(?:ly)?|reciprocal(?:ly)?|reciprocity)\b"
    r"|\bnon[-\s]?mutual\b"
)


def _check_mutuality(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    context_concepts = ["mutuality", "party_role_definition", "confidentiality_obligation"]
    search_patterns = _mutuality_search_patterns(clause)
    one_way_patterns = _clause_term_patterns(clause, "one_way_terms")
    mutual_signal_paragraphs = _mutuality_paragraphs(paragraphs, search_patterns)
    strong_mutual_paragraphs = _strong_mutuality_paragraphs(mutual_signal_paragraphs)
    weak_mutual_paragraphs = _weak_mutuality_paragraphs(
        mutual_signal_paragraphs,
        strong_mutual_paragraphs,
    )
    separated_role_paragraphs = _mutual_role_paragraphs(normalized, clause, paragraphs)
    one_way_paragraphs = _paragraph_matches(paragraphs, one_way_patterns)
    operative_one_way_paragraphs = _operative_one_way_paragraphs(one_way_paragraphs)
    analysis = _mutuality_analysis(
        strong_mutual_paragraphs=strong_mutual_paragraphs,
        weak_mutual_paragraphs=weak_mutual_paragraphs,
        role_definition_paragraphs=separated_role_paragraphs,
        one_way_paragraphs=one_way_paragraphs,
    )

    if operative_one_way_paragraphs:
        result = _check(
            clause,
            "Operative one-way confidentiality language needs review despite reciprocal boilerplate.",
            operative_one_way_paragraphs,
            what_to_fix="Revise the NDA so both parties are bound as both Disclosing Party and Receiving Party.",
        )
        _attach_mutuality_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if strong_mutual_paragraphs:
        result = _match(
            clause,
            "Mutual obligation language found.",
            strong_mutual_paragraphs + separated_role_paragraphs,
        )
        _attach_mutuality_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if one_way_paragraphs:
        result = _check(
            clause,
            "One-way or unilateral confidentiality language needs review.",
            one_way_paragraphs,
            what_to_fix="Revise the NDA so both parties are bound as both Disclosing Party and Receiving Party.",
        )
        _attach_mutuality_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if separated_role_paragraphs:
        result = _review(
            clause,
            (
                "Disclosing Party and Receiving Party definitions were found, but the text does not "
                "clearly create reciprocal confidentiality obligations."
            ),
            separated_role_paragraphs,
            what_to_verify=(
                "Confirm whether the role definitions are tied to obligations that bind each party "
                "as both a disclosing and receiving party."
            ),
        )
        _attach_mutuality_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if weak_mutual_paragraphs:
        result = _review(
            clause,
            (
                "A mutuality label or weak mutuality signal was found, but no clear reciprocal "
                "confidentiality obligation was detected."
            ),
            weak_mutual_paragraphs,
            what_to_verify=(
                "Confirm whether the document includes operative language binding both parties "
                "symmetrically, not only a title or label."
            ),
        )
        _attach_mutuality_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    result = _not_present(
        clause,
        "The text does not clearly create mutual confidentiality obligations.",
        [],
        what_to_fix="Add mutual confidentiality language that binds both parties symmetrically.",
    )
    _attach_mutuality_analysis(result, analysis)
    return attach_structure_context(result, review_context, context_concepts)


def _review(
    clause: Dict[str, object],
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    *,
    what_to_verify: str,
) -> ClauseResult:
    result = _match(clause, reason, matched_paragraphs)
    result["decision"] = "review"
    result["needs_review"] = True
    result["review_reason"] = reason
    result["decision_reason"] = reason
    result["what_to_fix"] = what_to_verify
    return result


def _mutual_role_paragraphs(
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
) -> List[Paragraph]:
    role_terms = _clause_terms(clause, "role_terms")
    reciprocity_patterns = _clause_term_patterns(clause, "role_reciprocity_terms")
    if len(role_terms) < 2 or not reciprocity_patterns:
        return []
    if not all(re.search(_literal_word_pattern(term), normalized, flags=re.IGNORECASE) for term in role_terms):
        return []

    evidence: List[Paragraph] = []
    for role_term in role_terms:
        role_pattern = _literal_word_pattern(role_term)
        role_evidence = [
            paragraph
            for paragraph in paragraphs
            if re.search(role_pattern, str(paragraph["text"]), flags=re.IGNORECASE)
            and any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in reciprocity_patterns)
        ]
        if not role_evidence:
            return []
        evidence.extend(role_evidence)

    return evidence


def _mutuality_paragraphs(paragraphs: List[Paragraph], search_patterns: List[str]) -> List[Paragraph]:
    matches: List[Paragraph] = []
    seen = set()
    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        if not _has_unnegated_mutuality_signal(paragraph_text, search_patterns):
            continue
        dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        if dedup_key in seen:
            continue
        matches.append(paragraph)
        seen.add(dedup_key)
    return matches


def _strong_mutuality_paragraphs(paragraphs: Iterable[Paragraph]) -> List[Paragraph]:
    return [
        paragraph
        for paragraph in paragraphs
        if _has_strong_mutuality_obligation(str(paragraph["text"]))
    ]


def _weak_mutuality_paragraphs(
    signal_paragraphs: Iterable[Paragraph],
    strong_paragraphs: Iterable[Paragraph],
) -> List[Paragraph]:
    strong_ids = {
        paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        for paragraph in strong_paragraphs
    }
    return [
        paragraph
        for paragraph in signal_paragraphs
        if (
            paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        ) not in strong_ids
    ]


def _operative_one_way_paragraphs(paragraphs: Iterable[Paragraph]) -> List[Paragraph]:
    return [
        paragraph
        for paragraph in paragraphs
        if _has_operative_one_way_confidentiality_language(str(paragraph["text"]))
    ]


def _has_operative_one_way_confidentiality_language(text: str) -> bool:
    if _is_administrative_one_way_duty(text):
        return False
    patterns = [
        r"\b(?:only\s+the\s+receiving\s+party|recipient\s+only|solely\s+the\s+recipient|receiving\s+party\s+only)\b"
        r".{0,180}\b(?:confidential|protect|safeguard|keep|maintain|hold|use|disclos(?:e|es|ing)|not\s+disclose|not\s+use)\b",
        r"\b(?:one[-\s]?way|unilateral|non[-\s]?mutual)\b"
        r".{0,180}\b(?:confidentiality\s+obligations?|non[-\s]?disclosure|nda|protect|safeguard|keep\s+confidential|not\s+disclose|not\s+use)\b",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _is_administrative_one_way_duty(text: str) -> bool:
    administrative_pattern = (
        r"\b(?:destroy|return|delete|erase|certif(?:y|ies|ication)|copies|copy|materials?)\b"
    )
    operative_confidentiality_pattern = (
        r"\b(?:keep\s+confidential|protect|safeguard|not\s+disclose|not\s+use|"
        r"confidentiality\s+obligations?|non[-\s]?disclosure|nda)\b"
    )
    return bool(re.search(administrative_pattern, text, flags=re.IGNORECASE)) and not bool(
        re.search(operative_confidentiality_pattern, text, flags=re.IGNORECASE)
    )


def _has_strong_mutuality_obligation(text: str) -> bool:
    scoped_parties = r"(?:(?:each|both|either)\s+part(?:y|ies)|(?:each|both)\s+of\s+the\s+parties)"
    strong_patterns = [
        rf"\b{scoped_parties}\b.{{0,220}}\b(?:confidential|disclos(?:e|es|ing)|receiv(?:e|es|ing)|disclosing\s+party|receiving\s+party)\b",
        rf"\b(?:confidential|disclos(?:e|es|ing)|receiv(?:e|es|ing)|disclosing\s+party|receiving\s+party)\b.{{0,220}}\b{scoped_parties}\b",
        r"\bparties\b.{0,220}\b(?:each\s+other|one\s+another)\b.{0,220}\bconfidential\b",
        r"\bthe\s+parties\b.{0,220}\b(?:confidential|disclos(?:e|es|ing)|receiv(?:e|es|ing))\b.{0,220}\b(?:each\s+other|one\s+another)\b",
        r"\b(?:mutual(?:ly)?|reciprocal(?:ly)?|reciprocity)\b.{0,140}\b(?:confidential|obligations?|duties?|undertakings?|bound|binding)\b",
        r"\b(?:confidential|obligations?|duties?|undertakings?|bound|binding)\b.{0,140}\b(?:mutual(?:ly)?|reciprocal(?:ly)?|reciprocity)\b",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in strong_patterns)


def _has_unnegated_mutuality_signal(text: str, search_patterns: List[str]) -> bool:
    negated_spans = [
        match.span()
        for match in re.finditer(NEGATED_MUTUALITY_PATTERN, text, flags=re.IGNORECASE)
    ]
    for pattern in search_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if not _match_inside_any_span(match.span(), negated_spans):
                return True
    return False


def _match_inside_any_span(match_span: tuple[int, int], spans: List[tuple[int, int]]) -> bool:
    match_start, match_end = match_span
    return any(start <= match_start and match_end <= end for start, end in spans)


def _mutuality_search_patterns(clause: Dict[str, object]) -> List[str]:
    return [
        MUTUALITY_VARIANT_PATTERNS.get(term, _literal_word_pattern(term))
        for term in _clause_terms(clause, "search_terms")
    ]


def _mutuality_analysis(
    *,
    strong_mutual_paragraphs: List[Paragraph],
    weak_mutual_paragraphs: List[Paragraph],
    role_definition_paragraphs: List[Paragraph],
    one_way_paragraphs: List[Paragraph],
) -> Dict[str, object]:
    return {
        "strong_mutuality_paragraph_ids": _paragraph_ids(strong_mutual_paragraphs),
        "weak_mutuality_paragraph_ids": _paragraph_ids(weak_mutual_paragraphs),
        "role_definition_paragraph_ids": _paragraph_ids(role_definition_paragraphs),
        "one_way_paragraph_ids": _paragraph_ids(one_way_paragraphs),
    }


def _attach_mutuality_analysis(result: ClauseResult, analysis: Dict[str, object]) -> None:
    result["mutuality_analysis"] = analysis


def _paragraph_ids(paragraphs: Iterable[Paragraph]) -> List[str]:
    return [str(paragraph.get("id") or "") for paragraph in paragraphs if paragraph.get("id")]


def reason_code(clause: Mapping[str, Any], decision: str) -> str:
    semantic_code = _semantic_review_code(clause, decision)
    if semantic_code:
        return semantic_code
    if decision != CLAUSE_DECISION_FAIL and _has_ids(clause, "mutuality_analysis", "strong_mutuality_paragraph_ids"):
        return "mutuality_obligation_found"
    if _has_ids(clause, "mutuality_analysis", "one_way_paragraph_ids"):
        return "one_way_mutuality_language"
    if _has_ids(clause, "mutuality_analysis", "role_definition_paragraph_ids"):
        return "role_definitions_without_operational_mutuality"
    if _has_ids(clause, "mutuality_analysis", "weak_mutuality_paragraph_ids"):
        return "weak_mutuality_signal"
    if _has_ids(clause, "mutuality_analysis", "strong_mutuality_paragraph_ids"):
        return "mutuality_obligation_found"
    if decision == CLAUSE_DECISION_FAIL:
        return "missing_mutuality_obligation"
    if decision == CLAUSE_DECISION_REVIEW:
        return "unclear_mutuality_obligation"
    return "mutuality_obligation_found"
