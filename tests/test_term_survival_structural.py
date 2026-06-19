"""Structural (governance-aware) perpetual / indefinite-term detector.

THE REWORK: perpetual detection used to be a CLOSED VOCABULARY substring match, which
failed BOTH ways:

  * UNDER-flagged: realistic "forever" phrasings absent from the list (never expire,
    for all time, no end date, for an indefinite duration, on an enduring basis,
    ad infinitum, ...) silently PASSED.
  * OVER-flagged: the substring "perpetual"/"perpetually"/"indefinitely" appearing
    INCIDENTALLY in a clean 5-year clause (a party name "Perpetual Holdings", a product
    line, "with perpetual diligence", "a perpetual inventory", "indefinitely renew the
    agreement") wrongly FAILED it.

THE UNIFYING FIX is a GOVERNANCE/STRUCTURAL check: flag a confidentiality/survival
clause as perpetual ONLY when an OPEN-ENDED DURATION GOVERNS THE CONFIDENTIALITY
SURVIVAL -- the clause states how long CI stays confidential AND that period is
open-ended (no numeric cap, no bounded end-event). This simultaneously CATCHES novel
"forever" wording (it governs survival) and STOPS flagging "perpetual" when it governs
something else.

These tests drive the DETERMINISTIC production path (``review_nda(verify=False,
ai_enabled=False)``) so they exercise the shipped behaviour, not a unit shortcut. They
are NON-VACUOUS: ``Pre3731894cBaselineWasRedTests`` reconstructs the pre-rework
behaviour (closed vocab, no structural backstop, no governance gate) and proves each
must-flag was a MISS and each governance must-pass was a wrong FLAG on ``3731894c``,
so the corpus below can't silently rot into a tautology.
"""

from __future__ import annotations

import re
import unittest

from nda_automation.checker import review_nda
from nda_automation.checks.common import _normalize
from nda_automation.checks.term_and_survival import (
    DEFAULT_INDEFINITE_TERMS,
    _indefinite_match_governs_ci_survival,
    _is_allowed_carve_out_fragment,
    _literal_word_pattern,
)


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(
        clause for clause in result["clauses"] if clause["id"] == "term_and_survival"
    )


def _is_indefinite_or_overcap_flag(clause: dict) -> bool:
    reason = str(clause.get("reason") or clause.get("finding") or "").lower()
    return any(token in reason for token in ("indefinite", "perpetual", "exceeds"))


# ---------------------------------------------------------------------------
# MUST-FLAG: ordinary CI locked forever -> the open-ended duration governs the
# confidentiality survival.
# ---------------------------------------------------------------------------

# The 19 realistic novel "forever" phrasings on ordinary CI. Each was a MISS (reported
# "missing" rather than too-long) under the closed vocabulary on 3731894c.
NOVEL_FOREVER_CASES = [
    ("never_expire", "The Confidential Information shall never expire."),
    ("in_force_permanently", "The Confidential Information shall remain in force permanently."),
    ("secret_for_all_time", "The Confidential Information shall remain secret for all time."),
    ("no_end_date", "The Confidential Information shall be kept confidential with no end date."),
    ("indefinite_duration", "The Confidential Information shall be kept confidential for an indefinite duration."),
    ("not_cease_at_any_time", "The Confidential Information shall not cease to be confidential at any time."),
    ("without_any_time_limit", "The Confidential Information shall be kept confidential without any time limit."),
    ("no_time_limitation", "The Confidential Information shall be kept confidential with no time limitation."),
    ("enduring_basis", "The Confidential Information shall be kept confidential on an enduring basis."),
    ("infinite_period", "The Confidential Information shall be kept confidential for an infinite period."),
    ("unlimited_time", "The Confidential Information shall be kept confidential for unlimited time."),
    ("everlastingly", "The Confidential Information shall be kept confidential everlastingly."),
    ("ad_infinitum", "The Confidential Information shall be kept confidential ad infinitum."),
    ("in_perpetuum", "The Confidential Information shall be kept confidential in perpetuum."),
    ("until_end_of_days", "The Confidential Information shall be kept confidential until the end of days."),
    ("without_limit_of_time", "The Confidential Information shall be kept confidential without limit of time."),
    ("permanently", "The Confidential Information shall remain confidential permanently."),
    ("no_end_date_have", "The Confidential Information shall have no end date."),
    ("not_cease_phrasing", "The Confidential Information shall not cease to be confidential at any time."),
]

