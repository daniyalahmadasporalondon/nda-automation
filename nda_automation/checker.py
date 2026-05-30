from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List

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

ClauseResult = Dict[str, object]
Paragraph = Dict[str, object]
CheckFn = Callable[[str, str, Dict[str, object], List[Paragraph]], ClauseResult]


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
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}

    clause_results = [
        check(source_text, normalized, clauses_by_id[clause_id], document_paragraphs)
        for clause_id, check in CLAUSE_CHECKS
    ]
    failed = [clause for clause in clause_results if not clause["passes"]]

    return {
        "overall_status": "does_not_meet_requirements" if failed else "meets_requirements",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requirements_passed": len(clause_results) - len(failed),
        "requirements_failed": len(failed),
        "paragraphs": document_paragraphs,
        "clauses": clause_results,
    }


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
    aligned: List[Paragraph] = []
    cursor = 0
    for paragraph in paragraphs:
        paragraph_text = str(paragraph.get("text", "")).strip()
        if not paragraph_text:
            continue

        start = source_text.find(paragraph_text, cursor)
        if start == -1:
            start = cursor
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


def _check_mutuality(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    mutual_patterns = [
        r"\bmutual\s+(?:non[- ]disclosure|confidentiality|nda)\b",
        r"\beach party\b",
        r"\bboth parties\b",
        r"\bdisclosing party\b.*\breceiving party\b",
        r"\breceiving party\b.*\bdisclosing party\b",
    ]
    one_way_patterns = [
        r"\bone[- ]way\b",
        r"\bunilateral\b",
        r"\bonly the receiving party\b",
        r"\brecipient only\b",
    ]
    has_mutual_language = any(
        re.search(pattern, normalized)
        for pattern in mutual_patterns
    )
    one_way_language = any(
        re.search(pattern, normalized)
        for pattern in one_way_patterns
    )

    if has_mutual_language and not one_way_language:
        return _match(clause, "Mutual obligation language found.", _paragraph_matches(paragraphs, mutual_patterns))
    if one_way_language:
        return _check(clause, "One-way or unilateral confidentiality language needs review.", _paragraph_matches(paragraphs, one_way_patterns))
    return _not_present(clause, "The text does not clearly create mutual confidentiality obligations.", [])


def _check_confidential_information(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    categories = [
        "financial",
        "business",
        "technical",
        "customer",
        "employee",
        "supplier",
        "pricing",
        "market",
        "trade secret",
        "proprietary",
        "source code",
    ]
    category_hits = [category for category in categories if category in normalized]
    broad_definition = "confidential information" in normalized and (
        "any and all information" in normalized or len(category_hits) >= 4
    )
    extra_exclusion_patterns = [
        r"independently developed",
        r"residual knowledge",
        r"residuals",
        r"reverse engineer",
        r"reverse engineering",
    ]
    extra_exclusions = [pattern for pattern in extra_exclusion_patterns if re.search(pattern, normalized)]

    if broad_definition and not extra_exclusions:
        return _match(
            clause,
            "Broad confidential information definition found with no extra exclusions detected.",
            _paragraph_matches(paragraphs, [r"confidential information\b.{0,80}\bmeans\b", r"confidential information\b.{0,120}\bincluding\b", r"any and all information"]),
        )

    if not broad_definition:
        if "confidential information" not in normalized:
            return _not_present(clause, "No Confidential Information definition was found.", [])
        return _check(
            clause,
            "The definition of Confidential Information is missing or too narrow.",
            _paragraph_matches(paragraphs, [r"confidential information"]),
        )
    else:
        return _check(
            clause,
            "The exclusions appear broader than the allowed standard carve-outs.",
            _paragraph_matches(paragraphs, [r"confidential information", *extra_exclusion_patterns]),
        )


def _check_governing_law(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    has_governing_anchor = any(anchor in normalized for anchor in ["governing law", "governed by", "laws of"])
    approved_patterns = [_literal_word_pattern(str(law)) for law in clause.get("approved_laws", [])]
    approved_law_found = any(re.search(pattern, normalized) for pattern in approved_patterns)

    if has_governing_anchor and approved_law_found:
        return _match(clause, "Approved governing law found.", _paragraph_matches(paragraphs, [r"governed by", r"governing law", *approved_patterns]))
    if has_governing_anchor:
        return _check(clause, "A governing law clause was found, but it does not use an approved law.", _paragraph_matches(paragraphs, [r"governed by", r"governing law", r"laws of"]))
    return _not_present(clause, "No governing law clause was found.", [])


def _check_term_and_survival(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    max_years = int(clause.get("max_term_years", clause.get("term_years", 5)))
    year_terms = _extract_year_terms(normalized)
    has_term_within_cap = any(1 <= years <= max_years for years in year_terms)
    has_term_over_cap = any(years > max_years for years in year_terms)
    ordinary_indefinite_term = any(
        phrase in normalized
        for phrase in [
            "for so long as the information remains confidential",
            "indefinitely",
            "perpetual confidentiality",
        ]
    )

    if has_term_over_cap:
        return _check(clause, "A term or survival period exceeds the five-year cap.", _paragraph_matches(paragraphs, [r"\b(?:six|seven|eight|nine|ten|\d{1,2})(?:\s*\(\s*\d{1,2}\s*\))?(?:\s*-\s*|\s+)years?\b"]))
    if ordinary_indefinite_term:
        return _check(clause, "Ordinary confidentiality appears indefinite rather than capped at five years.", _paragraph_matches(paragraphs, [r"indefinitely", r"perpetual confidentiality", r"for so long as the information remains confidential"]))
    if has_term_within_cap:
        return _match(clause, "Term or survival period is within the five-year cap.", _paragraph_matches(paragraphs, [r"\b(?:one|two|three|four|five|[1-5])(?:\s*\(\s*[1-5]\s*\))?(?:\s*-\s*|\s+)years?\b"]))
    return _not_present(clause, "No fixed term or survival period of up to five years was found.", _paragraph_matches(paragraphs, [r"term", r"survive", r"period"]))


def _extract_year_terms(normalized: str) -> List[int]:
    terms: List[int] = []
    pattern = r"\b(?:(one|two|three|four|five|six|seven|eight|nine|ten)|(\d{1,2}))(?:\s*\(\s*(\d{1,2})\s*\))?(?:\s*-\s*|\s+)years?\b"
    for match in re.finditer(pattern, normalized):
        word_value, digit_value, parenthetical_value = match.groups()
        if parenthetical_value:
            terms.append(int(parenthetical_value))
        elif digit_value:
            terms.append(int(digit_value))
        elif word_value:
            terms.append(YEAR_WORDS[word_value])
    return terms


def _check_non_circumvention(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    prohibited_patterns = [
        r"\bnon[- ]circumvention\b",
        r"\bcircumvent(?:ion|s|ed|ing)?\b",
        r"\bintroduced parties\b",
        r"\bsubstitute purpose\b",
        r"\bexclusive dealing\b",
    ]
    prohibited_language = [pattern for pattern in prohibited_patterns if re.search(pattern, normalized)]

    if not prohibited_language:
        return _not_present(clause, "No prohibited non-circumvention language detected.", [])
    return _check(clause, "Prohibited non-circumvention or substitute-purpose language found.", _paragraph_matches(paragraphs, prohibited_patterns))


def _check_signatures(text: str, normalized: str, clause: Dict[str, object], paragraphs: List[Paragraph]) -> ClauseResult:
    signature_patterns = [r"(?m)^\s*For\s+", r"By\s*:", r"Title\s*:", r"Date\s*:"]
    party_markers = len(re.findall(r"^\s*for\s+[a-z0-9&.,' -]{2,80}", text, flags=re.IGNORECASE | re.MULTILINE)) + len(re.findall(r"\bby\s*:", normalized))
    title_markers = len(re.findall(r"\btitle\s*:", normalized))
    date_markers = len(re.findall(r"\bdate\s*:", normalized)) + len(
        re.findall(r"\b\d{1,2}\s+[a-z]{3,9}\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", normalized)
    )

    if party_markers >= 2 and title_markers >= 2 and date_markers >= 1:
        return _match(clause, "Execution block appears to include both parties, titles, and a date.", _paragraph_matches(paragraphs, signature_patterns))
    partial_matches = _paragraph_matches(paragraphs, signature_patterns)
    if partial_matches:
        return _check(clause, "The execution block is missing both-party signatures, titles, or a date.", partial_matches)
    return _not_present(clause, "No execution block was found.", [])


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

    playbook_ids = [str(clause["id"]) for clause in load_playbook()["clauses"]]
    duplicate_playbook_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_playbook_ids:
        raise RuntimeError(f"Duplicate playbook IDs: {', '.join(duplicate_playbook_ids)}")

    missing_checks = sorted(set(playbook_ids) - set(check_ids))
    extra_checks = sorted(set(check_ids) - set(playbook_ids))
    if missing_checks or extra_checks:
        detail = []
        if missing_checks:
            detail.append(f"missing checks for: {', '.join(missing_checks)}")
        if extra_checks:
            detail.append(f"checks without playbook clauses: {', '.join(extra_checks)}")
        raise RuntimeError("Checker registry does not match playbook (" + "; ".join(detail) + ")")


_validate_check_registry()


def _match(clause: Dict[str, object], reason: str, matched_paragraphs: Iterable[Paragraph]) -> ClauseResult:
    return _result(clause, "match", reason, matched_paragraphs)


def _check(clause: Dict[str, object], reason: str, matched_paragraphs: Iterable[Paragraph]) -> ClauseResult:
    return _result(clause, "check", reason, matched_paragraphs)


def _not_present(clause: Dict[str, object], reason: str, matched_paragraphs: Iterable[Paragraph]) -> ClauseResult:
    return _result(clause, "not_present", reason, matched_paragraphs)


def _result(clause: Dict[str, object], status: str, reason: str, matched_paragraphs: Iterable[Paragraph]) -> ClauseResult:
    paragraph_matches = list(matched_paragraphs)[:3]
    matched_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraph_matches)
    result = {
        "id": clause["id"],
        "name": clause["name"],
        "requirement": clause["requirement"],
        "status": status,
        "passes": _status_passes_clause_type(status, clause),
        "reason": reason,
        "finding": reason,
        "matched_paragraph_ids": [paragraph["id"] for paragraph in paragraph_matches],
        "matched_text": matched_text,
        "evidence": [paragraph["text"] for paragraph in paragraph_matches],
    }
    for field in ["acceptable_language", "approved_laws", "max_term_years", "search_terms", "term_years", "type"]:
        if field in clause:
            result[field] = clause[field]
    return result


def _normalize(text: str) -> str:
    lowered = text.lower()
    return re.sub(r"\s+", " ", lowered).strip()


def _status_passes_clause_type(status: str, clause: Dict[str, object]) -> bool:
    clause_type = clause.get("type")
    if clause_type == "prohibited":
        return status == "not_present"
    return status == "match"


def _literal_word_pattern(value: str) -> str:
    words = re.escape(value.lower()).replace(r"\ ", r"\s+")
    return rf"\b{words}\b"


def _paragraph_matches(paragraphs: Iterable[Paragraph], patterns: Iterable[str]) -> List[Paragraph]:
    matches: List[Paragraph] = []
    seen = set()
    for paragraph in paragraphs:
        paragraph_text = str(paragraph["text"])
        for pattern in patterns:
            if not re.search(pattern, paragraph_text, flags=re.IGNORECASE):
                continue
            paragraph_id = paragraph["id"]
            if paragraph_id not in seen:
                matches.append(paragraph)
                seen.add(paragraph_id)
            break
    return matches[:3]
