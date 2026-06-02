from __future__ import annotations

import re
from typing import Dict, Iterable, List

from ..review_document import Paragraph

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
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
}
YEAR_WORD_PATTERN = "|".join(YEAR_WORDS)
YEAR_TERM_PATTERN = rf"\b(?:({YEAR_WORD_PATTERN})|(\d{{1,3}}))(?:\s*\(\s*(?:(\d{{1,3}})|({YEAR_WORD_PATTERN}))\s*\))?(?:\s*-\s*|\s+)(months?|years?)\b"
YEAR_TERM_EVIDENCE_PATTERN = rf"\b(?:{YEAR_WORD_PATTERN}|\d{{1,3}})(?:\s*\(\s*(?:\d{{1,3}}|{YEAR_WORD_PATTERN})\s*\))?(?:\s*-\s*|\s+)(?:months?|years?)\b"
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
RedlineEdit = Dict[str, object]


class PlaybookTemplateError(ValueError):
    pass


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
    values = list(clause.get(field, [])) if isinstance(clause.get(field, []), list) else []
    if field == "search_terms" and isinstance(clause.get("semantic_signals"), list):
        values.extend(clause["semantic_signals"])
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
    paragraph_matches = _dedupe_matched_paragraphs(matched_paragraphs)
    evidence_matches = paragraph_matches[:MAX_EVIDENCE_PARAGRAPHS]
    matched_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraph_matches)
    evidence_paragraphs = [_evidence_paragraph(paragraph) for paragraph in evidence_matches]
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
        "evidence": [paragraph["text"] for paragraph in evidence_matches],
        "evidence_paragraphs": evidence_paragraphs,
    }
    for field in [
        "acceptable_language",
        "allowed_exclusions",
        "approved_laws",
        "check_trigger",
        "law_phrases",
        "longer_survival_carve_out_terms",
        "max_term_years",
        "one_way_terms",
        "preferred_law",
        "preferred_position",
        "rationale",
        "redline_template",
        "evidence_guidance",
        "exclusion_context_terms",
        "indefinite_terms",
        "semantic_signals",
        "standard_exclusions_template",
        "taxonomy_groups",
        "term_years",
        "type",
    ]:
        if field in clause:
            result[field] = clause[field]
    return result

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
    if "source_part" in paragraph:
        evidence["source_part"] = paragraph["source_part"]
    return evidence

def _dedupe_matched_paragraphs(matched_paragraphs: Iterable[Paragraph]) -> List[Paragraph]:
    selected: List[Paragraph] = []
    seen = set()

    for paragraph in matched_paragraphs:
        dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
        if dedup_key in seen:
            continue

        selected.append(paragraph)
        seen.add(dedup_key)

    return selected

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
