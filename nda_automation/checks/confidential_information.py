from __future__ import annotations

import re
from typing import Dict, Iterable, List

from .common import (
    ClauseResult,
    INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW,
    Paragraph,
    _check,
    _clause_term_patterns,
    _clause_terms,
    _confidential_categories_label,
    _dedupe_terms,
    _literal_word_pattern,
    _match,
    _normalize,
    _not_present,
    _paragraph_matches,
)
from .context import attach_structure_context, merge_paragraphs, paragraphs_with_concepts
from ..review_state import _semantic_review_code, _has_ids, _issue_type, _generic_reason_code, CLAUSE_DECISION_REVIEW

USAGE_RIGHT_ACTION_PATTERN = (
    r"(?:use|using|retain|retaining|disclose|disclosing|exploit|exploiting|"
    r"reverse\s+engineer(?:ing)?)"
)
USAGE_RIGHT_PERMISSION_MODIFIER_PATTERN = (
    r"(?:(?:freely|directly|unrestrictedly|without\s+(?:restriction|limitation|limit)|"
    r"for\s+any\s+purpose)\s+){0,3}"
)
USAGE_RIGHT_BEFORE_PATTERN = (
    rf"(?:\b(?:may|can)\s+{USAGE_RIGHT_PERMISSION_MODIFIER_PATTERN}{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\b(?:shall|will|is|are|be|remain(?:s)?)\s+(?:free|permitted|allowed|entitled)\s+to\s+"
    rf"{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\b(?:has|have)\s+(?:the\s+)?right\s+to\s+{USAGE_RIGHT_ACTION_PATTERN}"
    rf"|\bnothing\b[^.;]{{0,120}}\b(?:prohibits|prevents|restricts|limits)\b[^.;]{{0,80}}\bfrom\s+"
    rf"{USAGE_RIGHT_ACTION_PATTERN})(?:\s+\w+){{0,8}}\s*$"
)
REVERSE_ENGINEERING_RIGHT_BEFORE_PATTERN = (
    r"(?:\b(?:may|can)\s+"
    r"|\b(?:shall|will|is|are|be|remain(?:s)?)\s+(?:free|permitted|allowed|entitled)\s+to\s+"
    r"|\b(?:has|have)\s+(?:the\s+)?right\s+to\s+)$"
)
USAGE_RIGHT_AFTER_PATTERN = (
    r"^(?:\s+\w+){0,8}\s+\b(?:may|can|shall|will)\s+be\s+"
    r"(?:used|retained|disclosed|exploited|reverse\s+engineered)\b"
)
NEGATED_RIGHT_BEFORE_PATTERN = r"\b(?:must|shall|may|can|will)\s+not\b[^.;]{0,80}$|\b(?:not|never)\s+[^.;]{0,40}$"
GENERAL_BROAD_DEFINITION_PATTERN = (
    r"\b(?:any\s+and\s+all|all)\s+(?:non[-\s]?public\s+)?(?:information|materials?|data)\b"
    r"|\bnon[-\s]?public\s+(?:information|materials?|data)\b"
    r"|\b(?:information|materials?|data)\b.{0,120}\b(?:oral|written|electronic|visual)\b"
    r"|\b(?:oral|written|electronic|visual)\b.{0,120}\b(?:information|materials?|data)\b"
)


