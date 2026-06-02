from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set

from .review_document import Paragraph

CONCEPT_CLASSIFIER_VERSION = 1

CONCEPT_DEFINITIONS: Dict[str, Dict[str, object]] = {
    "mutuality": {
        "label": "Mutuality",
        "patterns": [
            r"\bmutual(?:ity|ly)?\b",
            r"\breciprocal(?:ly)?\b",
            r"\beach\s+party\b.{0,140}\b(?:disclos(?:e|es|ing)|receiv(?:e|es|ing)|confidential)\b",
            r"\bboth\s+parties\b.{0,140}\b(?:disclos(?:e|es|ing)|receiv(?:e|es|ing)|confidential)\b",
            r"\beach\s+party\b.{0,140}\b(?:disclosing\s+party|receiving\s+party)\b",
        ],
    },
    "party_role_definition": {
        "label": "Party role definition",
        "patterns": [
            r"\bdisclosing\s+party\b.{0,80}\b(?:means|is|shall\s+mean)\b",
            r"\breceiving\s+party\b.{0,80}\b(?:means|is|shall\s+mean)\b",
            r"\b(?:disclosing|receiving)\s+party\b.{0,120}\b(?:disclos(?:e|es|ing)|receiv(?:e|es|ing))\b",
        ],
    },
    "confidential_information_definition": {
        "label": "Confidential Information definition",
        "patterns": [
            r"\bconfidential\s+information\b.{0,80}\b(?:means|includes?|shall\s+mean|is\s+defined)\b",
            r"\bdefinition\s+of\s+confidential\s+information\b",
        ],
    },
    "confidential_information_exclusion": {
        "label": "Confidential Information exclusion",
        "patterns": [
            r"\bconfidential\s+information\b.{0,120}\b(?:does\s+not\s+include|shall\s+not\s+include|excludes?|excluded)\b",
            r"\b(?:does\s+not\s+include|shall\s+not\s+include|excludes?|excluded)\b.{0,120}\bconfidential\s+information\b",
            r"\b(?:public\s+domain|prior\s+possession|independent(?:ly)?\s+(?:developed|develops|develop|development)|lawful\s+third\s+party)\b",
        ],
    },
    "confidentiality_obligation": {
        "label": "Confidentiality obligation",
        "patterns": [
            r"\bconfidentiality\s+(?:obligations?|undertakings?|provisions?|duties?)\b",
            r"\b(?:keep|maintain|protect|preserve)\b.{0,90}\bconfidential\b",
            r"\b(?:not|no)\s+disclos(?:e|ure|ing)\b",
            r"\bnon[-\s]?disclosure\b",
            r"\breceiving\s+party\b.{0,120}\bconfidential\s+information\b",
        ],
    },
    "use_restriction": {
        "label": "Use restriction",
        "patterns": [
            r"\b(?:use|used|using)\b.{0,90}\b(?:confidential\s+information|purpose|permitted\s+purpose)\b",
            r"\bconfidential\s+information\b.{0,90}\b(?:use|used|using)\b",
            r"\b(?:solely|only)\s+for\s+the\s+(?:purpose|permitted\s+purpose)\b",
            r"\bpurpose\b.{0,90}\bconfidential\s+information\b",
        ],
    },
    "permitted_disclosure": {
        "label": "Permitted disclosure",
        "patterns": [
            r"\bpermitted\s+disclos(?:e|ure|ures)\b",
            r"\bdisclos(?:e|ure)\b.{0,90}\b(?:representatives?|advisers?|affiliates?|employees?|agents?)\b",
            r"\brepresentatives?\b.{0,90}\bconfidential\s+information\b",
        ],
    },
    "return_or_destruction": {
        "label": "Return or destruction",
        "patterns": [
            r"\b(?:return|destroy|destruction|delete|certify\s+destruction)\b.{0,120}\bconfidential\s+information\b",
            r"\breturn\s+of\s+materials\b",
        ],
    },
    "term_or_survival": {
        "label": "Term or survival",
        "patterns": [
            r"\b(?:term|termination|expiry|expiration|effective\s+date)\b",
            r"\bsurviv(?:e|es|ed|ing|al)\b",
            r"\b(?:remain|continue|in\s+effect)\b.{0,80}\b(?:after|following)\b",
        ],
    },
    "trade_secret_or_legal_carveout": {
        "label": "Trade secret or legal carve-out",
        "patterns": [
            r"\btrade\s+secrets?\b",
            r"\b(?:required|requires?)\s+by\s+law\b",
            r"\bapplicable\s+law\b",
            r"\blegal\s+obligations?\b",
            r"\bpersonal\s+data\b",
            r"\bdata[-\s]?protection\b",
        ],
    },
    "governing_law": {
        "label": "Governing law",
        "patterns": [
            r"\bgoverning\s+law\b",
            r"\bgoverned\s+by\s+the\s+laws?\b",
            r"\blaws?\s+of\b.{0,80}\b(?:india|delaware|england|wales|difc)\b",
        ],
    },
    "execution": {
        "label": "Execution",
        "patterns": [
            r"\b(?:executed|authori[sz]ed\s+signatory)\b",
            r"\b(?:by|title|date)\s*:",
        ],
    },
    "non_circumvention": {
        "label": "Non-circumvention",
        "patterns": [
            r"\bnon[-\s]?circumvention\b",
            r"\bnon[-\s]?solicitation\b",
            r"\b(?:bypass|circumvent|direct\s+dealing|introduced\s+contacts?)\b",
        ],
    },
}

