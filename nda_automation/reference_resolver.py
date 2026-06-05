from __future__ import annotations

import re
from typing import Dict, Iterable, List

from .contract_structure import IDENTIFIER_PART_PATTERN
from .review_document import Paragraph

REFERENCE_RESOLVER_VERSION = 1
REFERENCE_KIND_PATTERN = (
    r"clause|clauses|article|articles|section|sections|schedule|schedules|"
    r"annex|annexes|annexure|annexures|appendix|appendices"
)
REFERENCE_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
REFERENCE_RANGE_SEPARATOR_PATTERN = r"(?:\s+(?:to|through)\s+|\s*[-\u2013\u2014]\s*)"
REFERENCE_NUMERIC_RANGE_PATTERN = rf"\d+{REFERENCE_RANGE_SEPARATOR_PATTERN}\d+"
REFERENCE_NUMBER_OR_RANGE_PATTERN = rf"(?:{REFERENCE_NUMERIC_RANGE_PATTERN}|{REFERENCE_NUMBER_PATTERN})"
REFERENCE_SEPARATOR_PATTERN = r"(?:\s*(?:,|;)\s*(?:(?:and|or)\s+)?|\s+(?:and|or|&)\s+)"
REFERENCE_EXPRESSION_RE = re.compile(
    rf"\b(?P<kind>{REFERENCE_KIND_PATTERN})\s+"
    rf"(?P<numbers>{REFERENCE_NUMBER_OR_RANGE_PATTERN}(?:{REFERENCE_SEPARATOR_PATTERN}{REFERENCE_NUMBER_OR_RANGE_PATTERN})*)"
    r"(?=$|[^A-Za-z0-9])",
    re.IGNORECASE,
)
REFERENCE_NUMBER_RE = re.compile(REFERENCE_NUMBER_PATTERN, re.IGNORECASE)
REFERENCE_NUMERIC_RANGE_RE = re.compile(
    rf"^(?P<start>\d+){REFERENCE_RANGE_SEPARATOR_PATTERN}(?P<end>\d+)$",
    re.IGNORECASE,
)
REFERENCE_SEPARATOR_RE = re.compile(REFERENCE_SEPARATOR_PATTERN, re.IGNORECASE)
MAX_REFERENCE_RANGE_SIZE = 50

REFERENCE_KIND_ALIASES = {
    "annex": "annex",
    "annexes": "annex",
    "annexure": "annexure",
    "annexures": "annexure",
    "appendices": "appendix",
    "appendix": "appendix",
    "article": "article",
    "articles": "article",
    "clause": "clause",
    "clauses": "clause",
    "schedule": "schedule",
    "schedules": "schedule",
    "section": "section",
    "sections": "section",
}


def resolve_document_references(
    paragraphs: List[Paragraph],
    contract_structure: Dict[str, object],
) -> Dict[str, object]:
    """Resolve explicit clause/article/section references against a structure map."""
    reference_index = contract_structure.get("reference_index")
    if not isinstance(reference_index, dict):
        reference_index = {}
    alias_lookup = _string_dict(reference_index.get("alias_to_section_id"))
    sections_by_id = _section_lookup(reference_index.get("sections_by_id"))
    paragraph_lookup = _string_dict(reference_index.get("paragraph_to_section_id"))

    references: List[Dict[str, object]] = []
    for paragraph in paragraphs:
        references.extend(_references_for_paragraph(
            paragraph,
            alias_lookup=alias_lookup,
            sections_by_id=sections_by_id,
            paragraph_lookup=paragraph_lookup,
            start_index=len(references) + 1,
        ))

    resolved_count = sum(1 for reference in references if reference["status"] == "resolved")
    partial_count = sum(1 for reference in references if reference["status"] == "partial")
    unresolved_count = sum(1 for reference in references if reference["status"] == "unresolved")
    target_ids = {
        section_id
        for reference in references
        for section_id in reference.get("resolved_section_ids", [])
        if isinstance(section_id, str)
    }

    return {
        "version": REFERENCE_RESOLVER_VERSION,
        "references": references,
        "stats": {
            "reference_count": len(references),
            "resolved_reference_count": resolved_count,
            "partial_reference_count": partial_count,
            "unresolved_reference_count": unresolved_count,
            "target_section_count": len(target_ids),
        },
    }


