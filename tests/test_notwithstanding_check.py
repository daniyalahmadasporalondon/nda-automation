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
    def test_noun_then_negation_is_flagged(self):
        text = (
            STANDARD_EXCLUSIONS
            + " 9. Override. Notwithstanding the foregoing, the exclusions in "
            "Section 2 shall not apply where the Discloser deems the information "
            "sensitive."
        )
        result = detect_carveout_negation(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE)
        self.assertIn("Section 2", result["message"])

    def test_negation_then_noun_is_flagged(self):
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding anything herein, the foregoing exclusions shall "
            "not be applicable to any information the Discloser later designates "
            "as proprietary."
        )
        result = detect_carveout_negation(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE)

    def test_exceptions_are_void_is_flagged(self):
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding the above, the foregoing exceptions are void and "
            "of no effect."
        )
        result = detect_carveout_negation(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE)

    def test_carveouts_inapplicable_is_flagged(self):
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding any other provision to the contrary, the Section 2 "
            "carve-outs are inapplicable."
        )
        result = detect_carveout_negation(_matter(text))
        self.assertIsNotNone(result)


class StaysSilentTests(unittest.TestCase):
    def test_normal_exclusions_clause_is_silent(self):
        # The standard carve-outs on their own must never be flagged.
        result = detect_carveout_negation(_matter(STANDARD_EXCLUSIONS))
        self.assertIsNone(result)

    def test_benign_notwithstanding_survival_is_silent(self):
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding any termination of this Agreement, the "
            "confidentiality obligations shall survive for five years."
        )
        self.assertIsNone(detect_carveout_negation(_matter(text)))

    def test_benign_notwithstanding_disclosure_carveout_is_silent(self):
        # This is itself a carve-out (a lawful-disclosure permission), not a negation
        # of the exclusions.
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, either party may disclose "
            "Confidential Information as required by law."
        )
        self.assertIsNone(detect_carveout_negation(_matter(text)))

    def test_notwithstanding_obligations_not_diminished_is_silent(self):
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, nothing herein shall be construed "
            "to diminish the confidentiality obligations of the Receiving Party."
        )
        self.assertIsNone(detect_carveout_negation(_matter(text)))

    def test_required_by_law_disapplication_is_silent(self):
        # Disapplying an exclusion only to the extent required by law restores a
        # lawful carve-out; it does not gut the protection.
        text = (
            STANDARD_EXCLUSIONS
            + " Notwithstanding the foregoing, the exclusions shall not apply to "
            "the extent required by applicable law."
        )
        self.assertIsNone(detect_carveout_negation(_matter(text)))

    def test_plain_document_without_notwithstanding_is_silent(self):
        text = (
            "This Mutual Non-Disclosure Agreement governs the exchange of "
            "Confidential Information between the parties. The exclusions in "
            "Section 2 apply to publicly available information."
        )
        self.assertIsNone(detect_carveout_negation(_matter(text)))


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
