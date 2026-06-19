import unittest

from nda_automation.notwithstanding_check import (
    REASON_CODE,
    detect_carveout_negation,
)


def _matter(text):
    return {"extracted_text": text}


# A realistic standard exclusions clause -- the protections that a sneaky draft keeps
# on the page while cancelling them elsewhere.
STANDARD_EXCLUSIONS = (
    "2. Exclusions. Confidential Information does not include information that "
    "(a) is or becomes publicly available through no fault of the Receiving Party; "
    "(b) was rightfully known to the Receiving Party without restriction before "
    "receipt; or (c) is independently developed without use of the Confidential "
    "Information."
)


class FlagsTheNegationTrickTests(unittest.TestCase):
    """Recall: every shape of a real negation of a named exclusions noun is caught,
    with OR without a 'notwithstanding'/'foregoing' lead-in."""

    def _assert_flagged(self, text):
        result = detect_carveout_negation(_matter(text))
        self.assertIsNotNone(result, f"expected a flag for: {text!r}")
        self.assertEqual(result["reason_code"], REASON_CODE)
        return result

    def test_notwithstanding_exclusions_shall_not_apply(self):
        result = self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " 9. Override. Notwithstanding the foregoing, the exclusions in "
            "Section 2 shall not apply where the Discloser deems the information "
            "sensitive."
        )
        self.assertIn("Section 2", result["message"])

    def test_foregoing_exceptions_are_void(self):
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " The foregoing exceptions are void and of no effect with respect to "
            "any information designated Restricted by the Disclosing Party."
        )

    def test_section_exceptions_are_inapplicable_without_lead_in(self):
        # No 'notwithstanding'/'foregoing' lead-in at all -- still a real negation.
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " The exceptions in Section 4 are inapplicable to any Confidential "
            "Information relating to the Disclosing Party's customers or pricing."
        )

    def test_carveouts_of_no_force_or_effect_without_lead_in(self):
        # Opens "For the avoidance of doubt", no override lead-in.
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " For the avoidance of doubt, the carve-outs in Section 2 shall be of "
            "no force or effect, and the Receiving Party shall treat all disclosed "
            "information as Confidential Information."
        )

    def test_supersede_and_override_the_exclusions(self):
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " In the event of any conflict, the confidentiality obligations shall "
            "supersede and override the exclusions stated above, which shall yield "
            "to the Disclosing Party's interest."
        )

    def test_exceptions_deemed_deleted_and_struck(self):
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " The parties agree that the Standard Exceptions paragraph above is "
            "deemed deleted and struck from this Agreement in its entirety."
        )

    def test_no_exception_shall_be_available(self):
        # Universal-quantifier disapplication; opens "Provided, however".
        self._assert_flagged(
            STANDARD_EXCLUSIONS
            + " Provided, however, that no exception, exclusion, or carve-out shall "
            "be available to the Receiving Party with respect to any Confidential "
            "Information disclosed under this Agreement."
        )

    def test_preceding_sentence_backref_negation_of_required_by_law(self):
        # The negated noun is "the preceding sentence" -- valid only because the prior
        # sentence stated a required-by-law carve-out.
        self._assert_flagged(
            "The obligations of confidentiality shall not apply where disclosure is "
            "required by law, court order, or governmental authority.\n"
            "Notwithstanding anything to the contrary, the preceding sentence shall "
            "not apply, and the Receiving Party shall not disclose any Confidential "
            "Information even where compelled by law, without prior written consent."
        )


class StaysSilentTests(unittest.TestCase):
    """Precision: benign uses, affirmations, and the normal exclusions clause itself
    must NEVER flag."""

    def _assert_silent(self, text):
        self.assertIsNone(
            detect_carveout_negation(_matter(text)),
            f"expected silence for: {text!r}",
        )

    def test_normal_exclusions_clause_is_silent(self):
        self._assert_silent(STANDARD_EXCLUSIONS)

    def test_obligations_not_apply_is_the_normal_clause_not_a_negation(self):
        # "the obligations ... shall not apply to public information" IS the exclusions
        # clause working correctly; the verb governs 'obligations', not an exclusions noun.
        self._assert_silent(
            "EXCLUSIONS. The obligations of confidentiality shall not apply to "
            "information that is publicly available, independently developed, or "
            "required to be disclosed by law."
        )

    def test_benign_notwithstanding_survival_is_silent(self):
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding any termination of this Agreement, the "
            "confidentiality obligations shall survive for five years."
        )

    def test_benign_notwithstanding_disclosure_carveout_is_silent(self):
        # Itself a carve-out (a lawful-disclosure permission), not a negation.
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, either party may disclose "
            "Confidential Information to the extent required by applicable law."
        )

    def test_notwithstanding_in_liability_clause_is_silent(self):
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding anything to the contrary in this Agreement, in no "
            "event shall either party be liable for indirect or consequential "
            "damages."
        )

    def test_notwithstanding_in_assignment_clause_is_silent(self):
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, either party may assign this "
            "Agreement to an affiliate or in connection with a merger."
        )

    def test_residuals_clause_is_silent(self):
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding any other provision, either party may use Residuals "
            "(information retained in the unaided memory of its personnel) for any "
            "purpose."
        )

    def test_affirming_exclusions_shall_apply_is_silent(self):
        # Inverted-polarity trap: 'the exclusions in Section 2 SHALL APPLY in full'.
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding anything to the contrary herein, the exclusions in "
            "Section 2 shall apply in full and nothing shall limit the Receiving "
            "Party's right to rely on them."
        )

    def test_required_by_law_disapplication_is_silent(self):
        # Disapplying an exclusion only to the extent required by law restores a
        # lawful carve-out; it does not gut the protection.
        self._assert_silent(
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, the exclusions shall not apply to "
            "the extent required by applicable law."
        )

    def test_backref_negation_without_exclusions_target_is_silent(self):
        # "the preceding sentence shall not apply" but the preceding sentence is an
        # unrelated notices clause -- no exclusion negated.
        self._assert_silent(
            "All notices under this Agreement shall be delivered by email.\n"
            "Notwithstanding the foregoing, the preceding sentence shall not apply "
            "to notices of termination, which must be sent by registered post."
        )

    def test_plain_document_without_trigger_is_silent(self):
        self._assert_silent(
            "This Mutual Non-Disclosure Agreement governs the exchange of "
            "Confidential Information between the parties. The exclusions in "
            "Section 2 apply to publicly available information."
        )


class FailSafeTests(unittest.TestCase):
    def test_none_matter_returns_none(self):
        self.assertIsNone(detect_carveout_negation(None))

    def test_empty_matter_returns_none(self):
        self.assertIsNone(detect_carveout_negation({}))

    def test_empty_text_returns_none(self):
        self.assertIsNone(detect_carveout_negation(_matter("")))

    def test_non_string_text_returns_none(self):
        self.assertIsNone(detect_carveout_negation(_matter(12345)))

    def test_non_mapping_matter_returns_none(self):
        self.assertIsNone(detect_carveout_negation("not a matter"))
        self.assertIsNone(detect_carveout_negation(42))
        self.assertIsNone(detect_carveout_negation([1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
