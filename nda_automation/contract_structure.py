from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .review_document import Paragraph

STRUCTURE_VERSION = 1
REFERENCE_INDEX_VERSION = 1
ROMAN_NUMBER_PATTERN = r"[IVXLCDM]{2,}"
IDENTIFIER_PART_PATTERN = rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z]|\d+[A-Za-z]*)"
EXPLICIT_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
NUMBERED_NUMBER_PATTERN = rf"(?:\d+[A-Za-z]*|{ROMAN_NUMBER_PATTERN})(?:\.{IDENTIFIER_PART_PATTERN})*"
NUMBER_PART_RE = re.compile(r"^(?P<digits>\d+)(?P<suffix>[A-Za-z]+)$")

EXPLICIT_HEADING_RE = re.compile(
    r"^\s*(?P<kind>clause|article|section|schedule|annex|annexure|appendix)\s+"
    rf"(?P<number>{EXPLICIT_NUMBER_PATTERN})(?:\s*[:.\-\u2013\u2014]\s*|\s+)"
    r"(?P<heading>.*)$",
    re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(
    rf"^\s*(?P<number>{NUMBERED_NUMBER_PATTERN})(?:\s*[:.\-\u2013\u2014]\s*|\s+)(?P<heading>.+)$"
)
UPPERCASE_PREFIX_RE = re.compile(
    r"^\s*(?P<heading>[A-Z][A-Z0-9 &,/()'\".\-]{2,90}):\s*(?P<body>.+)$"
)
UPPERCASE_STANDALONE_RE = re.compile(r"^[A-Z][A-Z0-9 &,/()'\".\-]{2,110}$")
TRAILING_NUMBER_DOT_RE = re.compile(r"\.$")

EXPLICIT_KIND_LABELS = {
    "annex": "Annex",
    "annexure": "Annexure",
    "appendix": "Appendix",
    "article": "Article",
    "clause": "Clause",
    "schedule": "Schedule",
    "section": "Section",
}

TITLE_WORDS = {
    "agreement",
    "disclosure",
    "non-disclosure",
    "confidentiality",
    "nda",
}


@dataclass
class _SectionCandidate:
    position: int
    kind: str
    label: str
    number: str | None
    heading: str
    level: int
    confidence: str
    heading_text: str


def build_contract_structure(paragraphs: List[Paragraph]) -> Dict[str, object]:
    """Build a document-specific map of contract headings and paragraph ranges."""
    document_paragraphs = [paragraph for paragraph in paragraphs if str(paragraph.get("text", "")).strip()]
    candidates = _detect_section_candidates(document_paragraphs)
    sections: List[Dict[str, object]] = []

    if document_paragraphs and (not candidates or candidates[0].position > 0):
        first_candidate_position = candidates[0].position if candidates else len(document_paragraphs)
        preamble_paragraphs = document_paragraphs[:first_candidate_position]
        if preamble_paragraphs:
            sections.append(_section_dict(
                section_id="section-1",
                kind="preamble",
                label="Preamble",
                number=None,
                heading="Preamble",
                level=0,
                paragraphs=preamble_paragraphs,
                parent_id=None,
                confidence="high",
                heading_text="Preamble",
            ))

    for candidate_index, candidate in enumerate(candidates):
        next_position = (
            candidates[candidate_index + 1].position
            if candidate_index + 1 < len(candidates)
            else len(document_paragraphs)
        )
        section_paragraphs = document_paragraphs[candidate.position:next_position]
        if not section_paragraphs:
            continue

        section_id = f"section-{len(sections) + 1}"
        parent_id = _find_parent_id(sections, candidate)
        sections.append(_section_dict(
            section_id=section_id,
            kind=candidate.kind,
            label=candidate.label,
            number=candidate.number,
            heading=candidate.heading,
            level=candidate.level,
            paragraphs=section_paragraphs,
            parent_id=parent_id,
            confidence=candidate.confidence,
            heading_text=candidate.heading_text,
        ))

    aliases = _build_aliases(sections)
    reference_index = _build_reference_index(sections, aliases)
    mapped_paragraph_ids = {
        paragraph_id
        for section in sections
        for paragraph_id in section.get("paragraph_ids", [])
        if isinstance(paragraph_id, str)
    }
    all_paragraph_ids = {
        _paragraph_id(paragraph)
        for paragraph in document_paragraphs
        if _paragraph_id(paragraph) is not None
    }

    return {
        "version": STRUCTURE_VERSION,
        "sections": sections,
        "aliases": aliases,
        "reference_index": reference_index,
        "stats": {
            "section_count": len(sections),
            "mapped_paragraph_count": len(mapped_paragraph_ids),
            "unmapped_paragraph_count": len(all_paragraph_ids - mapped_paragraph_ids),
        },
    }


def _detect_section_candidates(paragraphs: List[Paragraph]) -> List[_SectionCandidate]:
    candidates: List[_SectionCandidate] = []
    seen_positions = set()
    for position, paragraph in enumerate(paragraphs):
        candidate = _candidate_for_paragraph(position, paragraph)
        if candidate is None or position in seen_positions:
            continue
        candidates.append(candidate)
        seen_positions.add(position)
    return candidates


def _candidate_for_paragraph(position: int, paragraph: Paragraph) -> _SectionCandidate | None:
    text = _collapse_whitespace(str(paragraph.get("text", "")))
    if not text:
        return None

    explicit_match = EXPLICIT_HEADING_RE.match(text)
    if explicit_match:
        kind = explicit_match.group("kind").lower()
        number = TRAILING_NUMBER_DOT_RE.sub("", explicit_match.group("number").strip())
        heading = _clean_heading(explicit_match.group("heading")) or _display_kind(kind)
        label = f"{_display_kind(kind)} {number}"
        return _SectionCandidate(
            position=position,
            kind=kind,
            label=label,
            number=number,
            heading=heading,
            level=_level_for_number(number),
            confidence="high",
            heading_text=_preview(text),
        )

    numbered_match = NUMBERED_HEADING_RE.match(text)
    if numbered_match and _looks_like_numbered_heading(numbered_match.group("heading")):
        number = TRAILING_NUMBER_DOT_RE.sub("", numbered_match.group("number").strip())
        heading = _clean_heading(numbered_match.group("heading"))
        return _SectionCandidate(
            position=position,
            kind="numbered",
            label=number,
            number=number,
            heading=heading,
            level=_level_for_number(number),
            confidence="high",
            heading_text=_preview(text),
        )

    uppercase_prefix_match = UPPERCASE_PREFIX_RE.match(text)
    if uppercase_prefix_match:
        heading = _clean_heading(uppercase_prefix_match.group("heading"))
        if _looks_like_uppercase_heading(heading):
            return _SectionCandidate(
                position=position,
                kind="heading",
                label=heading,
                number=None,
                heading=heading,
                level=1,
                confidence="medium",
                heading_text=_preview(text),
            )

    if UPPERCASE_STANDALONE_RE.match(text) and _looks_like_uppercase_heading(text):
        if position == 0 and _looks_like_document_title(text):
            return None
        heading = _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="heading",
            label=heading,
            number=None,
            heading=heading,
            level=1,
            confidence="medium",
            heading_text=_preview(text),
        )

    return None


