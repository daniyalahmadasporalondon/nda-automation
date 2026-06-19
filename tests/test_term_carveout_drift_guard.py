"""Single-source-of-truth guard for the term/survival carve-out vocabularies.

THE DRIFT (fixed here): ``term_and_survival.py`` keeps an in-code fallback copy of
the playbook's ``longer_survival_carve_out_terms`` for degraded/legacy clause
configs that arrive without the field (e.g. a user-edited playbook that drops the
optional field -- it is ``required=False`` in the policy schema, so this is
reachable in prod, not just in tests). That fallback had drifted: it was MISSING
"personal data" / "data protection" / "data-protection", which the shipped
playbook HAS. When the fallback fired, a perpetual clause carving out personal-data
retention (often legally mandated) was NOT recognized as a legitimate longer-survival
carve-out and was WRONGLY FAILED.

The fix keeps the load-bearing fallback (removing it would make a field-less clause
recognize ZERO carve-out terms -- strictly worse) but makes it impossible to drift:
the in-code list is byte-identical to the playbook AND these tests assert that
parity, so a future playbook edit that diverges from the code fails CI.

The check module cannot read the live playbook at check-time without a circular
import (``playbook_runtime``/``checker`` import the checks), so a derive-at-load
approach is infeasible inside the check; the byte-identical-copy-plus-parity-test
is the single-source-of-truth mechanism that fits the wiring.
"""

import json
import os
import re
import unittest

from nda_automation.checker import review_nda
from nda_automation.checks.term_and_survival import (
    DEFAULT_INDEFINITE_NON_SURVIVAL_OBJECTS,
    DEFAULT_INDEFINITE_TERMS,
    DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS,
    _carve_out_context_patterns,
    _indefinite_term_patterns,
    _is_allowed_carve_out_fragment,
    _normalize,
)

_PLAYBOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playbook.json"
)


def _term_clause_from_playbook() -> dict:
    with open(_PLAYBOOK_PATH, encoding="utf-8") as handle:
        playbook = json.load(handle)
    return next(
        clause for clause in playbook["clauses"] if clause.get("id") == "term_and_survival"
    )


class CarveOutDriftGuardTests(unittest.TestCase):
    """Pin the in-code fallback lists to the playbook so they can't silently drift."""

    def test_longer_survival_carve_out_terms_match_playbook_exactly(self):
        clause = _term_clause_from_playbook()
        playbook_terms = clause["longer_survival_carve_out_terms"]
        # Exact equality (order + membership): the in-code fallback is a byte copy.
        self.assertEqual(
            list(DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS),
            playbook_terms,
            "In-code carve-out fallback drifted from playbook "
            "longer_survival_carve_out_terms. Keep them byte-identical "
            "(both must change together).",
        )

    def test_data_protection_terms_present_in_both(self):
        # The exact three terms whose absence caused the original drift bug. A
        # narrower belt-and-braces assertion so a partial regression is obvious.
        clause = _term_clause_from_playbook()
        for term in ("personal data", "data protection", "data-protection"):
            self.assertIn(term, clause["longer_survival_carve_out_terms"])
            self.assertIn(term, DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS)

    def test_indefinite_non_survival_objects_match_playbook_exactly(self):
        # The adjacent twin fallback (same footgun, same function family). It is
        # byte-identical today; pin it so it can't drift the way the carve-out list
        # did.
        clause = _term_clause_from_playbook()
        self.assertEqual(
            list(DEFAULT_INDEFINITE_NON_SURVIVAL_OBJECTS),
            clause["indefinite_non_survival_objects"],
            "In-code indefinite_non_survival_objects fallback drifted from "
            "playbook. Keep them byte-identical.",
        )

    def test_indefinite_terms_match_playbook_exactly(self):
        # The perpetual/indefinite vocabulary fallback. A degraded clause missing
        # ``indefinite_terms`` falls back to this in-code copy; a missing perpetual
        # phrasing would let an everlasting ordinary-CI rider slip through as
        # "missing" rather than be flagged too-long. Pin byte parity with the
        # playbook so the two can't drift.
        clause = _term_clause_from_playbook()
        self.assertEqual(
            list(DEFAULT_INDEFINITE_TERMS),
            clause["indefinite_terms"],
            "In-code DEFAULT_INDEFINITE_TERMS fallback drifted from playbook "
            "indefinite_terms. Keep them byte-identical (both must change together).",
        )

    def test_indefinite_terms_vocab_additions_present_in_both(self):
        # The exact perpetual phrasings added for the vocab-gap defect. A narrower
        # belt-and-braces assertion so a partial regression is obvious.
        clause = _term_clause_from_playbook()
        for term in (
            "forever",
            "everlasting",
            "no expiration",
            "no expiration date",
            "unlimited period",
            "without limitation of time",
            "until the end of time",
        ):
            self.assertIn(term, clause["indefinite_terms"])
            self.assertIn(term, DEFAULT_INDEFINITE_TERMS)


