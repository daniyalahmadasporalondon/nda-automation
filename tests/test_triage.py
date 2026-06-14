from __future__ import annotations

from nda_automation.triage import triage_review_result


def test_clean_all_pass_result_is_ready_to_sign():
    review_result = {
        "clauses": [{"id": "term", "decision": "pass"}],
        "requirements_passed": 1,
        "requirements_failed": 0,
        "requirements_needs_review": 0,
    }

    triage = triage_review_result(review_result)

    assert triage["triage_status"] == "ready_to_sign"
    assert triage["issue_count"] == 0


def test_tracked_changes_all_pass_result_is_flagged_for_human_review():
    # An all-pass clause set computed from a source with unresolved tracked
    # changes must NOT be triaged "ready to sign": the gate forces a human item.
    review_result = {
        "clauses": [{"id": "term", "decision": "pass"}],
        "requirements_passed": 1,
        "requirements_failed": 0,
        "requirements_needs_review": 0,
        "tracked_changes": {"has_tracked_changes": True, "tracked_insertions": 1, "tracked_deletions": 1},
    }

    triage = triage_review_result(review_result)

    assert triage["triage_status"] == "legal_review"
    assert triage["next_action"] == "Needs human review"
    assert triage["issue_count"] == 1
    assert triage["requirements_needs_review"] == 1


def test_tracked_changes_does_not_inflate_existing_review_count():
    review_result = {
        "clauses": [{"id": "term", "decision": "review"}],
        "requirements_passed": 0,
        "requirements_failed": 0,
        "requirements_needs_review": 1,
        "tracked_changes": {"has_tracked_changes": True},
    }

    triage = triage_review_result(review_result)

    assert triage["triage_status"] == "legal_review"
    assert triage["requirements_needs_review"] == 1