def _check_confidential_information(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    context_concepts = ["confidential_information_definition", "confidential_information_exclusion"]
    definition_name_terms, definition_coverage_terms = _confidential_definition_search_terms(clause)
    categories = _clause_terms(clause, "definition_categories")
    category_label = _confidential_categories_label(categories)
    definition_name_patterns = [_literal_word_pattern(term) for term in definition_name_terms]
    definition_paragraphs = merge_paragraphs(
        _paragraph_matches(paragraphs, definition_name_patterns),
        paragraphs_with_concepts(paragraphs, review_context, ["confidential_information_definition"]),
    )
    definition_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in definition_paragraphs))
    coverage_terms = _dedupe_terms(definition_coverage_terms + categories)
    coverage_hits = [term for term in coverage_terms if term in definition_normalized]
    broad_definition = bool(definition_paragraphs) and len(coverage_hits) >= 4
    broad_definition_needs_review = (
        bool(definition_paragraphs)
        and not broad_definition
        and _has_general_broad_definition_language(definition_normalized)
    )
    exclusion_analysis = _confidential_exclusion_analysis(
        paragraphs,
        _clause_term_patterns(clause, "exclusion_context_terms"),
        _clause_terms(clause, "problematic_exclusion_terms"),
        _clause_terms(clause, "independent_development_terms"),
        _clause_terms(clause, "independent_development_qualification_terms"),
    )

    analysis = _confidential_information_analysis(
        definition_paragraphs=definition_paragraphs,
        coverage_hits=coverage_hits,
        exclusion_analysis=exclusion_analysis,
    )
    explicit_exclusion_paragraphs = exclusion_analysis["explicit_exclusion_paragraphs"]
    usage_right_review_paragraphs = exclusion_analysis["usage_right_review_paragraphs"]

    if broad_definition and not explicit_exclusion_paragraphs and not usage_right_review_paragraphs:
        result = _match(
            clause,
            "Broad confidential information definition found with no extra exclusions detected.",
            definition_paragraphs,
        )
        _attach_confidential_information_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if broad_definition and usage_right_review_paragraphs and not explicit_exclusion_paragraphs:
        result = _review(
            clause,
            (
                "Broad confidential information definition found, but separate usage-right language "
                "may weaken confidentiality protections and needs human review."
            ),
            usage_right_review_paragraphs,
            what_to_verify=(
                "Confirm whether the usage-right language creates an extra residual-knowledge, "
                "reverse-engineering, or unqualified independent-development carve-out."
            ),
        )
        _attach_confidential_information_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if not broad_definition:
        if not definition_paragraphs:
            result = _not_present(
                clause,
                "No Confidential Information definition was found.",
                [],
                what_to_fix=(
                    "Add a broad Confidential Information definition "
                    f"covering non-public {category_label or 'required'} information."
                ),
            )
            _attach_confidential_information_analysis(result, analysis)
            return attach_structure_context(result, review_context, context_concepts)
        if explicit_exclusion_paragraphs:
            result = _check(
                clause,
                "The Confidential Information language includes exclusions beyond the allowed standard carve-outs.",
                explicit_exclusion_paragraphs,
                what_to_fix=(
                    "Remove residual knowledge, reverse-engineering, or unqualified independent-development exclusions "
                    "from Confidential Information."
                ),
            )
            _attach_confidential_information_analysis(result, analysis)
            return attach_structure_context(result, review_context, context_concepts)
        if broad_definition_needs_review:
            result = _review(
                clause,
                (
                    "A broad Confidential Information definition was found, but it does not clearly "
                    "cover enough required playbook categories."
                ),
                definition_paragraphs,
                what_to_verify=(
                    "Confirm whether the definition covers the required "
                    f"{category_label or 'playbook'} categories despite not listing them expressly."
                ),
            )
            _attach_confidential_information_analysis(result, analysis)
            return attach_structure_context(result, review_context, context_concepts)
        result = _check(
            clause,
            "The definition of Confidential Information is missing or too narrow.",
            definition_paragraphs,
            what_to_fix=(
                "Broaden the Confidential Information definition "
                f"to cover the required {category_label or 'playbook'} categories."
            ),
        )
        _attach_confidential_information_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    result = _check(
        clause,
        "The exclusions appear broader than the allowed standard carve-outs.",
        explicit_exclusion_paragraphs,
        what_to_fix=(
            "Remove residual knowledge, reverse-engineering, or unqualified independent-development exclusions "
            "from Confidential Information."
        ),
    )
    _attach_confidential_information_analysis(result, analysis)
    return attach_structure_context(result, review_context, context_concepts)

