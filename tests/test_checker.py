import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nda_automation import checker as checker_module
from nda_automation.checker import (
    ParagraphAlignmentError,
    PlaybookTemplateError,
    _paragraph_matches,
    load_playbook,
    review_nda,
    split_document_paragraphs,
)

ROOT = Path(__file__).resolve().parent.parent


class CheckerTests(unittest.TestCase):
    def redlines_for_clause(self, result, clause_id):
        return [edit for edit in result["redline_edits"] if edit["clause_id"] == clause_id]

    def redline_for_clause(self, result, clause_id):
        redlines = self.redlines_for_clause(result, clause_id)
        self.assertTrue(redlines, f"Expected a redline for {clause_id}")
        return redlines[0]

    def test_redline_registry_mirrors_checker_registry(self):
        self.assertEqual(
            [clause_id for clause_id, _builder in checker_module.REDLINE_BUILDERS],
            [clause_id for clause_id, _check in checker_module.CLAUSE_CHECKS],
        )

    def test_registry_validation_rejects_missing_redline_builder(self):
        playbook = deepcopy(load_playbook())
        playbook["clauses"].append({
            "id": "new_clause",
            "name": "New Clause",
            "requirement": "New requirement.",
            "type": "required",
            "search_terms": ["new clause"],
        })

        def dummy_check(text, normalized, clause, paragraphs):
            return {}

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            with patch.object(checker_module, "CLAUSE_CHECKS", checker_module.CLAUSE_CHECKS + [("new_clause", dummy_check)]):
                with self.assertRaisesRegex(RuntimeError, "missing redline builders for: new_clause"):
                    checker_module._validate_check_registry()

    def test_checker_has_no_default_policy_lists(self):
        default_policy_lists = [
            name
            for name, value in vars(checker_module).items()
            if name.startswith("DEFAULT_") and isinstance(value, (list, tuple, set))
        ]

        self.assertEqual(default_policy_lists, [])

    def test_pass_sample_meets_requirements(self):
        result = review_nda((ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8"))

        self.assertEqual(result["overall_status"], "meets_requirements")
        self.assertEqual(result["requirements_failed"], 0)
        self.assertTrue(all(clause["passes"] for clause in result["clauses"]))
        self.assertIn("paragraphs", result)

    def test_fail_sample_does_not_meet_requirements(self):
        result = review_nda((ROOT / "samples" / "fail-nda.txt").read_text(encoding="utf-8"))

        self.assertEqual(result["overall_status"], "does_not_meet_requirements")
        self.assertGreater(result["requirements_failed"], 0)
        failed_clause_ids = {clause["id"] for clause in result["clauses"] if not clause["passes"]}
        self.assertIn("governing_law", failed_clause_ids)
        self.assertIn("non_circumvention", failed_clause_ids)

    def test_term_and_survival_allows_less_than_five_years(self):
        text = (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")
        result = review_nda(text)

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "match")
        self.assertTrue(term_clause["passes"])

    def test_mutuality_terms_come_from_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["search_terms"] = ["reciprocal confidentiality"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement creates reciprocal confidentiality obligations.")

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(result_clause["status"], "match")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1"])

    def test_mutuality_accepts_separated_party_role_definitions(self):
        result = review_nda(
            """
            The Disclosing Party means a party that discloses Confidential Information under this Agreement.

            The Receiving Party means a party that receives Confidential Information under this Agreement.
            """
        )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(result_clause["status"], "match")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1", "p2"])

    def test_mutuality_rejects_fixed_one_way_role_labels(self):
        result = review_nda(
            """
            The Company is the Disclosing Party under this Agreement.

            The Recipient is the Receiving Party under this Agreement.

            The Receiving Party shall protect Confidential Information received from the Disclosing Party.
            """
        )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(result_clause["status"], "not_present")
        self.assertFalse(result_clause["passes"])

    def test_mutuality_role_terms_come_from_playbook(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["role_terms"] = ["alpha role", "beta role"]
        mutuality["role_reciprocity_terms"] = ["a side"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                The Alpha Role means a side that discloses Confidential Information.

                The Beta Role means a side that receives Confidential Information.
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(result_clause["status"], "match")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1", "p2"])

    def test_search_terms_are_checker_config_not_review_payload(self):
        result = review_nda("This Agreement creates reciprocal confidentiality obligations.")

        self.assertTrue(all("search_terms" not in clause for clause in result["clauses"]))

    def test_term_and_survival_picks_up_period_of_two_years(self):
        text = """
        This Agreement shall continue for a period of two (2) years.
        The undertakings set out in this Agreement will survive for a further period of two (2) years.
        """
        result = review_nda(text)

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "match")
        self.assertIn("within the cap of five years", term_clause["finding"])

    def test_term_and_survival_ignores_unrelated_year_references(self):
        result = review_nda("The parties have worked together for two years on commercial discussions.")

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "not_present")
        self.assertFalse(term_clause["passes"])
        self.assertEqual(term_clause["issue_type"], "missing")
        self.assertIn("Add a fixed term", term_clause["what_to_fix"])

    def test_term_and_survival_rejects_more_than_five_years(self):
        text = (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")
        result = review_nda(text.replace("three (3) years", "seven (7) years"))

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "check")
        self.assertFalse(term_clause["passes"])
        self.assertEqual(term_clause["issue_type"], "present_but_wrong")
        self.assertIn("five years or less", term_clause["what_to_fix"])

    def test_term_and_survival_uses_playbook_cap_in_details(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["max_term_years"] = 3

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("The confidentiality obligations survive for four (4) years.")

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "check")
        self.assertIn("cap of three years", term_clause["finding"])
        self.assertIn("three years or less", term_clause["what_to_fix"])
        self.assertNotIn("five", term_clause["finding"].lower())
        self.assertNotIn("five", term_clause["what_to_fix"].lower())
        term_redline = self.redline_for_clause(result, "term_and_survival")
        self.assertIn("up to three years", term_redline["replacement_text"])
        self.assertNotIn("five", term_redline["replacement_text"].lower())

    def test_term_redline_template_comes_from_playbook(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["max_term_years"] = 4
        term["redline_template"] = "Custom survival language capped at {max_term_years_label}."

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("The confidentiality obligations survive for seven (7) years.")

        term_redline = self.redline_for_clause(result, "term_and_survival")
        self.assertEqual(term_redline["replacement_text"], "Custom survival language capped at four years.")

    def test_bad_playbook_redline_template_fails_loud(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["redline_template"] = "Custom survival language capped at {unknown_placeholder}."

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            with self.assertRaisesRegex(PlaybookTemplateError, "term_and_survival"):
                review_nda("The confidentiality obligations survive for seven (7) years.")

    def test_term_and_survival_redline_replaces_existing_bad_term(self):
        result = review_nda("The confidentiality obligations survive for seven (7) years.")

        redline = self.redline_for_clause(result, "term_and_survival")
        self.assertEqual(len(self.redlines_for_clause(result, "term_and_survival")), 1)
        self.assertEqual(redline["clause_id"], "term_and_survival")
        self.assertEqual(redline["action"], "replace_paragraph")
        self.assertEqual(redline["action_label"], "Replace paragraph")
        self.assertEqual(redline["paragraph_id"], "p1")
        self.assertEqual(redline["original_text"], "The confidentiality obligations survive for seven (7) years.")
        self.assertEqual(
            redline["replacement_text"],
            "The confidentiality obligations survive for a fixed period of up to five years, except for trade secrets or legal obligations that require a longer period.",
        )

    def test_missing_term_and_survival_creates_insert_redline_after_anchor_paragraph(self):
        result = review_nda("The parties will discuss a possible transaction.")

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "not_present")
        redline = self.redline_for_clause(result, "term_and_survival")
        self.assertEqual(redline["action"], "insert_after_paragraph")
        self.assertEqual(redline["action_label"], "Insert after paragraph")
        self.assertEqual(redline["paragraph_id"], "p1")
        self.assertEqual(redline["target_position"], "after_paragraph")
        self.assertEqual(redline["original_text"], "")
        self.assertIn("up to five years", redline["insert_text"])

    def test_returns_numbered_paragraph_model(self):
        paragraphs = split_document_paragraphs("First paragraph.\n\nSecond paragraph.")

        self.assertEqual(
            paragraphs,
            [
                {"id": "p1", "index": 1, "text": "First paragraph.", "start": 0, "end": 16},
                {"id": "p2", "index": 2, "text": "Second paragraph.", "start": 18, "end": 35},
            ],
        )

    def test_paragraph_matches_deduplicates_matches(self):
        paragraph = {"id": "p1", "index": 1, "text": "This Agreement is governed by the laws of California."}
        matches = _paragraph_matches([paragraph, paragraph], [r"governed by", r"laws of"])

        self.assertEqual(matches, [paragraph])

    def test_clause_results_include_exact_evidence_paragraphs(self):
        result = review_nda(
            """
            Mutual Non-Disclosure Agreement

            This Agreement shall be governed in all respects by the laws of the DIFC.
            """
        )

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["status"], "match")
        self.assertEqual(governing_law["issue_type"], "none")
        self.assertEqual(governing_law["what_to_fix"], "No change needed.")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p2"])
        self.assertEqual(
            governing_law["matched_text"],
            "This Agreement shall be governed in all respects by the laws of the DIFC.",
        )
        self.assertEqual(governing_law["reason"], "Approved governing law found.")

    def test_governing_law_requires_approved_law_in_governing_paragraph(self):
        result = review_nda(
            """
            This Agreement shall be governed by the laws of California.

            The parties may hold meetings at the DIFC.
            """
        )

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["status"], "check")
        self.assertFalse(governing_law["passes"])
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p1"])
        self.assertEqual(governing_law["issue_type"], "present_but_wrong")
        self.assertEqual(governing_law["issue_label"], "Present but wrong")
        self.assertIn("India, Delaware, England and Wales, or DIFC", governing_law["what_to_fix"])
        governing_law_redline = self.redline_for_clause(result, "governing_law")
        self.assertEqual(governing_law_redline["action"], "replace_paragraph")
        self.assertEqual(governing_law_redline["clause_id"], "governing_law")
        self.assertEqual(governing_law_redline["paragraph_id"], "p1")
        self.assertEqual(
            governing_law_redline["original_text"],
            "This Agreement shall be governed by the laws of California.",
        )
        self.assertEqual(
            governing_law_redline["replacement_text"],
            "This Agreement shall be governed by the laws of England and Wales.",
        )
        self.assertEqual(
            [option["label"] for option in governing_law_redline["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC"],
        )
        self.assertEqual(governing_law_redline["selected_template_id"], "governing_law_england_and_wales")
        self.assertEqual(
            [option["text"] for option in governing_law_redline["template_options"]],
            [
                "This Agreement shall be governed by the laws of India.",
                "This Agreement shall be governed by the laws of Delaware.",
                "This Agreement shall be governed by the laws of England and Wales.",
                "This Agreement shall be governed by the laws of the DIFC.",
            ],
        )

    def test_can_use_supplied_structured_paragraphs(self):
        result = review_nda(
            "First paragraph.\n\nThis Agreement shall be governed by the laws of the DIFC.",
            paragraphs=[
                {"source_index": 4, "text": "First paragraph."},
                {"source_index": 5, "text": "This Agreement shall be governed by the laws of the DIFC."},
            ],
        )

        self.assertEqual(result["paragraphs"][1]["id"], "p2")
        self.assertEqual(result["paragraphs"][1]["source_index"], 5)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p2"])

    def test_structured_paragraph_alignment_fails_on_lookup_miss(self):
        with self.assertRaisesRegex(ParagraphAlignmentError, "source_index 8"):
            review_nda(
                "First paragraph.\n\nSecond paragraph.",
                paragraphs=[
                    {"source_index": 7, "text": "First paragraph."},
                    {"source_index": 8, "text": "Missing paragraph."},
                    {"source_index": 9, "text": "Second paragraph."},
                ],
            )

    def test_prohibited_clause_can_pass_as_not_present(self):
        result = review_nda((ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8"))

        non_circumvention = next(clause for clause in result["clauses"] if clause["id"] == "non_circumvention")
        self.assertEqual(non_circumvention["status"], "not_present")
        self.assertTrue(non_circumvention["passes"])
        self.assertEqual(non_circumvention["issue_type"], "none")
        self.assertEqual(non_circumvention["what_to_fix"], "No change needed.")
        self.assertEqual(non_circumvention["matched_paragraph_ids"], [])

    def test_standard_confidentiality_exclusions_can_pass(self):
        result = review_nda(
            """
            Confidential Information means any and all non-public business, financial, technical,
            customer, supplier, pricing, market, proprietary and trade secret information disclosed
            by either party.

            Confidential Information does not include information that is public, already known,
            received from a lawful third party, or independently developed without use of or
            reference to the Confidential Information.
            """
        )

        confidential_information = next(
            clause for clause in result["clauses"] if clause["id"] == "confidential_information"
        )
        self.assertEqual(confidential_information["status"], "match")
        self.assertTrue(confidential_information["passes"])

    def test_reverse_engineering_restriction_is_not_confidentiality_exclusion(self):
        result = review_nda(
            """
            Confidential Information means any and all non-public business, financial, technical,
            customer, supplier, pricing, market, proprietary and trade secret information disclosed
            by either party.

            The Receiving Party must not reverse engineer samples or prototypes supplied by the
            Disclosing Party.
            """
        )

        confidential_information = next(
            clause for clause in result["clauses"] if clause["id"] == "confidential_information"
        )
        self.assertEqual(confidential_information["status"], "match")
        self.assertTrue(confidential_information["passes"])

    def test_confidential_information_fix_copy_uses_playbook_categories(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["definition_categories"] = ["financial", "employee", "source code"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("The parties will discuss a possible transaction.")

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertIn("financial, employee, and source code", result_clause["what_to_fix"])
        self.assertNotIn("business", result_clause["what_to_fix"])

    def test_confidential_information_search_terms_define_broad_definition(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["search_terms"] = [
            "protected data",
            "source repositories",
            "roadmap",
            "customer list",
            "financial model",
        ]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                "Protected Data means source repositories, roadmap, customer list, and financial model."
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertEqual(result_clause["status"], "match")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1"])

    def test_confidential_information_exclusion_terms_come_from_playbook(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["problematic_exclusion_terms"] = ["model weights"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                Confidential Information means any and all non-public business, financial, technical,
                customer, supplier, pricing, market, proprietary and trade secret information disclosed
                by either party.

                Confidential Information excludes model weights.
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p2"])

    def test_confidential_information_exclusion_context_terms_come_from_playbook(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["exclusion_context_terms"] = ["carves out"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                Confidential Information means any and all non-public business, financial, technical,
                customer, supplier, pricing, market, proprietary and trade secret information disclosed
                by either party.

                Confidential Information carves out residual knowledge.
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p2"])

    def test_independent_development_terms_come_from_playbook(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["independent_development_terms"] = ["bench discovery"]
        confidential_information["independent_development_qualification_terms"] = ["without model access"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                Confidential Information means any and all non-public business, financial, technical,
                customer, supplier, pricing, market, proprietary and trade secret information disclosed
                by either party.

                Confidential Information does not include bench discovery.
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p2"])

    def test_independent_development_qualification_terms_come_from_playbook(self):
        playbook = deepcopy(load_playbook())
        confidential_information = next(
            clause for clause in playbook["clauses"] if clause["id"] == "confidential_information"
        )
        confidential_information["independent_development_terms"] = ["bench discovery"]
        confidential_information["independent_development_qualification_terms"] = ["without model access"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                Confidential Information means any and all non-public business, financial, technical,
                customer, supplier, pricing, market, proprietary and trade secret information disclosed
                by either party.

                Confidential Information does not include bench discovery created without model access.
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "confidential_information")
        self.assertEqual(result_clause["status"], "match")

    def test_broad_confidentiality_exclusion_still_needs_review(self):
        result = review_nda(
            """
            Confidential Information means any and all non-public business, financial, technical,
            customer, supplier, pricing, market, proprietary and trade secret information disclosed
            by either party.

            Confidential Information excludes residual knowledge retained in unaided memory.
            """
        )

        confidential_information = next(
            clause for clause in result["clauses"] if clause["id"] == "confidential_information"
        )
        self.assertEqual(confidential_information["status"], "check")
        self.assertFalse(confidential_information["passes"])
        self.assertEqual(confidential_information["matched_paragraph_ids"], ["p2"])
        self.assertEqual(confidential_information["issue_type"], "present_but_wrong")
        self.assertIn("Remove residual knowledge", confidential_information["what_to_fix"])

    def test_confidentiality_exclusion_scan_is_not_capped_before_problem_detection(self):
        result = review_nda(
            """
            Confidential Information means any and all non-public business, financial, technical,
            customer, supplier, pricing, market, proprietary and trade secret information disclosed
            by either party.

            Confidential Information does not include public information.

            Confidential Information does not include information already known by the Receiving Party.

            Confidential Information does not include information received from a lawful third party.

            Confidential Information excludes residual knowledge retained in unaided memory.
            """
        )

        confidential_information = next(
            clause for clause in result["clauses"] if clause["id"] == "confidential_information"
        )
        self.assertEqual(confidential_information["status"], "check")
        self.assertFalse(confidential_information["passes"])
        self.assertEqual(confidential_information["matched_paragraph_ids"], ["p5"])

    def test_prohibited_language_has_remove_details(self):
        result = review_nda(
            """
            The Recipient must not circumvent the Company or deal directly with introduced parties.
            """
        )

        non_circumvention = next(clause for clause in result["clauses"] if clause["id"] == "non_circumvention")
        self.assertEqual(non_circumvention["status"], "check")
        self.assertFalse(non_circumvention["passes"])
        self.assertEqual(non_circumvention["issue_type"], "present_but_wrong")
        self.assertIn("Remove non-circumvention", non_circumvention["what_to_fix"])
        redline = self.redline_for_clause(result, "non_circumvention")
        self.assertEqual(len(self.redlines_for_clause(result, "non_circumvention")), 1)
        self.assertEqual(redline["clause_id"], "non_circumvention")
        self.assertEqual(redline["action"], "delete_paragraph")
        self.assertEqual(redline["action_label"], "Remove paragraph")
        self.assertEqual(redline["paragraph_id"], "p1")
        self.assertEqual(
            redline["original_text"],
            "The Recipient must not circumvent the Company or deal directly with introduced parties.",
        )
        self.assertEqual(redline["replacement_text"], "")

    def test_non_circumvention_redlines_each_detected_paragraph(self):
        result = review_nda(
            """
            The Recipient must not circumvent the Company.

            The Recipient must not engage in exclusive dealing with introduced parties.
            """
        )

        non_circumvention_redlines = [
            edit for edit in result["redline_edits"] if edit["clause_id"] == "non_circumvention"
        ]
        self.assertEqual([edit["action"] for edit in non_circumvention_redlines], ["delete_paragraph", "delete_paragraph"])
        self.assertEqual([edit["paragraph_id"] for edit in non_circumvention_redlines], ["p1", "p2"])

    def test_approved_laws_are_read_from_playbook(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"] = ["DIFC"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement shall be governed by the laws of Delaware.")

        governing_law_result = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law_result["status"], "check")
        self.assertFalse(governing_law_result["passes"])
        self.assertEqual(governing_law_result["what_to_fix"], "Change the governing law to DIFC.")
        self.assertNotIn("Delaware", governing_law_result["what_to_fix"])

    def test_governing_law_redline_uses_preferred_playbook_law(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"] = ["DIFC"]
        governing_law["preferred_law"] = "DIFC"

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement shall be governed by the laws of California.")

        redline = self.redline_for_clause(result, "governing_law")
        self.assertEqual(redline["replacement_text"], "This Agreement shall be governed by the laws of the DIFC.")
        self.assertEqual([option["label"] for option in redline["template_options"]], ["DIFC"])

    def test_governing_law_phrase_comes_from_playbook(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["approved_laws"] = ["DIFC"]
        governing_law["preferred_law"] = "DIFC"
        governing_law["law_phrases"] = {"DIFC": "DIFC"}

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement shall be governed by the laws of California.")

        redline = self.redline_for_clause(result, "governing_law")
        self.assertEqual(redline["replacement_text"], "This Agreement shall be governed by the laws of DIFC.")

    def test_governing_law_anchor_terms_come_from_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        governing_law = next(clause for clause in playbook["clauses"] if clause["id"] == "governing_law")
        governing_law["search_terms"] = ["subject to"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement is subject to California.")

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1"])

    def test_term_context_terms_come_from_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["search_terms"] = ["hold period"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("The hold period is seven (7) years.")

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1"])

    def test_non_circumvention_terms_come_from_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        non_circumvention = next(clause for clause in playbook["clauses"] if clause["id"] == "non_circumvention")
        non_circumvention["search_terms"] = ["direct approach"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("The Recipient shall not make a direct approach to introduced customers.")

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "non_circumvention")
        self.assertEqual(result_clause["status"], "check")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1"])

    def test_signature_markers_come_from_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        signatures = next(clause for clause in playbook["clauses"] if clause["id"] == "signatures")
        signatures["search_terms"] = ["signed by:", "role:", "signed date:"]

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda(
                """
                For Party One Ltd
                Signed By: __________________
                Role: Director
                Signed Date: 2026-05-30

                For Party Two Ltd
                Signed By: __________________
                Role: CEO
                Signed Date: 2026-05-30
                """
            )

        result_clause = next(clause for clause in result["clauses"] if clause["id"] == "signatures")
        self.assertEqual(result_clause["status"], "match")
        self.assertEqual(result_clause["matched_paragraph_ids"], ["p1", "p2"])

    def test_missing_governing_law_creates_insert_redline_with_jurisdiction_options(self):
        result = review_nda("The parties will discuss a possible transaction.")

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["status"], "not_present")
        redline = self.redline_for_clause(result, "governing_law")
        self.assertEqual(redline["action"], "insert_after_paragraph")
        self.assertEqual(redline["paragraph_id"], "p1")
        self.assertEqual(redline["anchor_text"], "The parties will discuss a possible transaction.")
        self.assertEqual(redline["insert_text"], "This Agreement shall be governed by the laws of England and Wales.")
        self.assertEqual(
            [option["label"] for option in redline["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC"],
        )

    def test_missing_signatures_creates_insert_redline_at_end(self):
        result = review_nda(
            """
            This Agreement shall be governed by the laws of the DIFC.

            The confidentiality obligations survive for three (3) years.
            """
        )

        signatures = next(clause for clause in result["clauses"] if clause["id"] == "signatures")
        self.assertEqual(signatures["status"], "not_present")
        redline = self.redline_for_clause(result, "signatures")
        self.assertEqual(redline["action"], "insert_after_paragraph")
        self.assertEqual(redline["paragraph_id"], "p2")
        self.assertIn("For [Party 1 legal name]", redline["insert_text"])
        self.assertIn("For [Party 2 legal name]", redline["insert_text"])
        self.assertIn("Title:", redline["insert_text"])
        self.assertIn("Date:", redline["insert_text"])

    def test_signature_redline_template_comes_from_playbook(self):
        playbook = deepcopy(load_playbook())
        signatures = next(clause for clause in playbook["clauses"] if clause["id"] == "signatures")
        signatures["redline_template"] = "Signed for Party A\nBy:\nDate:\n\nSigned for Party B\nBy:\nDate:"

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda("This Agreement shall be governed by the laws of the DIFC.")

        redline = self.redline_for_clause(result, "signatures")
        self.assertIn("Signed for Party A", redline["insert_text"])
        self.assertIn("Signed for Party B", redline["insert_text"])
        self.assertNotIn("[Party 1 legal name]", redline["insert_text"])

    def test_prohibited_clause_polarity_comes_from_playbook_type(self):
        playbook = deepcopy(load_playbook())
        non_circumvention = next(clause for clause in playbook["clauses"] if clause["id"] == "non_circumvention")
        non_circumvention["type"] = "required"

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            result = review_nda((ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8"))

        non_circumvention_result = next(clause for clause in result["clauses"] if clause["id"] == "non_circumvention")
        self.assertEqual(non_circumvention_result["status"], "not_present")
        self.assertFalse(non_circumvention_result["passes"])

    def test_result_contract_stays_hard_clause_only(self):
        result = review_nda((ROOT / "samples" / "fail-nda.txt").read_text(encoding="utf-8"))
        encoded = json.dumps(result).lower()

        for clause in result["clauses"]:
            self.assertIn("issue_type", clause)
            self.assertIn("issue_label", clause)
            self.assertIn("what_to_fix", clause)
        self.assertIn("redline_edits", result)
        self.assertNotIn("sc" + "ore", encoded)
        self.assertNotIn("esca" + "late", encoded)


if __name__ == "__main__":
    unittest.main()
