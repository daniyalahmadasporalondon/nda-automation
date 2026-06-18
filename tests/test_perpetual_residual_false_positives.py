"""Regression guard for the two residual perpetual/indefinite false-positives.

The "forever wording" rework cut the term/survival perpetual false-positive rate
from 59% to 7%; the residual 7% was two clean-document classes the detector still
WRONGLY flagged as perpetual/indefinite:

  * CO-15 -- POLARITY: an "indefinite word" (perpetual / indefinitely / perpetually)
    that governs a NON-survival object -- "a perpetual *license* to use the
    platform", "remain indefinitely *available*" -- is benign and must not trip the
    indefinite-survival flag, and a carve-out-LED sentence ("With respect to trade
    secrets, ... shall survive perpetually") is a legitimate longer-survival
    carve-out.

  * CO-6 -- CAPPED DURATION CONNECTOR: a bare "for so/as long as" is only a duration
    connector, not inherently perpetual. When its own clause is capped ("...for as
    long as it is employed ... and for two (2) years") or it governs the Agreement
    term rather than confidentiality, the survival is not perpetual.

The fix is principled (playbook-sourced ``indefinite_non_survival_objects`` vocab +
narrow polarity / scope / capped-connector guards), NOT a literal skip of these
docs. These tests pin BOTH the demotions and the precision: the genuine perpetual
riders must STILL FAIL. All assertions drive the deterministic checker.
"""

from __future__ import annotations

import unittest

from nda_automation.checker import review_nda


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(
        clause for clause in result["clauses"] if clause["id"] == "term_and_survival"
    )


def _is_indefinite_flag(clause: dict) -> bool:
    reason = str(clause.get("reason") or clause.get("finding") or "").lower()
    return "indefinite" in reason or "perpetual" in reason


class ResidualPerpetualFalsePositiveTests(unittest.TestCase):
    # ---- CO-15 (POLARITY): indefinite word governs a NON-survival object ----

    def test_perpetual_license_does_not_flag(self) -> None:
        clause = _term_clause(
            "The Disclosing Party grants the Receiving Party a perpetual license to "
            "use the platform. The confidentiality obligations survive for five (5) "
            "years after termination."
        )
        self.assertFalse(_is_indefinite_flag(clause))
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_indefinitely_available_does_not_flag(self) -> None:
        clause = _term_clause(
            "The portal shall remain indefinitely available to the parties. The "
            "confidentiality obligations survive for three (3) years after termination."
        )
        self.assertFalse(_is_indefinite_flag(clause))
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_carve_out_led_perpetually_does_not_flag(self) -> None:
        # The sentence OPENS with "With respect to trade secrets," so the perpetual
        # survival is a legitimate trade-secret carve-out, not ordinary CI.
        clause = _term_clause(
            "With respect to trade secrets, the obligations of confidentiality shall "
            "survive perpetually. All other Confidential Information is protected for "
            "three (3) years."
        )
        self.assertFalse(_is_indefinite_flag(clause))

    # ---- CO-6 (CAPPED DURATION CONNECTOR): bare "for so/as long as" ----

    def test_for_as_long_as_with_trailing_year_cap_does_not_flag(self) -> None:
        clause = _term_clause(
            "The confidentiality obligations shall remain in effect for as long as "
            "this Agreement remains in effect, and for two (2) years thereafter."
        )
        self.assertFalse(_is_indefinite_flag(clause))
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_for_so_long_as_governing_agreement_term_does_not_flag(self) -> None:
        clause = _term_clause(
            "This Agreement shall continue in effect for so long as the parties have "
            "a business relationship, terminable on 30 days notice; confidentiality "
            "survives two (2) years after termination."
        )
        self.assertFalse(_is_indefinite_flag(clause))

    # ---- PRECISION: genuine perpetual riders must STILL FAIL ----

    def test_ordinary_ci_perpetuity_still_fails(self) -> None:
        clause = _term_clause(
            "The Confidential Information shall remain confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertTrue(_is_indefinite_flag(clause))

    def test_ordinary_ci_continue_indefinitely_still_fails(self) -> None:
        clause = _term_clause(
            "The confidentiality obligations shall continue indefinitely."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertTrue(_is_indefinite_flag(clause))

    def test_conjoined_ci_and_trade_secret_perpetuity_still_fails(self) -> None:
        # No LEADING scoping signal: ordinary CI co-governs the perpetual survival,
        # so the trade-secret mention must not launder it.
        clause = _term_clause(
            "The confidential information and trade secrets shall remain confidential "
            "in perpetuity."
        )
        self.assertFalse(clause["passes"])
        self.assertTrue(_is_indefinite_flag(clause))

    def test_for_so_long_as_ci_rider_with_no_trailing_cap_still_fails(self) -> None:
        # The connector governs confidentiality (not the Agreement term) and has NO
        # numeric cap after it -> a genuine uncapped perpetual rider.
        clause = _term_clause(
            "The confidentiality obligations continue until it ceases to have "
            "commercial value, and for so long as it retains commercial value."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertTrue(_is_indefinite_flag(clause))


if __name__ == "__main__":
    unittest.main()
