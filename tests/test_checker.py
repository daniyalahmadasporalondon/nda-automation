import json
import unittest
from pathlib import Path

from nda_automation.checker import review_nda

ROOT = Path(__file__).resolve().parent.parent


class CheckerTests(unittest.TestCase):
    def test_pass_sample_meets_requirements(self):
        result = review_nda((ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8"))

        self.assertEqual(result["overall_status"], "meets_requirements")
        self.assertEqual(result["requirements_failed"], 0)
        self.assertTrue(all(clause["status"] == "pass" for clause in result["clauses"]))

    def test_fail_sample_does_not_meet_requirements(self):
        result = review_nda((ROOT / "samples" / "fail-nda.txt").read_text(encoding="utf-8"))

        self.assertEqual(result["overall_status"], "does_not_meet_requirements")
        self.assertGreater(result["requirements_failed"], 0)
        failed_clause_ids = {clause["id"] for clause in result["clauses"] if clause["status"] == "fail"}
        self.assertIn("governing_law", failed_clause_ids)
        self.assertIn("non_circumvention", failed_clause_ids)

    def test_term_and_survival_allows_less_than_five_years(self):
        text = (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")
        result = review_nda(text)

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "pass")

    def test_term_and_survival_picks_up_period_of_two_years(self):
        text = """
        This Agreement shall continue for a period of two (2) years.
        The undertakings set out in this Agreement will survive for a further period of two (2) years.
        """
        result = review_nda(text)

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "pass")
        self.assertIn("within the five-year cap", term_clause["finding"])

    def test_term_and_survival_rejects_more_than_five_years(self):
        text = (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")
        result = review_nda(text.replace("three (3) years", "seven (7) years"))

        term_clause = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")
        self.assertEqual(term_clause["status"], "fail")

    def test_result_contract_stays_hard_clause_only(self):
        result = review_nda((ROOT / "samples" / "fail-nda.txt").read_text(encoding="utf-8"))
        encoded = json.dumps(result).lower()

        self.assertNotIn("sc" + "ore", encoded)
        self.assertNotIn("esca" + "late", encoded)


if __name__ == "__main__":
    unittest.main()
