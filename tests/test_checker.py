import json
import unittest
from pathlib import Path

from nda_automation.checker import review_nda, split_document_paragraphs

ROOT = Path(__file__).resolve().parent.parent


class CheckerTests(unittest.TestCase):
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

    def test_term_and_survival_picks_up_period_of_two_years(self):
        text = """
        This Agreement shall continue for a period of two (2) years.
        The undertakings set out in this Agreement will survive for a further period of two (2) years.
        """
        result = review_nda(text)

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "match")
        self.assertIn("within the five-year cap", term_clause["finding"])

    def test_term_and_survival_rejects_more_than_five_years(self):
        text = (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")
        result = review_nda(text.replace("three (3) years", "seven (7) years"))

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "check")
        self.assertFalse(term_clause["passes"])

    def test_returns_numbered_paragraph_model(self):
        paragraphs = split_document_paragraphs("First paragraph.\n\nSecond paragraph.")

        self.assertEqual(
            paragraphs,
            [
                {"id": "p1", "index": 1, "text": "First paragraph.", "start": 0, "end": 16},
                {"id": "p2", "index": 2, "text": "Second paragraph.", "start": 18, "end": 35},
            ],
        )

    def test_clause_results_include_exact_evidence_paragraphs(self):
        result = review_nda(
            """
            Mutual Non-Disclosure Agreement

            This Agreement shall be governed in all respects by the laws of the DIFC.
            """
        )

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["status"], "match")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p2"])
        self.assertEqual(
            governing_law["matched_text"],
            "This Agreement shall be governed in all respects by the laws of the DIFC.",
        )
        self.assertEqual(governing_law["reason"], "Approved governing law found.")

    def test_prohibited_clause_can_pass_as_not_present(self):
        result = review_nda((ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8"))

        non_circumvention = next(clause for clause in result["clauses"] if clause["id"] == "non_circumvention")
        self.assertEqual(non_circumvention["status"], "not_present")
        self.assertTrue(non_circumvention["passes"])
        self.assertEqual(non_circumvention["matched_paragraph_ids"], [])

    def test_result_contract_stays_hard_clause_only(self):
        result = review_nda((ROOT / "samples" / "fail-nda.txt").read_text(encoding="utf-8"))
        encoded = json.dumps(result).lower()

        self.assertNotIn("sc" + "ore", encoded)
        self.assertNotIn("esca" + "late", encoded)


if __name__ == "__main__":
    unittest.main()
