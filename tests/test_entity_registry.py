import unittest

from nda_automation import entity_registry as er
from nda_automation.checker import load_playbook

EXPECTED_ENTITY_IDS = {
    "aspora_technology",
    "vance_money",
    "real_transfer",
    "vance_techlabs",
}

# The mapping every entity bundle relies on. These ids must exist in the live
# playbook's governing_law approved_options.
EXPECTED_LAW_MAPPING = {
    "aspora_technology": "india",
    "vance_money": "delaware",
    "real_transfer": "england_and_wales",
    "vance_techlabs": "difc",
}


class EntityRegistryBundleTests(unittest.TestCase):
    def test_registers_the_four_signing_entities(self):
        ids = {entity["id"] for entity in er.list_entities()}
        self.assertEqual(ids, EXPECTED_ENTITY_IDS)

    def test_registry_is_internally_consistent(self):
        # Required fields + exactly-one-default-address invariant.
        er.validate_registry()

    def test_each_entity_has_exactly_one_default_address(self):
        for entity in er.list_entities():
            defaults = [a for a in entity["addresses"] if a.get("default")]
            self.assertEqual(
                len(defaults),
                1,
                f"{entity['id']} must have exactly one default address",
            )
            self.assertIsNotNone(er.default_address(entity))

    def test_bundle_carries_name_address_and_law_together(self):
        # The whole point of a bundle: picking it gives you name + address + law.
        for entity in er.list_entities():
            self.assertTrue(entity["legal_name"])
            self.assertTrue(entity["addresses"])
            self.assertIn("playbook_option_id", entity["governing_law"])
            self.assertTrue(entity["jurisdiction"])
            self.assertIn("name", entity["signatory"])

    def test_real_transfer_has_two_addresses_default_is_london_corporate(self):
        real_transfer = er.get_entity("real_transfer")
        self.assertEqual(len(real_transfer["addresses"]), 2)
        default = er.default_address(real_transfer)
        self.assertEqual(default["id"], "corporate")
        # Belfast (registered, Northern Ireland) must NOT be the default, because
        # it has no matching playbook governing-law position.
        self.assertTrue(
            any("London" in line for line in default["lines"]),
            "Real Transfer default address should be the London corporate office",
        )
        registered = next(
            a for a in real_transfer["addresses"] if a["id"] == "registered"
        )
        self.assertFalse(registered["default"])
        self.assertTrue(any("Belfast" in line for line in registered["lines"]))

    def test_single_address_entities_have_one_address(self):
        for entity_id in ("aspora_technology", "vance_money", "vance_techlabs"):
            self.assertEqual(len(er.get_entity(entity_id)["addresses"]), 1)

    def test_get_entity_returns_none_for_unknown(self):
        self.assertIsNone(er.get_entity("nope"))

    def test_accessors_return_copies(self):
        # Mutating a returned bundle must not corrupt the module-level registry.
        entity = er.get_entity("aspora_technology")
        entity["legal_name"] = "MUTATED"
        entity["addresses"][0]["default"] = False
        fresh = er.get_entity("aspora_technology")
        self.assertEqual(
            fresh["legal_name"], "Aspora Technology Services Private Limited"
        )
        self.assertTrue(fresh["addresses"][0]["default"])

    def test_legal_names_match_exact_provided_details(self):
        names = {e["id"]: e["legal_name"] for e in er.list_entities()}
        self.assertEqual(
            names["aspora_technology"],
            "Aspora Technology Services Private Limited",
        )
        self.assertEqual(names["vance_money"], "Vance Money Services LLC")
        self.assertEqual(names["real_transfer"], "Real Transfer Limited")
        self.assertEqual(names["vance_techlabs"], "Vance Techlabs Limited")


class GoverningLawMappingTests(unittest.TestCase):
    def test_mapping_matches_expected_positions(self):
        mapping = {
            row["entity_id"]: row["playbook_option_id"]
            for row in er.entity_law_mapping()
        }
        self.assertEqual(mapping, EXPECTED_LAW_MAPPING)

    def test_every_entity_maps_to_a_live_playbook_position(self):
        # This is the contract that keeps generation honest: if the playbook
        # renames or drops a governing_law option, this fails loudly.
        playbook = load_playbook()
        er.validate_registry_against_playbook(playbook)

    def test_mapping_against_playbook_marks_matches(self):
        playbook = load_playbook()
        for row in er.entity_law_mapping(playbook):
            self.assertTrue(
                row["matches_playbook"],
                f"{row['entity_id']} -> {row['playbook_option_id']} not in playbook",
            )

    def test_flags_are_present_for_ambiguous_entities(self):
        # Real Transfer (NI vs England) and Vance Techlabs (DIFC vs UAE federal)
        # are the two flagged-for-review cases.
        notes = er.ENTITY_LAW_MAPPING_NOTES
        self.assertIn("real_transfer", notes)
        self.assertIn("vance_techlabs", notes)
        self.assertIn("Northern Ireland", notes["real_transfer"])
        self.assertIn("DIFC", notes["vance_techlabs"])

        flagged = {
            row["entity_id"]
            for row in er.entity_law_mapping()
            if row["flag"]
        }
        self.assertEqual(flagged, {"real_transfer", "vance_techlabs"})

    def test_clean_entities_are_not_flagged(self):
        flags = {row["entity_id"]: row["flag"] for row in er.entity_law_mapping()}
        self.assertIsNone(flags["aspora_technology"])
        self.assertIsNone(flags["vance_money"])

    def test_validate_against_playbook_rejects_missing_position(self):
        # Simulate playbook drift: drop england_and_wales and confirm we fail.
        playbook = load_playbook()
        for clause in playbook["clauses"]:
            if clause["id"] == "governing_law":
                clause["rules"]["approved_options"] = [
                    opt
                    for opt in clause["rules"]["approved_options"]
                    if opt["id"] != "england_and_wales"
                ]
        with self.assertRaises(ValueError):
            er.validate_registry_against_playbook(playbook)

    def test_validate_against_playbook_rejects_empty_options(self):
        with self.assertRaises(ValueError):
            er.validate_registry_against_playbook({"clauses": []})


if __name__ == "__main__":
    unittest.main()
