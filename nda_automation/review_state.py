from __future__ import annotations

from typing import Any, Dict, Iterable, List

REVIEW_STATE_VERSION = 1
REVIEW_STATE_PASS = "pass"
REVIEW_STATE_REVIEW = "review"
REVIEW_STATE_CHECK = "check"
REVIEW_STATE_PENDING = "pending"
CLAUSE_DECISION_PASS = "pass"
CLAUSE_DECISION_REVIEW = "review"
CLAUSE_DECISION_FAIL = "fail"


def clause_review_state(clause: Dict[str, Any], decision: str | None = None) -> Dict[str, Any]:
    normalized_decision = _normalize_clause_decision(clause, decision)
    state = _state_for_clause_decision(normalized_decision)
    reason = str(clause.get("decision_reason") or clause.get("reason") or clause.get("finding") or "").strip()
    reason_codes = reason_codes_for_clause(clause, normalized_decision)
    return {
        "version": REVIEW_STATE_VERSION,
        "state": state,
        "decision": normalized_decision,
        "label": _state_label(state),
        "tone": _state_tone(state),
        "requires_attention": state in {REVIEW_STATE_REVIEW, REVIEW_STATE_CHECK},
        "requires_human_review": state == REVIEW_STATE_REVIEW,
        "requires_redline": state == REVIEW_STATE_CHECK,
        "blocks_send": state == REVIEW_STATE_REVIEW,
        "blocks_auto_send": state in {REVIEW_STATE_REVIEW, REVIEW_STATE_CHECK},
        "reason": reason,
        "reason_code": reason_codes[0],
        "reason_codes": reason_codes,
    }


def aggregate_review_state(
    clauses: Iterable[Dict[str, Any]],
    *,
    pass_count: int | None = None,
    review_count: int | None = None,
    check_count: int | None = None,
) -> Dict[str, Any]:
    clause_list = [clause for clause in clauses if isinstance(clause, dict)]
    pass_ids = [str(clause.get("id") or "") for clause in clause_list if clause_passes(clause)]
    review_ids = [str(clause.get("id") or "") for clause in clause_list if clause_needs_review(clause)]
    check_ids = [str(clause.get("id") or "") for clause in clause_list if clause_fails(clause)]
    reason_codes_by_state = _reason_codes_by_state(clause_list)
    passed = len(pass_ids) if pass_count is None else int(pass_count)
    review = len(review_ids) if review_count is None else int(review_count)
    checked = len(check_ids) if check_count is None else int(check_count)
    total = passed + review + checked
    if checked:
        state = REVIEW_STATE_CHECK
    elif review:
        state = REVIEW_STATE_REVIEW
    elif total:
        state = REVIEW_STATE_PASS
    else:
        state = REVIEW_STATE_PENDING
    return {
        "version": REVIEW_STATE_VERSION,
        "state": state,
        "overall_status": _overall_status_for_state(state),
        "label": _state_label(state),
        "tone": _state_tone(state),
        "requires_attention": state in {REVIEW_STATE_REVIEW, REVIEW_STATE_CHECK},
        "requires_human_review": review > 0,
        "requires_redline": checked > 0,
        "blocks_send": review > 0,
        "blocks_auto_send": review > 0 or checked > 0,
        "next_action": _next_action_for_counts(review, checked),
        "counts": {
            "pass": passed,
            "review": review,
            "check": checked,
            "total": total,
        },
        "clause_ids": {
            "pass": _clean_ids(pass_ids),
            "review": _clean_ids(review_ids),
            "check": _clean_ids(check_ids),
        },
        "reason_codes": _unique_codes(
            reason_codes_by_state["check"]
            + reason_codes_by_state["review"]
            + reason_codes_by_state["pass"]
        ),
        "reason_codes_by_state": reason_codes_by_state,
    }


