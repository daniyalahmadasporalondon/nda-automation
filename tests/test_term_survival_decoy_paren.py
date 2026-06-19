"""Regression guard for two cap-evasion tricks in the term/survival detector.

BUG E1 -- DECOY DURATION. ``_is_benign_indefinite_match``'s CAPPED-DURATION block
treated ANY ``YEAR_TERM_PATTERN`` match anywhere after the "for so/as long as"
connector as proof the survival is capped. A throwaway numeric period that governs
a DIFFERENT comma-segment (a cure / notice / payment period) -- e.g.
"...confidential for so long as it is secret, with a 6 months cure period." --
demoted the uncapped ordinary-CI rider to PASS. The numeric period must actually
GOVERN the confidentiality survival (live in the survival sub-clause) to cap it.

BUG E2 -- PARENTHETICAL-DIGIT TRICK. ``_extract_year_terms_with_context`` trusted
the parenthetical digit over the spelled-out word, so "survive for ten (5) years"
laundered a 10-year term to a compliant 5. When the spelled word and the
parenthetical digit DISAGREE, take the MAX. The AGREEMENT case ("five (5) years")
is unchanged -- only disagreement changes.

These drive the deterministic checker (``verify=False, ai_enabled=False``) so they
exercise the production review path, not a unit-level shortcut.
"""

from __future__ import annotations

import unittest

from nda_automation.checker import review_nda
from nda_automation.checks.common import YEAR_TERM_PATTERN  # noqa: F401  (parity import)
from nda_automation.checks.term_and_survival import _extract_year_terms_with_context


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(
        clause for clause in result["clauses"] if clause["id"] == "term_and_survival"
    )


def _is_indefinite_flag(clause: dict) -> bool:
    reason = str(clause.get("reason") or clause.get("finding") or "").lower()
    return "indefinite" in reason or "perpetual" in reason


class DecoyDurationTests(unittest.TestCase):
    """A decoy numeric period in a different comma-segment must NOT cancel an
    uncapped ordinary-CI perpetual rider (E1)."""

    def assertOrdinaryCiPerpetualFails(self, text: str) -> dict:
        clause = _term_clause(text)
        self.assertFalse(
            clause.get("passes"),
            f"Ordinary-CI perpetual rider was laundered to PASS: {clause.get('reason')!r}",
        )
        self.assertTrue(
            _is_indefinite_flag(clause),
            f"Expected an indefinite/perpetual flag, got: {clause.get('reason')!r}",
        )
        return clause

    def test_decoy_cure_period_does_not_cap_survival(self) -> None:
        # The "6 months" governs the CURE period (a different comma-segment), not the
        # confidentiality survival. The uncapped "for so long as it is secret" rider
        # must still FLAG.
        self.assertOrdinaryCiPerpetualFails(
            "The confidentiality obligations shall remain confidential for so long "
            "as it is secret, with a 6 months cure period."
        )

    def test_decoy_notice_period_does_not_cap_survival(self) -> None:
        self.assertOrdinaryCiPerpetualFails(
            "The confidentiality obligations shall remain confidential for so long "
            "as it retains commercial value, upon 30 days notice."
        )

    def test_control_no_cure_clause_still_flags(self) -> None:
        # Control: the genuine uncapped rider with NO decoy period must still flag
        # (it already did; this locks it so the fix did not over-narrow).
        self.assertOrdinaryCiPerpetualFails(
            "The confidentiality obligations shall remain confidential for so long "
            "as it is secret."
        )

    def test_real_numeric_cap_in_survival_subclause_still_passes(self) -> None:
        # A numeric period that genuinely GOVERNS the survival ("...and for two (2)
        # years following termination") must STILL demote the connector to PASS.
        clause = _term_clause(
            "The confidentiality obligations shall remain confidential for as long "
            "as the recipient is engaged and for two (2) years following termination."
        )
        self.assertTrue(
            clause.get("passes"),
            f"Genuine numeric survival cap was wrongly flagged: {clause.get('reason')!r}",
        )


class ParentheticalDigitTests(unittest.TestCase):
    """When the spelled word and the parenthetical digit disagree, take the MAX (E2).
    The agreement case is untouched."""

    def test_disagreement_word_higher_parses_max(self) -> None:
        # "ten (5)" -> 10, not 5.
        terms = _extract_year_terms_with_context("survive for ten (5) years")
        self.assertEqual([t["years"] for t in terms], [10])

    def test_disagreement_digit_higher_parses_max(self) -> None:
        # "five (10)" -> 10, not 5.
        terms = _extract_year_terms_with_context("survive for five (10) years")
        self.assertEqual([t["years"] for t in terms], [10])

    def test_agreement_unchanged(self) -> None:
        # "five (5)" -> 5 (must be unchanged).
        terms = _extract_year_terms_with_context("survive for five (5) years")
        self.assertEqual([t["years"] for t in terms], [5])

    def test_plain_digit_unchanged(self) -> None:
        # "5 (5)" -> 5 (digit lead, agreement).
        terms = _extract_year_terms_with_context("survive for 5 (5) years")
        self.assertEqual([t["years"] for t in terms], [5])

    def test_ten_paren_five_flags_over_cap(self) -> None:
        # End-to-end: "ten (5) years" must be read as 10 and FLAG over the 5-year cap.
        clause = _term_clause(
            "The confidentiality obligations shall survive for ten (5) years "
            "following termination of this Agreement."
        )
        self.assertFalse(
            clause.get("passes"),
            f"ten (5) years was laundered to a compliant 5: {clause.get('reason')!r}",
        )
        reason = str(clause.get("reason") or clause.get("finding") or "").lower()
        self.assertIn("exceeds", reason)

    def test_five_paren_five_passes_within_cap(self) -> None:
        # Agreement within cap still PASSES end-to-end (precision lock).
        clause = _term_clause(
            "The confidentiality obligations shall survive for five (5) years "
            "following termination of this Agreement."
        )
        self.assertTrue(
            clause.get("passes"),
            f"five (5) years within cap was wrongly flagged: {clause.get('reason')!r}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
