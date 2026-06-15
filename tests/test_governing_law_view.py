"""Unit tests for governing_law_view: the per-matter governing-law surface.

These lock the contract the dashboard smart-search depends on:
* a GENERATED NDA surfaces its manifest governing_law_value as a Playbook
  approved-option id (so "DIFC NDAs" can filter it deterministically),
* an INBOUND matter surfaces an APPROVED detected law from its review clause, and
  ONLY when approved -- an unapproved/unclear/absent law surfaces "",
* the option-id allowlist is sourced from the Playbook approved options.

NOTE: on the corpus-search branch the governing-law facet is surfaced via the
CORPUS payload (corpus_index facets), NOT public_matter -- governing_law is not in
PUBLIC_MATTER_FIELDS. The corpus_index facet tests assert the end-to-end surface;
these tests lock the derivation primitive.
"""
from __future__ import annotations

import unittest

from nda_automation import governing_law_view as glv


def _generated_matter(law_value: str) -> dict:
    return {
        "id": "m1",
        "artifacts": [
            {
                "id": "a1",
                "role": "generated",
                "version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {"generation": {"governing_law_value": law_value, "counterparty_name": "Acme"}},
            }
        ],
    }


def _inbound_matter(candidate_value: str, approved: bool) -> dict:
    return {
        "id": "m2",
        "review_result": {
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass" if approved else "check",
                    "governing_law_analysis": {
                        "candidate_records": [
                            {"paragraph_id": "p1", "value": candidate_value, "approved": approved, "needs_review": False}
                        ]
                    },
                }
            ]
        },
    }


class DeriveGoverningLawTests(unittest.TestCase):
    def setUp(self) -> None:
        glv.reset_caches()

    def tearDown(self) -> None:
        glv.reset_caches()

    def test_generated_manifest_value_normalises_to_option_id(self):
        self.assertEqual(glv.derive_governing_law(_generated_matter("DIFC")), "difc")
        self.assertEqual(glv.derive_governing_law(_generated_matter("England and Wales")), "england_and_wales")
        self.assertEqual(glv.derive_governing_law(_generated_matter("India")), "india")

    def test_inbound_approved_detected_law_is_surfaced(self):
        self.assertEqual(glv.derive_governing_law(_inbound_matter("India", approved=True)), "india")

    def test_inbound_unapproved_law_surfaces_nothing(self):
        # Honest: a non-approved detected law is not a filterable governing-law dim.
        self.assertEqual(glv.derive_governing_law(_inbound_matter("Narnia", approved=False)), "")

    def test_matter_with_no_law_surfaces_empty_string(self):
        self.assertEqual(glv.derive_governing_law({}), "")
        self.assertEqual(glv.derive_governing_law({"id": "x", "subject": "Some NDA"}), "")

    def test_unrecognised_manifest_value_is_dropped(self):
        self.assertEqual(glv.derive_governing_law(_generated_matter("Some Made Up Law")), "")

    def test_generated_preferred_over_inbound(self):
        # A matter with both a generated manifest law and an inbound clause uses the
        # exact generated value (the generator's own choice wins).
        matter = _generated_matter("DIFC")
        matter["review_result"] = _inbound_matter("India", approved=True)["review_result"]
        self.assertEqual(glv.derive_governing_law(matter), "difc")

    def test_option_id_allowlist_sourced_from_playbook(self):
        ids = glv.governing_law_option_ids()
        for expected in ("india", "delaware", "england_and_wales", "difc"):
            self.assertIn(expected, ids)

    def test_normalize_is_case_insensitive_and_alias_aware(self):
        self.assertEqual(glv.normalize_governing_law("DIFC"), "difc")
        self.assertEqual(glv.normalize_governing_law("  india  "), "india")
        self.assertEqual(glv.normalize_governing_law("nope"), "")


if __name__ == "__main__":
    unittest.main()
