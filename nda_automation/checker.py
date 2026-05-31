from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List

from .redline_actions import (
    REDLINE_ACTION_LABELS,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_PATH = ROOT / "playbook.json"
YEAR_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
YEAR_TERM_PATTERN = r"\b(?:(one|two|three|four|five|six|seven|eight|nine|ten)|(\d{1,2}))(?:\s*\(\s*(\d{1,2})\s*\))?(?:\s*-\s*|\s+)years?\b"
YEAR_TERM_EVIDENCE_PATTERN = r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})(?:\s*\(\s*\d{1,2}\s*\))?(?:\s*-\s*|\s+)years?\b"
INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW = 160
MAX_EVIDENCE_PARAGRAPHS = 3
ISSUE_TYPE_NONE = "none"
ISSUE_TYPE_MISSING = "missing"
ISSUE_TYPE_PRESENT_BUT_WRONG = "present_but_wrong"
ISSUE_TYPE_UNCLEAR = "unclear"
ISSUE_TYPE_LABELS = {
    ISSUE_TYPE_NONE: "No issue",
    ISSUE_TYPE_MISSING: "Missing",
    ISSUE_TYPE_PRESENT_BUT_WRONG: "Present but wrong",
    ISSUE_TYPE_UNCLEAR: "Unclear",
}
ClauseResult = Dict[str, object]
Paragraph = Dict[str, object]
RedlineEdit = Dict[str, object]
CheckFn = Callable[[str, str, Dict[str, object], List[Paragraph]], ClauseResult]
RedlineBuildFn = Callable[[ClauseResult, Dict[str, Paragraph], int], List[RedlineEdit]]


class ParagraphAlignmentError(ValueError):
    pass


class PlaybookTemplateError(ValueError):
    pass


def load_playbook() -> Dict[str, object]:
    with PLAYBOOK_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def review_nda(text: str, paragraphs: List[Paragraph] | None = None) -> Dict[str, object]:
    source_text = text or ""
    if paragraphs is None:
        document_paragraphs = split_document_paragraphs(source_text)
    else:
        if not source_text:
            source_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
        document_paragraphs = align_document_paragraphs(paragraphs, source_text)

    normalized = _normalize(source_text)
    playbook = load_playbook()
    _validate_playbook_contract(playbook)
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}

    clause_results = [
        check(source_text, normalized, clauses_by_id[clause_id], document_paragraphs)
        for clause_id, check in CLAUSE_CHECKS
    ]
    failed = [clause for clause in clause_results if not clause["passes"]]
    redline_edits = _build_redline_edits(clause_results, document_paragraphs)

    result = {
        "overall_status": "does_not_meet_requirements" if failed else "meets_requirements",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requirements_passed": len(clause_results) - len(failed),
        "requirements_failed": len(failed),
        "paragraphs": document_paragraphs,
        "clauses": clause_results,
        "redline_edits": redline_edits,
    }
    evidence_errors = validate_clause_evidence_trust(result, source_text)
    if evidence_errors:
        raise RuntimeError("Clause evidence provenance drift: " + "; ".join(evidence_errors))
    return result


def split_document_paragraphs(text: str) -> List[Paragraph]:
    source_text = text or ""
    has_blank_line_breaks = re.search(r"\n\s*\n", source_text) is not None
    separator = re.compile(r"\n\s*\n" if has_blank_line_breaks else r"\n+")
    paragraphs: List[Paragraph] = []
    cursor = 0

    for match in separator.finditer(source_text):
        _add_paragraph(paragraphs, source_text, cursor, match.start())
        cursor = match.end()

    _add_paragraph(paragraphs, source_text, cursor, len(source_text))
    return paragraphs


def align_document_paragraphs(paragraphs: List[Paragraph], source_text: str) -> List[Paragraph]:
    """Align extracted paragraphs and assign stable review IDs.

    `source_index` is the extractor's original paragraph ordinal and is preserved
    when supplied. `id`/`index` are generated as contiguous 1-based review
    ordinals after blank paragraphs are skipped; redlines target those review
    IDs and carry `source_index` only as provenance.
    """
    aligned: List[Paragraph] = []
    cursor = 0
    for paragraph in paragraphs:
        paragraph_text = str(paragraph.get("text", "")).strip()
        if not paragraph_text:
            continue

        start = source_text.find(paragraph_text, cursor)
        if start == -1:
            source_index = paragraph.get("source_index")
            paragraph_label = f"source_index {source_index}" if source_index is not None else f"position {len(aligned) + 1}"
            raise ParagraphAlignmentError(f"Could not align paragraph {paragraph_label} to source text.")
        end = start + len(paragraph_text)
        cursor = end

        index = len(aligned) + 1
        aligned_paragraph: Paragraph = {
            "id": f"p{index}",
            "index": index,
            "text": paragraph_text,
            "start": start,
            "end": end,
        }
        if "source_index" in paragraph:
            aligned_paragraph["source_index"] = paragraph["source_index"]
        aligned.append(aligned_paragraph)
    return aligned


