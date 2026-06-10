from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .ai_review import apply_ai_review
from .ai_verifier import apply_ai_verifier
from .checks.common import _normalize
from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .decision_arbiter import deterministic_decision
from .playbook_rules import normalize_playbook_policy
from .redline_rationale import attach_redline_rationales
from .reference_resolver import resolve_document_references
from .review_document import Paragraph, align_document_paragraphs, split_document_paragraphs
from .review_result_contract import build_review_result, review_result_clause_counts
from .review_state import aggregate_review_state
from .semantic import apply_semantic_fallback
from .semantic_crosscheck import apply_semantic_crosscheck

SemanticEvaluateFn = Callable[..., dict[str, Any] | None]
AIReviewFn = Callable[..., dict[str, Any] | None]
VerifierFn = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class ReviewCommand:
    """One command for the deterministic review workflow."""

    text: str
    paragraphs: Sequence[Paragraph] | None = None
    playbook: Mapping[str, Any] | None = None
    semantic_evaluator: SemanticEvaluateFn | None = None
    ai_reviewer: AIReviewFn | None = None
    ai_verifier: VerifierFn | None = None
    verify: bool = True
    ai_enabled: bool = True


def orchestrate_review(command: ReviewCommand) -> dict[str, Any]:
    """Run deterministic review, semantic overlays, AI overlays, evidence, and redlines."""
    from . import checker

    source_text = command.text or ""
    if command.paragraphs is None:
        document_paragraphs = split_document_paragraphs(source_text)
    else:
        if not source_text:
            source_text = "\n\n".join(str(paragraph["text"]) for paragraph in command.paragraphs)
        document_paragraphs = align_document_paragraphs(command.paragraphs, source_text)

    normalized = _normalize(source_text)
    review_playbook = dict(command.playbook) if isinstance(command.playbook, Mapping) else checker.load_playbook()
    checker._validate_playbook_contract(review_playbook)
    review_playbook = normalize_playbook_policy(review_playbook)
    clauses_by_id = {clause["id"]: clause for clause in review_playbook["clauses"]}

    contract_structure = build_contract_structure(document_paragraphs)
    reference_resolver = resolve_document_references(document_paragraphs, contract_structure)
    concept_classifier = classify_document_concepts(document_paragraphs, contract_structure)
    review_context: dict[str, object] = {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }

    clause_results = []
    for clause_id, check in checker.CLAUSE_CHECKS:
        clause = clauses_by_id[clause_id]
        clause_result = check(source_text, normalized, clause, document_paragraphs, review_context)
        clause_results.append(
            apply_semantic_fallback(
                text=source_text,
                normalized=normalized,
                clause=clause,
                paragraphs=document_paragraphs,
                current_result=clause_result,
                evaluator=command.semantic_evaluator,
            )
        )
    clause_results, semantic_crosscheck = apply_semantic_crosscheck(
        clause_results=clause_results,
        clauses_by_id=clauses_by_id,
        paragraphs=document_paragraphs,
    )
    for clause in clause_results:
        clause["deterministic_decision"] = deterministic_decision(clause)
    clause_results, ai_review = apply_ai_review(
        clause_results=clause_results,
        clauses_by_id=clauses_by_id,
        paragraphs=document_paragraphs,
        review_context=review_context,
        reviewer=command.ai_reviewer,
        ai_enabled=command.ai_enabled,
    )
    for clause in clause_results:
        checker._apply_clause_decision(clause)
    clause_results, ai_verifier_review = apply_ai_verifier(
        clause_results,
        source_text=source_text,
        verifier=command.ai_verifier,
        enabled=command.verify and command.ai_enabled,
    )
    checker._refinalize_verifier_changes(clause_results, ai_verifier_review)
    counts = review_result_clause_counts(clause_results)
    redline_edits = checker._build_redline_edits(clause_results, document_paragraphs)
    attach_redline_rationales(clause_results, redline_edits, playbook_clauses_by_id=clauses_by_id)
    review_state = aggregate_review_state(
        clause_results,
        pass_count=counts["passed"],
        review_count=counts["needs_review"],
        check_count=counts["failed"],
    )

    return build_review_result(
        source_text=source_text,
        review_engine_version=checker.REVIEW_ENGINE_VERSION,
        review_state=review_state,
        paragraphs=document_paragraphs,
        contract_structure=contract_structure,
        reference_resolver=reference_resolver,
        concept_classifier=concept_classifier,
        semantic_crosscheck=semantic_crosscheck,
        ai_review=ai_review,
        ai_verifier=ai_verifier_review,
        clauses=clause_results,
        redline_edits=redline_edits,
        result_fields={
            "unmatched_sections": checker.compute_unmatched_sections(contract_structure, clause_results),
        },
        evidence_error_prefix="Clause evidence provenance drift detected",
    )
