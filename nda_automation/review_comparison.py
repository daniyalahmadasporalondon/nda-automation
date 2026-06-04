from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from collections.abc import Callable, Sequence
from typing import Any

from .ai_assessor import AIAssessorError, assess_nda_with_ai
from .checker import review_nda
from .review_document import Paragraph
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
    aggregate_review_state,
)
from . import telemetry

REVIEW_COMPARISON_VERSION = 1
REVIEW_COMPARISON_MODE = "deterministic_vs_ai_first"
COMPARISON_SOURCE_AGREEMENT = "agreement"
COMPARISON_SOURCE_MOST_CONSERVATIVE = "most_conservative"
COMPARISON_SOURCE_DETERMINISTIC_ONLY = "deterministic_only"
COMPARISON_SOURCE_AI_FIRST_ONLY = "ai_first_only"


class ReviewComparisonError(RuntimeError):
    pass


ReviewFn = Callable[..., dict[str, Any]]


def compare_nda_reviews(
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    deterministic_review_func: ReviewFn = review_nda,
    ai_first_review_func: ReviewFn = assess_nda_with_ai,
    checked_at: str | None = None,
) -> dict[str, Any]:
    source_text = text or ""
    telemetry.increment("review_comparison_requests")
    try:
        deterministic_result = deterministic_review_func(source_text, paragraphs=paragraphs)
    except Exception as error:
        telemetry.increment("review_comparison_deterministic_failures")
        raise ReviewComparisonError(f"Deterministic comparison review failed: {error}") from error
    try:
        ai_first_result = ai_first_review_func(source_text, paragraphs=paragraphs)
    except AIAssessorError as error:
        telemetry.increment("review_comparison_ai_first_failures")
        raise ReviewComparisonError(f"AI-first comparison review failed: {error}") from error
    except Exception as error:
        telemetry.increment("review_comparison_ai_first_failures")
        raise ReviewComparisonError(f"AI-first comparison review failed: {error}") from error

    comparison = build_review_comparison(
        deterministic_result,
        ai_first_result,
        checked_at=checked_at,
    )
    telemetry.increment("review_comparison_completed")
    if comparison["summary"]["disagreement_count"]:
        telemetry.increment("review_comparison_disagreements", int(comparison["summary"]["disagreement_count"]))
    return comparison


def build_review_comparison(
    deterministic_result: dict[str, Any],
    ai_first_result: dict[str, Any],
    *,
    checked_at: str | None = None,
) -> dict[str, Any]:
    deterministic_clauses = _clauses_by_id(deterministic_result)
    ai_first_clauses = _clauses_by_id(ai_first_result)
    clause_ids = _ordered_clause_ids(deterministic_result, ai_first_result)
    clause_comparisons = [
        compare_clause_results(
            clause_id,
            deterministic_clauses.get(clause_id),
            ai_first_clauses.get(clause_id),
        )
        for clause_id in clause_ids
    ]
    final_clauses = [_final_clause_for_state(item) for item in clause_comparisons]
    final_review_state = aggregate_review_state(final_clauses)
    summary = _comparison_summary(clause_comparisons)
    return {
        "version": REVIEW_COMPARISON_VERSION,
        "mode": REVIEW_COMPARISON_MODE,
        "status": "completed",
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "deterministic": _result_summary(deterministic_result),
        "ai_first": _result_summary(ai_first_result),
        "final_review_state": final_review_state,
        "final_verdict": {
            "overall_status": final_review_state.get("overall_status"),
            "state": final_review_state.get("state"),
            "source": "agreement" if not summary["disagreement_count"] else COMPARISON_SOURCE_MOST_CONSERVATIVE,
            "requires_human_review": bool(summary["disagreement_count"] or final_review_state.get("requires_human_review")),
        },
        "clauses": clause_comparisons,
    }


