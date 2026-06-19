"""Regression guard for two related perpetual/indefinite-term detector defects.

DEFECT A (NB-W1b class) -- a TRAILING carve-out idiom laundered an ordinary-CI
perpetual rider to PASS. ``_carve_out_governs_term_sentence`` (the
``if not carve_out_matches:`` branch) and ``_is_benign_indefinite_match``'s POLARITY
block both demoted a hit WITHOUT inspecting the text BEFORE the trigger for an
ordinary-confidentiality subject -- unlike the sibling carve-out-present branch,
which already guards via ``_ordinary_ci_subject_present``. So:

  * "The confidential information shall remain confidential indefinitely to comply
    with applicable law." -> the trailing "...to comply with applicable law" carve-out
    idiom laundered an ORDINARY-CI perpetual rider to PASS (must now FAIL), and
  * "The confidential information shall remain perpetually available." -> the POLARITY
    block demoted on the trailing non-survival object "available" while ordinary CI
    ("the confidential information ... perpetually") was the SUBJECT (must now FAIL).

DEFECT B (vocab gap) -- common perpetual phrasings (forever, everlasting, no
expiration, unlimited period, without limitation of time, ...) were absent from the
``indefinite_terms`` vocabulary, so an everlasting rider was reported as MISSING
rather than TOO-LONG.

CRITICAL precision (the forever-rework lesson): a scoped NON-CI carve-out (personal
data, trade secrets) held longer / in perpetuity, and genuine perpetual-license
polarity cases, must STILL PASS. These tests lock both directions and drive the
deterministic checker (``verify=False, ai_enabled=False``) so they are the
production path, not a unit-level shortcut.
"""

from __future__ import annotations

import unittest

from nda_automation.checker import review_nda


def _term_clause(text: str) -> dict:
    result = review_nda(text, verify=False, ai_enabled=False)
    return next(
        clause for clause in result["clauses"] if clause["id"] == "term_and_survival"
    )


def _is_indefinite_flag(clause: dict) -> bool:
    reason = str(clause.get("reason") or clause.get("finding") or "").lower()
    return "indefinite" in reason or "perpetual" in reason


class DefectACarveOutLaunderTests(unittest.TestCase):
    """Trailing carve-out idiom / polarity object must NOT launder an ordinary-CI
    perpetual rider. Each of these was PASS/benign before the fix and must now FLAG."""

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

    def test_indefinitely_to_comply_with_applicable_law(self) -> None:
        # Trailing "to comply with applicable law" carve-out idiom over ordinary CI.
        self.assertOrdinaryCiPerpetualFails(
            "The confidential information shall remain confidential indefinitely to "
            "comply with applicable law."
        )

    def test_perpetuity_as_required_by_applicable_law(self) -> None:
        self.assertOrdinaryCiPerpetualFails(
            "All confidentiality obligations shall continue in perpetuity as required "
            "by applicable law."
        )

    def test_perpetually_available_polarity_object(self) -> None:
        # POLARITY path: "available" is a non-survival object, but ordinary CI is the
        # SUBJECT ("the confidential information ... perpetually"), so it must FAIL.
        self.assertOrdinaryCiPerpetualFails(
            "The confidential information shall remain perpetually available."
        )

    def test_in_perpetuity_royalty_free(self) -> None:
        self.assertOrdinaryCiPerpetualFails(
            "Receiving party shall hold the confidential information in perpetuity, "
            "royalty free."
        )


class DefectBVocabGapTests(unittest.TestCase):
    """Common perpetual phrasings must be caught as TOO-LONG (indefinite), not MISSING."""

    def assertIndefiniteTooLong(self, text: str) -> dict:
        clause = _term_clause(text)
        self.assertFalse(
            clause.get("passes"),
            f"Perpetual phrasing was not flagged: {clause.get('reason')!r}",
        )
        self.assertTrue(
            _is_indefinite_flag(clause),
            f"Expected an indefinite/perpetual (too-long) flag, got: {clause.get('reason')!r}",
        )
        # Must be reported as too-long, NOT as missing.
        issue = str(clause.get("issue_type") or "").lower()
        self.assertNotEqual(
            issue,
            "missing",
            f"Perpetual phrasing wrongly reported as MISSING: {clause.get('reason')!r}",
        )
        return clause

    def test_everlasting(self) -> None:
        self.assertIndefiniteTooLong(
            "The confidentiality obligations are everlasting."
        )

    def test_forever(self) -> None:
        self.assertIndefiniteTooLong(
            "The confidentiality obligations shall remain confidential forever."
        )

    def test_without_limitation_of_time(self) -> None:
        self.assertIndefiniteTooLong(
            "The confidentiality obligations shall continue without limitation of time."
        )

    def test_unlimited_period(self) -> None:
        self.assertIndefiniteTooLong(
            "The confidentiality obligations shall be maintained for an unlimited period."
        )

    def test_no_expiration_date(self) -> None:
        self.assertIndefiniteTooLong(
            "The confidentiality obligations have no expiration date."
        )


class PrecisionMustStillPassTests(unittest.TestCase):
    """DO-NOT-OVER-CORRECT: legitimate scoped non-CI carve-outs and genuine polarity
    cases must STILL PASS. A regression here re-opens the forever-rework wound."""

    def assertPasses(self, text: str) -> dict:
        clause = _term_clause(text)
        self.assertTrue(
            clause.get("passes"),
            f"Legitimate clause was wrongly flagged: "
            f"{clause.get('status')!r} / {clause.get('reason')!r}",
        )
        return clause

    def test_personal_data_retained_as_required_by_law(self) -> None:
        # Scoped NON-CI (personal data) carve-out, capped ordinary CI -> PASS.
        self.assertPasses(
            "The confidentiality obligations survive for five (5) years. Personal "
            "data shall be retained for as long as required by applicable law."
        )

    def test_trade_secret_perpetuity_carve_out(self) -> None:
        self.assertPasses(
            "The confidentiality obligations survive five (5) years; with respect to "
            "trade secrets, in perpetuity."
        )

    def test_genuine_perpetual_license_polarity(self) -> None:
        self.assertPasses(
            "The Disclosing Party grants the Receiving Party a perpetual license to "
            "use the platform. The confidentiality obligations survive for five (5) "
            "years after termination."
        )

    def test_capped_duration_connector(self) -> None:
        self.assertPasses(
            "This Agreement shall continue in effect for so long as the parties have "
            "a business relationship. The confidentiality obligations survive for "
            "three (3) years after termination."
        )


if __name__ == "__main__":
    unittest.main()
