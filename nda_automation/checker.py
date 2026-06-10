from __future__ import annotations

import json
import re
import string
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List

from .redline_actions import (
    REDLINE_ACTION_LABELS,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .inline_diff import diff_text_operation_dicts
from .redline_rationale import attach_redline_rationales
from .ai_review import AIReviewFn, apply_ai_review, validate_ai_draft_fix
from .ai_verifier import VerifierFn, apply_ai_verifier, refinalize_clause_grounding
from .checks import CLAUSE_CHECKS
from .checks.signatures import SIGNATURE_FOR_LINE_PATTERN
from .semantic import SemanticEvaluateFn, apply_semantic_fallback
from .semantic_crosscheck import apply_semantic_crosscheck
from .checks.common import (
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
    ClauseResult,
    PlaybookTemplateError,
    RedlineEdit,
    _approved_laws,
    _clause_template_text,
    _governing_law_phrase,
    _max_term_years,
    _normalize,
    _paragraph_matches,
    _year_count_label,
)
from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .reference_resolver import resolve_document_references
from .playbook_rules import (
    PlaybookRulesError,
    is_dynamic_clause,
    normalize_playbook_policy,
    validate_playbook_rules,
)
from .review_document import (
    EvidenceProvenanceError as EvidenceProvenanceError,
    Paragraph,
    ParagraphAlignmentError as ParagraphAlignmentError,
    align_document_paragraphs,
    split_document_paragraphs,
    validate_clause_evidence_trust,
)
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
    aggregate_review_state,
    clause_review_state,
    reason_codes_for_clause,
)
from .decision_arbiter import (
    SEMANTIC_REVIEW_THRESHOLD,
    arbitrate,
    deterministic_decision,
)

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_PATH = ROOT / "playbook.json"
# Bump whenever the review pipeline's OUTPUT changes (engine logic, the AI
# assessment prompt, or how a finding is worded) so stored reviews are flagged
# stale and re-run on Refresh Review. v8: AI-first prompt hardening (v7) + the
# reworded ungrounded-escalation message.
REVIEW_ENGINE_VERSION = 8
AUDIT_TRACE_VERSION = 1
RedlineBuildFn = Callable[[ClauseResult, Dict[str, Paragraph], int], List[RedlineEdit]]
SIGNATURE_MARKER_LINE_PATTERN = r"^\s*(?:by|title|date)\s*:"
MISSING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE = {
    "confidential_information": (
        r"\b(?:each|both|either)\s+part(?:y|ies)\b",
        r"\b(?:disclosing|receiving)\s+part(?:y|ies)\b",
        r"\bmutual(?:ly)?\b",
    ),
    "term_and_survival": (
        r"\bconfidential information\b",
        r"\bconfidentiality\b",
        r"\b(?:does not include|shall not include|exclusions?)\b",
    ),
    "governing_law": (
        r"\b(?:term|surviv(?:e|es|ed|ing|al)|expir(?:e|es|y|ation)|terminat(?:e|es|ed|ion))\b",
        r"\bconfidentiality obligations?\b",
    ),
}
FOLLOWING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE = {
    "term_and_survival": (
        r"\bgoverning\s+law\b",
        r"\bgoverned\b.{0,120}?\blaws?\s+of\b",
    ),
}
__all__ = [
    "AIDraftValidationError",
    "AISecondOpinionError",
    "EvidenceProvenanceError",
    "ParagraphAlignmentError",
    "PlaybookTemplateError",
    "ai_validate_draft_fix",
    "ai_second_opinion_for_clause",
    "_paragraph_matches",
    "load_playbook",
    "review_nda",
    "build_contract_structure",
    "compute_unmatched_sections",
    "split_document_paragraphs",
    "validate_playbook",
    "validate_clause_evidence_trust",
]


class AISecondOpinionError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class AIDraftValidationError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def load_playbook() -> Dict[str, object]:
    try:
        with PLAYBOOK_PATH.open("r", encoding="utf-8") as handle:
            playbook = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PlaybookTemplateError("Playbook must be valid JSON.") from exc
    if not isinstance(playbook, dict):
        raise PlaybookTemplateError("Playbook must be a JSON object.")
    return playbook


def validate_playbook(playbook: Dict[str, object]) -> None:
    _validate_playbook_contract(playbook)


def review_nda(
    text: str,
    paragraphs: List[Paragraph] | None = None,
    *,
    semantic_evaluator: SemanticEvaluateFn | None = None,
    ai_reviewer: AIReviewFn | None = None,
    ai_verifier: VerifierFn | None = None,
    verify: bool = True,
    ai_enabled: bool = True,
) -> Dict[str, object]:
    source_text = text or ""
    if paragraphs is None:
        document_paragraphs = split_document_paragraphs(source_text)
    else:
        if not source_text:
            source_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
        document_paragraphs = align_document_paragraphs(paragraphs, source_text)

    normalized = _normalize(source_text)
    playbook = load_playbook()
    _validate_playbook_contract(playbook)
    playbook = normalize_playbook_policy(playbook)
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}

    contract_structure = build_contract_structure(document_paragraphs)
    reference_resolver = resolve_document_references(document_paragraphs, contract_structure)
    concept_classifier = classify_document_concepts(document_paragraphs, contract_structure)
    review_context: Dict[str, object] = {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }

    clause_results = []
    for clause_id, check in CLAUSE_CHECKS:
        clause = clauses_by_id[clause_id]
        clause_result = check(source_text, normalized, clause, document_paragraphs, review_context)
        clause_results.append(
            apply_semantic_fallback(
                text=source_text,
                normalized=normalized,
                clause=clause,
                paragraphs=document_paragraphs,
                current_result=clause_result,
                evaluator=semantic_evaluator,
            )
        )
    clause_results, semantic_crosscheck = apply_semantic_crosscheck(
        clause_results=clause_results,
        clauses_by_id=clauses_by_id,
        paragraphs=document_paragraphs,
    )
    # Snapshot the deterministic verdict (checkers + crosscheck + fallback) before
    # the AI overlay runs, so the arbiter compares against a verdict the AI never
    # touched. AI is append-only from here; the arbiter owns final precedence.
    for clause in clause_results:
        clause["deterministic_decision"] = deterministic_decision(clause)
    clause_results, ai_review = apply_ai_review(
        clause_results=clause_results,
        clauses_by_id=clauses_by_id,
        paragraphs=document_paragraphs,
        review_context=review_context,
        reviewer=ai_reviewer,
        ai_enabled=ai_enabled,
    )
    for clause in clause_results:
        _apply_clause_decision(clause)
    # Adversarial verifier pass: a focused second look that justifies-or-refutes each
    # escalated finding against the clause text/evidence. Refuted findings are
    # downgraded; unsubstantiated ones are flagged for human review. Additive overlay
    # over already-finalized findings -- it never re-runs the checkers.
    clause_results, ai_verifier_review = apply_ai_verifier(
        clause_results,
        source_text=source_text,
        verifier=ai_verifier,
        enabled=verify and ai_enabled,
    )
    # Re-finalize the derived structures (structured evidence + audit trace) for any
    # clause the verifier rewrote, so the evidence-trust contract still holds.
    _refinalize_verifier_changes(clause_results, ai_verifier_review)
    failed = [clause for clause in clause_results if clause.get("decision") == CLAUSE_DECISION_FAIL]
    review = [clause for clause in clause_results if clause.get("decision") == CLAUSE_DECISION_REVIEW]
    passed = [clause for clause in clause_results if clause.get("decision") == CLAUSE_DECISION_PASS]
    redline_edits = _build_redline_edits(clause_results, document_paragraphs)
    # Explain WHY each proposed redline exists, drawn from the Playbook clause and
    # the clause's grounded citation. Only clauses that produced an edit get one.
    attach_redline_rationales(clause_results, redline_edits, playbook_clauses_by_id=clauses_by_id)
    review_state = aggregate_review_state(
        clause_results,
        pass_count=len(passed),
        review_count=len(review),
        check_count=len(failed),
    )

    result = {
        "review_engine_version": REVIEW_ENGINE_VERSION,
        "overall_status": review_state["overall_status"],
        "review_state": review_state,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requirements_passed": len(passed),
        "requirements_failed": len(failed),
        "requirements_needs_review": len(review),
        "paragraphs": document_paragraphs,
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
        "semantic_crosscheck": semantic_crosscheck,
        "ai_review": ai_review,
        "ai_verifier": ai_verifier_review,
        "clauses": clause_results,
        "redline_edits": redline_edits,
        # Additive coverage metadata: document sections that no Playbook clause
        # matched, surfaced so a reviewer sees full section coverage instead of
        # only the handful of clauses the Playbook checks. Purely informational --
        # it does not touch clause results, statuses, counts, or the approve gate.
        "unmatched_sections": compute_unmatched_sections(contract_structure, clause_results),
    }
    evidence_errors = validate_clause_evidence_trust(result, source_text)
    if evidence_errors:
        raise EvidenceProvenanceError("Clause evidence provenance drift detected: " + "; ".join(evidence_errors))
    result["evidence_trust"] = {"status": "verified", "errors": []}
    return result