def _add_paragraph(paragraphs: List[Paragraph], text: str, start: int, end: int) -> None:
    raw = text[start:end]
    paragraph_text = raw.strip()
    if not paragraph_text:
        return

    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    index = len(paragraphs) + 1
    paragraphs.append({
        "id": f"p{index}",
        "index": index,
        "text": paragraph_text,
        "start": start + leading,
        "end": end - trailing,
    })


def _check_mutuality(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    search_patterns = _clause_term_patterns(clause, "search_terms")
    one_way_patterns = _clause_term_patterns(clause, "one_way_terms")
    mutual_paragraphs = _paragraph_matches(paragraphs, search_patterns)
    separated_role_paragraphs = _mutual_role_paragraphs(normalized, clause, paragraphs)
    one_way_paragraphs = _paragraph_matches(paragraphs, one_way_patterns)

    if (mutual_paragraphs or separated_role_paragraphs) and not one_way_paragraphs:
        return _match(
            clause,
            "Mutual obligation language found.",
            mutual_paragraphs + separated_role_paragraphs,
        )
    if one_way_paragraphs:
        return _check(
            clause,
            "One-way or unilateral confidentiality language needs review.",
            one_way_paragraphs,
            what_to_fix="Revise the NDA so both parties are bound as both Disclosing Party and Receiving Party.",
        )
    return _not_present(
        clause,
        "The text does not clearly create mutual confidentiality obligations.",
        [],
        what_to_fix="Add mutual confidentiality language that binds both parties symmetrically.",
    )


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


def _check_confidential_information(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    definition_name_terms, definition_coverage_terms = _confidential_definition_search_terms(clause)
    categories = _clause_terms(clause, "definition_categories")
    category_label = _confidential_categories_label(categories)
    definition_name_patterns = [_literal_word_pattern(term) for term in definition_name_terms]
    definition_paragraphs = _paragraph_matches(paragraphs, definition_name_patterns)
    definition_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in definition_paragraphs))
    coverage_terms = _dedupe_terms(definition_coverage_terms + categories)
    coverage_hits = [term for term in coverage_terms if term in definition_normalized]
    broad_definition = bool(definition_paragraphs) and len(coverage_hits) >= 4
    exclusion_paragraphs = _paragraph_matches(paragraphs, _clause_term_patterns(clause, "exclusion_context_terms"))
    problematic_exclusion_paragraphs = _problematic_confidential_exclusion_paragraphs(
        exclusion_paragraphs,
        _clause_terms(clause, "problematic_exclusion_terms"),
        _clause_terms(clause, "independent_development_terms"),
        _clause_terms(clause, "independent_development_qualification_terms"),
    )

    if broad_definition and not problematic_exclusion_paragraphs:
        return _match(
            clause,
            "Broad confidential information definition found with no extra exclusions detected.",
            definition_paragraphs,
        )

    if not broad_definition:
        if not definition_paragraphs:
            return _not_present(
                clause,
                "No Confidential Information definition was found.",
                [],
                what_to_fix=(
                    "Add a broad Confidential Information definition "
                    f"covering non-public {category_label or 'required'} information."
                ),
            )
        return _check(
            clause,
            "The definition of Confidential Information is missing or too narrow.",
            definition_paragraphs,
            what_to_fix=(
                "Broaden the Confidential Information definition "
                f"to cover the required {category_label or 'playbook'} categories."
            ),
        )
    else:
        return _check(
            clause,
            "The exclusions appear broader than the allowed standard carve-outs.",
            problematic_exclusion_paragraphs,
            what_to_fix=(
                "Remove residual knowledge, reverse-engineering, or unqualified independent-development exclusions "
                "from Confidential Information."
            ),
        )


def _confidential_definition_search_terms(clause: Dict[str, object]) -> tuple[List[str], List[str]]:
    """Split confidential-information search terms by their playbook contract.

    For the `confidential_information` clause, `search_terms[0]` is the
    definition paragraph anchor. The remaining `search_terms[1:]` entries are
    coverage signals checked inside the anchored definition paragraphs.
    """
    search_terms = _clause_terms(clause, "search_terms")
    return search_terms[:1], search_terms[1:]


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