def _section_dict(
    *,
    section_id: str,
    kind: str,
    label: str,
    number: str | None,
    heading: str,
    level: int,
    paragraphs: List[Paragraph],
    parent_id: str | None,
    confidence: str,
    heading_text: str,
) -> Dict[str, object]:
    paragraph_ids = [
        paragraph_id
        for paragraph_id in (_paragraph_id(paragraph) for paragraph in paragraphs)
        if paragraph_id is not None
    ]
    first_paragraph = paragraphs[0]
    last_paragraph = paragraphs[-1]

    section: Dict[str, object] = {
        "id": section_id,
        "kind": kind,
        "label": label,
        "heading": heading,
        "number": number,
        "level": level,
        "paragraph_ids": paragraph_ids,
        "start_paragraph_id": _paragraph_id(first_paragraph),
        "end_paragraph_id": _paragraph_id(last_paragraph),
        "start_index": _paragraph_index(first_paragraph),
        "end_index": _paragraph_index(last_paragraph),
        "parent_id": parent_id,
        "confidence": confidence,
        "heading_text": heading_text,
    }
    return section


def _find_parent_id(sections: List[Dict[str, object]], candidate: _SectionCandidate) -> str | None:
    if candidate.number is None:
        return None

    for parent_number in _parent_number_candidates(candidate.number):
        parent_id = _find_section_id_by_number(sections, parent_number, candidate.level)
        if parent_id is not None:
            return parent_id

    for section in reversed(sections):
        parent_number = section.get("number")
        if not isinstance(parent_number, str):
            continue
        if candidate.number.startswith(parent_number + ".") and int(section.get("level", 0)) < candidate.level:
            section_id = section.get("id")
            return str(section_id) if section_id is not None else None
    return None


def _build_aliases(sections: Iterable[Dict[str, object]]) -> List[Dict[str, str]]:
    aliases: List[Dict[str, str]] = []
    seen_keys = set()
    for section in sections:
        section_id = str(section.get("id", ""))
        label = str(section.get("label", ""))
        heading = str(section.get("heading", ""))
        kind = str(section.get("kind", ""))
        number = section.get("number")

        alias_keys = []
        if isinstance(number, str) and number:
            alias_keys.append(f"number:{number.lower()}")
            if kind in EXPLICIT_KIND_LABELS:
                alias_keys.append(f"{kind}:{number.lower()}")
        heading_key = _normalize_heading_key(heading)
        if heading_key:
            alias_keys.append(f"heading:{heading_key}")

        for key in alias_keys:
            if not section_id or key in seen_keys:
                continue
            aliases.append({"key": key, "section_id": section_id, "label": label})
            seen_keys.add(key)
    return aliases