def reason_codes_for_clause(clause: Dict[str, Any], decision: str | None = None) -> List[str]:
    existing_codes = _existing_reason_codes(clause)
    if existing_codes:
        return existing_codes
    normalized_decision = _normalize_clause_decision(clause, decision)
    # Each check owns its clause-specific reason codes. The local import keeps
    # checks <-> review_state acyclic (the checks import this module's shared
    # scaffolding at module load; we reach back into them only at call time).
    from .checks.registry import REASON_CODE_FUNCTIONS

    reason_code_fn = REASON_CODE_FUNCTIONS.get(str(clause.get("id") or "").strip())
    if reason_code_fn is not None:
        return [reason_code_fn(clause, normalized_decision)]
    return [_generic_reason_code(clause, normalized_decision)]


def review_state_from_result(review_result: Dict[str, Any]) -> Dict[str, Any]:
    existing = review_result.get("review_state")
    if isinstance(existing, dict) and existing.get("state"):
        return existing
    clauses = review_result.get("clauses", [])
    if isinstance(clauses, list):
        clause_dicts = [clause for clause in clauses if isinstance(clause, dict)]
        if clause_dicts:
            # Single source of truth: derive the review state (and therefore the
            # send gate) from the actual clause decisions, not the
            # requirements_* integer summaries, which can drift from them.
            return aggregate_review_state(clause_dicts)
        return aggregate_review_state(
            [],
            pass_count=_optional_int(review_result.get("requirements_passed")),
            review_count=_optional_int(review_result.get("requirements_needs_review")),
            check_count=_optional_int(review_result.get("requirements_failed")),
        )
    status = str(review_result.get("overall_status") or "").strip()
    if status == "needs_review":
        return aggregate_review_state([], pass_count=0, review_count=1, check_count=0)
    if status == "does_not_meet_requirements":
        return aggregate_review_state([], pass_count=0, review_count=0, check_count=1)
    if status == "meets_requirements":
        return aggregate_review_state([], pass_count=1, review_count=0, check_count=0)
    return aggregate_review_state([], pass_count=0, review_count=0, check_count=0)


def result_requires_human_review(review_result: Dict[str, Any]) -> bool:
    state = review_state_from_result(review_result)
    if bool(state.get("requires_human_review")) or bool(state.get("blocks_send")):
        return True
    counts = state.get("counts", {})
    if isinstance(counts, dict):
        try:
            return int(counts.get("review") or 0) > 0
        except (TypeError, ValueError):
            return True
    return str(state.get("state") or "") == REVIEW_STATE_REVIEW


def clause_needs_review(clause: Dict[str, Any]) -> bool:
    review_state = clause.get("review_state")
    if isinstance(review_state, dict):
        return str(review_state.get("state") or "").strip().lower() == REVIEW_STATE_REVIEW
    return _normalize_clause_decision(clause) == CLAUSE_DECISION_REVIEW


def clause_fails(clause: Dict[str, Any]) -> bool:
    review_state = clause.get("review_state")
    if isinstance(review_state, dict):
        return str(review_state.get("state") or "").strip().lower() == REVIEW_STATE_CHECK
    return _normalize_clause_decision(clause) == CLAUSE_DECISION_FAIL


def clause_passes(clause: Dict[str, Any]) -> bool:
    review_state = clause.get("review_state")
    if isinstance(review_state, dict):
        return str(review_state.get("state") or "").strip().lower() == REVIEW_STATE_PASS
    return _normalize_clause_decision(clause) == CLAUSE_DECISION_PASS


def _normalize_clause_decision(clause: Dict[str, Any], decision: str | None = None) -> str:
    raw_decision = str(decision or clause.get("decision") or "").strip().lower()
    if raw_decision in {CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW, CLAUSE_DECISION_FAIL}:
        return raw_decision
    if bool(clause.get("needs_review")):
        return CLAUSE_DECISION_REVIEW
    if clause.get("passes") is False:
        return CLAUSE_DECISION_FAIL
    return CLAUSE_DECISION_PASS


def _state_for_clause_decision(decision: str) -> str:
    if decision == CLAUSE_DECISION_REVIEW:
        return REVIEW_STATE_REVIEW
    if decision == CLAUSE_DECISION_FAIL:
        return REVIEW_STATE_CHECK
    if decision == CLAUSE_DECISION_PASS:
        return REVIEW_STATE_PASS
    return REVIEW_STATE_PENDING


