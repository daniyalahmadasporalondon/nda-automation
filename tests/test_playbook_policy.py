import copy
import unittest

from nda_automation.checker import load_playbook
from nda_automation.playbook_policy import (
    build_playbook_policy_block,
    prohibited_restraint_labels,
)


class PlaybookPolicyBlockTests(unittest.TestCase):
    def setUp(self):
        self.playbook = load_playbook()
        self.block = build_playbook_policy_block(self.playbook)

    def test_block_carries_the_five_rules_and_scope(self):
        for marker in (
            "BINDING PLAYBOOK RULES",
            "RULE 1 — PROHIBITED BUSINESS RESTRAINTS",
            "RULE 2 — PENALTIES",
            "RULE 3 — ORDINARY CONFIDENTIALITY SURVIVAL",
            "RULE 4 — GOVERNING LAW",
            "RULE 5 — THE NDA MUST NOT BE SUBORDINATED",
            "SCOPE INSTRUCTION (MANDATORY).",
        ):
            self.assertIn(marker, self.block)

    def test_block_carries_the_five_year_cap_from_the_playbook(self):
        # RULE 3 load-bearing fact: the 5-year survival cap, derived from
        # term_and_survival.max_term_years == 5.
        self.assertIn("FIVE (5) YEARS", self.block)
        self.assertIn("five (5) years", self.block)
        self.assertIn("up to 5 years", self.block)

    def test_block_carries_the_approved_laws_and_preferred(self):
        # RULE 4 load-bearing fact: the approved governing-law set + preferred default.
        gov = next(c for c in self.playbook["clauses"] if c["id"] == "governing_law")
        for law in gov["approved_laws"]:
            self.assertIn(law, self.block)
        self.assertIn(gov["preferred_law"], self.block)
        self.assertIn("is the default/preferred", self.block)

    def test_block_carries_the_strike_in_full_and_align_forum_remedies(self):
        # The two distinctive remedies the validated golden text requires.
        self.assertIn("DELETED in full", self.block)
        self.assertIn("STRUCK, NOT NARROWED", self.block)
        self.assertIn("ALIGN THE FORUM", self.block)

    def test_block_lists_every_prohibited_restraint_category(self):
        # RULE 1 catalogue must name the restraint categories the playbook enumerates.
        for phrase in (
            "non-compete",
            "non-solicitation",
            "non-circumvention",
            "exclusivity",
            "IP assignment",
            "auto-renewal",
        ):
            self.assertIn(phrase, self.block)

    def test_block_quotes_the_playbook_drafting_note(self):
        non_circ = next(
            c for c in self.playbook["clauses"] if c["id"] == "non_circumvention"
        )
        drafting_note = non_circ["rules"]["redline_guidance"]["drafting_note"]
        self.assertIn(drafting_note, self.block)

    # ---- DERIVATION PROOF: the block FOLLOWS a mutated playbook ----

    def test_block_follows_a_mutated_max_term_years(self):
        # Prove the cap is DERIVED, not pinned: change max_term_years 5 -> 3 and assert
        # the block now caps at three years and no longer mentions five.
        mutated = copy.deepcopy(self.playbook)
        for clause in mutated["clauses"]:
            if clause["id"] == "term_and_survival":
                clause["max_term_years"] = 3
        block = build_playbook_policy_block(mutated)

        self.assertIn("THREE (3) YEARS", block)
        self.assertIn("up to 3 years", block)
        self.assertIn("max_term_years=3", block)
        self.assertNotIn("FIVE (5) YEARS", block)
        self.assertNotIn("up to 5 years", block)

    def test_block_follows_a_mutated_approved_laws_set(self):
        # Prove the approved-law list is DERIVED: shrink it to India + Delaware (preferred
        # India) and assert the dropped jurisdictions vanish from the block.
        mutated = copy.deepcopy(self.playbook)
        for clause in mutated["clauses"]:
            if clause["id"] == "governing_law":
                clause["approved_laws"] = ["India", "Delaware"]
                clause["preferred_law"] = "India"
                clause["law_phrases"] = {"India": "India", "Delaware": "Delaware"}
                clause["rules"]["approved_options"] = [
                    {"id": "india", "label": "India", "value": "India", "default": True},
                    {
                        "id": "delaware",
                        "label": "Delaware",
                        "value": "Delaware",
                        "default": False,
                    },
                ]
        block = build_playbook_policy_block(mutated)

        self.assertIn("India or Delaware", block)
        self.assertIn("India is the default/preferred", block)
        self.assertNotIn("DIFC", block)
        self.assertNotIn("Ontario", block)
        self.assertNotIn("England and Wales", block)

    def test_restraint_catalogue_equals_the_playbook_label_set(self):
        # Section-G caveat: the policy is exactly as complete as the playbook's rule
        # list. Dropping a prohibited_position_patterns label removes that category from
        # the RULE 1 catalogue -- adding a pattern is the ONLY way to extend coverage.
        labels = prohibited_restraint_labels(self.playbook)
        self.assertEqual(
            labels,
            [
                "non_compete",
                "non_solicit",
                "non_circumvention",
                "exclusivity",
                "ip_assignment",
                "perpetual_confidentiality",
                "penalty",
                "auto_renew_lock",
            ],
        )

        mutated = copy.deepcopy(self.playbook)
        for clause in mutated["clauses"]:
            if clause["id"] == "non_circumvention":
                clause["prohibited_position_patterns"] = [
                    pattern
                    for pattern in clause["prohibited_position_patterns"]
                    if pattern["label"] != "ip_assignment"
                ]
        block = build_playbook_policy_block(mutated)
        self.assertNotIn("IP assignment", block)

    def test_block_is_pure_string_and_stable(self):
        # Deterministic: same playbook -> identical block (no ordering nondeterminism).
        self.assertIsInstance(self.block, str)
        self.assertEqual(self.block, build_playbook_policy_block(self.playbook))


if __name__ == "__main__":
    unittest.main()