def _check_term_and_survival(_text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    max_years = _max_term_years(clause)
    cap_label = _year_count_label(max_years)
    term_context_patterns = _term_context_patterns(clause)
    indefinite_patterns = _clause_term_patterns(clause, "indefinite_terms")
    term_paragraphs = _paragraph_matches(paragraphs, term_context_patterns)
    term_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in term_paragraphs))
    year_terms = _extract_year_terms(term_normalized)
    has_term_within_cap = any(1 <= years <= max_years for years in year_terms)
    has_term_over_cap = any(years > max_years for years in year_terms)
    ordinary_indefinite_term = any(re.search(pattern, term_normalized) for pattern in indefinite_patterns)

    if has_term_over_cap:
        return _check(
            clause,
            f"A term or survival period exceeds the cap of {cap_label}.",
            _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN]),
            what_to_fix=(
                "Reduce the ordinary confidentiality term or survival period "
                f"to a fixed period of {cap_label} or less."
            ),
        )
    if ordinary_indefinite_term:
        return _check(
            clause,
            f"Ordinary confidentiality appears indefinite rather than capped at {cap_label}.",
            _paragraph_matches(term_paragraphs, indefinite_patterns),
            what_to_fix=(
                "Replace indefinite ordinary confidentiality language "
                f"with a fixed period of {cap_label} or less."
            ),
        )
    if has_term_within_cap:
        return _match(
            clause,
            f"Term or survival period is within the cap of {cap_label}.",
            _paragraph_matches(term_paragraphs, [YEAR_TERM_EVIDENCE_PATTERN]),
        )
    return _not_present(
        clause,
        f"No fixed term or survival period of up to {cap_label} was found.",
        term_paragraphs,
        what_to_fix=f"Add a fixed term or ordinary confidentiality survival period of {cap_label} or less.",
    )


def _extract_year_terms(normalized: str) -> List[int]:
    terms: List[int] = []
    for match in re.finditer(YEAR_TERM_PATTERN, normalized):
        word_value, digit_value, parenthetical_value = match.groups()
        if parenthetical_value:
            terms.append(int(parenthetical_value))
        elif digit_value:
            terms.append(int(digit_value))
        elif word_value:
            terms.append(YEAR_WORDS[word_value])
    return terms


def _year_count_label(years: int) -> str:
    number_label = next((word for word, value in YEAR_WORDS.items() if value == years), str(years))
    unit = "year" if years == 1 else "years"
    return f"{number_label} {unit}"


def _max_term_years(clause: Dict[str, object]) -> int:
    return int(clause.get("max_term_years", clause.get("term_years", 5)))


def _approved_laws(clause: Dict[str, object]) -> List[str]:
    return [str(law).strip() for law in clause.get("approved_laws", []) if str(law).strip()]


def _approved_laws_label(clause: Dict[str, object]) -> str:
    approved_laws = _approved_laws(clause)
    return _join_with_or(approved_laws)


def _governing_law_phrase(clause: Dict[str, object], law: str) -> str:
    law_phrases = clause.get("law_phrases", {})
    if isinstance(law_phrases, dict):
        phrase = str(law_phrases.get(law, "")).strip()
        if phrase:
            return phrase
    return law


def _confidential_categories_label(categories: Iterable[str]) -> str:
    return _join_with_and([str(category).strip() for category in categories if str(category).strip()])


def _join_with_or(values: List[str]) -> str:
    return _join_with_conjunction(values, "or")


def _join_with_and(values: List[str]) -> str:
    return _join_with_conjunction(values, "and")


def _join_with_conjunction(values: List[str], conjunction: str) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} {conjunction} {values[1]}"
    return ", ".join(values[:-1]) + f", {conjunction} {values[-1]}"


def _governing_law_change_fix(clause: Dict[str, object]) -> str:
    approved_law_label = _approved_laws_label(clause)
    if not approved_law_label:
        return "Change the governing law to an approved law."
    return f"Change the governing law to {approved_law_label}."


def _governing_law_missing_fix(clause: Dict[str, object]) -> str:
    approved_law_label = _approved_laws_label(clause)
    if not approved_law_label:
        return "Add a governing law clause using an approved law."
    return f"Add a governing law clause using {approved_law_label}."


def _clause_terms(clause: Dict[str, object], field: str) -> List[str]:
    values = clause.get(field, [])
    if not isinstance(values, list):
        return []
    return [str(term).lower().strip() for term in values if str(term).strip()]


def _dedupe_terms(terms: Iterable[str]) -> List[str]:
    deduped: List[str] = []
    seen = set()
    for term in terms:
        cleaned = str(term).lower().strip()
        if not cleaned or cleaned in seen:
            continue
        deduped.append(cleaned)
        seen.add(cleaned)
    return deduped


def _clause_term_patterns(clause: Dict[str, object], field: str) -> List[str]:
    return [_literal_word_pattern(term) for term in _clause_terms(clause, field)]


def _governing_anchor_patterns(clause: Dict[str, object]) -> List[str]:
    approved_laws = {law.lower() for law in _approved_laws(clause)}
    anchor_terms = [
        term
        for term in _clause_terms(clause, "search_terms")
        if term not in approved_laws
    ]
    return [_literal_word_pattern(term) for term in anchor_terms]