def _build_reference_index(
    sections: Iterable[Dict[str, object]],
    aliases: Iterable[Dict[str, str]],
) -> Dict[str, object]:
    section_lookup: Dict[str, Dict[str, object]] = {}
    paragraph_lookup: Dict[str, str] = {}
    section_ids: List[str] = []

    for section in sections:
        section_id = str(section.get("id") or "")
        if not section_id:
            continue
        section_ids.append(section_id)
        section_lookup[section_id] = _resolver_section_record(section)
        for paragraph_id in section.get("paragraph_ids", []):
            if isinstance(paragraph_id, str) and paragraph_id:
                paragraph_lookup[paragraph_id] = section_id

    return {
        "version": REFERENCE_INDEX_VERSION,
        "section_ids": section_ids,
        "sections_by_id": section_lookup,
        "alias_to_section_id": {
            alias["key"]: alias["section_id"]
            for alias in aliases
            if isinstance(alias.get("key"), str) and isinstance(alias.get("section_id"), str)
        },
        "paragraph_to_section_id": paragraph_lookup,
    }


def _resolver_section_record(section: Dict[str, object]) -> Dict[str, object]:
    return {
        "id": str(section.get("id") or ""),
        "kind": str(section.get("kind") or ""),
        "number": section.get("number") if isinstance(section.get("number"), str) else None,
        "label": str(section.get("label") or ""),
        "heading": str(section.get("heading") or ""),
        "level": int(section.get("level", 0)) if isinstance(section.get("level"), int) else 0,
        "paragraph_ids": [
            paragraph_id
            for paragraph_id in section.get("paragraph_ids", [])
            if isinstance(paragraph_id, str)
        ],
        "start_index": section.get("start_index") if isinstance(section.get("start_index"), int) else None,
        "end_index": section.get("end_index") if isinstance(section.get("end_index"), int) else None,
        "parent_id": section.get("parent_id") if isinstance(section.get("parent_id"), str) else None,
    }


def _looks_like_numbered_heading(heading: str) -> bool:
    cleaned = _clean_heading(heading)
    if not cleaned:
        return False
    if len(cleaned) <= 120:
        return True
    return ":" in cleaned[:90]


def _looks_like_uppercase_heading(text: str) -> bool:
    cleaned = _clean_heading(text)
    if len(cleaned) < 3:
        return False
    letters = [character for character in cleaned if character.isalpha()]
    if len(letters) < 3:
        return False
    uppercase_letters = [character for character in letters if character.isupper()]
    return len(uppercase_letters) / len(letters) >= 0.85


def _looks_like_document_title(text: str) -> bool:
    key = _normalize_heading_key(text)
    words = set(key.split())
    return bool(words & TITLE_WORDS) and len(words) <= 8


def _level_for_number(number: str | None) -> int:
    if not number:
        return 1
    parts = _number_parts(number)
    if not parts:
        return 1
    return len(parts) + sum(1 for part in parts if _strip_letter_suffix(part) is not None)


def _parent_number_candidates(number: str) -> List[str]:
    candidates: List[str] = []
    queue = [number]
    seen = {number}

    while queue:
        current = queue.pop(0)
        for parent_number in _immediate_parent_numbers(current):
            if parent_number in seen:
                continue
            candidates.append(parent_number)
            queue.append(parent_number)
            seen.add(parent_number)
    return candidates


def _immediate_parent_numbers(number: str) -> List[str]:
    parts = _number_parts(number)
    parents: List[str] = []
    if not parts:
        return parents

    stripped_last_part = _strip_letter_suffix(parts[-1])
    if stripped_last_part:
        parents.append(".".join([*parts[:-1], stripped_last_part]))
    if len(parts) > 1:
        parents.append(".".join(parts[:-1]))
    return [parent for parent in parents if parent and parent != number]


def _find_section_id_by_number(
    sections: List[Dict[str, object]],
    parent_number: str,
    candidate_level: int,
) -> str | None:
    for section in reversed(sections):
        section_number = section.get("number")
        if section_number != parent_number or int(section.get("level", 0)) >= candidate_level:
            continue
        section_id = section.get("id")
        return str(section_id) if section_id is not None else None
    return None


def _number_parts(number: str | None) -> List[str]:
    return [part for part in str(number or "").split(".") if part]


def _strip_letter_suffix(part: str) -> str | None:
    match = NUMBER_PART_RE.match(part)
    return match.group("digits") if match else None


def _display_kind(kind: str) -> str:
    return EXPLICIT_KIND_LABELS.get(kind.lower(), kind.capitalize())


def _paragraph_id(paragraph: Paragraph) -> str | None:
    paragraph_id = paragraph.get("id")
    return str(paragraph_id) if paragraph_id is not None else None


def _paragraph_index(paragraph: Paragraph) -> int | None:
    index = paragraph.get("index")
    return index if isinstance(index, int) else None


def _clean_heading(text: str) -> str:
    return _collapse_whitespace(text).strip(" .:-")


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_heading_key(text: str) -> str:
    lowered = text.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered)
    return _collapse_whitespace(normalized)


def _preview(text: str, limit: int = 220) -> str:
    collapsed = _collapse_whitespace(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."