ORDINARY_CONFIDENTIALITY_CONCEPTS = {
    "confidential_information_definition",
    "confidentiality_obligation",
    "permitted_disclosure",
    "return_or_destruction",
    "use_restriction",
}


def classify_document_concepts(
    paragraphs: List[Paragraph],
    contract_structure: Dict[str, object] | None = None,
) -> Dict[str, object]:
    """Classify deterministic legal concepts for paragraphs and detected sections."""
    paragraph_records = [_classify_paragraph(paragraph) for paragraph in paragraphs]
    concepts_by_paragraph_id = {
        str(record["paragraph_id"]): list(record["concepts"])
        for record in paragraph_records
        if record.get("paragraph_id")
    }
    section_records = _classify_sections(contract_structure or {}, concepts_by_paragraph_id)
    concepts_by_section_id = {
        str(record["section_id"]): list(record["concepts"])
        for record in section_records
        if record.get("section_id")
    }
    concept_counts = _concept_counts(paragraph_records, section_records)

    return {
        "version": CONCEPT_CLASSIFIER_VERSION,
        "paragraphs": paragraph_records,
        "sections": section_records,
        "concepts_by_paragraph_id": concepts_by_paragraph_id,
        "concepts_by_section_id": concepts_by_section_id,
        "stats": {
            "classified_paragraph_count": sum(1 for record in paragraph_records if record["concepts"]),
            "classified_section_count": sum(1 for record in section_records if record["concepts"]),
            "concept_count": len(concept_counts),
            "concept_counts": concept_counts,
        },
    }


def _classify_paragraph(paragraph: Paragraph) -> Dict[str, object]:
    text = str(paragraph.get("text") or "")
    concepts, signals = _concepts_for_text(text)
    return {
        "paragraph_id": str(paragraph.get("id") or ""),
        "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
        "concepts": concepts,
        "signals": signals,
    }


def _classify_sections(
    contract_structure: Dict[str, object],
    concepts_by_paragraph_id: Dict[str, List[str]],
) -> List[Dict[str, object]]:
    sections = contract_structure.get("sections")
    if not isinstance(sections, list):
        return []

    section_records: List[Dict[str, object]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        paragraph_ids = [str(paragraph_id) for paragraph_id in section.get("paragraph_ids", []) if paragraph_id]
        concepts: Set[str] = set()
        for paragraph_id in paragraph_ids:
            concepts.update(concepts_by_paragraph_id.get(paragraph_id, []))
        heading_concepts, heading_signals = _concepts_for_text(
            " ".join(str(section.get(field) or "") for field in ("label", "heading"))
        )
        concepts.update(heading_concepts)
        section_records.append({
            "section_id": str(section.get("id") or ""),
            "label": str(section.get("label") or section.get("heading") or ""),
            "paragraph_ids": paragraph_ids,
            "concepts": sorted(concepts),
            "heading_signals": heading_signals,
        })
    return section_records


def _concepts_for_text(text: str) -> tuple[List[str], List[Dict[str, str]]]:
    normalized = _normalize(text)
    concepts: List[str] = []
    signals: List[Dict[str, str]] = []
    for concept, definition in CONCEPT_DEFINITIONS.items():
        matched_pattern = _matched_pattern(normalized, definition.get("patterns", []))
        if not matched_pattern:
            continue
        concepts.append(concept)
        signals.append({
            "concept": concept,
            "label": str(definition.get("label") or concept),
            "pattern": matched_pattern,
        })
    return concepts, signals


def _matched_pattern(normalized: str, patterns: object) -> str:
    if not isinstance(patterns, list):
        return ""
    for pattern in patterns:
        pattern_text = str(pattern)
        if re.search(pattern_text, normalized, flags=re.IGNORECASE):
            return pattern_text
    return ""


def _concept_counts(
    paragraph_records: Iterable[Dict[str, object]],
    section_records: Iterable[Dict[str, object]],
) -> Dict[str, int]:
    counts = {concept: 0 for concept in CONCEPT_DEFINITIONS}
    for record in list(paragraph_records) + list(section_records):
        for concept in record.get("concepts", []):
            if isinstance(concept, str):
                counts[concept] = counts.get(concept, 0) + 1
    return {concept: count for concept, count in counts.items() if count}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()
