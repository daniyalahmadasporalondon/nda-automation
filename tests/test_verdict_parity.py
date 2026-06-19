"""Python half of the FE<->backend verdict-parity guard.

Asserts the checked-in ``verdict_parity_matrix.json`` fixture still matches what
the LIVE Python authority computes for every scenario. The frontend half
(``tests/frontend/utility-modules.mjs``) reads the SAME fixture and asserts the
FE twins agree with these recorded verdicts -- so a drift on EITHER side fails
CI:

  * Change the Python roll-up without regenerating the fixture -> this test goes
    RED (the recorded ``expected`` no longer matches the live functions).
  * Regenerate the fixture (capturing new Python verdicts) but leave a FE twin
    re-deriving the old way -> the JS parity assertions go RED.

Regenerate intentionally with:
    python -m tests.fixtures.verdict_parity_matrix
"""

from __future__ import annotations

import json

from tests.fixtures.verdict_parity_matrix import (
    FIXTURE_PATH,
    build_fixture,
    expected_for,
    scenarios,
)


def test_fixture_matches_live_python_authority():
    on_disk = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    regenerated = build_fixture()
    assert on_disk == regenerated, (
        "verdict_parity_matrix.json is stale against the live Python verdict "
        "roll-up. Regenerate it with `python -m tests.fixtures.verdict_parity_matrix` "
        "and re-run the FE parity suite so the frontend twins stay in lock-step."
    )


def test_pure_fail_blocks_send():
    # The regression the whole fix exists for: a hard FAIL with zero review items
    # must need a human AND block send.
    case = next(s for s in scenarios() if s["name"] == "pure_check_hard_fail")
    expected = expected_for(case["matter"])
    assert expected["needs_human_review"] is True
    assert expected["blocks_send"] is True
    assert expected["clause_states"]["governing_law"] == "check"


def test_human_resolution_lifts_send_block_but_not_review_flag():
    case = next(s for s in scenarios() if s["name"] == "fail_resolved_by_human")
    expected = expected_for(case["matter"])
    assert expected["needs_human_review"] is True
    assert expected["blocks_send"] is False