def compare_clause_results(
    clause_id: str,
    deterministic_clause: dict[str, Any] | None,
    ai_first_clause: dict[str, Any] | None,
) -> dict[str, Any]:
    deterministic_snapshot = _clause_snapshot(deterministic_clause)
    ai_first_snapshot = _clause_snapshot(ai_first_clause)
    deterministic_decision = str(deterministic_snapshot.get("decision") or "")
    ai_first_decision = str(ai_first_snapshot.get("decision") or "")
    evidence_delta = _evidence_delta(deterministic_snapshot, ai_first_snapshot)
    issue_type_changed = deterministic_snapshot.get("issue_type") != ai_first_snapshot.get("issue_type")
    reason_code_changed = deterministic_snapshot.get("reason_code") != ai_first_snapshot.get("reason_code")
    decision_changed = deterministic_decision != ai_first_decision
    final_verdict = _final_verdict(deterministic_decision, ai_first_decision)
    return {
        "clause_id": clause_id,
        "deterministic": deterministic_snapshot,
        "ai_first": ai_first_snapshot,
        "decision_changed": decision_changed,
        "issue_type_changed": issue_type_changed,
        "reason_code_changed": reason_code_changed,
        "evidence_changed": bool(evidence_delta["changed"]),
        "has_disagreement": bool(
            decision_changed
            or issue_type_changed
            or reason_code_changed
            or evidence_delta["changed"]
            or deterministic_clause is None
            or ai_first_clause is None
        ),
        "evidence_delta": evidence_delta,
        "final_verdict": final_verdict,
    }


