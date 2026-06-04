import unittest

from nda_automation.ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_CLAUSE_ASSESSMENT_SCHEMA,
    AI_REDLINE_NO_CHANGE,
    AIAssessmentContractError,
    validate_ai_clause_assessments,
)
from nda_automation.checker import load_playbook
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import split_document_paragraphs


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    "This Agreement shall be governed by the laws of California.",
])


def _valid_clause_ids():
    return [clause["id"] for clause in load_playbook()["clauses"]]


def _paragraphs():
    return split_document_paragraphs(SOURCE_TEXT)


def _valid_assessment(**overrides):
    assessment = {
        "clause_id": "governing_law",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "rationale": "California is not an approved governing law for this playbook.",
        "why_it_might_be_a_problem": "The clause uses a jurisdiction outside the approved set.",
        "why_it_may_be_fine": "None.",
        "evidence": [{
            "quote": "laws of california",
            "relevance": "Shows the governing-law jurisdiction.",
        }],
        "proposed_redline": {
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "text": "This Agreement shall be governed by the laws of England and Wales.",
            "jurisdiction": "England and Wales",
        },
        "confidence": 0.94,
        "blocks_send": False,
    }
    assessment.update(overrides)
    return assessment


class AIAssessmentContractTests(unittest.TestCase):
    def test_schema_freezes_required_ai_clause_assessment_fields(self):
        self.assertEqual(AI_CLAUSE_ASSESSMENT_SCHEMA["properties"]["schema_version"]["const"], AI_ASSESSMENT_CONTRACT_VERSION)
        self.assertIn("rationale", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertIn("why_it_might_be_a_problem", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertIn("why_it_may_be_fine", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertIn("proposed_redline", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertEqual(
            AI_CLAUSE_ASSESSMENT_SCHEMA["properties"]["decision"]["enum"],
            ["pass", "fail", "review"],
        )

    def test_valid_assessment_is_normalized_and_quote_evidence_is_resolved(self):
        assessments = validate_ai_clause_assessments(
            [_valid_assessment()],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        governing_law = assessments["governing_law"]
        self.assertEqual(governing_law["schema_version"], AI_ASSESSMENT_CONTRACT_VERSION)
        self.assertEqual(governing_law["evidence"], [{
            "paragraph_id": "p2",
            "quote": "laws of california",
            "relevance": "Shows the governing-law jurisdiction.",
        }])
        self.assertEqual(governing_law["proposed_redline"]["action"], REDLINE_REPLACE_PARAGRAPH)

    def test_pass_assessment_requires_no_change_redline_and_none_issue_type(self):
        assessments = validate_ai_clause_assessments(
            [{
                "clause_id": "mutuality",
                "decision": "pass",
                "issue_type": "none",
                "rationale": "Mutual disclosure language is present.",
                "why_it_might_be_a_problem": "None.",
                "why_it_may_be_fine": "The clause is reciprocal.",
                "evidence": [{
                    "paragraph_id": "p1",
                    "quote": "Each party may disclose Confidential Information",
                    "relevance": "Shows reciprocal disclosure.",
                }],
                "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
                "confidence": 0.9,
                "blocks_send": False,
            }],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        self.assertEqual(assessments["mutuality"]["proposed_redline"], {"action": AI_REDLINE_NO_CHANGE})

    def test_absence_based_pass_can_have_empty_evidence(self):
        assessments = validate_ai_clause_assessments(
            [{
                "clause_id": "non_circumvention",
                "decision": "pass",
                "issue_type": "none",
                "rationale": "No non-circumvention or substitute-purpose restriction was found.",
                "why_it_might_be_a_problem": "None.",
                "why_it_may_be_fine": "The prohibited restraint appears absent from the supplied text.",
                "evidence": [],
                "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
                "confidence": 0.82,
                "blocks_send": False,
            }],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        self.assertEqual(assessments["non_circumvention"]["evidence"], [])

    def test_invalid_assessment_reports_contract_errors(self):
        bad = _valid_assessment(
            rationale="",
            issue_type="none",
            evidence=[{"paragraph_id": "p2", "quote": "laws of France", "relevance": "Wrong quote."}],
            proposed_redline={"action": AI_REDLINE_NO_CHANGE},
            blocks_send=True,
        )

        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments([bad], valid_clause_ids=_valid_clause_ids(), paragraphs=_paragraphs())

        message = str(error.exception)
        self.assertIn("rationale must be non-empty text", message)
        self.assertIn("fail/review decisions must not use issue_type none", message)
        self.assertIn("quote does not appear in paragraph p2", message)
        self.assertIn("fail decisions require a proposed redline action", message)
        self.assertIn("blocks_send must be true only for review decisions", message)

    def test_duplicate_and_unknown_clause_ids_are_invalid(self):
        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                [_valid_assessment(), _valid_assessment(), _valid_assessment(clause_id="unknown_clause")],
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=_paragraphs(),
            )

        message = str(error.exception)
        self.assertIn("duplicate assessment for clause governing_law", message)
        self.assertIn("unknown clause_id unknown_clause", message)


if __name__ == "__main__":
    unittest.main()
