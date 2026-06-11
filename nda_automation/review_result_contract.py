"""Helpers that own the shared review result dictionary contract."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .redline_edit_contract import normalize_redline_edits, redline_inserted_text, redline_replacement_text
from .review_document import EvidenceProvenanceError, validate_clause_evidence_trust

PROPOSED_CHANGE_CONTRACT_VERSION = 1
PROPOSED_CHANGE_REPLACE = "replace"
PROPOSED_CHANGE_INSERT = "insert"
PROPOSED_CHANGE_DELETE = "delete"
PROPOSED_CHANGE_COMMENT_ONLY = "comment_only"
PROPOSED_CHANGE_NEEDS_HUMAN_CHOICE = "needs_human_choice"


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
    finalized_redline_edits = list(redline_edits)
    finalized_clauses = attach_proposed_changes_to_clauses(clauses, finalized_redline_edits)
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
        "clauses": finalized_clauses,
        "redline_edits": finalized_redline_edits,
        "proposed_changes": proposed_changes_from_clauses(finalized_clauses),
    })
    if result_fields:
        result.update(result_fields)

    evidence_errors = validate_clause_evidence_trust(result, source_text)
    if evidence_errors:
        raise EvidenceProvenanceError(f"{evidence_error_prefix}: " + "; ".join(evidence_errors))
    result["evidence_trust"] = {"status": "verified", "errors": []}
    return result


def attach_proposed_changes_to_clauses(
    clauses: Sequence[dict[str, Any]],
    redline_edits: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return clause copies carrying structured proposed-change records.

    Existing review payload keys stay untouched. This adds one public object for
    every non-pass clause so callers do not have to reverse-engineer proposed
    change intent from redline edits, evidence fields, and Playbook rationale.
    """
    redlines_by_clause = _redlines_by_clause_id(redline_edits)
    finalized: list[dict[str, Any]] = []
    for clause in clauses:
        clause_copy = dict(clause)
        if _clause_needs_proposed_change(clause_copy):
            clause_copy["proposed_change"] = build_proposed_change(
                clause_copy,
                redlines_by_clause.get(str(clause_copy.get("id") or "")),
            )
        else:
            clause_copy.pop("proposed_change", None)
        finalized.append(clause_copy)
    return finalized