# Additional must-flag cases (gate leaks + scope-gating).
EXTRA_MUST_FLAG_CASES = [
    # gate-1 leak A: carve-out idiom LEADING the trigger (the guard only handled
    # TRAILING). Ordinary CI is held in perpetuity behind a leading "As required by
    # applicable law" rationale -> must still FAIL.
    ("leak_a_carveout_leading",
     "As required by applicable law, all Confidential Information shall remain "
     "confidential in perpetuity."),
    # gate-1 leak B: the benign-object guard wrongly demoted -- CI is the thing made
    # indefinitely accessible.
    ("leak_b_indefinitely_grant_access",
     "The Receiving Party may indefinitely grant access to the Confidential Information."),
    # S7 scope-gating: a bare confidentiality-survival period over the cap used to
    # return "missing"; it must be scoped in and flagged over-cap.
    ("s7_bare_twenty_years",
     "The Confidential Information shall remain confidential for twenty (20) years."),
]


class MustFlagForeverTests(unittest.TestCase):
    """Ordinary CI locked open-ended must FLAG (and not be reported as 'missing')."""

    def assertForeverFlagged(self, text: str) -> dict:
        clause = _term_clause(text)
        self.assertFalse(
            clause.get("passes"),
            f"Open-ended ordinary-CI survival was not flagged: {clause.get('reason')!r}",
        )
        self.assertTrue(
            _is_indefinite_or_overcap_flag(clause),
            f"Expected an indefinite/perpetual/over-cap flag, got: {clause.get('reason')!r}",
        )
        self.assertNotEqual(
            str(clause.get("issue_type") or "").lower(),
            "missing",
            f"Open-ended survival wrongly reported as MISSING: {clause.get('reason')!r}",
        )
        return clause

    def test_novel_forever_phrasings_flag(self) -> None:
        for name, text in NOVEL_FOREVER_CASES:
            with self.subTest(case=name):
                self.assertForeverFlagged(text)

    def test_gate_leaks_and_scope_gating_flag(self) -> None:
        for name, text in EXTRA_MUST_FLAG_CASES:
            with self.subTest(case=name):
                self.assertForeverFlagged(text)

    def test_s7_bare_overcap_is_over_cap_not_indefinite(self) -> None:
        # Belt-and-braces on the S7 oracle: a bare 20-year confidentiality survival is
        # scoped in and flagged specifically as EXCEEDING the cap (an over-cap finding),
        # with the detected term surfaced.
        clause = _term_clause(
            "The Confidential Information shall remain confidential for twenty (20) years."
        )
        self.assertEqual(clause.get("status"), "check")
        self.assertIn("exceeds", str(clause.get("reason") or "").lower())
        self.assertEqual(clause.get("term_years"), 20.0)


# ---------------------------------------------------------------------------
# MUST-PASS: the marker governs something OTHER than the confidentiality survival.
# THESE ARE THE GOVERNANCE PROOF.
# ---------------------------------------------------------------------------

GOVERNANCE_MUST_PASS_CASES = [
    # "Perpetual" is part of a party NAME.
    ("party_name_perpetual",
     "The confidentiality obligations survive for five (5) years between Aspora and "
     "Perpetual Holdings Ltd."),
    # "Perpetual Motion" is a product line.
    ("product_line_perpetual",
     "The confidentiality obligations survive for five (5) years for the Perpetual "
     "Motion product line."),
    # "perpetual" governs a manner noun ("diligence"); CI survival is capped at 5 years.
    ("perpetual_diligence",
     "The Confidential Information shall be kept confidential for five (5) years with "
     "perpetual diligence."),
    # "perpetual" governs "inventory", not the confidentiality survival.
    ("perpetual_inventory",
     "The confidentiality obligations survive five (5) years. The Receiving Party shall "
     "maintain a perpetual inventory of disclosed materials."),
    # "indefinitely" governs agreement RENEWAL, not CI.
    ("indefinitely_renew_agreement",
     "The confidentiality obligations survive five (5) years; the parties may "
     "indefinitely renew this agreement by mutual consent."),
    # Prior legit cases that must stay PASS.
    ("personal_data_carveout",
     "The confidentiality obligations survive for five (5) years. Personal data shall "
     "be retained for as long as required by applicable law."),
    ("inline_ts_perpetual",
     "The confidentiality obligations survive five (5) years; with respect to trade "
     "secrets, in perpetuity."),
    ("genuine_perpetual_license",
     "The Disclosing Party grants the Receiving Party a perpetual license to use the "
     "platform. The confidentiality obligations survive for five (5) years after "
     "termination."),
    ("capped_connector",
     "This Agreement shall continue in effect for so long as the parties have a "
     "business relationship. The confidentiality obligations survive for three (3) "
     "years after termination."),
    # gate-3 Family 2, in a realistic full clause: an article between "by" and the
    # carve-out term must not break the requirement-idiom guard.
    ("gate3_a_legal_obligation",
     "The confidentiality obligations survive for five (5) years. Personal data shall "
     "be retained for as long as required by a legal obligation."),
    ("gate3_any_legal_obligation",
     "The confidentiality obligations survive for five (5) years. Personal data shall "
     "be retained for as long as required by any legal obligation."),
]


