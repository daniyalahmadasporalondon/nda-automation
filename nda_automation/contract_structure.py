from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .review_document import Paragraph

STRUCTURE_VERSION = 2
REFERENCE_INDEX_VERSION = 2
ROMAN_NUMBER_PATTERN = r"[IVXLCDM]+"
BASE_IDENTIFIER_PART_PATTERN = rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z]|\d+[A-Za-z]*)"
PARENTHETICAL_IDENTIFIER_PART_PATTERN = r"\([A-Za-z0-9]+\)"
IDENTIFIER_PART_PATTERN = (
    rf"(?:{BASE_IDENTIFIER_PART_PATTERN}(?:{PARENTHETICAL_IDENTIFIER_PART_PATTERN})*|"
    rf"{PARENTHETICAL_IDENTIFIER_PART_PATTERN})"
)
EXPLICIT_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
NUMBERED_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
NUMBER_PART_RE = re.compile(r"^(?P<digits>\d+)(?P<suffix>[A-Za-z]+)$")
PARENTHETICAL_SUFFIX_RE = re.compile(r"^(?P<prefix>.*)\([A-Za-z0-9]+\)$")
PARENTHETICAL_PART_RE = re.compile(r"\(([A-Za-z0-9]+)\)")
OPERATIVE_SENTENCE_RE = re.compile(
    r"\b(?:shall|must|will|may|can|agrees?|undertakes?|covenants?|represents?|warrants?|"
    r"is|are|was|were|has|have|means|includes?|excludes?|not|appl(?:y|ies)|"
    r"surviv(?:e|es|ed|ing)|remain(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)

EXPLICIT_HEADING_RE = re.compile(
    r"^\s*(?P<kind>clause|article|section|schedule|annex|annexure|appendix)\s+"
    rf"(?P<number>{EXPLICIT_NUMBER_PATTERN})(?P<separator>\s*[:.\-\u2013\u2014]\s*|\s+)"
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
    source: Dict[str, object] | None = None


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
                source=None,
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
            source=candidate.source,
        ))

    aliases, ambiguous_alias_keys = _build_aliases(sections)
    reference_index = _build_reference_index(sections, aliases, ambiguous_alias_keys)
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
            **_source_stats(document_paragraphs, sections),
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

    metadata_candidate = _candidate_from_source_metadata(position, paragraph, text)
    if metadata_candidate is not None:
        return metadata_candidate

    explicit_match = EXPLICIT_HEADING_RE.match(text)
    if explicit_match:
        kind = explicit_match.group("kind").lower()
        number = TRAILING_NUMBER_DOT_RE.sub("", explicit_match.group("number").strip())
        heading = _clean_heading(explicit_match.group("heading")) or _display_kind(kind)
        if not _looks_like_explicit_heading(number, heading, explicit_match.group("separator")):
            return None
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
    if numbered_match and _looks_like_numbered_heading(numbered_match.group("number"), numbered_match.group("heading")):
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


def _candidate_from_source_metadata(position: int, paragraph: Paragraph, text: str) -> _SectionCandidate | None:
    source = _source_metadata(paragraph)
    structure_number = _source_structure_number(paragraph)
    if structure_number:
        heading = _clean_heading(_strip_leading_number(text, structure_number)) or _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="numbered",
            label=structure_number,
            number=structure_number,
            heading=heading,
            level=_source_number_level(paragraph, structure_number),
            confidence="high",
            heading_text=_preview(_source_heading_text(paragraph, text)),
            source=source,
        )

    heading_level = paragraph.get("heading_level")
    if isinstance(heading_level, int) and heading_level > 0:
        heading = _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="heading",
            label=heading,
            number=None,
            heading=heading,
            level=heading_level,
            confidence="high",
            heading_text=_preview(text),
            source=source,
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
    source: Dict[str, object] | None = None,
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
    if source:
        section["source"] = source
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