class IndefiniteTermsFallbackPathTests(unittest.TestCase):
    """Drive the indefinite_terms FALLBACK path (clause missing the field) and prove
    the new perpetual phrasings are recognized there too."""

    def test_fallback_patterns_cover_full_playbook_indefinite_list(self):
        # The fallback compiles one pattern per vocab term PLUS the single structural
        # open-ended-survival backstop pattern (the negated-expiry / never-cease idiom
        # that no closed vocabulary enumerates). So the count is len(vocab) + 1.
        patterns = _indefinite_term_patterns({"id": "term_and_survival"})
        self.assertEqual(len(patterns), len(DEFAULT_INDEFINITE_TERMS) + 1)

    def test_fallback_matches_everlasting(self):
        patterns = _indefinite_term_patterns({"id": "term_and_survival"})
        normalized = _normalize("the confidentiality obligations are everlasting")
        self.assertTrue(
            any(re.search(pattern, normalized) for pattern in patterns),
            "Fallback indefinite vocab must catch 'everlasting'.",
        )


class FallbackPathHonorsDataProtectionTests(unittest.TestCase):
    """Drive the FALLBACK path (clause missing the field) and prove the data-protection
    carve-out is now honored. This is the path the original bug lived on -- the full
    review_nda path carries the playbook field and never reaches the fallback.

    NON-VACUOUS: with the pre-fix 6-term fallback these assertions FAIL (personal
    data / data-protection are absent, so the carve-out is not recognized); with the
    fixed 9-term fallback they pass. The companion ``_simulated_six_term`` checks pin
    that the *old* list really would have failed, so the test can't silently rot into
    a tautology.
    """

    # The exact pre-fix fallback the bug shipped with, reconstructed locally so the
    # regression direction is provable without time-travelling the source.
    _PRE_FIX_FALLBACK = (
        "trade secret",
        "trade secrets",
        "legal obligation",
        "legal obligations",
        "required by law",
        "applicable law",
    )

    def _fragment_allowed(self, sentence: str, trigger: str, clause: dict) -> bool:
        normalized = _normalize(sentence)
        match = re.search(re.escape(trigger), normalized)
        assert match is not None, f"trigger {trigger!r} not found in {normalized!r}"
        return _is_allowed_carve_out_fragment(
            normalized, match.start(), match.end(), clause
        )

    def test_personal_data_carve_out_recognized_on_fallback(self):
        # Field-LESS clause forces the in-code fallback.
        clause = {"id": "term_and_survival"}
        self.assertTrue(
            self._fragment_allowed(
                "personal data shall be retained for as long as required by "
                "data-protection law",
                "for as long as",
                clause,
            ),
            "Fixed fallback must recognize a personal-data / data-protection "
            "longer-survival carve-out.",
        )

    def test_data_protection_law_requires_idiom_on_fallback(self):
        clause = {"id": "term_and_survival"}
        self.assertTrue(
            self._fragment_allowed(
                "personal data survives for as long as data protection law requires",
                "for as long as",
                clause,
            )
        )

    def test_pre_fix_fallback_would_have_failed_personal_data(self):
        # Pin the regression: the OLD 6-term fallback does NOT recognize the
        # personal-data carve-out (proves the bug was real and the new test is not
        # vacuous). Inject the old list explicitly via the clause field.
        clause = {
            "id": "term_and_survival",
            "longer_survival_carve_out_terms": list(self._PRE_FIX_FALLBACK),
        }
        self.assertFalse(
            self._fragment_allowed(
                "personal data shall be retained for as long as required by "
                "data-protection law",
                "for as long as",
                clause,
            ),
            "Sanity: with the pre-fix 6-term list the personal-data carve-out is "
            "(correctly for this guard) NOT recognized -- proving the fix added "
            "real coverage.",
        )

    def test_trade_secret_still_recognized_on_fallback(self):
        # Don't regress the terms that were already in the fallback.
        clause = {"id": "term_and_survival"}
        self.assertTrue(
            self._fragment_allowed(
                "trade secrets shall remain confidential in perpetuity",
                "perpetuity",
                clause,
            )
        )

    def test_fallback_pattern_count_is_full_playbook_list(self):
        # The fallback compiles a pattern per playbook term (9), not the stale 6.
        patterns = _carve_out_context_patterns({"id": "term_and_survival"})
        self.assertEqual(len(patterns), len(DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS))
        self.assertEqual(len(patterns), 9)


class EndToEndPersonalDataCarveOutTests(unittest.TestCase):
    """Belt-and-braces: the personal-data carve-out also passes through the full
    deterministic review (the production path). This already worked because the
    playbook carries the field, but it guards against a future change that would
    strip the field before it reaches the check.
    """

    def _term_clause(self, text: str) -> dict:
        result = review_nda(text, verify=False, ai_enabled=False)
        return next(c for c in result["clauses"] if c["id"] == "term_and_survival")

    def test_personal_data_retention_perpetuity_passes_end_to_end(self):
        clause = self._term_clause(
            "The confidentiality obligations survive for five (5) years; personal "
            "data shall be retained for as long as data-protection law requires."
        )
        self.assertEqual(clause["status"], "match")
        self.assertTrue(clause["passes"])

    def test_ordinary_ci_perpetuity_still_fails_end_to_end(self):
        # Precision guard: no carve-out term governs the perpetuity -> still FAILS.
        clause = self._term_clause(
            "The Confidential Information shall remain confidential in perpetuity."
        )
        self.assertEqual(clause["status"], "check")
        self.assertFalse(clause["passes"])


if __name__ == "__main__":
    unittest.main()
