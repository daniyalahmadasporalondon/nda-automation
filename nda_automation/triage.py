from __future__ import annotations

LEGAL_REVIEW_CLAUSE_IDS = {"non_circumvention"}


def triage_review_result(review_result: dict) -> dict:
    clauses = review_result.get("clauses", [])
    failed_clauses = [
        clause
        for clause in clauses
        if isinstance(clause, dict) and clause.get("passes") is False
    ]
    failed_count = len(failed_clauses)

    if failed_count == 0:
        return {
            "triage_status": "ready_to_sign",
            "next_action": "Ready for signature",
            "issue_count": 0,
            "requirements_passed": int(review_result.get("requirements_passed") or len(clauses)),
            "requirements_failed": 0,
        }

    if any(str(clause.get("id") or "") in LEGAL_REVIEW_CLAUSE_IDS for clause in failed_clauses):
        triage_status = "legal_review"
        next_action = "Needs legal review"
    else:
        triage_status = "needs_redline"
        next_action = "Review redline"

    return {
        "triage_status": triage_status,
        "next_action": next_action,
        "issue_count": failed_count,
        "requirements_passed": int(review_result.get("requirements_passed") or 0),
        "requirements_failed": int(review_result.get("requirements_failed") or failed_count),
    }


def intake_error_triage() -> dict:
    return {
        "triage_status": "intake_error",
        "next_action": "Fix intake error",
        "issue_count": 0,
        "requirements_passed": 0,
        "requirements_failed": 0,
    }