def _build_aliases(sections: Iterable[Dict[str, object]]) -> tuple[List[Dict[str, str]], List[str]]:
    """Map alias keys to sections, refusing to bind a key shared by >1 section.

    When a document restarts numbering -- e.g. an appended Exhibit/Order Form whose
    "Section 2" follows the main body's "Section 2" -- a kind/number alias key like
    ``section:2`` is claimed by more than one section. Silently binding the key to the
    *first* occurrence (the old first-write-wins behaviour) makes every "Section 2"
    cross-reference resolve to the wrong target with full confidence and no ambiguity
    signal, which lets an unapproved governing law referenced via a duplicate section be
    silently cleared. Instead, a key claimed by two or more *distinct* sections is
    recorded as ambiguous and left out of the binding map, so the resolver treats those
    references as unresolved rather than mis-resolved (the conservative-correct stance
    the resolver already takes for the Schedule<->Section namespace collision)."""
    first_section_for_key: Dict[str, Dict[str, str]] = {}
    ambiguous_keys: set[str] = set()
    key_order: List[str] = []
    for section in sections:
        section_id = str(section.get("id", ""))
        if not section_id:
            continue
        label = str(section.get("label", ""))
        heading = str(section.get("heading", ""))
        kind = str(section.get("kind", ""))
        number = section.get("number")

        alias_keys: List[str] = []
        if isinstance(number, str) and number:
            alias_keys.append(f"number:{number.lower()}")
            if kind in EXPLICIT_KIND_LABELS:
                alias_keys.append(f"{kind}:{number.lower()}")
        heading_key = _normalize_heading_key(heading)
        if heading_key:
            alias_keys.append(f"heading:{heading_key}")

        for key in alias_keys:
            existing = first_section_for_key.get(key)
            if existing is None:
                first_section_for_key[key] = {"key": key, "section_id": section_id, "label": label}
                key_order.append(key)
            elif existing["section_id"] != section_id:
                # A second, distinct section claims this key -- the key is ambiguous and
                # must not bind to either occurrence.
                ambiguous_keys.add(key)

    aliases = [
        first_section_for_key[key]
        for key in key_order
        if key not in ambiguous_keys
    ]
    return aliases, sorted(ambiguous_keys)


def _build_reference_index(
    sections: Iterable[Dict[str, object]],
    aliases: Iterable[Dict[str, str]],
    ambiguous_alias_keys: Iterable[str] = (),
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
        # Alias keys claimed by more than one section (e.g. a "Section 2" that recurs in
        # an appended Exhibit with restarted numbering). The resolver must treat
        # references to these as unresolved rather than silently bind to one occurrence.
        "ambiguous_alias_keys": sorted(
            str(key) for key in ambiguous_alias_keys if isinstance(key, str) and key
        ),
        "paragraph_to_section_id": paragraph_lookup,
    }


def _resolver_section_record(section: Dict[str, object]) -> Dict[str, object]:
    record: Dict[str, object] = {
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
    if isinstance(section.get("source"), dict):
        record["source"] = section["source"]
    return record


def _source_stats(paragraphs: List[Paragraph], sections: List[Dict[str, object]]) -> Dict[str, object]:
    source_kinds = {
        str(paragraph.get("source_kind") or "")
        for paragraph in paragraphs
        if paragraph.get("source_kind")
    }
    source_parts = {
        str(paragraph.get("source_part") or "")
        for paragraph in paragraphs
        if paragraph.get("source_part")
    }
    return {
        "docx_numbered_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("numbering"), dict)),
        "docx_heading_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("heading_level"), int)),
        "table_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("table"), dict)),
        "source_backed_section_count": sum(1 for section in sections if isinstance(section.get("source"), dict)),
        "source_kinds": sorted(source_kinds),
        "source_parts": sorted(source_parts),
    }


def _looks_like_numbered_heading(number: str, heading: str) -> bool:
    cleaned = _clean_heading(heading)
    if not cleaned:
        return False
    if _requires_strict_outline_heading(number):
        return _looks_like_short_heading(cleaned)
    if len(cleaned) <= 120:
        return True
    return ":" in cleaned[:90]


def _looks_like_explicit_heading(number: str, heading: str, separator: str) -> bool:
    cleaned = _clean_heading(heading)
    if not cleaned:
        return True
    if re.match(r"^(?:and|or)\b", cleaned, flags=re.IGNORECASE):
        return False
    if separator.strip():
        return len(cleaned) <= 160 or ":" in cleaned[:90]
    if _requires_strict_outline_heading(number):
        return _looks_like_short_heading(cleaned)
    return _looks_like_short_heading(cleaned) or not re.search(OPERATIVE_SENTENCE_RE, cleaned)


def _requires_strict_outline_heading(number: str) -> bool:
    normalized_number = str(number or "").strip()
    if not normalized_number:
        return False
    if "(" in normalized_number or ")" in normalized_number:
        return True
    return "." not in normalized_number and bool(
        re.fullmatch(rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z])", normalized_number, flags=re.IGNORECASE)
    )