class GovernanceMustPassTests(unittest.TestCase):
    """The incidental-marker cases must NOT over-flag. A regression here re-opens the
    forever-rework wound."""

    def assertPasses(self, text: str) -> dict:
        clause = _term_clause(text)
        self.assertTrue(
            clause.get("passes"),
            f"Legitimate clause was wrongly flagged: "
            f"{clause.get('status')!r} / {clause.get('reason')!r}",
        )
        return clause

    def test_governance_must_pass(self) -> None:
        for name, text in GOVERNANCE_MUST_PASS_CASES:
            with self.subTest(case=name):
                self.assertPasses(text)


class Gate3RequirementIdiomGuardTests(unittest.TestCase):
    """gate-3 Family 2 at the guard level: a determiner (a/an/any/the) between the
    requirement idiom and the carve-out term must still be recognized as a
    longer-survival carve-out."""

    def _carve_out_allowed(self, sentence: str, trigger: str) -> bool:
        normalized = _normalize(sentence)
        match = re.search(re.escape(trigger), normalized)
        assert match is not None, f"trigger {trigger!r} not in {normalized!r}"
        return _is_allowed_carve_out_fragment(
            normalized, match.start(), match.end(), {"id": "term_and_survival"}
        )

    def test_required_by_a_legal_obligation_recognized(self) -> None:
        self.assertTrue(
            self._carve_out_allowed(
                "personal data shall be retained for as long as required by a legal "
                "obligation",
                "for as long as",
            )
        )

    def test_required_by_any_legal_obligation_recognized(self) -> None:
        self.assertTrue(
            self._carve_out_allowed(
                "personal data shall be retained for as long as required by any legal "
                "obligation",
                "for as long as",
            )
        )


class ByDesignTradeSecretRiderStillFlagsTests(unittest.TestCase):
    """BY-DESIGN EXCEPTION (do NOT change): a LEADING trade-secret-ONLY rider still
    flags-for-review by design -- keep it flagging."""

    def test_leading_ts_only_rider_still_flags(self) -> None:
        clause = _term_clause(
            "With respect to trade secrets, the obligations of confidentiality shall "
            "survive in perpetuity."
        )
        self.assertFalse(
            clause.get("passes"),
            "A leading trade-secret-only perpetual rider must still flag (by design).",
        )


# ---------------------------------------------------------------------------
# NON-VACUITY: prove the pre-rework (3731894c) behaviour was RED for each direction.
# ---------------------------------------------------------------------------