def compute_unmatched_sections(
    contract_structure: Dict[str, object] | None,
    clause_results: List[ClauseResult],
) -> List[Dict[str, object]]:
    """Return contract-structure sections that no clause covers, as neutral entries.

    A section is "covered" when at least one clause's ``matched_paragraph_ids``
    overlaps the section's ``paragraph_ids``. Sections with no overlap are surfaced
    as coverage metadata so the reviewer sees document sections the Playbook never
    examined (e.g. a 30-clause NDA where only 6 Playbook clauses match). This is
    purely additive and deterministic: it reads existing structure + clause data and
    never mutates either.
    """
    if not isinstance(contract_structure, dict):
        return []
    sections = contract_structure.get("sections")
    if not isinstance(sections, list):
        return []

    matched_paragraph_ids: set[str] = set()
    for clause in clause_results:
        if not isinstance(clause, dict):
            continue
        clause_ids = clause.get("matched_paragraph_ids")
        if not isinstance(clause_ids, list):
            continue
        for paragraph_id in clause_ids:
            if paragraph_id is not None:
                matched_paragraph_ids.add(str(paragraph_id))

    unmatched: List[Dict[str, object]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        paragraph_ids = [
            str(paragraph_id)
            for paragraph_id in section.get("paragraph_ids", [])
            if isinstance(section.get("paragraph_ids"), list) and paragraph_id is not None
        ]
        if not paragraph_ids:
            continue
        if matched_paragraph_ids.intersection(paragraph_ids):
            continue
        unmatched.append(
            {
                "id": str(section.get("id") or ""),
                "label": str(section.get("label") or section.get("heading") or "Section"),
                "heading": str(section.get("heading") or ""),
                "kind": str(section.get("kind") or ""),
                "paragraph_ids": paragraph_ids,
                "start_index": section.get("start_index") if isinstance(section.get("start_index"), int) else None,
                "end_index": section.get("end_index") if isinstance(section.get("end_index"), int) else None,
                "paragraph_count": len(paragraph_ids),
            }
        )
    return unmatched


def ai_second_opinion_for_clause(
    review_result: Dict[str, object],
    clause_id: str,
    *,
    ai_reviewer: AIReviewFn | None = None,
) -> Dict[str, object]:
    target_clause_id = str(clause_id or "").strip()
    if not target_clause_id:
        raise AISecondOpinionError("Provide a clause id for AI second opinion.")

    raw_clauses = review_result.get("clauses", [])
    if not isinstance(raw_clauses, list):
        raise AISecondOpinionError("Review result does not include clause results.")
    clauses = [deepcopy(clause) for clause in raw_clauses if isinstance(clause, dict)]
    selected_clause = next((clause for clause in clauses if str(clause.get("id") or "") == target_clause_id), None)
    if selected_clause is None:
        raise AISecondOpinionError("Selected clause was not found in the review result.", status=404)

    raw_paragraphs = review_result.get("paragraphs", [])
    if not isinstance(raw_paragraphs, list):
        raise AISecondOpinionError("Review result does not include document paragraphs.")
    paragraphs = [deepcopy(paragraph) for paragraph in raw_paragraphs if isinstance(paragraph, dict)]
    if not paragraphs:
        raise AISecondOpinionError("AI second opinion needs reviewed document paragraphs.")

    playbook = load_playbook()
    _validate_playbook_contract(playbook)
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}
    if target_clause_id not in clauses_by_id:
        raise AISecondOpinionError("Selected clause is not in the playbook.", status=404)

    review_context = _review_context_from_result(review_result, paragraphs)
    updated_clauses, ai_review = apply_ai_review(
        clause_results=[selected_clause],
        clauses_by_id=clauses_by_id,
        paragraphs=paragraphs,
        review_context=review_context,
        reviewer=ai_reviewer,
        target_clause_ids={target_clause_id},
    )
    if str(ai_review.get("status") or "") != "completed":
        error = str(ai_review.get("error") or "").strip()
        status = str(ai_review.get("status") or "unavailable").replace("_", " ")
        raise AISecondOpinionError(
            f"AI second opinion is {status}.{f' {error}' if error else ''}",
            status=409,
        )
    if not updated_clauses or int(ai_review.get("record_count") or 0) < 1:
        raise AISecondOpinionError("AI second opinion is not enabled for this clause.", status=400)

    updated_clause = updated_clauses[0]
    _apply_clause_decision(updated_clause)
    merged_clauses = [
        updated_clause if str(clause.get("id") or "") == target_clause_id else clause
        for clause in clauses
    ]
    review_state = _aggregate_clause_results(merged_clauses)
    ai_review["mode"] = "clause_second_opinion"
    ai_review["target_clause_id"] = target_clause_id
    return {
        "clause": updated_clause,
        "ai_review": ai_review,
        **review_state,
    }


