"""Tests for the definition-poison detector (precision-tuned, fail-safe)."""
import unittest

from nda_automation.definition_poison_check import (
    REASON_CODE_AFFILIATE_POISON,
    REASON_CODE_CI_POISON,
    ci_poison_severity,
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

    def test_non_public_ci_definition_is_silent(self):
        # REGRESSION: the bare substring "public" inside "non-public" must NOT be
        # read as the excluded category. A standard narrow CI scope stays silent.
        text = (
            "\"Confidential Information\" means any non-public information "
            "disclosed by one party to the other that is designated as "
            "confidential. The Receiving Party shall not disclose it."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    def test_utsa_independent_economic_value_is_silent(self):
        # REGRESSION: "derives independent economic value ... not being generally
        # known" (the canonical UTSA trade-secret definition) must stay silent.
        text = (
            "\"Confidential Information\" means information that derives "
            "independent economic value, actual or potential, from not being "
            "generally known to and not being readily ascertainable by other "
            "persons, and is the subject of reasonable efforts to maintain its "
            "secrecy."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    def test_exclusions_shall_not_apply_framing_is_silent(self):
        # REGRESSION: "the obligations of confidentiality shall not apply to "
        # information which is publicly available ..." is the standard exclusions
        # clause -> silent despite naming every trigger phrase.
        text = (
            "\"Confidential Information\" means non-public business information. "
            "The obligations of confidentiality shall not apply to information "
            "which is or becomes publicly available, is generally known to the "
            "public, was already known to the Receiving Party, or is "
            "independently developed by the Receiving Party."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    def test_obfuscated_negation_poison_flags(self):
        # tp07: negated-form poison -- "shall NOT cease to be Confidential
        # Information by reason of entering the public domain ... no exclusions
        # shall apply" -- must FLAG even though no "includes public" phrasing.
        text = (
            "\"Confidential Information\" means information disclosed by the "
            "Disclosing Party. For the avoidance of doubt, information shall not "
            "cease to be Confidential Information by reason of it entering the "
            "public domain, becoming generally known, or having been "
            "independently developed, and no exclusions of any kind shall apply."
        )
        result = detect_definition_poison(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_CI_POISON)

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

    def test_affiliate_sweeps_competitors_flags(self):
        # tp04: "Affiliate" sweeps in competitors / same-industry entities, and a
        # non-solicit restraint relies on the term -> flag.
        text = (
            "\"Affiliate\" means any actual or potential competitor of the "
            "Disclosing Party and any entity in the same industry, whether or not "
            "under common control. During the term and for two years thereafter, "
            "the Receiving Party shall not solicit or do business with any of its "
            "Affiliates."
        )
        result = detect_definition_poison(_matter(text))
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_AFFILIATE_POISON)

    def test_overbroad_affiliate_without_restraint_is_silent(self):
        # Over-broad definition but nothing leans on it -> precision: silent.
        text = (
            "\"Representative\" means any third party whatsoever that a Party may "
            "designate. The Parties acknowledge their mutual interest in the "
            "Purpose."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    def test_disclaimed_restraint_does_not_trigger_affiliate_poison(self):
        # fp09-shape: a broad-ish Affiliate definition used only for permitted
        # disclosure, with an explicit "there are no non-compete or
        # non-solicitation obligations" -> must stay silent.
        text = (
            "\"Affiliate\" means any entity under common control with a party and "
            "any joint-venture partner of the party. Each party may share "
            "Confidential Information with its Affiliates who need it for the "
            "Purpose. There are no non-compete or non-solicitation obligations in "
            "this Agreement."
        )
        self.assertIsNone(detect_definition_poison(_matter(text)))

    # ---- CI-poison SEVERITY (fail vs review vs silent) ------------------
    def test_severity_affirmative_inclusion_without_carveout_is_fail(self):
        # AFFIRMATIVE poison + NO surviving carve-out -> hard FAIL tier.
        text = (
            "\"Confidential Information\" means all information disclosed by a "
            "Party and includes information that is publicly available or already "
            "known to the Receiving Party or independently developed by the "
            "Receiving Party."
        )
        self.assertEqual(ci_poison_severity(text), "fail")

    def test_severity_obfuscated_negation_poison_is_fail(self):
        # The negated-form poison (tp07) with no surviving carve-out also FAILs.
        text = (
            "\"Confidential Information\" means information disclosed by the "
            "Disclosing Party. Information shall not cease to be Confidential "
            "Information by reason of it entering the public domain, becoming "
            "generally known, or having been independently developed, and no "
            "exclusions of any kind shall apply."
        )
        self.assertEqual(ci_poison_severity(text), "fail")

    def test_severity_proper_carveouts_is_silent(self):
        # A definition with the standard exclusion block is not poison at all.
        self.assertIsNone(ci_poison_severity(UTSA_CI_DEFINITION))

    def test_severity_merely_narrow_definition_is_silent(self):
        # A narrow definition that does NOT affirmatively include public info is not
        # poison -> severity None (never over-fails).
        text = (
            "\"Confidential Information\" means any non-public information "
            "disclosed by one party to the other that is designated as confidential."
        )
        self.assertIsNone(ci_poison_severity(text))

    def test_severity_poison_with_surviving_carveout_stays_review(self):
        # Affirmative inclusion AND a surviving exclusion block (a self-contradiction)
        # -> kept at REVIEW for a human, never FAILed. Conservative anti-over-fail.
        text = (
            "\"Confidential Information\" means all information disclosed by a "
            "Party and includes information that is publicly available. However, "
            "Confidential Information shall not include information that is publicly "
            "available or independently developed by the Receiving Party."
        )
        self.assertEqual(ci_poison_severity(text), "review")

    def test_severity_garbage_returns_none(self):
        for bad in ("", "asdf \x00 random ;;;; ....", "non-confidential text here"):
            self.assertIsNone(ci_poison_severity(bad))

    # ---- Adversarial-benign: INLINE carve-outs with ORDINARY connectives --
    # REGRESSION GUARD (over-fail). A clean NDA whose carve-out uses an ordinary
    # connective ("save for" / "aside from" / "to the exclusion of" / ...) INLINE in
    # the same sentence as a broad/public-touching inclusion must NOT be FAILed: the
    # connective is a surviving carve-out. Over-failing a clean NDA is the dangerous
    # direction; these all FAILed before the carve-out widening.
    def test_severity_inline_carveout_connectives_never_fail(self):
        templates = [
            "save for information that is publicly available or already known",
            "saving and excepting information that is publicly available",
            "aside from information already known to the Receiving Party",
            "apart from information that is publicly available",
            "to the exclusion of information independently developed",
            "unless and until it becomes publicly available through no fault",
            "less any information already known to the Receiving Party",
            "with the sole exception of information in the public domain",
            "except for information independently developed",
            "other than information that is publicly available",
            "excluding information that is in the public domain",
        ]
        for tail in templates:
            text = (
                "\"Confidential Information\" means all information disclosed and "
                "includes information that is publicly available, " + tail + "."
            )
            self.assertNotEqual(
                ci_poison_severity(text), "fail",
                f"inline carve-out '{tail}' must not be FAILed",
            )

    def test_severity_realistic_8category_inline_carveout_not_fail(self):
        # The exact realistic shape the gate flagged: a broad 8-category definition
        # with an inline "save for ... publicly available" carve-out.
        text = (
            "Confidential Information means all business, financial, technical, "
            "customer, supplier, pricing, market and trade secret information "
            "disclosed by either party, including information that may be publicly "
            "available, save for information that is publicly available through no "
            "fault of the Receiving Party or was already known to it."
        )
        self.assertNotEqual(ci_poison_severity(text), "fail")

    def test_severity_bare_public_non_category_does_not_trigger(self):
        # PRECISION: a bare "public" that is NOT the excluded category ("public
        # interest" / "public company" / "publicly traded") must not be read as the
        # public-information carve-out category -> no poison, never FAIL.
        text = (
            "\"Confidential Information\" means all information disclosed and "
            "includes information disclosed in the public interest or by a public "
            "company or that is publicly traded on a recognized exchange."
        )
        self.assertIsNone(ci_poison_severity(text))

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

    def test_wrong_type_extracted_text_returns_none(self):
        # REGRESSION: a non-string extracted_text must NOT be str()-coerced (its
        # repr could otherwise trip a finding) -- treated as no text -> None.
        for bad in ({"nested": "dict"}, ["a", "list"], 123, 4.5, True):
            self.assertIsNone(detect_definition_poison(_matter(bad)))


if __name__ == "__main__":
    unittest.main()