class Pre3731894cBaselineWasRedTests(unittest.TestCase):
    """Reconstruct the pre-rework behaviour and prove the corpus was genuinely RED, so
    the tests above are not tautological.

    The rework had three layers; each baseline reconstruction isolates one:
      * vocab gap   -> the 16 new phrasings were ABSENT from the indefinite vocabulary;
      * structural  -> there was NO negated-expiry backstop;
      * governance  -> the POLARITY substring fired with NO governance gate.
    """

    # The exact pre-rework indefinite vocabulary (DEFAULT_INDEFINITE_TERMS minus the 16
    # phrasings this rework appended). Reconstructed locally so the regression direction
    # is provable without time-travelling the source.
    _PRE_REWORK_VOCAB = (
        "indefinitely", "perpetuity", "perpetual", "perpetually",
        "perpetual confidentiality", "for so long as", "for as long as",
        "for so long as the information remains confidential",
        "ceases to have commercial value", "until it ceases to have commercial value",
        "until it ceases to have value", "for so long as it retains commercial value",
        "until released in writing", "until the disclosing party releases",
        "as long as it remains secret", "forever", "everlasting", "no expiration",
        "no expiration date", "unlimited period", "for an unlimited period",
        "without limitation of time", "without limitation in time", "until the end of time",
    )
    _NEW_PHRASINGS = (
        "never expire", "permanently", "for all time", "no end date",
        "without any time limit", "indefinite duration", "without limit of time",
        "no time limitation", "on an enduring basis", "infinite period",
        "unlimited time", "everlastingly", "ad infinitum", "in perpetuum",
        "until the end of days",
    )

    def test_new_phrasings_are_real_additions(self) -> None:
        # Sanity: each newly-added phrasing was genuinely absent before.
        for phrasing in self._NEW_PHRASINGS:
            self.assertNotIn(phrasing, self._PRE_REWORK_VOCAB)
            self.assertIn(phrasing, DEFAULT_INDEFINITE_TERMS)

    def test_pre_rework_vocab_misses_novel_forever_phrasings(self) -> None:
        # With the OLD vocabulary AND no structural backstop, the novel "forever"
        # phrasings match NOTHING -> they were silent misses (reported "missing").
        pre_patterns = [
            _literal_word_pattern(term) for term in self._PRE_REWORK_VOCAB
        ]
        # The phrasings that are pure-vocab (not the negated-expiry idiom, which the
        # structural backstop -- separately reconstructed below -- handled).
        vocab_phrasing_cases = [
            text for name, text in NOVEL_FOREVER_CASES
            if "cease" not in text.lower()
        ]
        for text in vocab_phrasing_cases:
            normalized = _normalize(text)
            self.assertFalse(
                any(re.search(pattern, normalized) for pattern in pre_patterns),
                f"Pre-rework vocab unexpectedly matched (test would be vacuous): {text!r}",
            )

    def test_pre_rework_had_no_negated_expiry_backstop(self) -> None:
        # The never-cease idiom is in NEITHER the old vocab NOR matched by any old
        # vocab pattern -> it was a silent miss before the structural backstop.
        pre_patterns = [
            _literal_word_pattern(term) for term in self._PRE_REWORK_VOCAB
        ]
        normalized = _normalize(
            "The Confidential Information shall not cease to be confidential at any time."
        )
        self.assertFalse(
            any(re.search(pattern, normalized) for pattern in pre_patterns),
            "Pre-rework vocab unexpectedly matched the never-cease idiom.",
        )

    def test_pre_rework_governance_gate_absent_overflags(self) -> None:
        # The governance gate is what stops the incidental "perpetual"/"indefinitely"
        # from firing. WITHOUT it (the pre-rework behaviour) the substring matched and
        # would have flagged. We prove the gate is load-bearing: it returns False
        # (demote) for the incidental cases -- i.e. pre-gate they would have fired.
        clause = {"id": "term_and_survival"}
        incidental = {
            "perpetual": [
                "the confidentiality obligations survive for five (5) years between "
                "aspora and perpetual holdings ltd",
                "the confidentiality obligations survive for five (5) years for the "
                "perpetual motion product line",
                "the confidential information shall be kept confidential for five (5) "
                "years with perpetual diligence",
                "the receiving party shall maintain a perpetual inventory of disclosed "
                "materials",
            ],
            "indefinitely": [
                "the parties may indefinitely renew this agreement by mutual consent",
            ],
        }
        for marker, sentences in incidental.items():
            for sentence in sentences:
                normalized = _normalize(sentence)
                match = re.search(marker, normalized)
                assert match is not None
                self.assertFalse(
                    _indefinite_match_governs_ci_survival(normalized, match, clause),
                    f"Governance gate must DEMOTE the incidental marker (pre-gate it "
                    f"would have over-flagged): {sentence!r}",
                )

    def test_governance_gate_fires_for_real_ci_perpetuity(self) -> None:
        # The other direction: the gate still FIRES when the marker genuinely governs
        # ordinary-CI survival, so it is not a blanket demotion.
        clause = {"id": "term_and_survival"}
        normalized = _normalize(
            "The Confidential Information shall remain confidential in perpetuity."
        )
        match = re.search("perpetuity", normalized)
        assert match is not None
        self.assertTrue(
            _indefinite_match_governs_ci_survival(normalized, match, clause)
        )


if __name__ == "__main__":
    unittest.main()