def _term_context_patterns(clause: Dict[str, object]) -> List[str]:
    patterns = []
    for term in _dedupe_terms(_clause_terms(clause, "search_terms") + _clause_terms(clause, "indefinite_terms")):
        if term in {"year", "years"}:
            continue
        if term.startswith("surviv"):
            patterns.append(r"\bsurviv(?:e|es|ed|ing|al)\b")
        else:
            patterns.append(_literal_word_pattern(term))
    return patterns


def _signature_evidence_patterns(clause: Dict[str, object]) -> List[str]:
    return [
        _literal_word_pattern(term)
        for term in _clause_terms(clause, "search_terms")
        if term not in {"signature", "signatures"}
    ]


def _signature_marker_patterns(clause: Dict[str, object], marker: str) -> List[str]:
    marker_aliases = {
        "party": ["by", "for"],
        "title": ["title", "role", "capacity"],
        "date": ["date", "dated"],
    }
    aliases = marker_aliases.get(marker, [marker])
    marker_terms = [
        term
        for term in _clause_terms(clause, "search_terms")
        if any(alias in term.replace(" ", "") for alias in aliases)
    ]
    return [_literal_word_pattern(term) for term in marker_terms]


def _count_pattern_matches(patterns: Iterable[str], text: str) -> int:
    return sum(len(re.findall(pattern, text, flags=re.IGNORECASE)) for pattern in patterns)


def _clause_template_text(
    clause: Dict[str, object],
    field: str,
    context: Dict[str, object] | None = None,
) -> str:
    template = str(clause.get(field, "")).strip()
    if not template:
        return ""
    try:
        return template.format(**(context or {}))
    except (IndexError, KeyError, ValueError) as error:
        clause_id = str(clause.get("id", "unknown"))
        raise PlaybookTemplateError(f"Invalid {field} template for clause {clause_id}: {error}") from error


def _problematic_confidential_exclusion_paragraphs(
    exclusion_paragraphs: Iterable[Paragraph],
    problematic_terms: Iterable[str],
    independent_development_terms: Iterable[str],
    independent_development_qualification_terms: Iterable[str],
) -> List[Paragraph]:
    problematic_patterns = [_literal_word_pattern(term) for term in problematic_terms]
    independent_development_patterns = [_literal_word_pattern(term) for term in independent_development_terms]
    qualification_patterns = [_literal_word_pattern(term) for term in independent_development_qualification_terms]
    matches: List[Paragraph] = []

    for paragraph in exclusion_paragraphs:
        paragraph_text = str(paragraph["text"])
        paragraph_normalized = _normalize(paragraph_text)
        has_problematic_term = any(re.search(pattern, paragraph_normalized) for pattern in problematic_patterns)
        has_unqualified_independent_development = _has_unqualified_independent_development(
            paragraph_normalized,
            independent_development_patterns,
            qualification_patterns,
        )

        if not has_problematic_term and not has_unqualified_independent_development:
            continue

        matches.append(paragraph)

    return matches


