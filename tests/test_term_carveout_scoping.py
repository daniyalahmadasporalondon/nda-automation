"""Cross-sentence / cross-clause longer-survival carve-out scoping.

PROBLEM 2 of the "forever wording" rework: the perpetual-survival carve-out guard
used to recognize a legitimate longer-survival carve-out only when the carve-out
term (trade secrets / required by law / personal data / data protection) sat in the
EXACT SAME comma/semicolon/period-bounded fragment as the perpetual trigger. Normal
multi-sentence / multi-clause carve-outs were therefore wrongly flagged.

These tests pin the widened behavior: a carve-out is recognized across sentence and
comma boundaries (sentence/clause-local scoping), while an ordinary-CI perpetual
rider with no governing carve-out term still FAILS (precision preserved).

All assertions drive the deterministic checker (verify=False, ai_enabled=False).
"""

import unittest

from nda_automation.checker import review_nda


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(c for c in result["clauses"] if c["id"] == "term_and_survival")


class CrossBoundaryCarveOutTests(unittest.TestCase):
    # ---- LEGITIMATE carve-outs that must now PASS (no perpetual flag) ----

    def test_semicolon_then_comma_split_trade_secret_perpetuity(self):
        # "with respect to trade secrets" scopes the perpetuity; the carve-out term
        # is split from the trigger by BOTH a semicolon and a comma.
        clause = _term_clause(
            "The confidentiality obligations survive five (5) years; with respect to "
            "trade secrets, confidentiality shall continue in perpetuity."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_required_by_applicable_law_across_comma(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years, except that "
            "information shall be retained for as long as required by applicable law."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_personal_data_data_protection_law_across_semicolon(self):
        clause = _term_clause(
            "Confidentiality survives for five (5) years; personal data shall be "
            "retained for as long as required by data-protection law."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_trade_secret_perpetuity_in_separate_sentence(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years. Trade "
            "secrets shall remain confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_trade_secret_commercial_value_across_two_sentences(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years. Trade "
            "secrets shall be kept confidential for as long as the trade secret "
            "retains its commercial value."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_data_protection_law_requires_idiom(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years; personal "
            "data survives for as long as data-protection law requires."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    # ---- PRECISION: ordinary-CI perpetual riders must still FAIL ----

    def test_ordinary_ci_perpetual_with_no_carveout_fails(self):
        clause = _term_clause(
            "The Confidential Information shall remain confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_perpetual_with_distant_trade_secret_mention_fails(self):
        # The doc merely MENTIONS trade secrets in an unrelated sentence; ordinary CI
        # is the thing made perpetual, so it must still FAIL.
        clause = _term_clause(
            "The Confidential Information shall remain confidential in perpetuity. "
            "Separately, this Agreement protects trade secrets disclosed during the term."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_co_governing_perpetuity_with_carveout_word_fails(self):
        # Ordinary CI is conjoined with the carve-out word into the SAME perpetual
        # survival; the carve-out word must not launder an ordinary-CI perpetual.
        clause = _term_clause(
            "The confidential information and trade secrets shall remain confidential "
            "in perpetuity."
        )
        self.assertFalse(clause["passes"])
        self.assertNotIn("within the cap", clause["finding"])

    def test_over_cap_ordinary_year_term_with_trade_secret_carveout_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for seven years, except trade "
            "secrets shall survive for ten years."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("exceeds the cap", clause["finding"])


if __name__ == "__main__":
    unittest.main()
