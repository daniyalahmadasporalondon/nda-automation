from __future__ import annotations

import re
from typing import Dict, List

Paragraph = Dict[str, object]
STRUCTURAL_METADATA_KEYS = (
    "heading_level",
    "numbering",
    "outline_level",
    "page_number",
    "runs",
    "source_kind",
    "source_part",
    "source_index",
    "structure_label",
    "structure_number",
    "style_id",
    "style_name",
    "table",
)


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
        paragraph_text = str(paragraph.get("text", ""))
        paragraph_parts = split_document_paragraphs(paragraph_text)
        if not paragraph_parts:
            continue

        for paragraph_part in paragraph_parts:
            paragraph_text = str(paragraph_part.get("text", "")).strip()
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
            for key in STRUCTURAL_METADATA_KEYS:
                if key in paragraph:
                    aligned_paragraph[key] = paragraph[key]
            # ``runs`` describes the whole extracted paragraph. If that paragraph
            # was re-split into parts here, the run breakdown no longer matches a
            # part, so drop it and fall back to the part's flat text.
            if "runs" in aligned_paragraph and (len(paragraph_parts) > 1 or paragraph_text != str(paragraph.get("text", "")).strip()):
                del aligned_paragraph["runs"]
            aligned.append(aligned_paragraph)
    return aligned