def ai_validate_draft_fix(
    review_result: Dict[str, object],
    clause_id: str,
    redline_edit: Dict[str, object],
    *,
    ai_reviewer: AIReviewFn | None = None,
) -> Dict[str, object]:
    target_clause_id = str(clause_id or "").strip()
    if not target_clause_id:
        raise AIDraftValidationError("Provide a clause id for AI draft validation.")
    if not isinstance(redline_edit, dict):
        raise AIDraftValidationError("Provide a redline draft to validate.")

    raw_clauses = review_result.get("clauses", [])
    if not isinstance(raw_clauses, list):
        raise AIDraftValidationError("Review result does not include clause results.")
    clauses = [deepcopy(clause) for clause in raw_clauses if isinstance(clause, dict)]
    selected_clause = next((clause for clause in clauses if str(clause.get("id") or "") == target_clause_id), None)
    if selected_clause is None:
        raise AIDraftValidationError("Selected clause was not found in the review result.", status=404)

    cleaned_redline = _clean_draft_validation_redline(redline_edit, target_clause_id)
    raw_paragraphs = review_result.get("paragraphs", [])
    if not isinstance(raw_paragraphs, list):
        raise AIDraftValidationError("Review result does not include document paragraphs.")
    paragraphs = [deepcopy(paragraph) for paragraph in raw_paragraphs if isinstance(paragraph, dict)]
    if not paragraphs:
        raise AIDraftValidationError("AI draft validation needs reviewed document paragraphs.")

    playbook = load_playbook()
    _validate_playbook_contract(playbook)
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}
    playbook_clause = clauses_by_id.get(target_clause_id)
    if not isinstance(playbook_clause, dict):
        raise AIDraftValidationError("Selected clause is not in the playbook.", status=404)

    review_context = _review_context_from_result(review_result, paragraphs)
    ai_review = validate_ai_draft_fix(
        clause=selected_clause,
        playbook_clause=playbook_clause,
        redline_edit=cleaned_redline,
        paragraphs=paragraphs,
        review_context=review_context,
        reviewer=ai_reviewer,
    )
    if str(ai_review.get("status") or "") != "completed":
        error = str(ai_review.get("error") or "").strip()
        status = str(ai_review.get("status") or "unavailable").replace("_", " ")
        raise AIDraftValidationError(
            f"AI draft validation is {status}.{f' {error}' if error else ''}",
            status=409,
        )
    validation = ai_review.get("validation")
    if not isinstance(validation, dict):
        raise AIDraftValidationError("AI draft validation did not return a validation result.", status=500)
    return {
        "clause_id": target_clause_id,
        "redline_id": str(cleaned_redline.get("id") or ""),
        "validation": validation,
        "ai_review": ai_review,
    }


def _clean_draft_validation_redline(redline_edit: Dict[str, object], clause_id: str) -> Dict[str, object]:
    redline_clause_id = str(redline_edit.get("clause_id") or "").strip()
    if redline_clause_id and redline_clause_id != clause_id:
        raise AIDraftValidationError("Redline draft does not belong to the selected clause.")
    redline_id = str(redline_edit.get("id") or "").strip()
    if not redline_id:
        raise AIDraftValidationError("Redline draft is missing an id.")
    action = str(redline_edit.get("action") or "").strip()
    if action not in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
        raise AIDraftValidationError("Redline draft has an unsupported action.")

    original_text = str(redline_edit.get("original_text") or "").strip()
    anchor_text = str(redline_edit.get("anchor_text") or "").strip()
    replacement_text = str(redline_edit.get("replacement_text") or "").strip()
    insert_text = str(redline_edit.get("insert_text") or "").strip()
    if action == REDLINE_REPLACE_PARAGRAPH and not replacement_text:
        raise AIDraftValidationError("Replacement draft must include replacement text.")
    if action == REDLINE_INSERT_AFTER_PARAGRAPH and not (insert_text or replacement_text):
        raise AIDraftValidationError("Insertion draft must include inserted text.")
    if action == REDLINE_DELETE_PARAGRAPH and not (original_text or anchor_text):
        raise AIDraftValidationError("Deletion draft must include source text.")

    cleaned = {
        "id": redline_id,
        "clause_id": clause_id,
        "action": action,
        "action_label": str(redline_edit.get("action_label") or REDLINE_ACTION_LABELS.get(action) or ""),
        "original_text": original_text,
        "replacement_text": replacement_text,
        "insert_text": insert_text,
        "anchor_text": anchor_text,
    }
    for key in ["paragraph_id", "paragraph_index", "source_index", "source_part"]:
        if redline_edit.get(key) is not None:
            cleaned[key] = redline_edit[key]
    return cleaned


def _review_context_from_result(review_result: Dict[str, object], paragraphs: List[Paragraph]) -> Dict[str, object]:
    contract_structure = review_result.get("contract_structure")
    if not isinstance(contract_structure, dict):
        contract_structure = build_contract_structure(paragraphs)

    reference_resolver = review_result.get("reference_resolver")
    if not isinstance(reference_resolver, dict):
        reference_resolver = resolve_document_references(paragraphs, contract_structure)

    concept_classifier = review_result.get("concept_classifier")
    if not isinstance(concept_classifier, dict):
        concept_classifier = classify_document_concepts(paragraphs, contract_structure)

    return {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }


