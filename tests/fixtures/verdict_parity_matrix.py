"""Shared verdict-parity scenario matrix.

ONE source of scenarios, exercised by BOTH sides of the verdict roll-up so the
frontend (a pure renderer) can never silently drift from the Python authority:

  * ``tests/test_verdict_parity.py`` runs each scenario's matter through the
    live Python authority (``matter_view.public_matter`` /
    ``review_state.result_requires_human_review`` / ``clause_review_state``) and
    asserts the regenerated fixture below matches -- so changing the Python
    computation without regenerating the fixture FAILS CI.

  * ``tests/frontend/utility-modules.mjs`` loads the SAME generated fixture
    (``verdict_parity_matrix.json``) and asserts the FE twins
    (``needsHumanReview`` / ``sendIsBlockedByReview`` / ``clauseStatus``) AGREE
    with the recorded Python verdicts -- so a FE re-derivation that drops an
    axis (the original pure-FAIL bug) FAILS CI.

Regenerate the JSON after intentionally changing the matrix or the Python
roll-up:  ``python -m tests.fixtures.verdict_parity_matrix``
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from nda_automation.matter_view import public_matter
from nda_automation.review_state import (
    clause_review_state,
    result_requires_human_review,
)

FIXTURE_PATH = pathlib.Path(__file__).with_name("verdict_parity_matrix.json")

# Each scenario is a self-contained matter the FE could receive. The recipient
# fields are populated so the send gate is decided by the REVIEW axis (not a
# missing-recipient block), isolating the verdict divergence under test.
_RECIPIENT = {"sender": "counterparty@example.com", "reply_to": "counterparty@example.com"}


def _matter(clauses: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    matter: Dict[str, Any] = {"id": "matter", **_RECIPIENT}
    matter["review_result"] = {"clauses": clauses, **extra.pop("review_result", {})}
    matter.update(extra)
    return matter


def scenarios() -> List[Dict[str, Any]]:
    return [
        {
            "name": "all_clear",
            "matter": _matter([
                {"id": "confidential_information", "decision": "pass"},
                {"id": "governing_law", "decision": "pass"},
            ]),
        },
        {
            "name": "has_review_only",
            "matter": _matter([
                {"id": "confidential_information", "decision": "pass"},
                {"id": "non_solicitation", "decision": "review"},
            ]),
        },
        {
            # The original divergence: a hard FAIL with ZERO review items. The FE
            # twin used to false-clear this (only looked at the review axis).
            "name": "pure_check_hard_fail",
            "matter": _matter([
                {"id": "confidential_information", "decision": "pass"},
                {"id": "governing_law", "decision": "fail"},
            ]),
        },
        {
            "name": "mixed_review_and_check",
            "matter": _matter([
                {"id": "confidential_information", "decision": "pass"},
                {"id": "non_solicitation", "decision": "review"},
                {"id": "governing_law", "decision": "fail"},
            ]),
        },
        {
            # An unknown/garbage decision must escalate to review (unknown -> review),
            # never silently clear.
            "name": "unknown_error_clause",
            "matter": _matter([
                {"id": "confidential_information", "decision": "pass"},
                {"id": "mystery", "decision": "totally-unknown"},
            ]),
        },
        {
            # A clean fail resolved by a human: needs_human_review stays True but the
            # send block is RESOLVED (human_reviewed) so blocks_send is False.
            "name": "fail_resolved_by_human",
            "matter": _matter(
                [{"id": "governing_law", "decision": "fail"}],
                human_reviewed=True,
            ),
        },
    ]


def expected_for(matter: Dict[str, Any]) -> Dict[str, Any]:
    """The authoritative verdicts, straight from the live Python functions."""
    pm = public_matter(matter)
    clauses = matter.get("review_result", {}).get("clauses", [])
    clause_states = {
        str(clause.get("id")): clause_review_state(clause)["state"]
        for clause in clauses
        if isinstance(clause, dict)
    }
    return {
        "needs_human_review": bool(pm["needs_human_review"]),
        "blocks_send": bool(pm["blocks_send"]),
        "result_requires_human_review": bool(
            result_requires_human_review(matter["review_result"])
        ),
        "clause_states": clause_states,
    }


def build_fixture() -> Dict[str, Any]:
    cases = []
    for scenario in scenarios():
        cases.append({
            "name": scenario["name"],
            "matter": scenario["matter"],
            "expected": expected_for(scenario["matter"]),
        })
    return {"version": 1, "cases": cases}


def write_fixture() -> None:
    FIXTURE_PATH.write_text(json.dumps(build_fixture(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_fixture()
    print(f"wrote {FIXTURE_PATH}")
