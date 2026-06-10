"""Helpers that own the shared review result dictionary contract."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .review_document import EvidenceProvenanceError, validate_clause_evidence_trust


def review_result_clause_counts(clauses: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Return canonical requirement counts from finalized clause decisions."""
    counts = {"passed": 0, "needs_review": 0, "failed": 0}
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        decision = str(clause.get("decision") or "")
        if decision == "pass":
            counts["passed"] += 1
        elif decision == "review":
            counts["needs_review"] += 1
        elif decision == "fail":
            counts["failed"] += 1
    return counts


def build_review_result(
    *,
    source_text: str,
    review_engine_version: int,
    review_state: dict[str, Any],
    paragraphs: Sequence[dict[str, Any]],
    contract_structure: dict[str, Any],
    reference_resolver: dict[str, Any],
    concept_classifier: dict[str, Any],
    semantic_crosscheck: dict[str, Any],
    ai_review: dict[str, Any],
    ai_verifier: dict[str, Any],
    clauses: Sequence[dict[str, Any]],
    redline_edits: Sequence[dict[str, Any]],
    checked_at: str | None = None,
    metadata_fields: dict[str, Any] | None = None,
    review_fields: dict[str, Any] | None = None,
    result_fields: dict[str, Any] | None = None,
    evidence_error_prefix: str = "Clause evidence provenance drift detected",
) -> dict[str, Any]:
    """Assemble and verify the shared review result contract."""
    counts = review_result_clause_counts(clauses)
    result: dict[str, Any] = {"review_engine_version": review_engine_version}
    if metadata_fields:
        result.update(metadata_fields)
    result.update({
        "overall_status": review_state["overall_status"],
        "review_state": review_state,
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "requirements_passed": counts["passed"],
        "requirements_failed": counts["failed"],
        "requirements_needs_review": counts["needs_review"],
        "paragraphs": list(paragraphs),
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
        "semantic_crosscheck": semantic_crosscheck,
        "ai_review": ai_review,
    })
    if review_fields:
        result.update(review_fields)
    result.update({
        "ai_verifier": ai_verifier,
        "clauses": list(clauses),
        "redline_edits": list(redline_edits),
    })
    if result_fields:
        result.update(result_fields)

    evidence_errors = validate_clause_evidence_trust(result, source_text)
    if evidence_errors:
        raise EvidenceProvenanceError(f"{evidence_error_prefix}: " + "; ".join(evidence_errors))
    result["evidence_trust"] = {"status": "verified", "errors": []}
    return result


def extracted_text_from_paragraphs(paragraphs: Sequence[dict[str, Any]]) -> str:
    """Return the canonical text serialization for extracted review paragraphs."""
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)


def attach_document_source(
    review_result: dict[str, Any],
    *,
    filename: str,
    document_type: str,
    extracted_paragraphs: Sequence[dict[str, Any]],
    extracted_text: str | None = None,
    extraction_quality: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Attach the source metadata every document-backed review result carries."""
    resolved_text = (
        extracted_text
        if extracted_text is not None
        else extracted_text_from_paragraphs(extracted_paragraphs)
    )
    review_result["source"] = {
        "filename": filename,
        "type": document_type,
        "extracted_characters": len(resolved_text),
        "extracted_paragraphs": len(extracted_paragraphs),
    }
    if extraction_quality:
        review_result["source"]["extraction_quality"] = extraction_quality
        _append_extraction_warnings(review_result, extraction_quality)
    review_result["extracted_text"] = resolved_text
    return review_result


def review_result_paragraphs(review_result: object) -> list[dict[str, Any]] | None:
    """Return cleaned review paragraphs from a review result, or None."""
    if not isinstance(review_result, dict):
        return None
    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list):
        return None
    cleaned = [paragraph for paragraph in paragraphs if isinstance(paragraph, dict)]
    return cleaned or None


def _append_extraction_warnings(
    review_result: dict[str, Any],
    extraction_quality: dict[str, object],
) -> None:
    warnings = extraction_quality.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return
    review_warnings = review_result.setdefault("review_warnings", [])
    if isinstance(review_warnings, list):
        review_warnings.extend(warnings)