def _overall_status_for_state(state: str) -> str:
    if state == REVIEW_STATE_PASS:
        return "meets_requirements"
    if state == REVIEW_STATE_REVIEW:
        return "needs_review"
    if state == REVIEW_STATE_CHECK:
        return "does_not_meet_requirements"
    return "pending_review"


def _state_label(state: str) -> str:
    labels = {
        REVIEW_STATE_PASS: "PASS",
        REVIEW_STATE_REVIEW: "REVIEW",
        REVIEW_STATE_CHECK: "CHECK",
        REVIEW_STATE_PENDING: "PENDING",
    }
    return labels.get(state, "PENDING")


def _state_tone(state: str) -> str:
    tones = {
        REVIEW_STATE_PASS: "pass",
        REVIEW_STATE_REVIEW: "review",
        REVIEW_STATE_CHECK: "check",
        REVIEW_STATE_PENDING: "pending",
    }
    return tones.get(state, "pending")


def _next_action_for_counts(review_count: int, check_count: int) -> str:
    if review_count and check_count:
        return "Resolve checked clauses and human-review items"
    if review_count:
        return "Human review required"
    if check_count:
        return "Review redline"
    return "Ready for signature"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_ids(values: List[str]) -> List[str]:
    return [value for value in values if value]


def _reason_codes_by_state(clauses: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {
        REVIEW_STATE_PASS: [],
        REVIEW_STATE_REVIEW: [],
        REVIEW_STATE_CHECK: [],
    }
    for clause in clauses:
        state = _state_for_clause_decision(_normalize_clause_decision(clause))
        if state not in buckets:
            continue
        buckets[state].extend(reason_codes_for_clause(clause))
    return {state: _unique_codes(codes) for state, codes in buckets.items()}


def _existing_reason_codes(clause: Dict[str, Any]) -> List[str]:
    raw_codes = clause.get("reason_codes")
    if isinstance(raw_codes, list):
        return _unique_codes(str(code) for code in raw_codes)
    raw_code = clause.get("reason_code")
    if raw_code:
        return _unique_codes([str(raw_code)])
    review_state = clause.get("review_state")
    if isinstance(review_state, dict):
        state_codes = review_state.get("reason_codes")
        if isinstance(state_codes, list):
            return _unique_codes(str(code) for code in state_codes)
        state_code = review_state.get("reason_code")
        if state_code:
            return _unique_codes([str(state_code)])
    return []


def _unique_codes(values: Iterable[str]) -> List[str]:
    codes: List[str] = []
    seen = set()
    for value in values:
        code = _normalize_reason_code(value)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _normalize_reason_code(value: str) -> str:
    code = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "".join(character for character in code if character.isalnum() or character == "_")


def _has_ids(clause: Dict[str, Any], analysis_key: str, field: str) -> bool:
    analysis = clause.get(analysis_key)
    if not isinstance(analysis, dict):
        return False
    raw_values = analysis.get(field, [])
    return isinstance(raw_values, list) and any(str(value).strip() for value in raw_values)


def _issue_type(clause: Dict[str, Any]) -> str:
    return str(clause.get("issue_type") or "").strip().lower()


def _semantic_review_code(clause: Dict[str, Any], decision: str) -> str | None:
    confidence = clause.get("semantic_confidence")
    if confidence is None:
        confidence = clause.get("confidence")
    if decision == CLAUSE_DECISION_REVIEW and confidence is not None:
        return "semantic_confidence_below_threshold"
    if clause.get("semantic_fallback"):
        return "semantic_fallback_decision"
    return None


def _generic_reason_code(clause: Dict[str, Any], decision: str) -> str:
    semantic_code = _semantic_review_code(clause, decision)
    if semantic_code:
        return semantic_code
    issue = _issue_type(clause)
    if issue == "missing":
        return "missing_required_clause"
    if issue == "present_but_wrong":
        return "present_but_wrong"
    if issue == "unclear" or decision == CLAUSE_DECISION_REVIEW:
        return "unclear_or_ambiguous"
    if decision == CLAUSE_DECISION_FAIL:
        return "clause_failed_playbook"
    if decision == CLAUSE_DECISION_PASS:
        return "pass_evidence_found"
    return "unclassified_review_reason"