def _aggregate_clause_results(clauses: List[ClauseResult]) -> Dict[str, object]:
    failed = [clause for clause in clauses if clause.get("decision") == CLAUSE_DECISION_FAIL]
    review = [clause for clause in clauses if clause.get("decision") == CLAUSE_DECISION_REVIEW]
    passed = [clause for clause in clauses if clause.get("decision") == CLAUSE_DECISION_PASS]
    review_state = aggregate_review_state(
        clauses,
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


def _apply_clause_decision(clause: ClauseResult) -> None:
    # The DecisionArbiter is the single owner of verdict precedence (deterministic
    # vs AI, with the fail-floor). Everything else here just records its result.
    verdict = arbitrate(clause)
    decision = verdict["decision"]
    clause["decision"] = decision
    clause["needs_review"] = decision == CLAUSE_DECISION_REVIEW
    clause["decision_source"] = verdict["source"]
    if verdict["source"] == "ai":
        review_reason = str(verdict.get("reason") or "").strip() or "AI semantic review requires human review."
        clause["review_reason"] = review_reason
        clause["decision_reason"] = review_reason
        reason_code = str(verdict.get("reason_code") or "ai_semantic_review")
        clause["reason_code"] = reason_code
        clause["reason_codes"] = [reason_code]
    else:
        if not str(clause.get("decision_reason") or "").strip():
            clause["decision_reason"] = _clause_decision_reason(clause, decision)
        reason_codes = reason_codes_for_clause(clause, decision)
        clause["reason_code"] = reason_codes[0]
        clause["reason_codes"] = reason_codes
    clause["review_state"] = clause_review_state(clause, decision)
    _finalize_structured_evidence(clause, decision)
    _attach_audit_trace(clause, decision)


def _semantic_confidence(clause: ClauseResult) -> float | None:
    confidence = clause.get("semantic_confidence")
    if confidence is None:
        confidence = clause.get("confidence")
    if confidence is None:
        return None
    try:
        return float(confidence)
    except (TypeError, ValueError):
        return None


def _clause_decision_reason(clause: ClauseResult, decision: str) -> str:
    configured_reason = str(clause.get("review_reason") or "").strip()
    if configured_reason:
        return configured_reason
    confidence = _semantic_confidence(clause)
    if decision == CLAUSE_DECISION_REVIEW and confidence is not None and confidence < SEMANTIC_REVIEW_THRESHOLD:
        return f"Semantic confidence {confidence:.2f} is below the review threshold of {SEMANTIC_REVIEW_THRESHOLD:.2f}."
    if decision == CLAUSE_DECISION_REVIEW:
        return str(clause.get("reason") or clause.get("finding") or "Human review is required.").strip()
    if decision == CLAUSE_DECISION_FAIL:
        return str(clause.get("reason") or clause.get("finding") or "Clause does not satisfy the playbook.").strip()
    return str(clause.get("reason") or clause.get("finding") or "Clause satisfies the playbook.").strip()


def _finalize_structured_evidence(clause: ClauseResult, decision: str) -> None:
    structured_evidence = clause.get("structured_evidence")
    if not isinstance(structured_evidence, list):
        return
    for record in structured_evidence:
        if not isinstance(record, dict):
            continue
        record["decision"] = decision
        record["result_status"] = str(clause.get("status") or "")
        record["issue_type"] = str(clause.get("issue_type") or "")
        record["issue_label"] = str(clause.get("issue_label") or "")
        record["decision_reason"] = str(clause.get("decision_reason") or clause.get("reason") or "")
        record["reason_code"] = str(clause.get("reason_code") or "")
        record["reason_codes"] = list(clause.get("reason_codes") or [])
        if decision == CLAUSE_DECISION_REVIEW:
            record["signal_type"] = "review_evidence"
        elif decision == CLAUSE_DECISION_FAIL:
            record["signal_type"] = "check_evidence"
        elif decision == CLAUSE_DECISION_PASS and not record.get("signal_type"):
            record["signal_type"] = "pass_evidence"


def _attach_audit_trace(clause: ClauseResult, decision: str) -> None:
    structured_evidence = [
        record
        for record in clause.get("structured_evidence", [])
        if isinstance(record, dict)
    ]
    analysis_outputs = _audit_analysis_outputs(clause)
    analysis_signals = _audit_analysis_signals(clause)
    evidence_summary = _audit_evidence_summary(clause, structured_evidence, analysis_signals)
    clause["audit_trace"] = {
        "version": AUDIT_TRACE_VERSION,
        "clause_id": str(clause.get("id") or ""),
        "decision": decision,
        "status": str(clause.get("status") or ""),
        "issue_type": str(clause.get("issue_type") or ""),
        "decision_reason": str(clause.get("decision_reason") or clause.get("reason") or ""),
        "reason_code": str(clause.get("reason_code") or ""),
        "reason_codes": list(clause.get("reason_codes") or []),
        "evidence_summary": evidence_summary,
        "analysis_outputs": analysis_outputs,
        "analysis_signals": analysis_signals,
        "steps": _audit_steps(clause, decision, evidence_summary, analysis_outputs),
    }


def _refinalize_verifier_changes(
    clause_results: List[ClauseResult],
    verifier_review: Dict[str, object],
) -> None:
    """Re-derive structured evidence + audit trace for verifier-changed clauses.

    The verifier owns the new decision/reason on a clause it rewrote, but the
    structured-evidence records and audit trace are derived data the checker owns.
    Re-running the same finalizers the decision step uses keeps the evidence-trust
    contract intact without the verifier reaching into checker internals.
    """
    changed_ids = {
        str(record.get("clause_id") or "")
        for record in verifier_review.get("records", [])
        if isinstance(record, dict) and record.get("changed")
    }
    if not changed_ids:
        return
    for clause in clause_results:
        if str(clause.get("id") or "") not in changed_ids:
            continue
        decision = str(clause.get("decision") or "")
        # When the verifier cleared a disproven finding it dropped the stale reason
        # code so the clause re-derives its natural one (e.g. a refuted prohibited
        # restriction becomes "no_<clause>_restriction"). Re-derive only when absent
        # so a verifier-owned escalation code is preserved.
        if not clause.get("reason_code") and not clause.get("reason_codes"):
            reason_codes = reason_codes_for_clause(clause, decision)
            clause["reason_code"] = reason_codes[0]
            clause["reason_codes"] = reason_codes
        # Order: rebuild structured evidence -> re-derive grounding/citation from it
        # -> review_state -> audit trace. Grounding is owned by the evidence pass
        # (#16); we re-derive it after the evidence it summarizes is rebuilt.
        _finalize_structured_evidence(clause, decision)
        refinalize_clause_grounding(clause)
        clause["review_state"] = clause_review_state(clause, decision)
        _attach_audit_trace(clause, decision)


def _audit_evidence_summary(
    clause: ClauseResult,
    structured_evidence: List[Dict[str, object]],
    analysis_signals: List[Dict[str, object]],
) -> Dict[str, object]:
    matched_terms: List[str] = []
    signal_counts: Dict[str, int] = {}
    for record in structured_evidence:
        signal_type = str(record.get("signal_type") or "evidence")
        signal_counts[signal_type] = signal_counts.get(signal_type, 0) + 1
        raw_terms = record.get("matched_terms", [])
        if isinstance(raw_terms, list):
            for term in raw_terms:
                term_text = str(term).strip()
                if term_text and term_text not in matched_terms:
                    matched_terms.append(term_text)
    ignored_count = sum(1 for signal in analysis_signals if signal.get("counted") is False)
    review_signal_count = sum(1 for signal in analysis_signals if str(signal.get("signal_type") or "") == "review_evidence")
    return {
        "matched_paragraph_count": len(clause.get("matched_paragraph_ids", []))
        if isinstance(clause.get("matched_paragraph_ids"), list)
        else 0,
        "structured_evidence_count": len(structured_evidence),
        "analysis_signal_count": len(analysis_signals),
        "ignored_signal_count": ignored_count,
        "review_signal_count": review_signal_count,
        "matched_terms": matched_terms,
        "signal_counts": signal_counts,
        "paragraph_ids": [
            str(paragraph_id)
            for paragraph_id in clause.get("matched_paragraph_ids", [])
            if str(paragraph_id)
        ] if isinstance(clause.get("matched_paragraph_ids"), list) else [],
    }


def _audit_steps(
    clause: ClauseResult,
    decision: str,
    evidence_summary: Dict[str, object],
    analysis_outputs: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    structure_context = clause.get("structure_context")
    concepts = []
    reference_count = 0
    if isinstance(structure_context, dict):
        raw_concepts = structure_context.get("concepts", [])
        if isinstance(raw_concepts, list):
            concepts = [str(concept) for concept in raw_concepts if str(concept)]
        if isinstance(structure_context.get("reference_count"), int):
            reference_count = int(structure_context["reference_count"])
    matched_count = int(evidence_summary.get("matched_paragraph_count") or 0)
    analysis_keys = [str(output.get("key") or "") for output in analysis_outputs if output.get("key")]
    decision_reason = str(clause.get("decision_reason") or clause.get("reason") or "")
    reason_codes = list(clause.get("reason_codes") or [])
    return [
        {
            "name": "Input context",
            "outcome": "available" if structure_context else "not_available",
            "details": "Shared structure, reference, and concept context was attached to the checker result."
            if structure_context else "No shared structure context was attached.",
            "concepts": concepts,
            "reference_count": reference_count,
        },
        {
            "name": "Evidence collection",
            "outcome": "matched" if matched_count else "no_match",
            "details": f"{matched_count} reviewed paragraph(s) were linked to this checker result.",
            "paragraph_ids": evidence_summary.get("paragraph_ids", []),
            "structured_evidence_count": evidence_summary.get("structured_evidence_count", 0),
        },
        {
            "name": "Signal classification",
            "outcome": _audit_signal_outcome(evidence_summary),
            "details": "Structured evidence was classified into pass, review, check, or ignored signal buckets.",
            "signal_counts": evidence_summary.get("signal_counts", {}),
            "matched_terms": evidence_summary.get("matched_terms", []),
            "ignored_signal_count": evidence_summary.get("ignored_signal_count", 0),
            "review_signal_count": evidence_summary.get("review_signal_count", 0),
        },
        {
            "name": "Analysis outputs",
            "outcome": "available" if analysis_outputs else "not_available",
            "details": "Clause-specific analysis objects were attached for audit."
            if analysis_outputs else "No clause-specific analysis object was attached.",
            "analysis_keys": analysis_keys,
        },
        {
            "name": "Decision",
            "outcome": decision,
            "details": decision_reason,
            "reason_code": str(clause.get("reason_code") or ""),
            "reason_codes": reason_codes,
        },
    ]


def _audit_signal_outcome(evidence_summary: Dict[str, object]) -> str:
    signal_counts = evidence_summary.get("signal_counts", {})
    if isinstance(signal_counts, dict):
        if signal_counts.get("check_evidence"):
            return "check_evidence"
        if signal_counts.get("review_evidence"):
            return "review_evidence"
        if signal_counts.get("pass_evidence"):
            return "pass_evidence"
    if evidence_summary.get("ignored_signal_count"):
        return "ignored_signals_only"
    return "no_structured_signal"


def _audit_analysis_outputs(clause: ClauseResult) -> List[Dict[str, object]]:
    outputs: List[Dict[str, object]] = []
    for key in sorted(clause.keys()):
        if key != "structure_context" and not key.endswith("_analysis"):
            continue
        value = clause.get(key)
        if not isinstance(value, dict):
            continue
        outputs.append({
            "key": key,
            "summary": _audit_analysis_summary(value),
        })
    return outputs


def _audit_analysis_summary(value: Dict[str, object]) -> Dict[str, object]:
    summary: Dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, list):
            if all(isinstance(entry, (str, int, float, bool)) or entry is None for entry in item):
                summary[key] = item[:20]
            else:
                summary[key] = {"count": len(item)}
        elif isinstance(item, dict):
            summary[key] = {"keys": sorted(str(nested_key) for nested_key in item.keys())}
        elif isinstance(item, (str, int, float, bool)) or item is None:
            summary[key] = item
    return summary


def _audit_analysis_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    signals: List[Dict[str, object]] = []
    signals.extend(_mutuality_audit_signals(clause))
    signals.extend(_confidential_information_audit_signals(clause))
    signals.extend(_governing_law_audit_signals(clause))
    signals.extend(_term_survival_audit_signals(clause))
    signals.extend(_non_circumvention_audit_signals(clause))
    return signals[:60]


def _mutuality_audit_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    analysis = clause.get("mutuality_analysis")
    if not isinstance(analysis, dict):
        return []
    mapping = [
        ("strong_mutuality_paragraph_ids", "strong_mutuality", "pass_evidence", True),
        ("weak_mutuality_paragraph_ids", "weak_mutuality", "review_evidence", True),
        ("role_definition_paragraph_ids", "role_definition", "review_evidence", True),
        ("one_way_paragraph_ids", "one_way_language", "check_evidence", True),
    ]
    return _paragraph_id_analysis_signals("mutuality_analysis", analysis, mapping)


def _confidential_information_audit_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    analysis = clause.get("confidential_information_analysis")
    if not isinstance(analysis, dict):
        return []
    mapping = [
        ("definition_paragraph_ids", "definition_anchor", "pass_evidence", True),
        ("explicit_problematic_exclusion_paragraph_ids", "problematic_exclusion", "check_evidence", True),
        ("usage_right_review_paragraph_ids", "usage_right_language", "review_evidence", True),
    ]
    signals = _paragraph_id_analysis_signals("confidential_information_analysis", analysis, mapping)
    coverage_hits = analysis.get("coverage_hits", [])
    if isinstance(coverage_hits, list):
        for hit in coverage_hits[:20]:
            signals.append({
                "source": "confidential_information_analysis",
                "classification": "coverage_hit",
                "signal_type": "pass_evidence",
                "counted": True,
                "matched_text": str(hit),
                "reason": "Coverage term was found inside the Confidential Information definition.",
            })
    return signals


def _governing_law_audit_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    analysis = clause.get("governing_law_analysis")
    if not isinstance(analysis, dict):
        return []
    mapping = [
        ("approved_paragraph_ids", "approved_governing_law", "pass_evidence", True),
        ("unclear_paragraph_ids", "unclear_governing_law", "review_evidence", True),
        ("unapproved_paragraph_ids", "unapproved_governing_law", "check_evidence", True),
        ("heading_only_paragraph_ids", "heading_only", "review_evidence", True),
    ]
    signals = _paragraph_id_analysis_signals("governing_law_analysis", analysis, mapping)
    candidate_records = analysis.get("candidate_records", [])
    if isinstance(candidate_records, list):
        for record in candidate_records[:30]:
            if not isinstance(record, dict):
                continue
            needs_review = bool(record.get("needs_review"))
            approved = bool(record.get("approved"))
            signals.append({
                "source": "governing_law_analysis",
                "paragraph_id": str(record.get("paragraph_id") or ""),
                "classification": "candidate_record",
                "signal_type": "review_evidence" if needs_review else ("pass_evidence" if approved else "check_evidence"),
                "counted": True,
                "matched_text": str(record.get("value") or ""),
                "reason": "Governing-law candidate was classified as approved, unclear, or unapproved.",
                "metadata": {
                    "approved": approved,
                    "needs_review": needs_review,
                },
            })
    return signals


def _term_survival_audit_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    analysis = clause.get("term_survival_analysis")
    if not isinstance(analysis, dict):
        return []
    signals: List[Dict[str, object]] = []
    references = analysis.get("references", [])
    if isinstance(references, list):
        for reference in references[:30]:
            if not isinstance(reference, dict):
                continue
            status = str(reference.get("status") or "")
            ordinary_confidentiality = bool(reference.get("ordinary_confidentiality"))
            unresolved = bool(reference.get("unresolved_numbers"))
            review_signal = status in {"partial", "unresolved"} or unresolved or not ordinary_confidentiality
            signals.append({
                "source": "term_survival_analysis",
                "paragraph_id": str(reference.get("paragraph_id") or ""),
                "classification": "survival_reference",
                "signal_type": "review_evidence" if review_signal else "pass_evidence",
                "counted": True,
                "matched_text": str(reference.get("reference_text") or ""),
                "reason": "Survival cross-reference was resolved and checked against ordinary confidentiality concepts.",
                "metadata": {
                    "status": status,
                    "ordinary_confidentiality": ordinary_confidentiality,
                    "unresolved_numbers": reference.get("unresolved_numbers", []),
                },
            })
    return signals


def _non_circumvention_audit_signals(clause: ClauseResult) -> List[Dict[str, object]]:
    analysis = clause.get("non_circumvention_analysis")
    if not isinstance(analysis, dict):
        return []
    mapping = [
        ("prohibited_paragraph_ids", "prohibited_restriction", "check_evidence", True),
        ("review_paragraph_ids", "possible_restriction", "review_evidence", True),
        ("lawful_circumvention_paragraph_ids", "lawful_circumvention_context", "ignored_evidence", False),
        ("negated_reference_paragraph_ids", "negated_reference", "ignored_evidence", False),
    ]
    signals = _paragraph_id_analysis_signals("non_circumvention_analysis", analysis, mapping)
    signal_records = analysis.get("signal_records", [])
    if isinstance(signal_records, list):
        for record in signal_records[:30]:
            if not isinstance(record, dict):
                continue
            classification = str(record.get("classification") or "")
            signals.append({
                "source": "non_circumvention_analysis",
                "paragraph_id": str(record.get("paragraph_id") or ""),
                "classification": classification,
                "signal_type": _non_circumvention_signal_type(classification),
                "counted": classification != "negated_reference",
                "reason": "Non-circumvention signal was classified as prohibited, review-only, or ignored.",
                "metadata": {
                    "matched_pattern_count": record.get("matched_pattern_count", 0),
                },
            })
    references = analysis.get("references", [])
    if isinstance(references, list):
        for reference in references[:30]:
            if not isinstance(reference, dict):
                continue
            status = str(reference.get("status") or "")
            signals.append({
                "source": "non_circumvention_analysis",
                "paragraph_id": str(reference.get("paragraph_id") or ""),
                "classification": "non_circumvention_reference",
                "signal_type": _non_circumvention_reference_signal_type(status),
                "counted": status != "negated",
                "matched_text": str(reference.get("reference_text") or ""),
                "reason": "Non-circumvention cross-reference was resolved and classified.",
                "metadata": {
                    "status": status,
                    "resolver_status": reference.get("resolver_status", ""),
                    "unresolved_numbers": reference.get("unresolved_numbers", []),
                },
            })
    return signals


def _paragraph_id_analysis_signals(
    source: str,
    analysis: Dict[str, object],
    mapping: List[tuple[str, str, str, bool]],
) -> List[Dict[str, object]]:
    signals: List[Dict[str, object]] = []
    for field, classification, signal_type, counted in mapping:
        paragraph_ids = analysis.get(field, [])
        if not isinstance(paragraph_ids, list):
            continue
        for paragraph_id in paragraph_ids:
            if not str(paragraph_id):
                continue
            signals.append({
                "source": source,
                "paragraph_id": str(paragraph_id),
                "classification": classification,
                "signal_type": signal_type,
                "counted": counted,
                "reason": f"{field} included this paragraph.",
            })
    return signals


def _non_circumvention_signal_type(classification: str) -> str:
    if classification == "prohibited":
        return "check_evidence"
    if classification == "review":
        return "review_evidence"
    return "ignored_evidence"


def _non_circumvention_reference_signal_type(status: str) -> str:
    if status == "prohibited":
        return "check_evidence"
    if status in {"partial", "unresolved", "review", "no_non_circumvention_signal"}:
        return "review_evidence"
    return "ignored_evidence"


def _validate_check_registry() -> None:
    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    duplicate_check_ids = sorted({clause_id for clause_id in check_ids if check_ids.count(clause_id) > 1})
    if duplicate_check_ids:
        raise RuntimeError(f"Duplicate checker IDs: {', '.join(duplicate_check_ids)}")

    playbook_clauses = load_playbook()["clauses"]
    playbook_ids = [str(clause["id"]) for clause in playbook_clauses]
    duplicate_playbook_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_playbook_ids:
        raise RuntimeError(f"Duplicate playbook IDs: {', '.join(duplicate_playbook_ids)}")

    missing_search_terms = [
        str(clause["id"])
        for clause in playbook_clauses
        if not _required_clause_terms(clause, "search_terms")
    ]
    if missing_search_terms:
        raise RuntimeError(f"Playbook clauses missing search_terms: {', '.join(missing_search_terms)}")

    # Only native clauses are backed by checks; dynamic clauses are reviewed
    # generically and intentionally have no Python check.
    native_ids = {str(clause["id"]) for clause in playbook_clauses if not is_dynamic_clause(clause)}
    missing_checks = sorted(set(check_ids) - native_ids)
    extra_checks = sorted(native_ids - set(check_ids))
    if missing_checks or extra_checks:
        detail = []
        if missing_checks:
            detail.append(f"missing checks for: {', '.join(missing_checks)}")
        if extra_checks:
            detail.append(f"checks without playbook clauses: {', '.join(extra_checks)}")
        raise RuntimeError("Checker registry does not match playbook (" + "; ".join(detail) + ")")

    builder_ids = [clause_id for clause_id, _builder in REDLINE_BUILDERS]
    duplicate_builder_ids = sorted({clause_id for clause_id in builder_ids if builder_ids.count(clause_id) > 1})
    if duplicate_builder_ids:
        raise RuntimeError(f"Duplicate redline builder IDs: {', '.join(duplicate_builder_ids)}")

    if builder_ids != check_ids:
        missing_builders = sorted(set(check_ids) - set(builder_ids))
        extra_builders = sorted(set(builder_ids) - set(check_ids))
        detail = []
        if missing_builders:
            detail.append(f"missing redline builders for: {', '.join(missing_builders)}")
        if extra_builders:
            detail.append(f"redline builders without checks: {', '.join(extra_builders)}")
        if not detail:
            detail.append("redline builder order differs from checker order")
        raise RuntimeError("Redline registry does not mirror checker registry (" + "; ".join(detail) + ")")


def _validate_playbook_contract(playbook: Dict[str, object]) -> None:
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        raise PlaybookTemplateError("Playbook clauses must be a list.")

    playbook_ids = []
    for clause in clauses:
        if not isinstance(clause, dict):
            raise PlaybookTemplateError("Each playbook clause must be an object.")
        clause_id = str(clause.get("id", "")).strip()
        if not clause_id:
            raise PlaybookTemplateError("Each playbook clause must include an id.")
        playbook_ids.append(clause_id)
        for field in ["name", "requirement", "type"]:
            if not isinstance(clause.get(field), str) or not str(clause.get(field)).strip():
                raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")
        if clause["type"] not in {"required", "prohibited"}:
            raise PlaybookTemplateError(f"Playbook clause {clause_id} has invalid type.")
        if not _required_clause_terms(clause, "search_terms"):
            raise PlaybookTemplateError(f"Playbook clause {clause_id} must include search_terms.")
        for optional_list_field in ["taxonomy_groups", "semantic_signals"]:
            if optional_list_field in clause and not isinstance(clause[optional_list_field], list):
                raise PlaybookTemplateError(f"Playbook clause {clause_id} {optional_list_field} must be a list.")
        for optional_text_field in ["rationale", "evidence_guidance"]:
            if optional_text_field in clause and not isinstance(clause[optional_text_field], str):
                raise PlaybookTemplateError(f"Playbook clause {clause_id} {optional_text_field} must be text.")

    duplicate_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_ids:
        raise PlaybookTemplateError(f"Duplicate playbook IDs: {', '.join(duplicate_ids)}")

    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    # Native clauses are backed by a Python check and must match the checker
    # registry exactly. Dynamic clauses are reviewed generically from their data
    # by the AI-first engine and must NOT have a Python check.
    native_ids = {str(clause["id"]) for clause in clauses if not is_dynamic_clause(clause)}
    dynamic_ids = {str(clause["id"]) for clause in clauses if is_dynamic_clause(clause)}

    missing_native_ids = sorted(set(check_ids) - native_ids)
    dynamic_with_check = sorted(dynamic_ids & set(check_ids))
    unknown_native_ids = sorted(native_ids - set(check_ids))
    detail = []
    if missing_native_ids:
        detail.append(f"missing native clauses: {', '.join(missing_native_ids)}")
    if unknown_native_ids:
        detail.append(
            "native clauses without checks: "
            + ", ".join(unknown_native_ids)
            + " (mark them engine=dynamic to define as data)"
        )
    if dynamic_with_check:
        detail.append(f"dynamic clauses shadow a native check: {', '.join(dynamic_with_check)}")
    if detail:
        raise PlaybookTemplateError("Playbook clause IDs do not match checker IDs (" + "; ".join(detail) + ")")

    clauses_by_id = {str(clause["id"]): clause for clause in clauses}
    # Per-clause template requirements apply only to the native clauses that are
    # present. Dynamic clauses carry their fallback wording in their own schema.
    if "governing_law" in clauses_by_id:
        _validate_governing_law_playbook(clauses_by_id["governing_law"])
    if "mutuality" in clauses_by_id:
        _require_template(clauses_by_id["mutuality"], "redline_template")
    if "confidential_information" in clauses_by_id:
        _require_template(clauses_by_id["confidential_information"], "redline_template")
        _require_template(clauses_by_id["confidential_information"], "standard_exclusions_template")
    if "term_and_survival" in clauses_by_id:
        _require_template(
            clauses_by_id["term_and_survival"],
            "redline_template",
            allowed_placeholders={"max_term_years", "max_term_years_label"},
        )
    if "signatures" in clauses_by_id:
        _require_template(clauses_by_id["signatures"], "redline_template")
    try:
        validate_playbook_rules(playbook)
    except PlaybookRulesError as error:
        raise PlaybookTemplateError(str(error)) from error


def _validate_governing_law_playbook(clause: Dict[str, object]) -> None:
    approved_laws = _approved_laws(clause)
    if not approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law must include approved_laws.")
    preferred_law = str(clause.get("preferred_law", "")).strip()
    if preferred_law and preferred_law not in approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law preferred_law must be approved.")
    law_phrases = clause.get("law_phrases", {})
    if not isinstance(law_phrases, dict):
        raise PlaybookTemplateError("Playbook clause governing_law law_phrases must be an object.")
    missing_phrases = [law for law in approved_laws if not str(law_phrases.get(law, "")).strip()]
    if missing_phrases:
        raise PlaybookTemplateError(
            "Playbook clause governing_law law_phrases missing: " + ", ".join(missing_phrases)
        )


def _require_template(
    clause: Dict[str, object],
    field: str,
    *,
    allowed_placeholders: set[str] | None = None,
) -> None:
    clause_id = str(clause.get("id", "unknown"))
    template = clause.get(field)
    if not isinstance(template, str) or not template.strip():
        raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")
    unknown_placeholders = sorted(
        placeholder
        for placeholder in _template_placeholders(template, clause_id=clause_id, field=field)
        if placeholder not in (allowed_placeholders or set())
    )
    if unknown_placeholders:
        raise PlaybookTemplateError(
            f"Playbook clause {clause_id} {field} has unknown placeholder(s): "
            + ", ".join(unknown_placeholders)
        )


def _template_placeholders(template: str, *, clause_id: str, field: str) -> set[str]:
    placeholders: set[str] = set()
    formatter = string.Formatter()
    try:
        for _literal_text, field_name, format_spec, _conversion in formatter.parse(template):
            if field_name is not None:
                if not field_name or "." in field_name or "[" in field_name:
                    raise PlaybookTemplateError(
                        f"Playbook clause {clause_id} {field} has invalid placeholder: {field_name!r}."
                    )
                placeholders.add(field_name)
            if format_spec:
                placeholders.update(_template_placeholders(format_spec, clause_id=clause_id, field=field))
    except PlaybookTemplateError:
        raise
    except ValueError as error:
        raise PlaybookTemplateError(f"Playbook clause {clause_id} {field} has invalid placeholder syntax.") from error
    return placeholders


def _required_clause_terms(clause: Dict[str, object], field: str) -> List[str]:
    values = clause.get(field, [])
    if not isinstance(values, list):
        return []
    return [str(term).lower().strip() for term in values if str(term).strip()]


def _build_redline_edits(clause_results: List[ClauseResult], paragraphs: List[Paragraph]) -> List[RedlineEdit]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    edits: List[RedlineEdit] = []

    for clause in clause_results:
        edits.extend(_redline_edits_for_clause(clause, paragraphs_by_id, len(edits) + 1))

    return edits


def _redline_edits_for_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    # Native clauses have a registered builder; dynamic clause types are redlined
    # generically from their own fallback wording so no per-clause Python is needed.
    builder = REDLINE_BUILDERS_BY_ID.get(str(clause["id"]))
    if builder is None:
        return _dynamic_clause_redlines(clause, paragraphs_by_id, start_number)
    return builder(clause, paragraphs_by_id, start_number)


def _dynamic_clause_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    """Build redline edits for a dynamic clause from its fallback wording.

    Prohibited clauses that are present-but-wrong get the offending paragraphs
    deleted. Required clauses get the clause's fallback wording inserted (when
    missing) or used to replace the offending paragraph (when present-but-wrong).
    The fallback block names the redline action; an absent or no_change action
    yields no edit.
    """
    fallback = clause.get("fallback")
    fallback = fallback if isinstance(fallback, dict) else {}
    action = str(fallback.get("redline_action") or "").strip()
    wording = str(fallback.get("wording") or "").strip()

    if action == REDLINE_DELETE_PARAGRAPH:
        if not _is_present_but_wrong_check(clause):
            return []
        edits: List[RedlineEdit] = []
        for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id):
            edits.append(_redline_edit(start_number + len(edits), clause, paragraph, REDLINE_DELETE_PARAGRAPH))
        return edits

    if action == REDLINE_REPLACE_PARAGRAPH and wording:
        if not _is_present_but_wrong_check(clause):
            return []
        paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
        if not paragraphs:
            return []
        return [
            _redline_edit(start_number, clause, paragraphs[0], REDLINE_REPLACE_PARAGRAPH, replacement_text=wording)
        ]

    if action == REDLINE_INSERT_AFTER_PARAGRAPH and wording:
        edit = _template_redline_for_required_clause(clause, paragraphs_by_id, start_number, wording)
        return [edit] if edit else []

    return []