def _looks_like_short_heading(text: str) -> bool:
    cleaned = _clean_heading(text)
    if not cleaned or len(cleaned) > 90:
        return False
    if re.search(OPERATIVE_SENTENCE_RE, cleaned):
        return False
    words = [word for word in re.split(r"\s+", cleaned) if word]
    if len(words) > 8:
        return False
    first_alpha = next((character for character in cleaned if character.isalpha()), "")
    return bool(first_alpha and first_alpha.isupper())


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
    parents: List[str] = []
    normalized_number = str(number or "").strip()
    if not normalized_number:
        return parents

    parenthetical_match = PARENTHETICAL_SUFFIX_RE.match(normalized_number)
    if parenthetical_match:
        parents.append(parenthetical_match.group("prefix"))
    else:
        raw_parts = [part for part in normalized_number.split(".") if part]
        if raw_parts:
            stripped_last_part = _strip_letter_suffix(raw_parts[-1])
            if stripped_last_part:
                parents.append(".".join([*raw_parts[:-1], stripped_last_part]))
            if len(raw_parts) > 1:
                parents.append(".".join(raw_parts[:-1]))
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


def _source_metadata(paragraph: Paragraph) -> Dict[str, object] | None:
    source: Dict[str, object] = {}
    for key in ("source_kind", "style_id", "style_name", "heading_level", "outline_level", "structure_label"):
        value = paragraph.get(key)
        if isinstance(value, (str, int)) and str(value) != "":
            source[key] = value
    for key in ("numbering", "table"):
        value = paragraph.get(key)
        if isinstance(value, dict):
            source[key] = value
    if "source_index" in paragraph and isinstance(paragraph.get("source_index"), int):
        source["source_index"] = paragraph["source_index"]
    if "source_part" in paragraph and isinstance(paragraph.get("source_part"), str):
        source["source_part"] = paragraph["source_part"]
    return source or None


def _source_structure_number(paragraph: Paragraph) -> str:
    direct_number = paragraph.get("structure_number")
    if isinstance(direct_number, str) and direct_number.strip():
        return _clean_source_number(direct_number)
    numbering = paragraph.get("numbering")
    if not isinstance(numbering, dict):
        return ""
    number_format = str(numbering.get("format") or "")
    if number_format in {"bullet", "none"}:
        return ""
    label = str(numbering.get("label") or "").strip()
    return _clean_source_number(label)


def _clean_source_number(value: str) -> str:
    cleaned = re.sub(r"^[^\w(]+|[^\w)]+$", "", value or "").strip()
    return cleaned if re.fullmatch(EXPLICIT_NUMBER_PATTERN, cleaned, flags=re.IGNORECASE) else ""


def _source_number_level(paragraph: Paragraph, number: str) -> int:
    numbering = paragraph.get("numbering")
    if isinstance(numbering, dict) and isinstance(numbering.get("level"), int):
        return int(numbering["level"]) + 1
    return _level_for_number(number)


def _source_heading_text(paragraph: Paragraph, text: str) -> str:
    structure_label = paragraph.get("structure_label")
    if isinstance(structure_label, str) and structure_label.strip():
        return f"{structure_label.strip()} {text}"
    return text


def _strip_leading_number(text: str, number: str) -> str:
    pattern = rf"^\s*{re.escape(number)}(?:\s*[:.)\-\u2013\u2014]\s*|\s+)"
    return re.sub(pattern, "", text, count=1)


def _number_parts(number: str | None) -> List[str]:
    parts: List[str] = []
    for raw_part in str(number or "").split("."):
        part = raw_part.strip()
        if not part:
            continue
        prefix = PARENTHETICAL_SUFFIX_RE.sub(r"\g<prefix>", part).strip()
        if prefix:
            parts.append(prefix)
        parts.extend(match.group(1) for match in PARENTHETICAL_PART_RE.finditer(part))
    return parts


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
