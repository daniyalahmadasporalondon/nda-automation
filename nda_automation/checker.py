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
CheckFn = Callable[[str, str, Dict[str, object]], ClauseResult]


def load_playbook() -> Dict[str, object]:
    with PLAYBOOK_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def review_nda(text: str) -> Dict[str, object]:
    source_text = text or ""
    normalized = _normalize(source_text)
    playbook = load_playbook()
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}

    clause_results = [
        check(source_text, normalized, clauses_by_id[clause_id])
        for clause_id, check in CLAUSE_CHECKS
    ]
    failed = [clause for clause in clause_results if clause["status"] == "fail"]

    return {
        "overall_status": "does_not_meet_requirements" if failed else "meets_requirements",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requirements_passed": len(clause_results) - len(failed),
        "requirements_failed": len(failed),
        "clauses": clause_results,
    }


def _check_mutuality(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
    has_mutual_language = any(
        re.search(pattern, normalized)
        for pattern in [
            r"\bmutual\s+(?:non[- ]disclosure|confidentiality|nda)\b",
            r"\beach party\b",
            r"\bboth parties\b",
            r"\bdisclosing party\b.*\breceiving party\b",
            r"\breceiving party\b.*\bdisclosing party\b",
        ]
    )
    one_way_language = any(
        re.search(pattern, normalized)
        for pattern in [
            r"\bone[- ]way\b",
            r"\bunilateral\b",
            r"\bonly the receiving party\b",
            r"\brecipient only\b",
        ]
    )

    if has_mutual_language and not one_way_language:
        return _pass(clause, "Mutual obligation language found.", _evidence(text, [r"each party", r"both parties", r"disclosing party", r"mutual"]))
    return _fail(clause, "The text does not clearly create mutual confidentiality obligations.", _evidence(text, [r"one[- ]way", r"unilateral", r"receiving party"]))


def _check_confidential_information(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
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
        return _pass(
            clause,
            "Broad confidential information definition found with no extra exclusions detected.",
            _evidence(text, [r"confidential information", r"any and all information", r"financial", r"customer", r"trade secret"]),
        )

    if not broad_definition:
        finding = "The definition of Confidential Information is missing or too narrow."
    else:
        finding = "The exclusions appear broader than the allowed standard carve-outs."
    return _fail(clause, finding, _evidence(text, [r"confidential information", *extra_exclusion_patterns]))


def _check_governing_law(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
    has_governing_anchor = any(anchor in normalized for anchor in ["governing law", "governed by", "laws of"])
    approved_patterns = [
        r"\bindia\b",
        r"\bdelaware\b",
        r"\bengland and wales\b",
        r"\bdifc\b",
    ]
    approved_law_found = any(re.search(pattern, normalized) for pattern in approved_patterns)

    if has_governing_anchor and approved_law_found:
        return _pass(clause, "Approved governing law found.", _evidence(text, [r"governed by", r"governing law", r"India", r"Delaware", r"England and Wales", r"DIFC"]))
    return _fail(clause, "No approved governing law found.", _evidence(text, [r"governed by", r"governing law", r"laws of"]))


def _check_term_and_survival(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
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
        return _fail(clause, "A term or survival period exceeds the five-year cap.", _evidence(text, [r"\b(?:six|seven|eight|nine|ten|\d{1,2})(?:\s*\(\s*\d{1,2}\s*\))?(?:\s*-\s*|\s+)years?\b"]))
    if ordinary_indefinite_term:
        return _fail(clause, "Ordinary confidentiality appears indefinite rather than capped at five years.", _evidence(text, [r"indefinitely", r"perpetual confidentiality", r"for so long as the information remains confidential"]))
    if has_term_within_cap:
        return _pass(clause, "Term or survival period is within the five-year cap.", _evidence(text, [r"\b(?:one|two|three|four|five|[1-5])(?:\s*\(\s*[1-5]\s*\))?(?:\s*-\s*|\s+)years?\b"]))
    return _fail(clause, "No fixed term or survival period of up to five years was found.", _evidence(text, [r"term", r"survive", r"period"]))


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


def _check_non_circumvention(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
    prohibited_patterns = [
        r"\bnon[- ]circumvention\b",
        r"\bcircumvent(?:ion|s|ed|ing)?\b",
        r"\bintroduced parties\b",
        r"\bsubstitute purpose\b",
        r"\bexclusive dealing\b",
    ]
    prohibited_language = [pattern for pattern in prohibited_patterns if re.search(pattern, normalized)]

    if not prohibited_language:
        return _pass(clause, "No prohibited non-circumvention language detected.", [])
    return _fail(clause, "Prohibited non-circumvention or substitute-purpose language found.", _evidence(text, prohibited_patterns))


def _check_signatures(text: str, normalized: str, clause: Dict[str, object]) -> ClauseResult:
    party_markers = len(re.findall(r"\bfor\s+[a-z0-9&.,' -]{2,80}", normalized)) + len(re.findall(r"\bby\s*:", normalized))
    title_markers = len(re.findall(r"\btitle\s*:", normalized))
    date_markers = len(re.findall(r"\bdate\s*:", normalized)) + len(
        re.findall(r"\b\d{1,2}\s+[a-z]{3,9}\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", normalized)
    )

    if party_markers >= 2 and title_markers >= 2 and date_markers >= 1:
        return _pass(clause, "Execution block appears to include both parties, titles, and a date.", _evidence(text, [r"Title\s*:", r"Date\s*:", r"For\s+"]))
    return _fail(clause, "The execution block is missing both-party signatures, titles, or a date.", _evidence(text, [r"Title\s*:", r"Date\s*:", r"For\s+", r"By\s*:"]))


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


def _pass(clause: Dict[str, object], finding: str, evidence: Iterable[str]) -> ClauseResult:
    return _result(clause, "pass", finding, evidence)


def _fail(clause: Dict[str, object], finding: str, evidence: Iterable[str]) -> ClauseResult:
    return _result(clause, "fail", finding, evidence)


def _result(clause: Dict[str, object], status: str, finding: str, evidence: Iterable[str]) -> ClauseResult:
    result = {
        "id": clause["id"],
        "name": clause["name"],
        "requirement": clause["requirement"],
        "status": status,
        "finding": finding,
        "evidence": list(evidence)[:3],
    }
    for field in ["approved_laws", "max_term_years", "search_terms", "term_years", "type"]:
        if field in clause:
            result[field] = clause[field]
    return result


def _normalize(text: str) -> str:
    lowered = text.lower()
    return re.sub(r"\s+", " ", lowered).strip()


def _evidence(text: str, patterns: Iterable[str]) -> List[str]:
    snippets: List[str] = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        start = max(match.start() - 80, 0)
        end = min(match.end() + 160, len(text))
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        if snippet not in snippets:
            snippets.append(snippet)
    return snippets