def _has_unqualified_independent_development(
    normalized_text: str,
    independent_development_patterns: Iterable[str],
    qualification_patterns: Iterable[str],
) -> bool:
    qualification_patterns = list(qualification_patterns)
    for pattern in independent_development_patterns:
        for match in re.finditer(pattern, normalized_text):
            window_start = max(0, match.start() - INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
            window_end = min(len(normalized_text), match.end() + INDEPENDENT_DEVELOPMENT_QUALIFICATION_WINDOW)
            context = normalized_text[window_start:window_end]
            if not any(re.search(qualification_pattern, context) for qualification_pattern in qualification_patterns):
                return True
    return False


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


CLAUSE_CHECKS: List[tuple[str, CheckFn]] = [
    ("mutuality", _check_mutuality),
    ("confidential_information", _check_confidential_information),
    ("governing_law", _check_governing_law),
    ("term_and_survival", _check_term_and_survival),
    ("non_circumvention", _check_non_circumvention),
    ("signatures", _check_signatures),
]


def _validate_check_registry() -> None:
    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    duplicate_check_ids = sorted({clause_id for clause_id in check_ids if check_ids.count(clause_id) > 1})
    if duplicate_check_ids:
        raise RuntimeError(f"Duplicate checker IDs: {', '.join(duplicate_check_ids)}")

    playbook_clauses = load_playbook()["clauses"]
    playbook_ids = [str(clause["id"]) for clause in playbook_clauses]
    duplicate_playbook_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_playbook_ids:
        raise RuntimeError(f"Duplicate playbook IDs: {', '.join(duplicate_playbook_ids)}")

    missing_search_terms = [
        str(clause["id"])
        for clause in playbook_clauses
        if not _clause_terms(clause, "search_terms")
    ]
    if missing_search_terms:
        raise RuntimeError(f"Playbook clauses missing search_terms: {', '.join(missing_search_terms)}")

    missing_checks = sorted(set(playbook_ids) - set(check_ids))
    extra_checks = sorted(set(check_ids) - set(playbook_ids))
    if missing_checks or extra_checks:
        detail = []
        if missing_checks:
            detail.append(f"missing checks for: {', '.join(missing_checks)}")
        if extra_checks:
            detail.append(f"checks without playbook clauses: {', '.join(extra_checks)}")
        raise RuntimeError("Checker registry does not match playbook (" + "; ".join(detail) + ")")

    builder_ids = [clause_id for clause_id, _builder in REDLINE_BUILDERS]
    duplicate_builder_ids = sorted({clause_id for clause_id in builder_ids if builder_ids.count(clause_id) > 1})
    if duplicate_builder_ids:
        raise RuntimeError(f"Duplicate redline builder IDs: {', '.join(duplicate_builder_ids)}")

    if builder_ids != check_ids:
        missing_builders = sorted(set(check_ids) - set(builder_ids))
        extra_builders = sorted(set(builder_ids) - set(check_ids))
        detail = []
        if missing_builders:
            detail.append(f"missing redline builders for: {', '.join(missing_builders)}")
        if extra_builders:
            detail.append(f"redline builders without checks: {', '.join(extra_builders)}")
        if not detail:
            detail.append("redline builder order differs from checker order")
        raise RuntimeError("Redline registry does not mirror checker registry (" + "; ".join(detail) + ")")


def _validate_playbook_contract(playbook: Dict[str, object]) -> None:
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        raise PlaybookTemplateError("Playbook clauses must be a list.")

    playbook_ids = []
    for clause in clauses:
        if not isinstance(clause, dict):
            raise PlaybookTemplateError("Each playbook clause must be an object.")
        clause_id = str(clause.get("id", "")).strip()
        if not clause_id:
            raise PlaybookTemplateError("Each playbook clause must include an id.")
        playbook_ids.append(clause_id)
        for field in ["name", "requirement", "type"]:
            if not isinstance(clause.get(field), str) or not str(clause.get(field)).strip():
                raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")
        if clause["type"] not in {"required", "prohibited"}:
            raise PlaybookTemplateError(f"Playbook clause {clause_id} has invalid type.")
        if not _clause_terms(clause, "search_terms"):
            raise PlaybookTemplateError(f"Playbook clause {clause_id} must include search_terms.")

    duplicate_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_ids:
        raise PlaybookTemplateError(f"Duplicate playbook IDs: {', '.join(duplicate_ids)}")

    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    missing_playbook_ids = sorted(set(check_ids) - set(playbook_ids))
    extra_playbook_ids = sorted(set(playbook_ids) - set(check_ids))
    if missing_playbook_ids or extra_playbook_ids:
        detail = []
        if missing_playbook_ids:
            detail.append(f"missing clauses: {', '.join(missing_playbook_ids)}")
        if extra_playbook_ids:
            detail.append(f"unknown clauses: {', '.join(extra_playbook_ids)}")
        raise PlaybookTemplateError("Playbook clause IDs do not match checker IDs (" + "; ".join(detail) + ")")

    clauses_by_id = {str(clause["id"]): clause for clause in clauses}
    _validate_governing_law_playbook(clauses_by_id["governing_law"])
    _require_template(clauses_by_id["term_and_survival"], "redline_template")
    _require_template(clauses_by_id["signatures"], "redline_template")


def _validate_governing_law_playbook(clause: Dict[str, object]) -> None:
    approved_laws = _approved_laws(clause)
    if not approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law must include approved_laws.")
    preferred_law = str(clause.get("preferred_law", "")).strip()
    if preferred_law and preferred_law not in approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law preferred_law must be approved.")
    law_phrases = clause.get("law_phrases", {})
    if not isinstance(law_phrases, dict):
        raise PlaybookTemplateError("Playbook clause governing_law law_phrases must be an object.")
    missing_phrases = [law for law in approved_laws if not str(law_phrases.get(law, "")).strip()]
    if missing_phrases:
        raise PlaybookTemplateError(
            "Playbook clause governing_law law_phrases missing: " + ", ".join(missing_phrases)
        )


def _require_template(clause: Dict[str, object], field: str) -> None:
    clause_id = str(clause.get("id", "unknown"))
    if not isinstance(clause.get(field), str) or not str(clause.get(field)).strip():
        raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")


def _match(clause: Dict[str, object], reason: str, matched_paragraphs: Iterable[Paragraph]) -> ClauseResult:
    return _result(clause, "match", reason, matched_paragraphs, issue_type=ISSUE_TYPE_NONE, what_to_fix="No change needed.")


def _check(
    clause: Dict[str, object],
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    issue_type: str = ISSUE_TYPE_PRESENT_BUT_WRONG,
    what_to_fix: str = "Revise this clause so it satisfies the requirement.",
) -> ClauseResult:
    return _result(clause, "check", reason, matched_paragraphs, issue_type=issue_type, what_to_fix=what_to_fix)


def _not_present(
    clause: Dict[str, object],
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    what_to_fix: str | None = None,
) -> ClauseResult:
    passes = _status_passes_clause_type("not_present", clause)
    issue_type = ISSUE_TYPE_NONE if passes else ISSUE_TYPE_MISSING
    fallback_fix = "No change needed." if passes else "Add language that satisfies this requirement."
    return _result(
        clause,
        "not_present",
        reason,
        matched_paragraphs,
        issue_type=issue_type,
        what_to_fix=what_to_fix or fallback_fix,
    )


def _result(
    clause: Dict[str, object],
    status: str,
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    issue_type: str,
    what_to_fix: str,
) -> ClauseResult:
    paragraph_matches = _select_evidence_paragraphs(matched_paragraphs)
    matched_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraph_matches)
    evidence_paragraphs = [_evidence_paragraph(paragraph) for paragraph in paragraph_matches]
    passes = _status_passes_clause_type(status, clause)
    result = {
        "id": clause["id"],
        "name": clause["name"],
        "requirement": clause["requirement"],
        "status": status,
        "passes": passes,
        "issue_type": issue_type,
        "issue_label": ISSUE_TYPE_LABELS.get(issue_type, "Needs review"),
        "what_to_fix": what_to_fix,
        "reason": reason,
        "finding": reason,
        "matched_paragraph_ids": [paragraph["id"] for paragraph in paragraph_matches],
        "matched_text": matched_text,
        "evidence": [paragraph["text"] for paragraph in paragraph_matches],
        "evidence_paragraphs": evidence_paragraphs,
    }
    for field in [
        "acceptable_language",
        "approved_laws",
        "law_phrases",
        "max_term_years",
        "one_way_terms",
        "preferred_law",
        "redline_template",
        "exclusion_context_terms",
        "indefinite_terms",
        "term_years",
        "type",
    ]:
        if field in clause:
            result[field] = clause[field]
    return result


