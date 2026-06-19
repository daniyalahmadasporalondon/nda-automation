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


class InlineCarveOutLaunderingNBW1bTests(unittest.TestCase):
    """NB-W1b: the inline carve-out clearance must not launder an ordinary-CI
    perpetual rider that merely carries a carve-out SIGNAL while ordinary
    CONFIDENTIAL INFORMATION is the governed perpetual subject.

    The discriminator: an unconditional indefinite word (perpetual / perpetually /
    indefinitely / in perpetuity) governing ordinary CI in the trigger's own
    sub-clause FAILS, even with a carve-out lead or trailing carve-out scope; a
    legitimate carve-out whose perpetual subject is the carve-out itself (trade
    secret / required-by-law retention) still PASSES.
    """

    # ---- LAUNDERING vectors that must FAIL (ordinary-CI perpetual riders) ----

    def test_nbw1b_canonical_indefinitely_wrt_all_ci_fails(self):
        clause = _term_clause(
            "The confidentiality obligations shall continue indefinitely with "
            "respect to all Confidential Information disclosed."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_nbw1b_word_order_survive_perpetuity_for_all_ci_fails(self):
        clause = _term_clause(
            "The obligations of confidentiality shall survive in perpetuity for all "
            "Confidential Information."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_nbw1b_word_order_perpetually_as_to_the_ci_fails(self):
        clause = _term_clause(
            "Confidentiality shall continue perpetually as to the Confidential "
            "Information."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_carveout_lead_then_ordinary_ci_perpetual_subject_fails(self):
        # "with respect to trade secrets" leads, but ORDINARY CI is the perpetual
        # subject in the same sub-clause -- the carve-out signal must not launder it.
        clause = _term_clause(
            "With respect to trade secrets, all Confidential Information shall remain "
            "confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_perpetual_with_trailing_law_garnish_fails(self):
        # Unconditional "in perpetuity" on ordinary CI, with "as required by
        # applicable law" appended as a trailing garnish (NOT a genuine condition).
        clause = _term_clause(
            "The Confidential Information shall survive in perpetuity, as required by "
            "applicable law."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_indefinitely_with_trailing_law_garnish_fails(self):
        clause = _term_clause(
            "All Confidential Information shall remain confidential indefinitely, as "
            "required by applicable law."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_perpetual_with_trailing_trade_secret_scope_fails(self):
        clause = _term_clause(
            "All Confidential Information shall remain confidential in perpetuity, "
            "with respect to trade secrets."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_ordinary_ci_indefinitely_for_trade_secrets_fails(self):
        clause = _term_clause(
            "Confidential Information shall survive indefinitely for trade secrets."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    # ---- LEGITIMATE carve-outs that must STILL PASS (discriminator holds) ----

    def test_legit_inline_trade_secret_perpetuity_still_passes(self):
        # No ordinary-CI DATA noun in the trigger sub-clause -- the carve-out term
        # itself is the perpetual subject.
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years; with respect "
            "to trade secrets, confidentiality shall continue in perpetuity."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_legit_required_by_law_retention_of_information_still_passes(self):
        # Ordinary "information" is the subject, but the trigger is the CONDITIONAL
        # "for as long as ... required by applicable law" connector (not an
        # unconditional perpetual word) -- a genuine required-by-law retention.
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years, except that "
            "information shall be retained for as long as required by applicable law."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_legit_capped_ci_then_except_trade_secret_perpetuity_still_passes(self):
        # Ordinary CI is CAPPED at five years in the PRIOR sub-clause; only the
        # trade-secret carve-out (after the exception connector) is perpetual.
        clause = _term_clause(
            "The confidentiality obligations of the Receiving Party shall survive for "
            "five (5) years following termination, except that trade secrets shall "
            "survive in perpetuity."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])


class NBW1bDecoyCapLaunderingProbesTests(unittest.TestCase):
    """The seven canonical NB-W1b laundering probes, each with a 5-year DECOY cap so
    a year-term is present and the perpetual rider is the only defect.

    These cover the two failure modes the first (inert) fix missed:
      (a) POST-TRIGGER CI noun -- the ordinary-CI subject sits AFTER the perpetual
          trigger ("...continues indefinitely with respect to all Confidential
          Information disclosed");
      (b) SYNONYM CI nouns -- "materials", "data", "items", defined disclosed-info
          terms ("Discloser Data", "disclosed items", "such materials").
    Plus the conditional+unconditional co-occurrence (probe 7): a conditional
    "for as long as ... required by applicable law" connector must NOT exempt a
    separate unconditional "in perpetuity" in the same sentence.
    """

    def test_probe1_posttrigger_all_ci_disclosed_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. With respect to trade secrets, the duty continues "
            "indefinitely with respect to all Confidential Information disclosed."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe2_such_materials_perpetuity_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. With respect to trade secrets, such materials shall be held "
            "in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe3_the_data_perpetuity_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. With respect to trade secrets, the data shall remain "
            "confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe4_the_disclosed_items_perpetuity_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. With respect to trade secrets, the disclosed items shall "
            "remain confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe5_discloser_data_perpetuity_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. With respect to trade secrets, the Discloser Data shall be "
            "held in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe6_perpetual_then_applies_to_all_ci_fails(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination. As to trade secrets, survival is perpetual, and this "
            "applies to all Confidential Information."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    def test_probe7_conditional_connector_does_not_exempt_unconditional_perpetuity(self):
        # A conditional "for as long as ... required by applicable law" co-occurs with
        # a SEPARATE unconditional "in perpetuity" on ordinary CI in the same
        # sentence; the unconditional perpetuity must still be caught.
        clause = _term_clause(
            "The Confidential Information shall remain confidential for as long as "
            "required by applicable law, and in any event in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])
        self.assertIn("indefinite or perpetual", clause["finding"])

    # ---- The legit cousins each probe must NOT also flag ----

    def test_legit_inline_ts_with_decoy_cap_still_passes(self):
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years following "
            "termination; with respect to trade secrets, confidentiality shall "
            "continue in perpetuity."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_legit_pure_required_by_law_retention_no_perpetuity_passes(self):
        # PURE required-by-law retention with NO unconditional perpetuity anywhere.
        clause = _term_clause(
            "The confidentiality obligations survive for five (5) years, except that "
            "the Confidential Information shall be retained for as long as required by "
            "applicable law."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])


if __name__ == "__main__":
    unittest.main()
