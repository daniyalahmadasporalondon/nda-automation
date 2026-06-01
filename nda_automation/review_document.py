from __future__ import annotations

import re
from typing import Dict, List

Paragraph = Dict[str, object]


class ParagraphAlignmentError(ValueError):
    pass


class EvidenceProvenanceError(RuntimeError):
    pass


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
        if "source_part" in paragraph:
            aligned_paragraph["source_part"] = paragraph["source_part"]
        aligned.append(aligned_paragraph)
    return aligned


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
        expected_evidence_paragraphs = expected_paragraphs[:len(evidence_paragraphs)]
        expected_evidence_texts = [str(paragraph.get("text", "")) for paragraph in expected_paragraphs[:len(evidence)]]
        if len(evidence) > len(expected_paragraphs):
            errors.append(f"{clause_id}: evidence has more entries than matched source paragraphs")
        if len(evidence_paragraphs) > len(expected_paragraphs):
            errors.append(f"{clause_id}: evidence_paragraphs has more entries than matched source paragraphs")
        if len(evidence) != len(evidence_paragraphs):
            errors.append(f"{clause_id}: evidence and evidence_paragraphs have different lengths")
        if evidence != expected_evidence_texts:
            errors.append(f"{clause_id}: evidence text does not equal evidence source paragraphs")
        if [str(item.get("id")) for item in evidence_paragraphs if isinstance(item, dict)] != [str(paragraph.get("id")) for paragraph in expected_evidence_paragraphs]:
            errors.append(f"{clause_id}: evidence_paragraphs ids do not match evidence source paragraphs")
        for evidence_paragraph, source_paragraph in zip(evidence_paragraphs, expected_evidence_paragraphs):
            if not isinstance(evidence_paragraph, dict):
                errors.append(f"{clause_id}: evidence_paragraph is not an object")
                continue
            for key in ["id", "index", "text", "start", "end", "source_index", "source_part"]:
                if key in source_paragraph and evidence_paragraph.get(key) != source_paragraph.get(key):
                    errors.append(f"{clause_id}: evidence paragraph {source_paragraph.get('id')} has drifted {key}")
                elif key not in source_paragraph and key in evidence_paragraph:
                    errors.append(f"{clause_id}: evidence paragraph {source_paragraph.get('id')} has unexpected {key}")

    return errors


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
