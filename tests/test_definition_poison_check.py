"""Tests for the definition-poison detector helpers (precision-tuned, fail-safe).

The review-OVERLAY entry point (``detect_definition_poison``) was RETIRED together
with the other structural-override overlays. The module is kept because the
deterministic CI-clause checker consumes ``ci_poison_severity``; these tests cover
``ci_poison_severity`` and the ``detect_ci_poison`` / ``detect_affiliate_poison``
helpers it builds on, which remain fully intact.
"""
import unittest

from nda_automation.definition_poison_check import (
    REASON_CODE_AFFILIATE_POISON,
    REASON_CODE_CI_POISON,
    ci_poison_severity,
    detect_affiliate_poison,
    detect_ci_poison,
)


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
        result = detect_ci_poison(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_CI_POISON)
        self.assertTrue(result["message"])

    def test_ci_definition_shall_include_already_known_flags(self):
        text = (
            "The term \"Confidential Information\" shall include, without "
            "limitation, information that is already known to the public or in "
            "the public domain at the time of disclosure."
        )
        result = detect_ci_poison(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_CI_POISON)

    # ---- CI-definition poison: SILENT (the key precision case) -----------
    def test_normal_utsa_ci_definition_is_silent(self):
        # A standard UTSA definition + standard exclusions must NOT flag.
        self.assertIsNone(detect_ci_poison(UTSA_CI_DEFINITION))

    def test_standard_exclusion_block_is_silent(self):
        # The exclusion carve-out names the very categories the poison detector
        # cares about, but with "shall not include" framing -> safe.
        text = (
            "Confidential Information does not include information that is "
            "publicly available, already known to the Receiving Party, or "
            "independently developed by the Receiving Party."
        )
        self.assertIsNone(detect_ci_poison(text))

    def test_non_public_ci_definition_is_silent(self):
        # REGRESSION: the bare substring "public" inside "non-public" must NOT be
        # read as the excluded category. A standard narrow CI scope stays silent.
        text = (
            "\"Confidential Information\" means any non-public information "
            "disclosed by one party to the other that is designated as "
            "confidential. The Receiving Party shall not disclose it."
        )
        self.assertIsNone(detect_ci_poison(text))

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
        self.assertIsNone(detect_ci_poison(text))

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
        self.assertIsNone(detect_ci_poison(text))

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
        result = detect_ci_poison(text)
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
        result = detect_affiliate_poison(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_AFFILIATE_POISON)
        self.assertTrue(result["message"])

    # ---- Affiliate poison: SILENT ---------------------------------------
    def test_normal_affiliate_definition_is_silent(self):
        self.assertIsNone(detect_affiliate_poison(NORMAL_AFFILIATE_DEFINITION))

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
        result = detect_affiliate_poison(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason_code"], REASON_CODE_AFFILIATE_POISON)

    def test_overbroad_affiliate_without_restraint_is_silent(self):
        # Over-broad definition but nothing leans on it -> precision: silent.
        text = (
            "\"Representative\" means any third party whatsoever that a Party may "
            "designate. The Parties acknowledge their mutual interest in the "
            "Purpose."
        )
        self.assertIsNone(detect_affiliate_poison(text))

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
        self.assertIsNone(detect_affiliate_poison(text))

    # ---- CI-poison SEVERITY (STRUCTURAL, polarity-correct) --------------
    #
    # This is the path that REMAINS LOAD-BEARING after the overlay retirement: the
    # deterministic CI-clause checker (checks/confidential_information.py) fails a CI
    # definition only when ``ci_poison_severity`` returns "fail".
    #
    # The severity logic is "innocent until proven guilty": it reaches the FAIL tier
    # ONLY when affirmative inclusion of an excluded category is present AND there is
    # GENUINELY ZERO exclusion signal of ANY kind anywhere in the definition. If ANY
    # plausible carve-out signal is present, it caps at REVIEW, never FAIL. These
    # tests are STRUCTURAL (one shape per principle), not a connective allowlist --
    # the over-fail trap was an allowlist that biased toward guilty.

    # GROUP A: clean inline carve-outs with DIVERSE connectives must NEVER fail. This
    # is the over-fail trap the gate blocked on; the structural rule is "any exclusion
    # signal -> not fail", verified across a wide span of lawyer idioms.
    def test_groupA_clean_inline_carveouts_never_fail(self):
        carveouts = [
            "but excluding information that is publicly available",
            "not including information already in the public domain",
            "excluding however any information that is publicly known",
            "with the carve-out that publicly available information is not covered",
            "subject to the exclusion of information in the public domain",
            "other than information that is publicly available",
            "save where such information is in the public domain",
            "except where the information is publicly available",
            "unless such information is publicly known at disclosure",
            "provided however that publicly available information is not covered",
            "minus any information in the public domain",
            "barring information that is publicly available",
            "setting aside information already in the public domain",
            "save only information that is publicly available",
            "less any information already known to the Receiving Party",
            "aside from information already known to the Receiving Party",
            "apart from information that is publicly available",
            "to the exclusion of information independently developed",
            "save and excepting information that is in the public domain",
            "excepting any information already known to the Receiving Party",
            "exclusive of any publicly available information",
            "to the extent not already in the public domain",
            "but not information that is publicly available",
        ]
        for tail in carveouts:
            text = (
                "Confidential Information includes all information disclosed, "
                + tail + "."
            )
            self.assertNotEqual(
                ci_poison_severity(text), "fail",
                f"clean inline carve-out '{tail}' must NOT be FAILed",
            )

    def test_groupA_em_dash_and_parenthetical_carveouts_never_fail(self):
        for text in (
            "Confidential Information includes all disclosed materials -- excluding "
            "publicly available information -- of the Disclosing Party.",
            "Confidential Information includes all disclosed materials (other than "
            "information that is publicly available).",
        ):
            self.assertNotEqual(ci_poison_severity(text), "fail", text)

    # GROUP B: genuine poison -- affirmative inclusion of an excluded category with NO
    # exclusion signal anywhere -> FAIL (the rare, high-confidence path).
    def test_groupB_genuine_poison_no_carveout_anywhere_fails(self):
        for text in (
            "Confidential Information includes all information disclosed, including "
            "information that is publicly available.",
            "Confidential Information shall include any information that is generally "
            "known to the public.",
            "Confidential Information encompasses all materials, including those in "
            "the public domain.",
            "Confidential Information shall be deemed to include information "
            "independently developed by the Receiving Party.",
            "Confidential Information includes information already known to the "
            "Receiving Party.",
            "Confidential Information extends to public knowledge concerning the "
            "Disclosing Party.",
        ):
            self.assertEqual(ci_poison_severity(text), "fail", text)

    def test_groupB_obfuscated_negation_poison_fails(self):
        # tp07: a frame that OVERRIDES the carve-outs ("no exclusions shall apply" /
        # "shall not cease to be Confidential Information") is the INVERSE of a carve-
        # out and must FAIL even though it carries "no"/"not" negation tokens.
        for text in (
            "Information shall not cease to be Confidential Information by reason of "
            "entering the public domain, and no exclusions of any kind shall apply.",
            "Such information remains Confidential Information notwithstanding that it "
            "is publicly available.",
        ):
            self.assertEqual(ci_poison_severity(text), "fail", text)

    # GROUP C1: boundary cases -> exact expected severity.
    def test_groupC1_boundary_severities(self):
        cases = [
            # merely-narrow / no public category / UTSA polarity -> None (not poison).
            ("Confidential Information means any proprietary data disclosed by the "
             "Disclosing Party.", None),
            (UTSA_CI_DEFINITION, None),
            ("Confidential Information means all non-public information disclosed by "
             "the Disclosing Party.", None),
            # non-category "public" -> None.
            ("Confidential Information includes all data shared with any public "
             "company affiliate of the Disclosing Party.", None),
            ("Confidential Information includes information disclosed in the public "
             "interest by the Disclosing Party.", None),
            # self-contradiction: affirmative inclusion + SEPARATE real carve-out
            # sentence -> review (never fail).
            ("Confidential Information includes information that is publicly available."
             " Confidential Information shall not include information that is publicly "
             "available through no fault of the Receiving Party.", "review"),
        ]
        for text, expected in cases:
            self.assertEqual(ci_poison_severity(text), expected, text)

    # GROUP C2: a FAKE carve-out word that does not truly carve out the public
    # category. ACCEPTED tradeoff: this caps at REVIEW (the safe under-fail
    # direction), never silences (None) and never over-fails for a clean doc.
    def test_groupC2_fake_carveout_caps_at_review_not_silent(self):
        for text in (
            "Confidential Information includes information that is publicly available, "
            "except for the company logo.",
            "Confidential Information includes information that is publicly available, "
            "excluding the parties' names.",
            "Confidential Information includes publicly available information, other "
            "than the office address.",
        ):
            self.assertEqual(ci_poison_severity(text), "review", text)

    def test_severity_garbage_returns_none(self):
        for bad in ("", "asdf \x00 random ;;;; ....", "non-confidential text here"):
            self.assertIsNone(ci_poison_severity(bad))

    # ---- Fail-safe: garbage / empty -> None, no crash --------------------
    def test_empty_text_helpers_return_none(self):
        self.assertIsNone(detect_ci_poison(""))
        self.assertIsNone(detect_affiliate_poison(""))

    def test_garbage_text_returns_none(self):
        garbage = "asdf \x00\x01 ||| 99999 \n\n\t ;;;; .... random tokens"
        self.assertIsNone(detect_ci_poison(garbage))
        self.assertIsNone(detect_affiliate_poison(garbage))


if __name__ == "__main__":
    unittest.main()