def validate_clause_evidence_trust(review_result: Dict[str, object], source_text: str | None = None) -> List[str]:
    errors: List[str] = []
    paragraphs = review_result.get("paragraphs", [])
    clauses = review_result.get("clauses", [])
    if not isinstance(paragraphs, list):
        return ["review result paragraphs must be a list"]
    if not isinstance(clauses, list):
        return ["review result clauses must be a list"]

    paragraphs_by_id = {
        str(paragraph.get("id")): paragraph
        for paragraph in paragraphs
        if isinstance(paragraph, dict) and paragraph.get("id") is not None
    }
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            errors.append("review paragraph is not an object")
            continue
        paragraph_id = str(paragraph.get("id", "unknown"))
        start = paragraph.get("start")
        end = paragraph.get("end")
        text = str(paragraph.get("text", ""))
        if isinstance(start, int) and isinstance(end, int) and source_text is not None and source_text[start:end] != text:
            errors.append(f"{paragraph_id}: paragraph offsets do not resolve to paragraph text")
        index = paragraph.get("index")
        if isinstance(index, int) and 1 <= index <= len(paragraphs):
            indexed_paragraph = paragraphs[index - 1]
            if isinstance(indexed_paragraph, dict) and indexed_paragraph.get("id") != paragraph.get("id"):
                errors.append(f"{paragraph_id}: paragraph index points to {indexed_paragraph.get('id')}")

    for clause in clauses:
        if not isinstance(clause, dict):
            errors.append("clause result is not an object")
            continue
        clause_id = str(clause.get("id", "unknown"))
        matched_ids = clause.get("matched_paragraph_ids", [])
        evidence = clause.get("evidence", [])
        evidence_paragraphs = clause.get("evidence_paragraphs", [])
        if not isinstance(matched_ids, list):
            errors.append(f"{clause_id}: matched_paragraph_ids must be a list")
            continue
        if not isinstance(evidence, list):
            errors.append(f"{clause_id}: evidence must be a list")
            evidence = []
        if not isinstance(evidence_paragraphs, list):
            errors.append(f"{clause_id}: evidence_paragraphs must be a list")
            evidence_paragraphs = []

        expected_paragraphs = []
        for paragraph_id in matched_ids:
            paragraph = paragraphs_by_id.get(str(paragraph_id))
            if paragraph is None:
                errors.append(f"{clause_id}: matched paragraph {paragraph_id} is not in reviewed source")
                continue
            expected_paragraphs.append(paragraph)

        expected_texts = [str(paragraph.get("text", "")) for paragraph in expected_paragraphs]
        expected_text = "\n\n".join(expected_texts)
        if clause.get("matched_text", "") != expected_text:
            errors.append(f"{clause_id}: matched_text does not equal matched source paragraphs")
        if evidence != expected_texts:
            errors.append(f"{clause_id}: evidence text does not equal matched source paragraphs")
        if [str(item.get("id")) for item in evidence_paragraphs if isinstance(item, dict)] != [str(paragraph.get("id")) for paragraph in expected_paragraphs]:
            errors.append(f"{clause_id}: evidence_paragraphs ids do not equal matched_paragraph_ids")
        for evidence_paragraph, source_paragraph in zip(evidence_paragraphs, expected_paragraphs):
            if not isinstance(evidence_paragraph, dict):
                errors.append(f"{clause_id}: evidence_paragraph is not an object")
                continue
            for key in ["id", "index", "text", "start", "end", "source_index"]:
                if key in source_paragraph and evidence_paragraph.get(key) != source_paragraph.get(key):
                    errors.append(f"{clause_id}: evidence paragraph {source_paragraph.get('id')} has drifted {key}")
                elif key not in source_paragraph and key in evidence_paragraph:
                    errors.append(f"{clause_id}: evidence paragraph {source_paragraph.get('id')} has unexpected {key}")

    return errors


