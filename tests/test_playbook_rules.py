import unittest
from copy import deepcopy

from nda_automation.ai_assessment_contract import AI_CLAUSE_ASSESSMENT_SCHEMA
from nda_automation.checker import PlaybookTemplateError, load_playbook, validate_playbook
from nda_automation.playbook_rules import (
    PLAYBOOK_POLICY_SCHEMA,
    PLAYBOOK_POLICY_SCHEMA_VERSION,
    PLAYBOOK_RULES_VERSION,
    PlaybookRulesError,
    normalize_playbook_policy,
    playbook_rules_for_ai,
    validate_playbook_rules,
)


class PlaybookRulesTests(unittest.TestCase):
    def test_playbook_policy_schema_documents_editable_fields(self):
        self.assertEqual(PLAYBOOK_POLICY_SCHEMA["version"], PLAYBOOK_POLICY_SCHEMA_VERSION)
        self.assertIn("preferred_position", PLAYBOOK_POLICY_SCHEMA["clause"]["required_text"])
        self.assertIn("governing_law", PLAYBOOK_POLICY_SCHEMA["clause_overrides"])
        self.assertEqual(
            PLAYBOOK_POLICY_SCHEMA["governing_law"]["rules_option_source"],
            "approved_laws",
        )
        self.assertEqual(
            PLAYBOOK_POLICY_SCHEMA["term_and_survival"]["max_term_years"],
            {"type": "integer", "minimum": 1, "maximum": 25},
        )
        self.assertIn(
            "longer_survival_carve_out_terms",
            PLAYBOOK_POLICY_SCHEMA["term_and_survival"]["optional_text_lists"],
        )

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
        self.assertEqual([option["label"] for option in options], governing_law["approved_laws"])
        self.assertEqual(
            [option["value"] for option in options if option.get("default")],
            [governing_law["preferred_law"]],
        )
        for option, law in zip(options, governing_law["approved_laws"], strict=True):
            with self.subTest(law=law):
                self.assertTrue(option["id"])
                self.assertEqual(option["label"], law)
                self.assertEqual(option["value"], law)
                self.assertIn(law, governing_law["law_phrases"])

    def test_playbook_rules_for_ai_exposes_assessment_schema_and_clause_rules(self):
        packet = playbook_rules_for_ai(load_playbook())

        self.assertEqual(packet["version"], PLAYBOOK_RULES_VERSION)
        self.assertEqual(packet["assessment_schema"], AI_CLAUSE_ASSESSMENT_SCHEMA)
        self.assertEqual(
            [clause["clause_id"] for clause in packet["clauses"]],
            [clause["id"] for clause in load_playbook()["clauses"]],
        )
        governing_law = next(clause for clause in packet["clauses"] if clause["clause_id"] == "governing_law")
        active_governing_law = next(clause for clause in load_playbook()["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(
            governing_law["rules"]["approved_options"],
            active_governing_law["rules"]["approved_options"],
        )
        non_circumvention = next(clause for clause in packet["clauses"] if clause["clause_id"] == "non_circumvention")
        active_non_circumvention = next(
            clause for clause in load_playbook()["clauses"] if clause["id"] == "non_circumvention"
        )
        self.assertEqual(non_circumvention["acceptable_language"], active_non_circumvention["acceptable_language"])
        self.assertEqual(non_circumvention["evidence_guidance"], active_non_circumvention["evidence_guidance"])
        self.assertEqual(non_circumvention["semantic_signals"], active_non_circumvention["semantic_signals"])

    def test_ai_rules_packet_carries_wave_one_judgment_guidance(self):
        packet = playbook_rules_for_ai(load_playbook())
        clauses = {clause["clause_id"]: clause for clause in packet["clauses"]}

        mutuality = clauses["mutuality"]
        self.assertIn("non-mutual labels", mutuality["evidence_guidance"])
        self.assertTrue(
            any("administrative duties" in signal for signal in mutuality["semantic_signals"])
        )
        self.assertIn(
            "Administrative one-way duties",
            mutuality["rules"]["pass_conditions"][0]["description"],
        )
        self.assertIn(
            "operative one-way confidentiality obligations",
            mutuality["rules"]["fail_conditions"][1]["description"],
        )

        confidential_information = clauses["confidential_information"]
        self.assertIn("negated prohibition", confidential_information["evidence_guidance"])
        self.assertTrue(
            any("reverse-engineering usage right" in signal for signal in confidential_information["semantic_signals"])
        )
        review_trigger_ids = {
            trigger["id"] for trigger in confidential_information["rules"]["review_triggers"]
        }
        self.assertIn("unqualified_independent_development_exclusion", review_trigger_ids)
        self.assertIn("usage_right_language_outside_exclusion", review_trigger_ids)
        self.assertIn(
            "no use of, access to, or reference to Confidential Information",
            confidential_information["rules"]["pass_conditions"][0]["description"],
        )

        non_circumvention = clauses["non_circumvention"]
        self.assertTrue(
            any("co-located prohibition controls" in signal for signal in non_circumvention["semantic_signals"])
        )

    def test_ai_rules_packet_carries_wave_two_judgment_guidance(self):
        packet = playbook_rules_for_ai(load_playbook())
        clauses = {clause["clause_id"]: clause for clause in packet["clauses"]}

        term = clauses["term_and_survival"]
        self.assertIn("audit, tax, payment", term["evidence_guidance"])
        self.assertTrue(any("duration decoys" in signal for signal in term["semantic_signals"]))
        self.assertIn("trade secrets, legal obligations, regulatory duties", term["evidence_guidance"])
        self.assertIn("ordinary confidentiality", term["rules"]["review_triggers"][0]["description"])

        non_circumvention = clauses["non_circumvention"]
        active_non_circumvention = next(clause for clause in load_playbook()["clauses"] if clause["id"] == "non_circumvention")
        search_terms = set(active_non_circumvention["search_terms"])
        for expected in {
            "non-compete",
            "solicit or hire",
            "intellectual property assignment",
            "liquidated damages",
            "automatically renew",
            "may not terminate",
        }:
            with self.subTest(term=expected):
                self.assertIn(expected, search_terms)
        self.assertTrue(any("IP assignment" in signal for signal in non_circumvention["semantic_signals"]))
        self.assertTrue(any("liquidated damages" in signal for signal in non_circumvention["semantic_signals"]))
        self.assertIn("auto-renewal", non_circumvention["rules"]["acceptable_position"])
        self.assertIn("no-termination lock", non_circumvention["rules"]["fail_conditions"][0]["description"])

    def test_normalized_governing_law_policy_uses_editable_fields_as_source_of_truth(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"] = ["UAE", "Singapore"]
        governing_law["preferred_law"] = "Singapore"
        governing_law["law_phrases"] = {"UAE": "the UAE", "Singapore": "Singapore"}
        governing_law["requirement"] = "Governing law must be India, Delaware, England and Wales, or DIFC."
        governing_law["preferred_position"] = "Prefer England and Wales."
        governing_law["check_trigger"] = "Names a jurisdiction outside the old approved list."
        governing_law["rules"]["acceptable_position"] = "Old England and Wales policy text."
        _sync_governing_law_options(governing_law)

        normalized = normalize_playbook_policy(playbook)
        normalized_governing_law = next(
            clause for clause in normalized["clauses"] if clause["id"] == "governing_law"
        )

        self.assertEqual(governing_law["requirement"], "Governing law must be India, Delaware, England and Wales, or DIFC.")
        self.assertEqual(normalized_governing_law["requirement"], "Governing law must be UAE or Singapore.")
        self.assertIn("preferably Singapore", normalized_governing_law["preferred_position"])
        self.assertIn("outside UAE or Singapore", normalized_governing_law["check_trigger"])
        self.assertIn("Singapore as the preferred option", normalized_governing_law["rules"]["acceptable_position"])
        self.assertEqual(
            [option["value"] for option in normalized_governing_law["rules"]["approved_options"]],
            ["UAE", "Singapore"],
        )
        self.assertEqual(
            [option["value"] for option in normalized_governing_law["rules"]["approved_options"] if option.get("default")],
            ["Singapore"],
        )

    def test_ai_rules_packet_derives_term_survival_guidance_from_cap(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["max_term_years"] = 3
        term["requirement"] = "The NDA term and ordinary confidentiality survival must be fixed at up to five years."
        term["preferred_position"] = "Old five year preferred position."
        term["check_trigger"] = "Old five year trigger."
        term["rules"]["acceptable_position"] = "Old five year acceptable position."

        packet = playbook_rules_for_ai(playbook)
        term_rules = next(clause for clause in packet["clauses"] if clause["clause_id"] == "term_and_survival")

        self.assertIn("three years", term_rules["requirement"])
        self.assertIn("three years", term_rules["preferred_position"])
        self.assertIn("longer than three years", term_rules["check_trigger"])
        self.assertIn("three years", term_rules["rules"]["acceptable_position"])
        self.assertIn("three years", term_rules["rules"]["pass_conditions"][0]["description"])
        self.assertIn("longer than three years", term_rules["rules"]["fail_conditions"][1]["description"])

    def test_rule_validator_rejects_missing_rules(self):
        playbook = deepcopy(load_playbook())
        del playbook["clauses"][0]["rules"]

        with self.assertRaisesRegex(PlaybookRulesError, "must include rules"):
            validate_playbook_rules(playbook)

    def test_rule_validator_rejects_non_object_conditions(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["rules"]["pass_conditions"].append("not a condition object")

        with self.assertRaisesRegex(PlaybookRulesError, r"pass_conditions\[1\] must be an object"):
            validate_playbook_rules(playbook)

    def test_rule_validator_rejects_duplicate_condition_ids(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["rules"]["fail_conditions"][1]["id"] = mutuality["rules"]["fail_conditions"][0]["id"]

        with self.assertRaisesRegex(PlaybookRulesError, "must not contain duplicate id"):
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

    def test_validate_playbook_rejects_governing_law_redline_template_field(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["redline_template"] = "This Agreement shall be governed by the laws of India."

        with self.assertRaisesRegex(PlaybookTemplateError, "unsupported field\\(s\\): redline_template"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_duplicate_governing_law_options(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"].append("india")
        governing_law["law_phrases"]["india"] = "India"

        with self.assertRaisesRegex(PlaybookTemplateError, "approved_laws must not contain duplicate value india"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_orphan_governing_law_phrase(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["law_phrases"]["California"] = "California"

        with self.assertRaisesRegex(PlaybookTemplateError, "law_phrases has unsupported key"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_governing_law_default_drift(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["preferred_law"] = "DIFC"

        with self.assertRaisesRegex(PlaybookTemplateError, "approved_options default must match preferred_law"):
            validate_playbook(playbook)

    def test_validate_playbook_rejects_invalid_survival_cap(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["max_term_years"] = 30

        with self.assertRaisesRegex(PlaybookTemplateError, "max_term_years must be between 1 and 25"):
            validate_playbook(playbook)

    def test_validate_playbook_allows_empty_longer_survival_carveouts(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["longer_survival_carve_out_terms"] = []

        validate_playbook(playbook)


def _sync_governing_law_options(governing_law):
    governing_law["rules"]["approved_options"] = [
        {
            "id": law.lower().replace(" ", "_"),
            "label": law,
            "value": law,
            "default": law == governing_law["preferred_law"],
        }
        for law in governing_law["approved_laws"]
    ]


if __name__ == "__main__":
    unittest.main()