def _clauses_by_id(review_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        clause_id = str(clause.get("id") or "").strip()
        if clause_id:
            result[clause_id] = clause
    return result


def _ordered_clause_ids(deterministic_result: dict[str, Any], ai_first_result: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for result in (deterministic_result, ai_first_result):
        for clause in result.get("clauses", []) if isinstance(result.get("clauses"), list) else []:
            if not isinstance(clause, dict):
                continue
            clause_id = str(clause.get("id") or "").strip()
            if clause_id and clause_id not in ordered:
                ordered.append(clause_id)
    return ordered


def _clause_snapshot(clause: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(clause, dict):
        return {
            "present": False,
            "decision": "",
            "issue_type": "",
            "reason_code": "",
            "matched_paragraph_ids": [],
            "finding": "",
            "decision_source": "",
        }
    return {
        "present": True,
        "decision": _clause_decision(clause),
        "issue_type": str(clause.get("issue_type") or ""),
        "reason_code": str(clause.get("reason_code") or ""),
        "matched_paragraph_ids": _matched_paragraph_ids(clause),
        "finding": str(clause.get("finding") or clause.get("decision_reason") or ""),
        "decision_source": str(clause.get("decision_source") or ""),
    }


def _clause_decision(clause: dict[str, Any]) -> str:
    decision = str(clause.get("decision") or "").strip()
    if decision in {CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW, CLAUSE_DECISION_FAIL}:
        return decision
    if "decision" in clause:
        return CLAUSE_DECISION_REVIEW
    if bool(clause.get("needs_review")):
        return CLAUSE_DECISION_REVIEW
    if clause.get("passes") is False:
        return CLAUSE_DECISION_FAIL
    if clause.get("passes") is True:
        return CLAUSE_DECISION_PASS
    return CLAUSE_DECISION_REVIEW


def _matched_paragraph_ids(clause: dict[str, Any]) -> list[str]:
    raw_ids = clause.get("matched_paragraph_ids")
    if isinstance(raw_ids, list):
        return _clean_ids(raw_ids)
    structured_ids = [
        record.get("paragraph_id")
        for record in clause.get("structured_evidence", [])
        if isinstance(record, dict)
    ] if isinstance(clause.get("structured_evidence"), list) else []
    return _clean_ids(structured_ids)


def _clean_ids(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _evidence_delta(deterministic_snapshot: dict[str, Any], ai_first_snapshot: dict[str, Any]) -> dict[str, Any]:
    deterministic_ids = list(deterministic_snapshot.get("matched_paragraph_ids") or [])
    ai_first_ids = list(ai_first_snapshot.get("matched_paragraph_ids") or [])
    deterministic_set = set(deterministic_ids)
    ai_first_set = set(ai_first_ids)
    return {
        "deterministic_paragraph_ids": deterministic_ids,
        "ai_first_paragraph_ids": ai_first_ids,
        "shared_paragraph_ids": [paragraph_id for paragraph_id in deterministic_ids if paragraph_id in ai_first_set],
        "only_deterministic_paragraph_ids": [paragraph_id for paragraph_id in deterministic_ids if paragraph_id not in ai_first_set],
        "only_ai_first_paragraph_ids": [paragraph_id for paragraph_id in ai_first_ids if paragraph_id not in deterministic_set],
        "changed": deterministic_set != ai_first_set,
    }


def _final_verdict(deterministic_decision: str, ai_first_decision: str) -> dict[str, Any]:
    if deterministic_decision and not ai_first_decision:
        return {
            "decision": deterministic_decision,
            "source": COMPARISON_SOURCE_DETERMINISTIC_ONLY,
            "requires_human_review": True,
        }
    if ai_first_decision and not deterministic_decision:
        return {
            "decision": ai_first_decision,
            "source": COMPARISON_SOURCE_AI_FIRST_ONLY,
            "requires_human_review": True,
        }
    if deterministic_decision == ai_first_decision:
        return {
            "decision": deterministic_decision,
            "source": COMPARISON_SOURCE_AGREEMENT,
            "requires_human_review": deterministic_decision == CLAUSE_DECISION_REVIEW,
        }
    final_decision = max([deterministic_decision, ai_first_decision], key=_decision_rank)
    return {
        "decision": final_decision,
        "source": COMPARISON_SOURCE_MOST_CONSERVATIVE,
        "requires_human_review": True,
    }


def _decision_rank(decision: str) -> int:
    return {
        CLAUSE_DECISION_PASS: 0,
        CLAUSE_DECISION_REVIEW: 1,
        CLAUSE_DECISION_FAIL: 2,
    }.get(str(decision or ""), -1)


def _final_clause_for_state(clause_comparison: dict[str, Any]) -> dict[str, Any]:
    final_verdict = clause_comparison.get("final_verdict")
    decision = str(final_verdict.get("decision") or "") if isinstance(final_verdict, dict) else ""
    return {
        "id": str(clause_comparison.get("clause_id") or ""),
        "decision": decision,
        "reason_code": str(final_verdict.get("source") or "") if isinstance(final_verdict, dict) else "",
    }


def _comparison_summary(clause_comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    disagreements = [item for item in clause_comparisons if item.get("has_disagreement")]
    decision_disagreements = [item for item in clause_comparisons if item.get("decision_changed")]
    evidence_disagreements = [item for item in clause_comparisons if item.get("evidence_changed")]
    return {
        "compared_clause_count": len(clause_comparisons),
        "agreement_count": len(clause_comparisons) - len(disagreements),
        "disagreement_count": len(disagreements),
        "decision_disagreement_count": len(decision_disagreements),
        "evidence_disagreement_count": len(evidence_disagreements),
        "issue_type_disagreement_count": sum(1 for item in clause_comparisons if item.get("issue_type_changed")),
        "reason_code_disagreement_count": sum(1 for item in clause_comparisons if item.get("reason_code_changed")),
        "disagreement_clause_ids": [str(item.get("clause_id") or "") for item in disagreements],
        "decision_disagreement_clause_ids": [str(item.get("clause_id") or "") for item in decision_disagreements],
        "evidence_disagreement_clause_ids": [str(item.get("clause_id") or "") for item in evidence_disagreements],
    }


def _result_summary(review_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_mode": str(review_result.get("review_mode") or "deterministic"),
        "overall_status": str(review_result.get("overall_status") or ""),
        "requirements_passed": int(review_result.get("requirements_passed") or 0),
        "requirements_needs_review": int(review_result.get("requirements_needs_review") or 0),
        "requirements_failed": int(review_result.get("requirements_failed") or 0),
        "review_engine_version": review_result.get("review_engine_version"),
        "ai_first_review": deepcopy(review_result.get("ai_first_review")) if isinstance(review_result.get("ai_first_review"), dict) else {},
    }