def _evidence_paragraph(paragraph: Paragraph) -> Paragraph:
    evidence = {
        "id": paragraph["id"],
        "index": paragraph["index"],
        "text": paragraph["text"],
        "start": paragraph["start"],
        "end": paragraph["end"],
    }
    if "source_index" in paragraph:
        evidence["source_index"] = paragraph["source_index"]
    return evidence


def _select_evidence_paragraphs(matched_paragraphs: Iterable[Paragraph]) -> List[Paragraph]:
    selected: List[Paragraph] = []
    seen = set()

    for paragraph in matched_paragraphs:
        dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        if dedup_key in seen:
            continue

        selected.append(paragraph)
        seen.add(dedup_key)
        if len(selected) == MAX_EVIDENCE_PARAGRAPHS:
            break

    return selected


def _build_redline_edits(clause_results: List[ClauseResult], paragraphs: List[Paragraph]) -> List[RedlineEdit]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    edits: List[RedlineEdit] = []

    for clause in clause_results:
        edits.extend(_redline_edits_for_clause(clause, paragraphs_by_id, len(edits) + 1))

    return edits


def _redline_edits_for_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    builder = REDLINE_BUILDERS_BY_ID[str(clause["id"])]
    return builder(clause, paragraphs_by_id, start_number)


def _is_present_but_wrong_check(clause: ClauseResult) -> bool:
    return clause.get("status") == "check" and clause.get("issue_type") == ISSUE_TYPE_PRESENT_BUT_WRONG


def _is_missing_required_check(clause: ClauseResult) -> bool:
    return (
        clause.get("status") == "not_present"
        and clause.get("issue_type") == ISSUE_TYPE_MISSING
        and not clause.get("passes")
    )


def _matched_redline_paragraphs(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> List[Paragraph]:
    paragraph_ids = clause.get("matched_paragraph_ids", [])
    if not isinstance(paragraph_ids, list):
        return []
    return [
        paragraph
        for paragraph_id in paragraph_ids
        if (paragraph := paragraphs_by_id.get(str(paragraph_id))) is not None
    ]


def _insertion_anchor_paragraph(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> Paragraph | None:
    matched_paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if matched_paragraphs:
        return matched_paragraphs[-1]
    if not paragraphs_by_id:
        return None
    return max(paragraphs_by_id.values(), key=lambda paragraph: int(paragraph.get("index", 0)))


def _redline_edit(
    edit_number: int,
    clause: ClauseResult,
    paragraph: Paragraph,
    action: str,
    replacement_text: str = "",
    insert_text: str = "",
    template_options: List[Dict[str, object]] | None = None,
) -> RedlineEdit:
    proposed_text = insert_text or replacement_text
    edit = {
        "id": f"r{edit_number}",
        "clause_id": clause["id"],
        "clause_name": clause["name"],
        "paragraph_id": paragraph["id"],
        "paragraph_index": paragraph.get("index"),
        "action": action,
        "action_label": REDLINE_ACTION_LABELS.get(action, "Proposed edit"),
        "status": "proposed",
        "original_text": "" if action == REDLINE_INSERT_AFTER_PARAGRAPH else paragraph["text"],
        "replacement_text": proposed_text,
        "reason": clause.get("what_to_fix") or clause.get("reason"),
    }
    if "source_index" in paragraph:
        edit["source_index"] = paragraph["source_index"]
    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        edit["target_position"] = "after_paragraph"
        edit["anchor_text"] = paragraph["text"]
        edit["insert_text"] = proposed_text
    if template_options:
        edit["template_options"] = template_options
        selected_option = next((option for option in template_options if option.get("selected")), template_options[0])
        edit["selected_template_id"] = selected_option.get("id")
    return edit


def _governing_law_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    template_options = _governing_law_template_options(clause)
    if not template_options:
        return None

    selected_template = _selected_template_option(template_options)

    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=str(selected_template["text"]),
            template_options=template_options,
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=str(selected_template["text"]),
        template_options=template_options,
    )


def _governing_law_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _governing_law_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _term_and_survival_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=_term_and_survival_replacement_text(clause),
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=_term_and_survival_replacement_text(clause),
    )