def _is_present_but_wrong_check(clause: ClauseResult) -> bool:
    return clause.get("status") == "check" and clause.get("issue_type") == ISSUE_TYPE_PRESENT_BUT_WRONG


def _is_missing_required_check(clause: ClauseResult) -> bool:
    return (
        clause.get("status") == "not_present"
        and clause.get("issue_type") == ISSUE_TYPE_MISSING
        and not clause.get("passes")
    )


def _matched_redline_paragraphs(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> List[Paragraph]:
    paragraph_ids = clause.get("matched_paragraph_ids", [])
    if not isinstance(paragraph_ids, list):
        return []
    return [
        paragraph
        for paragraph_id in paragraph_ids
        if (paragraph := paragraphs_by_id.get(str(paragraph_id))) is not None
    ]


def _insertion_anchor_paragraph(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> Paragraph | None:
    matched_paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if matched_paragraphs:
        return matched_paragraphs[-1]
    paragraphs = _ordered_paragraphs(paragraphs_by_id)
    if not paragraphs:
        return None
    if str(clause.get("id")) == "signatures":
        return paragraphs[-1]

    anchor = _logical_missing_clause_anchor(clause, paragraphs)
    if anchor:
        return anchor
    return _last_non_signature_paragraph(paragraphs) or paragraphs[-1]


def _logical_missing_clause_anchor(clause: ClauseResult, paragraphs: List[Paragraph]) -> Paragraph | None:
    clause_id = str(clause.get("id"))
    if clause_id == "mutuality":
        return _first_non_signature_paragraph(paragraphs)

    anchor = _last_matching_non_signature_paragraph(
        paragraphs,
        MISSING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE.get(clause_id, ()),
    )
    if anchor:
        return anchor

    anchor = _paragraph_before_first_matching(
        paragraphs,
        FOLLOWING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE.get(clause_id, ()),
    )
    if anchor:
        return anchor

    return _paragraph_before_first_signature_block(paragraphs)


def _ordered_paragraphs(paragraphs_by_id: Dict[str, Paragraph]) -> List[Paragraph]:
    return sorted(paragraphs_by_id.values(), key=lambda paragraph: int(paragraph.get("index", 0)))


def _first_non_signature_paragraph(paragraphs: List[Paragraph]) -> Paragraph | None:
    return next((paragraph for paragraph in paragraphs if not _is_signature_anchor_paragraph(paragraph)), None)


def _last_non_signature_paragraph(paragraphs: List[Paragraph]) -> Paragraph | None:
    return next((paragraph for paragraph in reversed(paragraphs) if not _is_signature_anchor_paragraph(paragraph)), None)


def _last_matching_non_signature_paragraph(paragraphs: List[Paragraph], patterns: tuple[str, ...]) -> Paragraph | None:
    if not patterns:
        return None
    return next(
        (
            paragraph
            for paragraph in reversed(paragraphs)
            if not _is_signature_anchor_paragraph(paragraph)
            and any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in patterns)
        ),
        None,
    )


def _paragraph_before_first_matching(paragraphs: List[Paragraph], patterns: tuple[str, ...]) -> Paragraph | None:
    if not patterns:
        return None
    previous: Paragraph | None = None
    for paragraph in paragraphs:
        if any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in patterns):
            if previous and not _is_signature_anchor_paragraph(previous):
                return previous
            return None
        if not _is_signature_anchor_paragraph(paragraph):
            previous = paragraph
    return None