def validate_clause_evidence_trust(review_result: Dict[str, object], source_text: str | None = None) -> List[str]:
    errors: List[str] = []
    paragraphs = review_result.get("paragraphs", [])
    clauses = review_result.get("clauses", [])
    review_state = review_result.get("review_state", {})
    if not isinstance(paragraphs, list):
        return ["review result paragraphs must be a list"]
    if not isinstance(clauses, list):
        return ["review result clauses must be a list"]
    if not isinstance(review_state, dict):
        errors.append("review_state must be an object")
        review_state = {}

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
        structured_evidence = clause.get("structured_evidence", [])
        audit_trace = clause.get("audit_trace", {})
        clause_review_state = clause.get("review_state", {})
        reason_code = clause.get("reason_code")
        reason_codes = clause.get("reason_codes")
        if not isinstance(matched_ids, list):
            errors.append(f"{clause_id}: matched_paragraph_ids must be a list")
            continue
        if not isinstance(evidence, list):
            errors.append(f"{clause_id}: evidence must be a list")
            evidence = []
        if not isinstance(evidence_paragraphs, list):
            errors.append(f"{clause_id}: evidence_paragraphs must be a list")
            evidence_paragraphs = []
        if not isinstance(structured_evidence, list):
            errors.append(f"{clause_id}: structured_evidence must be a list")
            structured_evidence = []
        if not isinstance(audit_trace, dict):
            errors.append(f"{clause_id}: audit_trace must be an object")
            audit_trace = {}
        if not isinstance(clause_review_state, dict):
            errors.append(f"{clause_id}: review_state must be an object")
            clause_review_state = {}
        if reason_code is not None and not isinstance(reason_code, str):
            errors.append(f"{clause_id}: reason_code must be a string")
        if not isinstance(reason_codes, list):
            errors.append(f"{clause_id}: reason_codes must be a list")
            reason_codes = []
        elif reason_code is not None and reason_codes and reason_codes[0] != reason_code:
            errors.append(f"{clause_id}: reason_code does not match first reason_codes entry")
        review_state_decision = clause_review_state.get("decision")
        if review_state_decision is not None and review_state_decision != clause.get("decision"):
            errors.append(f"{clause_id}: review_state decision does not match clause decision")
        review_state_reason_code = clause_review_state.get("reason_code")
        if review_state_reason_code is not None and review_state_reason_code != reason_code:
            errors.append(f"{clause_id}: review_state reason_code does not match clause reason_code")
        review_state_reason_codes = clause_review_state.get("reason_codes")
        if review_state_reason_codes is not None and review_state_reason_codes != reason_codes:
            errors.append(f"{clause_id}: review_state reason_codes do not match clause reason_codes")
        review_state_value = clause_review_state.get("state")
        if review_state_value == "review" and clause.get("needs_review") is not True:
            errors.append(f"{clause_id}: review_state review does not match needs_review")
        if review_state_value == "check" and clause.get("decision") != "fail":
            errors.append(f"{clause_id}: review_state check does not match clause decision")
        if review_state_value == "pass" and clause.get("decision") != "pass":
            errors.append(f"{clause_id}: review_state pass does not match clause decision")

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

        structured_ids = []
        for record in structured_evidence:
            if not isinstance(record, dict):
                errors.append(f"{clause_id}: structured_evidence record is not an object")
                continue
            paragraph_id = str(record.get("paragraph_id") or "")
            structured_ids.append(paragraph_id)
            source_paragraph = paragraphs_by_id.get(paragraph_id)
            if source_paragraph is None:
                errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id or 'unknown'} is not in reviewed source")
                continue
            if record.get("text") != source_paragraph.get("text"):
                errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} has drifted text")
            if record.get("reason_code") is not None and record.get("reason_code") != reason_code:
                errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} reason_code does not match clause")
            record_reason_codes = record.get("reason_codes")
            if record_reason_codes is not None and record_reason_codes != reason_codes:
                errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} reason_codes do not match clause")
            for key in ["start", "end", "source_index", "source_part", "source_kind"]:
                if key in source_paragraph and record.get(key) != source_paragraph.get(key):
                    errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} has drifted {key}")
                elif key not in source_paragraph and record.get(key) is not None:
                    errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} has unexpected {key}")
            match_spans = record.get("match_spans", [])
            if not isinstance(match_spans, list):
                errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} match_spans must be a list")
                continue
            for span in match_spans:
                if not isinstance(span, dict):
                    errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} match span is not an object")
                    continue
                start = span.get("start")
                end = span.get("end")
                span_text = str(span.get("text") or "")
                if not isinstance(start, int) or not isinstance(end, int) or source_text is None:
                    continue
                if source_text[start:end] != span_text:
                    errors.append(f"{clause_id}: structured evidence paragraph {paragraph_id} match span has drifted text")
        expected_structured_ids = [str(paragraph.get("id")) for paragraph in expected_paragraphs]
        if structured_ids != expected_structured_ids:
            errors.append(f"{clause_id}: structured_evidence ids do not match matched source paragraphs")

        trace_decision = audit_trace.get("decision")
        if trace_decision is not None and trace_decision != clause.get("decision"):
            errors.append(f"{clause_id}: audit_trace decision does not match clause decision")
        trace_reason = audit_trace.get("decision_reason")
        if trace_reason is not None and trace_reason != clause.get("decision_reason"):
            errors.append(f"{clause_id}: audit_trace decision_reason does not match clause decision_reason")
        trace_reason_code = audit_trace.get("reason_code")
        if trace_reason_code is not None and trace_reason_code != reason_code:
            errors.append(f"{clause_id}: audit_trace reason_code does not match clause reason_code")
        trace_reason_codes = audit_trace.get("reason_codes")
        if trace_reason_codes is not None and trace_reason_codes != reason_codes:
            errors.append(f"{clause_id}: audit_trace reason_codes do not match clause reason_codes")
        evidence_summary = audit_trace.get("evidence_summary", {})
        if isinstance(evidence_summary, dict):
            trace_paragraph_ids = evidence_summary.get("paragraph_ids", [])
            if isinstance(trace_paragraph_ids, list) and [str(paragraph_id) for paragraph_id in trace_paragraph_ids] != expected_structured_ids:
                errors.append(f"{clause_id}: audit_trace paragraph ids do not match matched source paragraphs")
            trace_structured_count = evidence_summary.get("structured_evidence_count")
            if isinstance(trace_structured_count, int) and trace_structured_count != len(structured_evidence):
                errors.append(f"{clause_id}: audit_trace structured evidence count does not match structured_evidence")
        elif audit_trace:
            errors.append(f"{clause_id}: audit_trace evidence_summary must be an object")

    if review_state:
        counts = review_state.get("counts", {})
        if isinstance(counts, dict):
            expected_counts = {
                "pass": review_result.get("requirements_passed", 0),
                "review": review_result.get("requirements_needs_review", 0),
                "check": review_result.get("requirements_failed", 0),
            }
            for key, expected in expected_counts.items():
                try:
                    if int(counts.get(key) or 0) != int(expected or 0):
                        errors.append(f"review_state {key} count does not match requirements count")
                except (TypeError, ValueError):
                    errors.append(f"review_state {key} count is not an integer")
        else:
            errors.append("review_state counts must be an object")
        if review_state.get("overall_status") and review_state.get("overall_status") != review_result.get("overall_status"):
            errors.append("review_state overall_status does not match review result")

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
