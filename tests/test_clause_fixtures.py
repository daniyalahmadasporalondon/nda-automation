import json
import unittest
from pathlib import Path

from nda_automation.checker import review_nda


ROOT = Path(__file__).resolve().parent.parent
TARGETED_REGRESSION_FIXTURES_PATH = ROOT / "tests" / "fixtures" / "targeted_clause_regressions.json"

CLAUSE_FIXTURES = [
    (
        "mutuality_pass_each_party",
        "mutuality",
        "match",
        "Each party may disclose Confidential Information and each party acts as both a Disclosing Party and Receiving Party.",
    ),
    (
        "mutuality_check_unilateral",
        "mutuality",
        "check",
        "This one-way NDA requires only the Receiving Party to protect the Company's information.",
    ),
    (
        "confidential_information_pass_broad_standard_exclusions",
        "confidential_information",
        "match",
        """
        Confidential Information means any and all non-public business, financial, technical,
        customer, supplier, pricing, market, proprietary and trade secret information disclosed by either party.

        Confidential Information does not include information that is public, already known,
        received from a lawful third party, or independently developed without use of the Confidential Information.
        """,
    ),
    (
        "confidential_information_check_residuals",
        "confidential_information",
        "check",
        """
        Confidential Information means any and all non-public business, financial, technical,
        customer, supplier, pricing, market, proprietary and trade secret information disclosed by either party.

        Confidential Information excludes residual knowledge retained in unaided memory.
        """,
    ),
    (
        "governing_law_pass_difc",
        "governing_law",
        "match",
        "This Agreement shall be governed by the laws of the DIFC.",
    ),
    (
        "governing_law_check_california",
        "governing_law",
        "check",
        "This Agreement shall be governed by the laws of California.",
    ),
    (
        "term_and_survival_pass_three_years",
        "term_and_survival",
        "match",
        "The confidentiality obligations survive for three (3) years.",
    ),
    (
        "term_and_survival_check_seven_years",
        "term_and_survival",
        "check",
        "The confidentiality obligations survive for seven (7) years.",
    ),
    (
        "signatures_pass_two_party_execution_blocks",
        "signatures",
        "match",
        """
        For Aspora Ltd
        By: A. Signatory
        Title: Director
        Date: 2026-05-30

        For Counterparty Ltd
        By: B. Signatory
        Title: CEO
        Date: 2026-05-30
        """,
    ),
    (
        "signatures_check_one_party_only",
        "signatures",
        "check",
        """
        For Aspora Ltd
        By: A. Signatory
        Title: Director
        Date: 2026-05-30
        """,
    ),
    (
        "signatures_not_present_for_ordinary_signature_reference",
        "signatures",
        "not_present",
        "The parties agree that electronic signatures may be used for this Agreement.",
    ),
]


class ClauseFixtureTests(unittest.TestCase):
    def test_clause_fixtures_hold_expected_statuses(self):
        for name, clause_id, expected_status, text in CLAUSE_FIXTURES:
            with self.subTest(name=name):
                result = review_nda(text)
                clause = next(item for item in result["clauses"] if item["id"] == clause_id)

                self.assertEqual(clause["status"], expected_status)

    def test_targeted_clause_regression_fixtures_hold_expected_decisions(self):
        for fixture in _load_targeted_regression_fixtures():
            with self.subTest(name=fixture["name"]):
                result = review_nda(fixture["text"])
                clause = next(item for item in result["clauses"] if item["id"] == fixture["clause_id"])
                expected = fixture["expected"]

                self.assertEqual(clause["status"], expected["status"])
                self.assertEqual(clause["decision"], expected["decision"])
                self.assertEqual(clause["reason_code"], expected["reason_code"])
                self.assertEqual(clause["review_state"]["reason_code"], expected["reason_code"])
                self.assertEqual(clause["audit_trace"]["reason_code"], expected["reason_code"])
                self.assertIn(expected["reason_code"], clause["reason_codes"])
                self.assertIn(expected["reason_code"], clause["review_state"]["reason_codes"])
                self.assertIn(expected["reason_code"], clause["audit_trace"]["reason_codes"])

                for record in clause["structured_evidence"]:
                    self.assertEqual(record["reason_code"], expected["reason_code"])
                    self.assertIn(expected["reason_code"], record["reason_codes"])

                if "redline_count" in expected:
                    redlines = [edit for edit in result["redline_edits"] if edit["clause_id"] == fixture["clause_id"]]
                    self.assertEqual(len(redlines), expected["redline_count"])

                for path, expected_value in expected.get("analysis", {}).items():
                    self.assertEqual(_path_value(clause, path), expected_value, path)

                for path in expected.get("analysis_absent", []):
                    self.assertIsNone(_path_value(clause, path), path)


def _load_targeted_regression_fixtures():
    with TARGETED_REGRESSION_FIXTURES_PATH.open(encoding="utf-8") as handle:
        fixtures = json.load(handle)
    if not isinstance(fixtures, list):
        raise AssertionError("targeted clause regression fixtures must be a list")
    return fixtures


def _path_value(value, path):
    current = value
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return None
            current = current[segment]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


if __name__ == "__main__":
    unittest.main()