def _paragraph_before_first_signature_block(paragraphs: List[Paragraph]) -> Paragraph | None:
    previous: Paragraph | None = None
    for paragraph in paragraphs:
        if _is_signature_anchor_paragraph(paragraph):
            return previous
        previous = paragraph
    return None


def _is_signature_anchor_paragraph(paragraph: Paragraph) -> bool:
    text = str(paragraph["text"])
    marker_count = len(re.findall(SIGNATURE_MARKER_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    has_for_line = bool(re.search(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    return marker_count >= 2 or (has_for_line and marker_count >= 1)


def _redline_edit(
    edit_number: int,
    clause: ClauseResult,
    paragraph: Paragraph,
    action: str,
    replacement_text: str = "",
    insert_text: str = "",
    template_options: List[Dict[str, object]] | None = None,
) -> RedlineEdit:
    proposed_text = insert_text or replacement_text
    edit = {
        "id": f"r{edit_number}",
        "clause_id": clause["id"],
        "clause_name": clause["name"],
        "paragraph_id": paragraph["id"],
        "paragraph_index": paragraph.get("index"),
        "action": action,
        "action_label": REDLINE_ACTION_LABELS.get(action, "Proposed edit"),
        "status": "proposed",
        "original_text": "" if action == REDLINE_INSERT_AFTER_PARAGRAPH else paragraph["text"],
        "replacement_text": proposed_text,
        "reason": clause.get("what_to_fix") or clause.get("reason"),
    }
    if "source_index" in paragraph:
        edit["source_index"] = paragraph["source_index"]
    if "source_part" in paragraph:
        edit["source_part"] = paragraph["source_part"]
    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        edit["anchor_text"] = paragraph["text"]
        edit["insert_text"] = proposed_text
    elif action == REDLINE_REPLACE_PARAGRAPH:
        edit["inline_diff_operations"] = diff_text_operation_dicts(str(paragraph["text"]), proposed_text)
    if template_options:
        edit["template_options"] = _redline_template_options_with_diff(paragraph, action, template_options)
    return edit


def _redline_template_options_with_diff(
    paragraph: Paragraph,
    action: str,
    template_options: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    if action != REDLINE_REPLACE_PARAGRAPH:
        return template_options
    return [
        {
            **option,
            "inline_diff_operations": diff_text_operation_dicts(
                str(paragraph["text"]),
                str(option.get("replacement_text") or option.get("text") or ""),
            ),
        }
        for option in template_options
    ]


def _governing_law_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    template_options = _governing_law_template_options(clause)
    if not template_options:
        return None

    selected_template = _selected_template_option(template_options)

    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=str(selected_template["text"]),
            template_options=template_options,
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=str(selected_template["text"]),
        template_options=template_options,
    )


def _governing_law_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _governing_law_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _template_redline_for_required_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
    template_text: str,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=template_text,
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=template_text,
    )


def _mutuality_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _template_redline_for_required_clause(
        clause,
        paragraphs_by_id,
        start_number,
        _clause_template_text(clause, "redline_template"),
    )
    return [edit] if edit else []


def _confidential_information_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    template_field = "standard_exclusions_template" if _confidential_issue_is_exclusion_based(clause) else "redline_template"
    edit = _template_redline_for_required_clause(
        clause,
        paragraphs_by_id,
        start_number,
        _clause_template_text(clause, template_field),
    )
    return [edit] if edit else []


def _confidential_issue_is_exclusion_based(clause: ClauseResult) -> bool:
    issue_text = f"{clause.get('reason', '')} {clause.get('what_to_fix', '')}".lower()
    return "exclusion" in issue_text or "residual" in issue_text or "reverse-engineering" in issue_text


def _term_and_survival_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=_term_and_survival_replacement_text(clause),
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=_term_and_survival_replacement_text(clause),
    )


def _term_and_survival_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _term_and_survival_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _signatures_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None

        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=_signature_block_template(clause),
        )

    if clause.get("status") != "check" or clause.get("issue_type") != ISSUE_TYPE_UNCLEAR:
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=_signature_block_template(clause),
    )