def _confidential_definition_search_terms(clause: Dict[str, object]) -> tuple[List[str], List[str]]:
    """Split confidential-information search terms by their playbook contract.

    For the `confidential_information` clause, `search_terms[0]` is the
    definition paragraph anchor. The remaining `search_terms[1:]` entries are
    coverage signals checked inside the anchored definition paragraphs.
    """
    search_terms = _clause_terms(clause, "search_terms")
    definition_aliases = ["proprietary information"]
    definition_terms = _dedupe_terms(search_terms[:1] + definition_aliases)
    return definition_terms, search_terms[1:]

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


def _has_general_broad_definition_language(definition_normalized: str) -> bool:
    return bool(re.search(GENERAL_BROAD_DEFINITION_PATTERN, definition_normalized))


def _confidential_exclusion_analysis(
    paragraphs: Iterable[Paragraph],
    exclusion_context_patterns: Iterable[str],
    problematic_terms: Iterable[str],
    independent_development_terms: Iterable[str],
    independent_development_qualification_terms: Iterable[str],
) -> Dict[str, List[Paragraph]]:
    exclusion_context_patterns = list(exclusion_context_patterns)
    problematic_patterns = [_literal_word_pattern(term) for term in problematic_terms]
    independent_development_patterns = [_literal_word_pattern(term) for term in independent_development_terms]
    qualification_patterns = [_literal_word_pattern(term) for term in independent_development_qualification_terms]
    explicit_exclusion_paragraphs: List[Paragraph] = []
    usage_right_review_paragraphs: List[Paragraph] = []

    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        paragraph_normalized = _normalize(paragraph_text)
        has_exclusion_context = any(re.search(pattern, paragraph_normalized) for pattern in exclusion_context_patterns)
        has_usage_right_context = _has_problematic_usage_right(
            paragraph_normalized,
            [*problematic_patterns, *independent_development_patterns],
        )
        if not has_exclusion_context and not has_usage_right_context:
            continue

        has_problematic_term = any(re.search(pattern, paragraph_normalized) for pattern in problematic_patterns)
        has_unqualified_independent_development = _has_unqualified_independent_development(
            paragraph_normalized,
            independent_development_patterns,
            qualification_patterns,
        )

        if not has_problematic_term and not has_unqualified_independent_development:
            continue

        if has_exclusion_context:
            explicit_exclusion_paragraphs.append(paragraph)
        elif has_usage_right_context:
            usage_right_review_paragraphs.append(paragraph)

    return {
        "explicit_exclusion_paragraphs": explicit_exclusion_paragraphs,
        "usage_right_review_paragraphs": usage_right_review_paragraphs,
    }


def _confidential_information_analysis(
    *,
    definition_paragraphs: List[Paragraph],
    coverage_hits: List[str],
    exclusion_analysis: Dict[str, List[Paragraph]],
) -> Dict[str, object]:
    return {
        "coverage_hits": coverage_hits,
        "coverage_hit_count": len(coverage_hits),
        "definition_paragraph_ids": _paragraph_ids(definition_paragraphs),
        "explicit_problematic_exclusion_paragraph_ids": _paragraph_ids(
            exclusion_analysis["explicit_exclusion_paragraphs"]
        ),
        "usage_right_review_paragraph_ids": _paragraph_ids(
            exclusion_analysis["usage_right_review_paragraphs"]
        ),
    }


def _attach_confidential_information_analysis(result: ClauseResult, analysis: Dict[str, object]) -> None:
    result["confidential_information_analysis"] = analysis


def _paragraph_ids(paragraphs: Iterable[Paragraph]) -> List[str]:
    return [
        str(paragraph.get("id"))
        for paragraph in paragraphs
        if paragraph.get("id")
    ]

