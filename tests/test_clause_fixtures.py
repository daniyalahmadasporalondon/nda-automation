import unittest

from nda_automation.checker import review_nda


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
        "non_circumvention_pass_absent",
        "non_circumvention",
        "not_present",
        "The parties will use Confidential Information solely to evaluate a potential commercial relationship.",
    ),
    (
        "non_circumvention_check_present",
        "non_circumvention",
        "check",
        "The Recipient must not circumvent the Company or deal directly with introduced parties.",
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


if __name__ == "__main__":
    unittest.main()