def _signatures_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _signatures_redline(clause, paragraphs_by_id, start_number)
    if not edit:
        return []
    edits = [edit]
    if edit["action"] != REDLINE_REPLACE_PARAGRAPH:
        return edits
    for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id)[1:]:
        edits.append(
            _redline_edit(
                start_number + len(edits),
                clause,
                paragraph,
                REDLINE_DELETE_PARAGRAPH,
            )
        )
    return edits


def _no_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    return []


REDLINE_BUILDERS: List[tuple[str, RedlineBuildFn]] = [
    # Every checked clause must declare its redline behavior. Use
    # _no_redlines when the absence of a proposed edit is intentional.
    ("mutuality", _mutuality_redlines),
    ("confidential_information", _confidential_information_redlines),
    ("governing_law", _governing_law_redlines),
    ("term_and_survival", _term_and_survival_redlines),
    ("signatures", _signatures_redlines),
]
REDLINE_BUILDERS_BY_ID: Dict[str, RedlineBuildFn] = dict(REDLINE_BUILDERS)


_validate_check_registry()


def _preferred_governing_law(clause: ClauseResult) -> str | None:
    approved_laws = _approved_laws(clause)
    preferred_law = str(clause.get("preferred_law", "")).strip()

    if preferred_law and (not approved_laws or preferred_law in approved_laws):
        return preferred_law
    if approved_laws:
        return approved_laws[0]
    return None


