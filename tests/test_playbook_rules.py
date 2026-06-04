import unittest
from copy import deepcopy

from nda_automation.ai_assessment_contract import AI_CLAUSE_ASSESSMENT_SCHEMA
from nda_automation.checker import PlaybookTemplateError, load_playbook, validate_playbook
from nda_automation.playbook_rules import (
    PLAYBOOK_RULES_VERSION,
    PlaybookRulesError,
    playbook_rules_for_ai,
    validate_playbook_rules,
)


class PlaybookRulesTests(unittest.TestCase):
    def test_all_playbook_clauses_have_structured_rules(self):
        playbook = load_playbook()
        validate_playbook_rules(playbook)

        for clause in playbook["clauses"]:
            with self.subTest(clause=clause["id"]):
                rules = clause["rules"]
                self.assertEqual(rules["version"], PLAYBOOK_RULES_VERSION)
                self.assertEqual(rules["clause_type"], clause["type"])
                self.assertIsInstance(rules["acceptable_position"], str)
                self.assertTrue(rules["pass_conditions"])
                self.assertTrue(rules["fail_conditions"])
                self.assertTrue(rules["review_triggers"])
                self.assertIn("evidence_requirements", rules)
                self.assertIn("redline_guidance", rules)

    def test_required_clause_rules_cover_missing_and_present_but_wrong_failures(self):
        playbook = load_playbook()

        for clause in playbook["clauses"]:
            if clause["rules"]["clause_type"] != "required":
                continue
            with self.subTest(clause=clause["id"]):
                issue_types = {condition["issue_type"] for condition in clause["rules"]["fail_conditions"]}
                self.assertIn("missing", issue_types)
                self.assertIn("present_but_wrong", issue_types)

    def test_prohibited_clause_rules_cover_present_prohibited_language(self):
        non_circumvention = next(
            clause for clause in load_playbook()["clauses"] if clause["id"] == "non_circumvention"
        )

        self.assertEqual(non_circumvention["type"], "prohibited")
        self.assertEqual(non_circumvention["rules"]["evidence_requirements"]["minimum_evidence_for_pass"], 0)
        self.assertEqual(
            [condition["redline_action"] for condition in non_circumvention["rules"]["fail_conditions"]],
            ["delete_paragraph"],
        )
        self.assertIn(
            "present_but_wrong",
            {condition["issue_type"] for condition in non_circumvention["rules"]["fail_conditions"]},
        )

    def test_governing_law_rules_expose_all_approved_jurisdiction_options(self):
        governing_law = next(clause for clause in load_playbook()["clauses"] if clause["id"] == "governing_law")
        options = governing_law["rules"]["approved_options"]

        self.assertEqual([option["value"] for option in options], governing_law["approved_laws"])
        self.assertEqual([option["label"] for option in options], ["India", "Delaware", "England and Wales", "DIFC"])
        self.assertEqual([option["value"] for option in options if option.get("default")], ["England and Wales"])

    def test_playbook_rules_for_ai_exposes_assessment_schema_and_clause_rules(self):
        packet = playbook_rules_for_ai(load_playbook())

        self.assertEqual(packet["version"], PLAYBOOK_RULES_VERSION)
        self.assertEqual(packet["assessment_schema"], AI_CLAUSE_ASSESSMENT_SCHEMA)
        self.assertEqual(
            [clause["clause_id"] for clause in packet["clauses"]],
            [clause["id"] for clause in load_playbook()["clauses"]],
        )
        governing_law = next(clause for clause in packet["clauses"] if clause["clause_id"] == "governing_law")
        self.assertEqual(governing_law["rules"]["approved_options"][2]["value"], "England and Wales")

    def test_rule_validator_rejects_missing_rules(self):
        playbook = deepcopy(load_playbook())
        del playbook["clauses"][0]["rules"]

        with self.assertRaisesRegex(PlaybookRulesError, "must include rules"):
            validate_playbook_rules(playbook)

    def test_validate_playbook_rejects_malformed_structured_rules(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["rules"]["approved_options"][0]["default"] = True

        with self.assertRaisesRegex(PlaybookTemplateError, "approved_options must have exactly one default option"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_governing_law_policy_drift(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"] = ["India", "England and Wales", "DIFC"]

        with self.assertRaisesRegex(PlaybookTemplateError, "approved_options values must match approved_laws"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_governing_law_default_drift(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["preferred_law"] = "DIFC"

        with self.assertRaisesRegex(PlaybookTemplateError, "approved_options default must match preferred_law"):
            validate_playbook(playbook)


if __name__ == "__main__":
    unittest.main()