def _has_problematic_usage_right(normalized_text: str, problematic_patterns: Iterable[str]) -> bool:
    for pattern in problematic_patterns:
        for match in re.finditer(pattern, normalized_text):
            before = _current_clause_prefix(normalized_text, match.start())
            after = _current_clause_suffix(normalized_text, match.end())
            if re.search(NEGATED_RIGHT_BEFORE_PATTERN, before):
                continue
            if (
                re.search(USAGE_RIGHT_BEFORE_PATTERN, before)
                or (
                    _pattern_matches_reverse_engineering(pattern)
                    and re.search(REVERSE_ENGINEERING_RIGHT_BEFORE_PATTERN, before)
                )
                or re.search(USAGE_RIGHT_AFTER_PATTERN, after)
            ):
                return True
    return False

def _current_clause_prefix(text: str, end: int) -> str:
    left = max(text.rfind(separator, 0, end) for separator in (".", ";"))
    return text[left + 1:end]

def _current_clause_suffix(text: str, start: int) -> str:
    right_candidates = [
        position
        for position in (text.find(separator, start) for separator in (".", ";"))
        if position != -1
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return text[start:right]

def _pattern_matches_reverse_engineering(pattern: str) -> bool:
    return "reverse" in pattern and "engineer" in pattern

def _has_unqualified_independent_development(
    normalized_text: str,
    independent_development_patterns: Iterable[str],
    qualification_patterns: Iterable[str],
) -> bool:
    qualification_patterns = list(qualification_patterns)
    for pattern in independent_development_patterns:
        for match in re.finditer(pattern, normalized_text):
            if not _independent_development_is_qualified(
                normalized_text,
                match.start(),
                match.end(),
                qualification_patterns,
            ):
                return True
    return False


def _independent_development_is_qualified(
    normalized_text: str,
    start: int,
    end: int,
    qualification_patterns: Iterable[str],
) -> bool:
    after_context = _independent_development_qualification_context_after(normalized_text, end)
    if any(re.search(qualification_pattern, after_context) for qualification_pattern in qualification_patterns):
        return True
    before_context = _independent_development_qualification_context_before(normalized_text, start)
    for qualification_pattern in qualification_patterns:
        matches = list(re.finditer(qualification_pattern, before_context))
        if not matches:
            continue
        trailing_context = before_context[matches[-1].end():]
        if not re.search(r",\s*(?:and|or)\b", trailing_context):
            return True
    return False


def _independent_development_qualification_context_after(normalized_text: str, start: int) -> str:
    window_end = min(len(normalized_text), start + INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
    context = normalized_text[start:window_end]
    boundary = re.search(r"[.;]|,\s+(?:and|or)\b", context)
    if boundary:
        return context[:boundary.start()]
    return context


def _independent_development_qualification_context_before(normalized_text: str, end: int) -> str:
    window_start = max(0, end - INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
    context = normalized_text[window_start:end]
    boundary_positions = [
        match.end()
        for match in re.finditer(r"[.;]|,\s+(?:and|or)\b", context)
    ]
    if boundary_positions:
        return context[boundary_positions[-1]:]
    return context


def reason_code(clause: Dict[str, object], decision: str) -> List[str]:
    semantic_code = _semantic_review_code(clause, decision)
    if semantic_code:
        return [semantic_code]
    if _has_ids(clause, "confidential_information_analysis", "explicit_problematic_exclusion_paragraph_ids"):
        return ["problematic_confidential_information_exclusion"]
    if _has_ids(clause, "confidential_information_analysis", "usage_right_review_paragraph_ids"):
        return ["usage_right_language_needs_review"]
    issue = _issue_type(clause)
    if issue == "missing":
        return ["missing_confidential_information_definition"]
    if issue == "present_but_wrong":
        return ["narrow_confidential_information_definition"]
    if decision == CLAUSE_DECISION_REVIEW:
        return ["broad_definition_needs_category_review"]
    if _has_ids(clause, "confidential_information_analysis", "definition_paragraph_ids"):
        return ["broad_confidential_information_definition"]
    return [_generic_reason_code(clause, decision)]
