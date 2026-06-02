from __future__ import annotations

from .review_state import clause_fails, clause_needs_review, review_state_from_result

LEGAL_REVIEW_CLAUSE_IDS = {"non_circumvention"}


def triage_review_result(review_result: dict) -> dict:
    clauses = review_result.get("clauses", [])
    if not isinstance(clauses, list):
        return {
            "triage_status": "needs_redline",
            "next_action": "Review redline",
            "issue_count": 1,
            "requirements_passed": 0,
            "requirements_failed": 1,
            "requirements_needs_review": 0,
        }
    review_clauses = [
        clause
        for clause in clauses
        if isinstance(clause, dict) and clause_needs_review(clause)
    ]
    failed_clauses = [
        clause
        for clause in clauses
        if isinstance(clause, dict) and clause_fails(clause)
    ]
    state = review_state_from_result(review_result)
    counts = state.get("counts", {})
    review_count = _count_from_state(counts, "review", int(review_result.get("requirements_needs_review") or len(review_clauses)))
    failed_count = _count_from_state(counts, "check", len(failed_clauses))

    if failed_count == 0 and review_count == 0:
        return {
            "triage_status": "ready_to_sign",
            "next_action": "Ready for signature",
            "issue_count": 0,
            "requirements_passed": int(review_result.get("requirements_passed") or len(clauses)),
            "requirements_failed": 0,
            "requirements_needs_review": 0,
        }

    if review_count or any(str(clause.get("id") or "") in LEGAL_REVIEW_CLAUSE_IDS for clause in failed_clauses):
        triage_status = "legal_review"
        next_action = "Needs human review" if review_count and not failed_count else "Needs legal review"
    else:
        triage_status = "needs_redline"
        next_action = "Review redline"

    return {
        "triage_status": triage_status,
        "next_action": next_action,
        "issue_count": failed_count + review_count,
        "requirements_passed": int(review_result.get("requirements_passed") or 0),
        "requirements_failed": int(review_result.get("requirements_failed") or failed_count),
        "requirements_needs_review": review_count,
    }


def _count_from_state(counts: object, key: str, fallback: int) -> int:
    if isinstance(counts, dict):
        if key not in counts:
            return fallback
        try:
            return int(counts.get(key) or 0)
        except (TypeError, ValueError):
            return fallback
    return fallback