def _term_and_survival_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _term_and_survival_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _non_circumvention_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    if not _is_present_but_wrong_check(clause):
        return []

    edits: List[RedlineEdit] = []
    for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id):
        edits.append(
            _redline_edit(
                start_number + len(edits),
                clause,
                paragraph,
                REDLINE_DELETE_PARAGRAPH,
            )
        )
    return edits


def _signatures_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if not _is_missing_required_check(clause):
        return None

    anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
    if not anchor:
        return None

    return _redline_edit(
        edit_number,
        clause,
        anchor,
        REDLINE_INSERT_AFTER_PARAGRAPH,
        insert_text=_signature_block_template(clause),
    )


def _signatures_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _signatures_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _no_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    return []


REDLINE_BUILDERS: List[tuple[str, RedlineBuildFn]] = [
    # Every checked clause must declare its redline behavior. Use
    # _no_redlines when the absence of a proposed edit is intentional.
    ("mutuality", _no_redlines),
    ("confidential_information", _no_redlines),
    ("governing_law", _governing_law_redlines),
    ("term_and_survival", _term_and_survival_redlines),
    ("non_circumvention", _non_circumvention_redlines),
    ("signatures", _signatures_redlines),
]
REDLINE_BUILDERS_BY_ID: Dict[str, RedlineBuildFn] = dict(REDLINE_BUILDERS)


_validate_check_registry()


def _preferred_governing_law(clause: ClauseResult) -> str | None:
    approved_laws = _approved_laws(clause)
    preferred_law = str(clause.get("preferred_law", "")).strip()

    if preferred_law and (not approved_laws or preferred_law in approved_laws):
        return preferred_law
    if approved_laws:
        return approved_laws[0]
    return None


def _selected_template_option(template_options: List[Dict[str, object]]) -> Dict[str, object]:
    return next((option for option in template_options if option.get("selected")), template_options[0])


def _governing_law_template_options(clause: ClauseResult) -> List[Dict[str, object]]:
    approved_laws = _approved_laws(clause)
    preferred_law = _preferred_governing_law(clause)
    if not approved_laws:
        return []

    return [
        {
            "id": f"governing_law_{_template_slug(law)}",
            "label": law,
            "text": _governing_law_replacement_text(clause, law),
            "replacement_text": _governing_law_replacement_text(clause, law),
            "insert_text": _governing_law_replacement_text(clause, law),
            "selected": law == preferred_law,
        }
        for law in approved_laws
    ]


def _template_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _governing_law_replacement_text(clause: ClauseResult, law: str) -> str:
    law_label = law.strip()
    law_phrase = _governing_law_phrase(clause, law_label)
    return f"This Agreement shall be governed by the laws of {law_phrase}."


def _term_and_survival_replacement_text(clause: ClauseResult) -> str:
    cap_label = _year_count_label(_max_term_years(clause))
    return _clause_template_text(
        clause,
        "redline_template",
        {
            "max_term_years": _max_term_years(clause),
            "max_term_years_label": cap_label,
        },
    )


def _signature_block_template(clause: ClauseResult) -> str:
    return _clause_template_text(clause, "redline_template")


def _normalize(text: str) -> str:
    lowered = text.lower()
    return re.sub(r"\s+", " ", lowered).strip()


def _status_passes_clause_type(status: str, clause: Dict[str, object]) -> bool:
    clause_type = clause.get("type")
    if clause_type == "prohibited":
        return status == "not_present"
    return status == "match"


def _literal_word_pattern(value: str) -> str:
    term = value.lower().strip()
    words = re.escape(term).replace(r"\ ", r"\s+")
    prefix = r"\b" if term and term[0].isalnum() else ""
    suffix = r"\b" if term and term[-1].isalnum() else ""
    return rf"{prefix}{words}{suffix}"


def _paragraph_matches(paragraphs: Iterable[Paragraph], patterns: Iterable[str]) -> List[Paragraph]:
    matches: List[Paragraph] = []
    seen = set()
    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        for pattern in patterns:
            if not re.search(pattern, paragraph_text, flags=re.IGNORECASE):
                continue
            dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
            if dedup_key in seen:
                break
            matches.append(paragraph)
            seen.add(dedup_key)
            break
    return matches