def proposed_changes_from_clauses(clauses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        proposed_change = clause.get("proposed_change")
        if isinstance(proposed_change, dict):
            changes.append(proposed_change)
    return changes


def build_proposed_change(
    clause: dict[str, Any],
    redline_edit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    redline_edit = normalize_redline_edits([redline_edit], require_content=False)[0] if redline_edit else None
    proposed_action = _proposed_change_action(clause, redline_edit)
    safety = _proposed_change_safety(clause, proposed_action, redline_edit)
    evidence = _proposed_change_evidence(clause, redline_edit)
    proposed: dict[str, Any] = {
        "version": PROPOSED_CHANGE_CONTRACT_VERSION,
        "clause_id": str(clause.get("id") or ""),
        "clause_name": str(clause.get("name") or clause.get("clause_name") or clause.get("id") or ""),
        "decision": str(clause.get("decision") or ""),
        "issue_type": str(clause.get("issue_type") or ""),
        "issue_summary": _issue_summary(clause),
        "playbook_rationale": _playbook_rationale(clause),
        "evidence": evidence,
        "action": proposed_action,
        "confidence": _clean_confidence(clause.get("confidence")),
        "safety": safety,
    }
    if redline_edit is not None:
        proposed["redline_edit_id"] = str(redline_edit.get("id") or "")
        proposed["redline_action"] = str(redline_edit.get("action") or "")
        proposed["source_text"] = _redline_source_text(redline_edit)
        proposed_text = _redline_proposed_text(redline_edit)
        if proposed_text:
            proposed["proposed_text"] = proposed_text
        for key in ("paragraph_id", "paragraph_index", "source_index", "source_part"):
            value = redline_edit.get(key)
            if value not in (None, ""):
                proposed[key] = value
    return proposed


def _clause_needs_proposed_change(clause: dict[str, Any]) -> bool:
    return str(clause.get("decision") or "") in {"fail", "review"}


def _redlines_by_clause_id(redline_edits: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    redlines: dict[str, dict[str, Any]] = {}
    for redline in normalize_redline_edits(list(redline_edits), require_content=False):
        clause_id = str(redline.get("clause_id") or "")
        if clause_id and clause_id not in redlines:
            redlines[clause_id] = redline
    return redlines


def _proposed_change_action(clause: dict[str, Any], redline_edit: dict[str, Any] | None) -> str:
    if redline_edit is not None:
        redline_action = str(redline_edit.get("action") or "")
        if redline_action == REDLINE_REPLACE_PARAGRAPH:
            return PROPOSED_CHANGE_REPLACE
        if redline_action == REDLINE_INSERT_AFTER_PARAGRAPH:
            return PROPOSED_CHANGE_INSERT
        if redline_action == REDLINE_DELETE_PARAGRAPH:
            return PROPOSED_CHANGE_DELETE
    if str(clause.get("decision") or "") == "review":
        return PROPOSED_CHANGE_NEEDS_HUMAN_CHOICE
    return PROPOSED_CHANGE_COMMENT_ONLY


def _proposed_change_safety(
    clause: dict[str, Any],
    proposed_action: str,
    redline_edit: dict[str, Any] | None,
) -> dict[str, Any]:
    if redline_edit is not None:
        return {
            "status": "proposed_redline_available",
            "requires_human_approval": True,
            "reason": "A normalized redline is available, but it must be approved by a human before use.",
        }
    if proposed_action == PROPOSED_CHANGE_NEEDS_HUMAN_CHOICE:
        return {
            "status": "needs_human_choice",
            "requires_human_approval": True,
            "reason": _no_redline_reason(clause) or "The clause needs human review before a safe edit can be proposed.",
        }
    return {
        "status": "comment_only",
        "requires_human_approval": True,
        "reason": _no_redline_reason(clause) or "No safe automatic redline is available for this finding.",
    }


def _proposed_change_evidence(clause: dict[str, Any], redline_edit: dict[str, Any] | None) -> dict[str, Any]:
    citation = clause.get("citation")
    if isinstance(citation, dict):
        quote = _clean_text(citation.get("quote"))
        paragraph_id = _clean_text(citation.get("paragraph_id"))
        if quote or paragraph_id:
            return {"quote": quote, "paragraph_id": paragraph_id}
    structured_evidence = clause.get("structured_evidence")
    if isinstance(structured_evidence, list):
        for record in structured_evidence:
            if not isinstance(record, dict):
                continue
            quote = _clean_text(record.get("matched_text")) or _clean_text(record.get("text"))
            paragraph_id = _clean_text(record.get("paragraph_id"))
            if quote or paragraph_id:
                return {"quote": quote, "paragraph_id": paragraph_id}
    evidence_paragraphs = clause.get("evidence_paragraphs")
    if isinstance(evidence_paragraphs, list) and evidence_paragraphs:
        paragraph = evidence_paragraphs[0]
        if isinstance(paragraph, dict):
            return {
                "quote": _clean_text(paragraph.get("text")),
                "paragraph_id": _clean_text(paragraph.get("id")),
            }
    if redline_edit is not None:
        return {
            "quote": _redline_source_text(redline_edit),
            "paragraph_id": _clean_text(redline_edit.get("paragraph_id")),
        }
    return {"quote": "", "paragraph_id": ""}


def _issue_summary(clause: dict[str, Any]) -> str:
    for key in ("issue_label", "finding", "decision_reason", "reason"):
        value = _clean_text(clause.get(key))
        if value:
            return value
    return "This clause needs attention under the Playbook."


def _playbook_rationale(clause: dict[str, Any]) -> str:
    redline_rationale = clause.get("redline_rationale")
    if isinstance(redline_rationale, dict):
        explanation = _clean_text(redline_rationale.get("explanation"))
        if explanation:
            return explanation
    for key in ("rationale", "requirement", "evidence_guidance", "what_to_fix"):
        value = _clean_text(clause.get(key))
        if value:
            return value
    return ""


def _redline_source_text(redline_edit: dict[str, Any]) -> str:
    for key in ("original_text", "anchor_text"):
        value = _clean_text(redline_edit.get(key))
        if value:
            return value
    return ""


def _redline_proposed_text(redline_edit: dict[str, Any]) -> str:
    if str(redline_edit.get("action") or "") == REDLINE_DELETE_PARAGRAPH:
        return ""
    return _clean_text(redline_replacement_text(redline_edit) or redline_inserted_text(redline_edit))


def _no_redline_reason(clause: dict[str, Any]) -> str:
    ai_first = clause.get("ai_first_assessment")
    if isinstance(ai_first, dict) and str(ai_first.get("grounding_status") or ""):
        status = str(ai_first.get("grounding_status") or "")
        if status != "grounded":
            return "The AI finding was not grounded enough to propose an automatic redline."
    return _clean_text(clause.get("what_to_fix"))


def _clean_confidence(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, confidence))


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


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