def _selected_template_option(template_options: List[Dict[str, object]]) -> Dict[str, object]:
    return next((option for option in template_options if option.get("selected")), template_options[0])


def _governing_law_template_options(clause: ClauseResult) -> List[Dict[str, object]]:
    approved_laws = _approved_laws(clause)
    preferred_law = _preferred_governing_law(clause)
    if not approved_laws:
        return []

    return [
        {
            "id": f"governing_law_{_template_slug(law)}",
            "label": law,
            "text": _governing_law_replacement_text(clause, law),
            "replacement_text": _governing_law_replacement_text(clause, law),
            "insert_text": _governing_law_replacement_text(clause, law),
            "selected": law == preferred_law,
        }
        for law in approved_laws
    ]


def _template_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _governing_law_replacement_text(clause: ClauseResult, law: str) -> str:
    law_label = law.strip()
    law_phrase = _governing_law_phrase(clause, law_label)
    return f"This Agreement shall be governed by the laws of {law_phrase}."


def _term_and_survival_replacement_text(clause: ClauseResult) -> str:
    cap_label = _year_count_label(_max_term_years(clause))
    return _clause_template_text(
        clause,
        "redline_template",
        {
            "max_term_years": _max_term_years(clause),
            "max_term_years_label": cap_label,
        },
    )


def _signature_block_template(clause: ClauseResult) -> str:
    return _clause_template_text(clause, "redline_template")
