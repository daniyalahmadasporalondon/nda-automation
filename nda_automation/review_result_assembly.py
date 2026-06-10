from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .checks.common import ClauseResult, RedlineEdit
from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .redline_rationale import attach_redline_rationales
from .reference_resolver import resolve_document_references
from .review_document import (
    EvidenceProvenanceError,
    Paragraph,
    align_document_paragraphs,
    split_document_paragraphs,
    validate_clause_evidence_trust,
)
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
    aggregate_review_state,
)

ReviewContext = dict[str, object]
RedlineBuildFn = Callable[[list[ClauseResult], list[Paragraph]], list[RedlineEdit]]
VerifierClauseFinalizer = Callable[[ClauseResult], None]


def prepare_review_document(
    source_text: str,
    paragraphs: Sequence[Paragraph] | None = None,
    *,
    infer_text_from_paragraphs: bool = False,
) -> tuple[str, list[Paragraph]]:
    """Return the source text and review-aligned paragraphs for a review run."""
    text = source_text or ""
    if paragraphs is None:
        return text, split_document_paragraphs(text)

    raw_paragraphs = list(paragraphs)
    if text:
        return text, align_document_paragraphs(raw_paragraphs, text)
    if infer_text_from_paragraphs:
        text = "\n\n".join(str(paragraph["text"]) for paragraph in raw_paragraphs)
        return text, align_document_paragraphs(raw_paragraphs, text)
    return text, [deepcopy(paragraph) for paragraph in raw_paragraphs]


def build_review_context(
    paragraphs: Sequence[Paragraph],
    *,
    review_result: Mapping[str, Any] | None = None,
) -> ReviewContext:
    """Build or reuse the shared document context attached to review results."""
    paragraph_list = list(paragraphs)
    source = review_result or {}

    contract_structure = source.get("contract_structure")
    if not isinstance(contract_structure, dict):
        contract_structure = build_contract_structure(paragraph_list)

    reference_resolver = source.get("reference_resolver")
    if not isinstance(reference_resolver, dict):
        reference_resolver = resolve_document_references(paragraph_list, contract_structure)

    concept_classifier = source.get("concept_classifier")
    if not isinstance(concept_classifier, dict):
        concept_classifier = classify_document_concepts(paragraph_list, contract_structure)

    return {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }


def aggregate_clause_results(clauses: Sequence[ClauseResult]) -> dict[str, object]:
    """Return document-level review counts and aggregate review state."""
    clause_list = list(clauses)
    failed = [clause for clause in clause_list if clause.get("decision") == CLAUSE_DECISION_FAIL]
    review = [clause for clause in clause_list if clause.get("decision") == CLAUSE_DECISION_REVIEW]
    passed = [clause for clause in clause_list if clause.get("decision") == CLAUSE_DECISION_PASS]
    review_state = aggregate_review_state(
        clause_list,
        pass_count=len(passed),
        review_count=len(review),
        check_count=len(failed),
    )
    return {
        "overall_status": review_state["overall_status"],
        "review_state": review_state,
        "requirements_passed": len(passed),
        "requirements_failed": len(failed),
        "requirements_needs_review": len(review),
    }


def assemble_redline_edits(
    clause_results: Sequence[ClauseResult],
    paragraphs: Sequence[Paragraph],
    *,
    playbook_clauses_by_id: Mapping[str, Mapping[str, Any]],
    build_redline_edits: RedlineBuildFn,
) -> list[RedlineEdit]:
    """Build redlines and attach their Playbook-grounded rationales."""
    clause_list = list(clause_results)
    redline_edits = build_redline_edits(clause_list, list(paragraphs))
    attach_redline_rationales(
        clause_list,
        redline_edits,
        playbook_clauses_by_id=playbook_clauses_by_id,
    )
    return redline_edits


def verifier_changed_clause_ids(verifier_review: Mapping[str, Any]) -> set[str]:
    """Return clause IDs whose result was changed by the verifier."""
    records = verifier_review.get("records", [])
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return set()
    return {
        str(record.get("clause_id") or "")
        for record in records
        if isinstance(record, Mapping) and record.get("changed")
    }


def refinalize_verifier_changed_clauses(
    clause_results: Sequence[ClauseResult],
    verifier_review: Mapping[str, Any],
    finalizer: VerifierClauseFinalizer,
) -> None:
    """Run a caller-owned finalizer for clauses rewritten by the verifier."""
    changed_ids = verifier_changed_clause_ids(verifier_review)
    if not changed_ids:
        return
    for clause in clause_results:
        if str(clause.get("id") or "") in changed_ids:
            finalizer(clause)


def verify_evidence_trust(
    result: dict[str, Any],
    source_text: str,
    *,
    error_message: str,
    evidence_validator: Callable[[dict[str, Any], str], list[str]] = validate_clause_evidence_trust,
) -> None:
    """Validate evidence provenance and stamp the verified trust marker."""
    evidence_errors = evidence_validator(result, source_text)
    if evidence_errors:
        raise EvidenceProvenanceError(error_message + "; ".join(evidence_errors))
    result["evidence_trust"] = {"status": "verified", "errors": []}