def _references_for_paragraph(
    paragraph: Paragraph,
    *,
    alias_lookup: Dict[str, str],
    sections_by_id: Dict[str, Dict[str, object]],
    paragraph_lookup: Dict[str, str],
    start_index: int,
) -> List[Dict[str, object]]:
    paragraph_text = str(paragraph.get("text") or "")
    paragraph_id = _paragraph_id(paragraph)
    source_section_id = paragraph_lookup.get(paragraph_id or "")
    references: List[Dict[str, object]] = []

    for match in REFERENCE_EXPRESSION_RE.finditer(paragraph_text):
        kind = _canonical_kind(match.group("kind"))
        numbers = _reference_numbers(match.group("numbers"))
        if not kind or not numbers:
            continue
        items = [
            _resolve_reference_item(kind, number, alias_lookup, sections_by_id)
            for number in numbers
        ]
        resolved_section_ids = _dedupe(
            str(item["section_id"])
            for item in items
            if isinstance(item.get("section_id"), str) and item.get("section_id")
        )
        if _is_self_heading_reference(match, source_section_id, resolved_section_ids):
            continue
        unresolved_numbers = [
            str(item["number"])
            for item in items
            if not item.get("section_id")
        ]
        references.append({
            "id": f"reference-{start_index + len(references)}",
            "paragraph_id": paragraph_id,
            "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
            "source_section_id": source_section_id,
            "reference_text": match.group(0),
            "kind": kind,
            "numbers": numbers,
            "items": items,
            "resolved_section_ids": resolved_section_ids,
            "unresolved_numbers": unresolved_numbers,
            "targets": [
                sections_by_id[section_id]
                for section_id in resolved_section_ids
                if section_id in sections_by_id
            ],
            "status": _reference_status(items),
        })

    return references


def _resolve_reference_item(
    kind: str,
    number: str,
    alias_lookup: Dict[str, str],
    sections_by_id: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    alias_keys = [f"{kind}:{number.lower()}", f"number:{number.lower()}"]
    matched_alias = ""
    section_id = ""
    for alias_key in alias_keys:
        candidate_section_id = alias_lookup.get(alias_key)
        if candidate_section_id:
            matched_alias = alias_key
            section_id = candidate_section_id
            break
    return {
        "number": number,
        "alias_keys": alias_keys,
        "matched_alias": matched_alias or None,
        "section_id": section_id or None,
        "label": str(sections_by_id.get(section_id, {}).get("label") or "") if section_id else "",
        "status": "resolved" if section_id else "unresolved",
    }


def _reference_status(items: Iterable[Dict[str, object]]) -> str:
    item_list = list(items)
    resolved = [item for item in item_list if item.get("section_id")]
    if resolved and len(resolved) == len(item_list):
        return "resolved"
    if resolved:
        return "partial"
    return "unresolved"


def _reference_numbers(value: str) -> List[str]:
    numbers: List[str] = []
    for part in REFERENCE_SEPARATOR_RE.split(value or ""):
        number = part.strip()
        numbers.extend(_reference_number_part_values(number))
    return numbers


def _reference_number_part_values(value: str) -> List[str]:
    number = value.strip()
    range_match = REFERENCE_NUMERIC_RANGE_RE.fullmatch(number)
    if range_match:
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        if start <= end and end - start < MAX_REFERENCE_RANGE_SIZE:
            return [str(item) for item in range(start, end + 1)]
        return []
    if REFERENCE_NUMBER_RE.fullmatch(number):
        return [number]
    return []


def _canonical_kind(kind: str) -> str:
    return REFERENCE_KIND_ALIASES.get(str(kind or "").lower(), "")


def _is_self_heading_reference(match: re.Match[str], source_section_id: str | None, resolved_section_ids: List[str]) -> bool:
    if match.start() != 0 or not source_section_id or not resolved_section_ids:
        return False
    return all(section_id == source_section_id for section_id in resolved_section_ids)


def _string_dict(value: object) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _section_lookup(value: object) -> Dict[str, Dict[str, object]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, dict)
    }


def _paragraph_id(paragraph: Paragraph) -> str | None:
    paragraph_id = paragraph.get("id")
    return str(paragraph_id) if paragraph_id is not None else None


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    results: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        results.append(value)
        seen.add(value)
    return results
