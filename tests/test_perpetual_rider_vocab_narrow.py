"""Regression guard for the narrowed perpetual-survival rider vocabulary.

PROBLEM 1 of the "forever wording" rework: the earlier perpetual-rider vocabulary
over-reached because it added BARE common phrases ("commercial value", "retains
value", "no longer useful") to ``term_and_survival.indefinite_terms``. Those bare
phrases fire on ordinary language -- most critically the standard UTSA Confidential
Information definition ("derives independent commercial value from not being
generally known") and ordinary IP-ownership clauses ("retains all commercial value
in its IP") -- producing a high false-positive rate.

The fix adds ONLY full, specific perpetual phrasings that genuinely mean "forever
survival" and never the bare phrases. These tests lock that contract in via the
deterministic checker (``verify=False, ai_enabled=False``):

  * ordinary NDAs that contain the bare phrases inside normal CI / IP / goodwill
    language must NOT be flagged as indefinite, and
  * a genuine full-phrasing rider on ordinary confidentiality must STILL FAIL.
"""

from __future__ import annotations

import unittest

from nda_automation.checker import review_nda


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(
        clause for clause in result["clauses"] if clause["id"] == "term_and_survival"
    )


class PerpetualRiderVocabNarrowTests(unittest.TestCase):
    def assertNotIndefinite(self, text: str) -> dict:
        clause = _term_clause(text)
        reason = str(clause.get("reason") or "").lower()
        self.assertNotIn(
            "indefinite",
            reason,
            f"Ordinary language was mis-flagged as perpetual: {clause.get('reason')!r}",
        )
        self.assertNotIn(
            "perpetual",
            reason,
            f"Ordinary language was mis-flagged as perpetual: {clause.get('reason')!r}",
        )
        return clause

    def test_clean_five_year_survival_with_utsa_ci_definition_passes(self) -> None:
        # The bare phrase "commercial value" appears inside the standard UTSA
        # Confidential Information definition; the term itself is a clean 5-year cap.
        text = (
            "Mutual Non-Disclosure Agreement. The parties have reciprocal "
            "confidentiality obligations. Confidential Information means information "
            "that derives independent commercial value from not being generally "
            "known. The confidentiality obligations survive for a period of five (5) "
            "years after termination."
        )
        clause = self.assertNotIndefinite(text)
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_ip_retains_commercial_value_does_not_flag(self) -> None:
        text = (
            "Mutual Non-Disclosure Agreement. The parties have reciprocal "
            "confidentiality obligations. The confidentiality obligations survive for "
            "three (3) years after termination. The Disclosing Party retains all "
            "commercial value in its intellectual property and grants no license."
        )
        clause = self.assertNotIndefinite(text)
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_goodwill_transfer_disclaimer_does_not_flag(self) -> None:
        text = (
            "Mutual Non-Disclosure Agreement. The parties have reciprocal "
            "confidentiality obligations. The confidentiality obligations survive for "
            "two (2) years after termination. Nothing in this Agreement transfers any "
            "commercial value or goodwill to the Receiving Party."
        )
        clause = self.assertNotIndefinite(text)
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_genuine_full_phrasing_rider_still_fails(self) -> None:
        # A genuine perpetual rider on ORDINARY confidentiality must still flag.
        text = (
            "Mutual Non-Disclosure Agreement. The parties have reciprocal "
            "confidentiality obligations. The confidentiality obligations continue "
            "until it ceases to have commercial value, and for so long as it retains "
            "commercial value."
        )
        clause = _term_clause(text)
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite", str(clause.get("reason") or "").lower())


if __name__ == "__main__":
    unittest.main()
