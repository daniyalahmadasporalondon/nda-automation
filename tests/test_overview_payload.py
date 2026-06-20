"""Read-side projection tests for the Review-workstation "Overview" tab contract.

The Overview tab is data-driven off the existing matter/review payload. These
tests pin the exact fields the Overview frontend reads for each of its five
needs, so an additive change to ``public_matter`` / ``review_matter`` that drops
or renames one of them fails here.

Contract (per matter, all under ``payload["matter"]`` unless noted):

1. Per-clause AI VERDICT + human REVIEWED sign-off:
   - AI verdict: ``review_result.clauses[].decision`` ("pass"|"review"|"fail"),
     aggregated in ``matter.review_state.clause_ids`` / ``counts``.
   - Human reviewed sign-off (per clause): ``redline_draft.reviewed_clause_ids``
     (clause_id -> bool), with ``matter.human_reviewed`` as the matter-level
     fallback default.
2. Counterparty: ``matter.counterparty`` (name) +
   ``matter.counterparty_needs_confirmation`` (confirmed/unconfirmed).
3. Matter facts: ``matter.governing_law`` / ``matter.term_years`` /
   ``matter.term_label`` / ``matter.received_at``.
4. Progress: total clauses = ``matter.review_state.counts.total``; reviewed count
   = size of ``redline_draft.reviewed_clause_ids`` set to True.
5. Empty state: ``matter.has_ai_review`` (False == "No review yet").
"""

from __future__ import annotations

from nda_automation.matter_view import public_matter, review_matter


def _reviewed_matter() -> dict:
    return {
        "id": "m1",
        "subject": "RE: Mutual NDA",
        "received_at": "2026-06-10T09:00:00Z",
        "human_reviewed": False,
        "intake_metadata": {
            "counterparty": {
                "name": "Acme Ltd",
                "confidence": 0.91,
                "verified": True,
                "source": "ai",
            }
        },
        "extracted_text": "Mutual NDA text",
        "review_result": {
            # An AI (ai_first) review ran -- the active-engine marker is what gates the
            # surfaced review_state/counts in public_matter (deterministic-ghost
            # demotion), so this fixture represents a genuinely AI-reviewed matter.
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [
                {"id": "confidentiality", "decision": "pass"},
                {"id": "term_and_survival", "decision": "pass", "term_years": 1},
                {"id": "non_solicitation", "decision": "review"},
                {"id": "governing_law", "decision": "fail"},
            ],
            "requirements_passed": 2,
            "requirements_needs_review": 1,
            "requirements_failed": 1,
        },
        "redline_draft": {
            "reviewed_clause_ids": {"non_solicitation": True, "governing_law": False},
        },
    }


# --- Need 1: per-clause AI verdict ----------------------------------------


def test_clause_ai_verdicts_are_exposed_per_clause():
    payload = review_matter(_reviewed_matter())
    clauses = payload["review_result"]["clauses"]
    verdicts = {clause["id"]: clause["decision"] for clause in clauses}
    assert verdicts == {
        "confidentiality": "pass",
        "term_and_survival": "pass",
        "non_solicitation": "review",
        "governing_law": "fail",
    }


def test_clause_verdicts_aggregated_in_review_state():
    matter = public_matter(_reviewed_matter())
    state = matter["review_state"]
    assert state["clause_ids"]["pass"] == ["confidentiality", "term_and_survival"]
    assert state["clause_ids"]["review"] == ["non_solicitation"]
    assert state["clause_ids"]["check"] == ["governing_law"]


# --- Need 1: per-clause human REVIEWED sign-off ---------------------------


def test_per_clause_reviewed_signoff_is_exposed_in_redline_draft():
    payload = review_matter(_reviewed_matter())
    reviewed = payload["redline_draft"]["reviewed_clause_ids"]
    assert reviewed == {"non_solicitation": True, "governing_law": False}


def test_matter_level_human_reviewed_is_the_signoff_fallback():
    matter = public_matter(_reviewed_matter())
    # Matter-level boolean is the default for clauses absent from the per-clause map.
    assert matter["human_reviewed"] is False


# --- Need 2: counterparty name + confirmed state --------------------------


def test_counterparty_name_and_confirmed_state():
    matter = public_matter(_reviewed_matter())
    assert matter["counterparty"]  # a usable display name is always present
    assert matter["counterparty_needs_confirmation"] is False
    assert matter["counterparty_verified"] is True


def test_unconfirmed_counterparty_flags_needs_confirmation():
    base = _reviewed_matter()
    base["intake_metadata"]["counterparty"]["verified"] = False
    matter = public_matter(base)
    assert matter["counterparty_needs_confirmation"] is True


# --- Need 3: matter facts (governing law, term, received date) ------------


def test_received_date_is_exposed():
    matter = public_matter(_reviewed_matter())
    assert matter["received_at"] == "2026-06-10T09:00:00Z"


def test_term_year_and_label_derived_from_term_clause():
    matter = public_matter(_reviewed_matter())
    assert matter["term_years"] == 1.0
    assert matter["term_label"] == "1 year"


