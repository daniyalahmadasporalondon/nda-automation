from __future__ import annotations

from .review_state import clause_fails, clause_needs_review, review_state_from_result


def _clause_is_prohibited(clause: dict) -> bool:
    """A FAILED prohibited clause forces legal review.

    Keyed off the playbook clause ``type`` / ``rules.clause_type == "prohibited"``
    (carried through onto each review-result clause) rather than a hardcoded id
    set, so a second prohibited clause added to the playbook also forces
    RED/legal review without a code change. ``non_circumvention`` is currently
    the only prohibited clause, so today's behavior is unchanged.
    """
    if not isinstance(clause, dict):
        return False
    if str(clause.get("type") or "").strip().lower() == "prohibited":
        return True
    rules = clause.get("rules")
    if isinstance(rules, dict) and str(rules.get("clause_type") or "").strip().lower() == "prohibited":
        return True
    return False


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
    # Document-level gates (unresolved tracked changes, truncated AI packet) force
    # human review even when every clause passes -- the verdict was computed from a
    # source the firm has not yet agreed to. Surface that as a human-review item so
    # the matter is never flagged "ready to sign" while the gate blocks send.
    forced_human_review = bool(state.get("tracked_changes_forced_review") or state.get("truncation_forced_review"))

    if failed_count == 0 and review_count == 0 and not forced_human_review:
        return {
            "triage_status": "ready_to_sign",
            "next_action": "Ready for signature",
            "issue_count": 0,
            "requirements_passed": int(review_result.get("requirements_passed") or len(clauses)),
            "requirements_failed": 0,
            "requirements_needs_review": 0,
        }

    # A forced human-review item counts toward the review tally when no clause
    # already raised one, so the matter shows an outstanding item rather than zero.
    effective_review_count = review_count or (1 if forced_human_review else 0)

    if effective_review_count or any(_clause_is_prohibited(clause) for clause in failed_clauses):
        triage_status = "legal_review"
        next_action = "Needs human review" if effective_review_count and not failed_count else "Needs legal review"
    else:
        triage_status = "needs_redline"
        next_action = "Review redline"

    return {
        "triage_status": triage_status,
        "next_action": next_action,
        "issue_count": failed_count + effective_review_count,
        "requirements_passed": int(review_result.get("requirements_passed") or 0),
        "requirements_failed": int(review_result.get("requirements_failed") or failed_count),
        "requirements_needs_review": effective_review_count,
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
