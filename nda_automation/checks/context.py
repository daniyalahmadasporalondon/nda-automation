from __future__ import annotations

from typing import Dict, Iterable, List

from ..review_document import Paragraph
from .common import ClauseResult


def paragraphs_with_concepts(
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None,
    concept_ids: Iterable[str],
) -> List[Paragraph]:
    concept_set = {str(concept) for concept in concept_ids if str(concept)}
    if not concept_set or not isinstance(review_context, dict):
        return []
    classifier = review_context.get("concept_classifier")
    if not isinstance(classifier, dict) or not isinstance(classifier.get("concepts_by_paragraph_id"), dict):
        return []
    concepts_by_paragraph_id = classifier["concepts_by_paragraph_id"]
    matches: List[Paragraph] = []
    for paragraph in paragraphs:
        paragraph_id = str(paragraph.get("id") or "")
        concepts = concepts_by_paragraph_id.get(paragraph_id, [])
        if not isinstance(concepts, list) or not concept_set.intersection(str(concept) for concept in concepts):
            continue
        matches.append(paragraph)
    return matches


def attach_structure_context(
    result: ClauseResult,
    review_context: Dict[str, object] | None,
    concept_ids: Iterable[str],
) -> ClauseResult:
    concept_set = {str(concept) for concept in concept_ids if str(concept)}
    result["structure_context"] = {
        "concepts": sorted(concept_set),
        "sections": _section_context(review_context, concept_set),
        "reference_count": _reference_count(review_context),
    }
    return result


def merge_paragraphs(*paragraph_groups: Iterable[Paragraph]) -> List[Paragraph]:
    merged: List[Paragraph] = []
    seen = set()
    for paragraphs in paragraph_groups:
        for paragraph in paragraphs:
            dedup_key = paragraph.get("id") or (paragraph.get("start"), paragraph.get("end"), paragraph.get("text"))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            merged.append(paragraph)
    return merged


def _section_context(review_context: Dict[str, object] | None, concept_set: set[str]) -> List[Dict[str, object]]:
    if not concept_set or not isinstance(review_context, dict):
        return []
    classifier = review_context.get("concept_classifier")
    if not isinstance(classifier, dict) or not isinstance(classifier.get("sections"), list):
        return []
    sections: List[Dict[str, object]] = []
    for section in classifier["sections"]:
        if not isinstance(section, dict):
            continue
        concepts = section.get("concepts", [])
        if not isinstance(concepts, list):
            continue
        matched_concepts = sorted(concept_set.intersection(str(concept) for concept in concepts))
        if not matched_concepts:
            continue
        sections.append({
            "section_id": str(section.get("section_id") or ""),
            "label": str(section.get("label") or ""),
            "concepts": matched_concepts,
            "paragraph_ids": [
                str(paragraph_id)
                for paragraph_id in section.get("paragraph_ids", [])
                if paragraph_id
            ],
        })
    return sections


def _reference_count(review_context: Dict[str, object] | None) -> int:
    if not isinstance(review_context, dict):
        return 0
    reference_resolver = review_context.get("reference_resolver")
    if not isinstance(reference_resolver, dict) or not isinstance(reference_resolver.get("references"), list):
        return 0
    return len(reference_resolver["references"])
