"""Tests for the definition-poison detector (precision-tuned, fail-safe)."""
import unittest

from nda_automation.definition_poison_check import (
    REASON_CODE_AFFILIATE_POISON,
    REASON_CODE_CI_POISON,
    detect_definition_poison,
)


def _matter(text):
    return {"extracted_text": text}


# A real UTSA-style CI definition (condensed from the slice-bank NDA fixture).
# This is the KEY precision case: it must stay SILENT (no false positive).
UTSA_CI_DEFINITION = (
    "\"Confidential Information\" means any data or information that is "
    "proprietary to the Disclosing Party and not generally known to the public, "
    "whether in tangible or intangible form, and which derives independent "
    "economic value from not being generally known to or readily ascertainable "
    "by the public. Notwithstanding the foregoing, the term \"Confidential "
    "Information\" shall not include information which is generally known by the "
    "public through no fault of the Receiving Party, or is or has been "
    "independently developed by the Receiving Party without reference to any "
    "Confidential Information."
)

# A normal corporate-control Affiliate definition. Must stay SILENT.
NORMAL_AFFILIATE_DEFINITION = (
    "\"Affiliate\" means any entity that, directly or indirectly, controls, is "
    "controlled by, or is under common control with a Party, where control means "
    "ownership of more than fifty percent of the voting securities. The Receiving "
    "Party shall not disclose Confidential Information to any third party."
)


class DefinitionPoisonTests(unittest.TestCase):
    # ---- CI-definition poison: FLAGS ------------------------------------
    def test_ci_definition_including_public_info_flags(self):
        text = (
            "\"Confidential Information\" means all information disclosed by a "
            "Party and includes information that is publicly available or already "
            "known to the Receiving Party or independently developed by the "
            "Receiving Party."
        )
        result = detect_definition_poison(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_CI_POISON)
        self.assertTrue(result["message"])

    def test_ci_definition_shall_include_already_known_flags(self):
        text = (
            "The term \"Confidential Information\" shall include, without "
            "limitation, information that is already known to the public or in "
            "the public domain at the time of disclosure."
        )
        result = detect_definition_poison(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_CI_POISON)

    # ---- CI-definition poison: SILENT (the key precision case) -----------
    def test_normal_utsa_ci_definition_is_silent(self):
        # A standard UTSA definition + standard exclusions must NOT flag.
        self.assertIsNone(detect_definition_poison(_matter(UTSA_CI_DEFINITION)))

    def test_standard_exclusion_block_is_silent(self):
        # The exclusion carve-out names the very categories the poison detector
        # cares about, but with "shall not include" framing -> safe.
        text = (
            "Confidential Information does not include information that is "
            "publicly available, already known to the Receiving Party, or "
            "independently developed by the Receiving Party."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    # ---- Affiliate poison: FLAGS ----------------------------------------
    def test_overbroad_affiliate_feeding_restraint_flags(self):
        text = (
            "\"Affiliate\" means any person or entity that the Disclosing Party "
            "may designate, whether or not affiliated with the Disclosing Party. "
            "The Receiving Party shall not solicit or do business with any "
            "Affiliate for a period of two years."
        )
        result = detect_definition_poison(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_AFFILIATE_POISON)
        self.assertTrue(result["message"])

    # ---- Affiliate poison: SILENT ---------------------------------------
    def test_normal_affiliate_definition_is_silent(self):
        self.assertIsNone(
            detect_definition_poison(_matter(NORMAL_AFFILIATE_DEFINITION))
        )

    def test_overbroad_affiliate_without_restraint_is_silent(self):
        # Over-broad definition but nothing leans on it -> precision: silent.
        text = (
            "\"Representative\" means any third party whatsoever that a Party may "
            "designate. The Parties acknowledge their mutual interest in the "
            "Purpose."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    # ---- Fail-safe: garbage / empty -> None, no crash --------------------
    def test_empty_text_returns_none(self):
        self.assertIsNone(detect_definition_poison(_matter("")))
        self.assertIsNone(detect_definition_poison({}))

    def test_non_mapping_inputs_return_none(self):
        for bad in (None, "a string", 12345, ["a", "list"], object()):
            self.assertIsNone(detect_definition_poison(bad))

    def test_garbage_text_returns_none(self):
        garbage = "asdf \x00\x01 ||| 99999 \n\n\t ;;;; .... random tokens"
        self.assertIsNone(detect_definition_poison(_matter(garbage)))

    def test_missing_extracted_text_key_returns_none(self):
        self.assertIsNone(detect_definition_poison({"other": "field"}))


if __name__ == "__main__":
    unittest.main()
