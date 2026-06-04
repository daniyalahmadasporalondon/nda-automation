import unittest
import json
from unittest.mock import patch

from nda_automation.ai_assessment_prompt import AI_ASSESSMENT_RESPONSE_SCHEMA, AI_ASSESSMENT_TASK
from nda_automation.ai_assessor import (
    AI_FIRST_ASSESSOR_MODE,
    AIAssessorError,
    InMemoryAssessmentReviewer,
    alibaba_ai_assessment_request_body,
    assess_nda_with_ai,
    gemini_ai_assessment_request_body,
    openrouter_ai_assessment_request_body,
)
from nda_automation.gemini_schema import GEMINI_UNSUPPORTED_SCHEMA_KEYS
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, supplier, pricing, market, product, proprietary and trade secret information disclosed by either party.',
    "This Agreement shall be governed by the laws of California.",
    "The confidentiality obligations survive for a fixed period of five years.",
    "Each party remains free to deal with third parties outside the Purpose.",
    "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
])


def _assessment(clause_id, decision, *, paragraph_id="", quote="", issue_type=None, proposed_redline=None):
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "present_but_wrong"
    evidence = []
    if paragraph_id and quote:
        evidence.append({
            "paragraph_id": paragraph_id,
            "quote": quote,
            "relevance": "Supports the AI-first verdict.",
        })
    return {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed by the AI-first assessor.",
        "why_it_might_be_a_problem": "None." if decision == "pass" else "This clause may not satisfy the playbook.",
        "why_it_may_be_fine": "None.",
        "evidence": evidence,
        "proposed_redline": proposed_redline or {"action": "no_change"},
        "confidence": 0.91,
        "blocks_send": decision == "review",
    }


def _complete_response():
    return {
        "assessments": [
            _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            _assessment("confidential_information", "pass", paragraph_id="p2", quote='"Confidential Information" means non-public business'),
            _assessment(
                "governing_law",
                "fail",
                paragraph_id="p3",
                quote="laws of California",
                proposed_redline={
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p3",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                    "jurisdiction": "England and Wales",
                },
            ),
            _assessment("term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"),
            _assessment("non_circumvention", "pass"),
            _assessment("signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"),
        ],
    }


class AIAssessorTests(unittest.TestCase):
    def test_ai_first_assessor_builds_existing_review_result_contract(self):
        reviewer = InMemoryAssessmentReviewer(response=_complete_response())

        result = assess_nda_with_ai(
            SOURCE_TEXT,
            reviewer=reviewer,
            checked_at="2026-06-04T00:00:00+00:00",
        )

        self.assertEqual(len(reviewer.packets), 1)
        packet = reviewer.packets[0]
        self.assertEqual(packet["task"], AI_ASSESSMENT_TASK)
        self.assertIn("playbook", packet)
        self.assertIn("output_contract", packet)
        self.assertEqual(result["checked_at"], "2026-06-04T00:00:00+00:00")
        self.assertEqual(result["ai_first_review"]["status"], "completed")
        self.assertEqual(result["ai_first_review"]["mode"], AI_FIRST_ASSESSOR_MODE)
        self.assertEqual(result["ai_first_review"]["record_count"], 6)
        self.assertEqual(result["requirements_failed"], 1)
        self.assertEqual(result["requirements_needs_review"], 0)
        self.assertEqual(result["requirements_passed"], 5)

        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["decision"], "fail")
        self.assertEqual(governing_law["decision_source"], "ai")
        self.assertEqual(governing_law["matched_paragraph_ids"], ["p3"])
        redline = next(edit for edit in result["redline_edits"] if edit["clause_id"] == "governing_law")
        self.assertEqual(redline["action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertEqual(redline["paragraph_id"], "p3")

    def test_ai_first_assessor_partial_response_fails_missing_clauses_to_review(self):
        reviewer = InMemoryAssessmentReviewer(response={
            "assessments": [
                _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            ],
        })

        result = assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

        self.assertEqual(result["ai_first_review"]["status"], "partial")
        self.assertIn("governing_law", result["ai_first_review"]["missing_clause_ids"])
        self.assertEqual(result["review_state"]["state"], "review")
        self.assertTrue(result["review_state"]["blocks_send"])
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(governing_law["decision"], "review")
        self.assertEqual(governing_law["reason_code"], "ai_first_missing_assessment")

    def test_ai_first_assessor_rejects_invalid_ai_response(self):
        reviewer = InMemoryAssessmentReviewer(response={
            "assessments": [
                _assessment("mutuality", "pass", paragraph_id="p1", quote="quote that is not present"),
            ],
        })

        with self.assertRaisesRegex(AIAssessorError, "quote does not appear in paragraph p1"):
            assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

    def test_ai_first_assessor_rejects_bad_response_envelope(self):
        reviewer = InMemoryAssessmentReviewer(response={"assessments": [], "extra": "not allowed"})

        with self.assertRaisesRegex(AIAssessorError, "unsupported response field extra"):
            assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

    def test_ai_first_assessor_disabled_without_injected_reviewer(self):
        with patch("nda_automation.ai_assessor._ai_review_settings", return_value={
            "enabled": False,
            "provider": "gemini",
            "model": "gemini-3.5-flash",
            "timeout_seconds": 20,
        }):
            with self.assertRaisesRegex(AIAssessorError, "disabled"):
                assess_nda_with_ai(SOURCE_TEXT)

    def test_provider_request_bodies_use_ai_first_assessment_schema(self):
        reviewer = InMemoryAssessmentReviewer(response=_complete_response())
        result = assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)
        packet = reviewer.packets[0]

        gemini_body = gemini_ai_assessment_request_body(packet)
        self.assertNotEqual(gemini_body["generationConfig"]["responseSchema"], AI_ASSESSMENT_RESPONSE_SCHEMA)
        self.assertEqual(gemini_body["generationConfig"]["responseMimeType"], "application/json")
        gemini_schema_json = json.dumps(gemini_body["generationConfig"]["responseSchema"])
        for unsupported_key in GEMINI_UNSUPPORTED_SCHEMA_KEYS:
            self.assertNotIn(f'"{unsupported_key}"', gemini_schema_json)
        self.assertIn('"minimum"', gemini_schema_json)
        self.assertIn('"maximum"', gemini_schema_json)
        self.assertIn('"additionalProperties"', gemini_schema_json)

        openrouter_body = openrouter_ai_assessment_request_body(packet, "openai/gpt-4o-mini")
        self.assertEqual(openrouter_body["response_format"]["json_schema"]["schema"], AI_ASSESSMENT_RESPONSE_SCHEMA)
        self.assertEqual(openrouter_body["response_format"]["json_schema"]["name"], "nda_ai_first_clause_assessment")

        alibaba_body = alibaba_ai_assessment_request_body(packet, "qwen3.5-plus")
        self.assertIn("Schema:", alibaba_body["messages"][0]["content"])
        self.assertIn("assessments", alibaba_body["messages"][0]["content"])
        self.assertEqual(result["ai_first_review"]["mode"], AI_FIRST_ASSESSOR_MODE)


if __name__ == "__main__":
    unittest.main()