def test_multi_year_term_label_pluralizes():
    base = _reviewed_matter()
    for clause in base["review_result"]["clauses"]:
        if clause["id"] == "term_and_survival":
            clause["term_years"] = 3
    matter = public_matter(base)
    assert matter["term_years"] == 3.0
    assert matter["term_label"] == "3 years"


def test_term_unknown_degrades_to_none_and_blank_label():
    base = _reviewed_matter()
    base["review_result"]["clauses"] = [
        clause for clause in base["review_result"]["clauses"]
        if clause["id"] != "term_and_survival"
    ]
    matter = public_matter(base)
    assert matter["term_years"] is None
    assert matter["term_label"] == ""


def test_governing_law_field_is_always_present():
    # Present as a string on every matter; "" when no approved law is detectable.
    matter = public_matter(_reviewed_matter())
    assert isinstance(matter["governing_law"], str)


def test_governing_law_detected_from_review_clause():
    base = _reviewed_matter()
    for clause in base["review_result"]["clauses"]:
        if clause["id"] == "governing_law":
            clause["governing_law_analysis"] = {
                "candidate_records": [{"value": "England and Wales", "approved": True}]
            }
    matter = public_matter(base)
    assert matter["governing_law"] == "england_and_wales"


# --- Need 4: progress (reviewed count + total clause count) ---------------


def test_total_clause_count_for_progress_line():
    matter = public_matter(_reviewed_matter())
    assert matter["review_state"]["counts"]["total"] == 4


def test_reviewed_clause_count_derivable_from_redline_draft():
    payload = review_matter(_reviewed_matter())
    reviewed = payload["redline_draft"]["reviewed_clause_ids"]
    reviewed_count = sum(1 for value in reviewed.values() if value is True)
    assert reviewed_count == 1  # only non_solicitation marked reviewed


# --- Need 5: whether ANY AI review has run (empty state) ------------------


def test_has_ai_review_true_when_review_result_present():
    assert public_matter(_reviewed_matter())["has_ai_review"] is True


def test_has_ai_review_false_for_unreviewed_matter():
    matter = public_matter({"id": "m2", "subject": "NDA"})
    assert matter["has_ai_review"] is False


def test_has_ai_review_true_from_ai_first_review_result_only():
    matter = public_matter(
        {"id": "m3", "subject": "NDA", "ai_first_review_result": {"clauses": []}}
    )
    assert matter["has_ai_review"] is True


# --- Deterministic-ghost demotion at the SOURCE (public_matter) ------------


def _deterministic_only_matter() -> dict:
    """A matter with stored clause verdicts but NO AI (ai_first) review.

    Mirrors an outbound-generated NDA / an inbound matter reviewed while the AI
    engine was off: review_result.clauses are present (a deterministic fail among
    them) but executed_engine != "ai_first", so ai_review_ran is False.
    """
    return {
        "id": "det1",
        "subject": "Generated NDA",
        "extracted_text": "Generated NDA text",
        "review_result": {
            "active_review_engine": {"executed_engine": "deterministic"},
            "clauses": [
                {"id": "confidentiality", "decision": "pass"},
                {"id": "governing_law", "decision": "fail"},
            ],
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 1,
        },
    }


def test_deterministic_only_matter_surfaces_no_verdict_state():
    # DEMOTION at the source: the surfaced review_state is PENDING (no deterministic
    # verdict), counts are zeroed, and the raw deterministic requirements_* integers
    # are dropped from the public payload so NO consumer can read a verdict.
    matter = public_matter(_deterministic_only_matter())
    assert matter["ai_review_ran"] is False
    state = matter["review_state"]
    assert state["state"] == "pending"
    assert state["label"] == "PENDING"
    assert state["counts"]["total"] == 0
    assert state.get("ai_review_ran") is False
    # The deterministic clause_ids must NOT leak (no "check"/"pass" verdicts shown).
    assert state["clause_ids"]["check"] == []
    assert state["clause_ids"]["pass"] == []
    # The raw deterministic requirement integers are dropped, not surfaced.
    assert "requirements_failed" not in matter
    assert "requirements_needs_review" not in matter
    assert "requirements_passed" not in matter


def test_deterministic_only_fail_still_blocks_send():
    # SEND AUTHORITY is unchanged by the display demotion: the send gate derives from
    # the RAW review_result, so a deterministic FAIL still blocks send / needs human.
    matter = public_matter(_deterministic_only_matter())
    assert matter["needs_human_review"] is True
    assert matter["blocks_send"] is True


def test_ai_reviewed_matter_still_surfaces_verdict_and_counts():
    # The AI-reviewed path is untouched: verdicts, counts and clause_ids surface.
    matter = public_matter(_reviewed_matter())
    assert matter["ai_review_ran"] is True
    state = matter["review_state"]
    assert state["counts"]["total"] == 4
    assert state["clause_ids"]["check"] == ["governing_law"]
    # And a fail still blocks send on the AI path too.
    assert matter["blocks_send"] is True
