import unittest
import json
from unittest.mock import MagicMock, patch

from nda_automation.ai_assessment_prompt import AI_ASSESSMENT_TASK
from nda_automation.ai_assessor import (
    AI_FIRST_ASSESSOR_MODE,
    AIAssessorError,
    InMemoryAssessmentReviewer,
    OpenRouterAIAssessmentReviewer,
    assess_nda_with_ai,
    configured_ai_assessment_reviewer,
    openrouter_ai_assessment_request_body,
)
from nda_automation.ai_review import DEFAULT_OPENROUTER_MODEL
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
        "rationale": f"{clause_id} assessed by the AI-first assessor using the playbook and cited evidence.",
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
            "provider": "openrouter",
            "model": DEFAULT_OPENROUTER_MODEL,
            "timeout_seconds": 20,
        }):
            with self.assertRaisesRegex(AIAssessorError, "disabled"):
                assess_nda_with_ai(SOURCE_TEXT)

    def test_provider_request_bodies_use_ai_first_assessment_schema(self):
        reviewer = InMemoryAssessmentReviewer(response=_complete_response())
        result = assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)
        packet = reviewer.packets[0]

        body = openrouter_ai_assessment_request_body(packet, model=DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["model"], DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertIn(AI_ASSESSMENT_TASK, json.dumps(body))
        self.assertIn("assessments", json.dumps(body))

        self.assertEqual(result["ai_first_review"]["mode"], AI_FIRST_ASSESSOR_MODE)


class AIAssessorProviderAdapterTests(unittest.TestCase):
    def test_openrouter_request_body_uses_ai_first_prompt_and_json_mode(self):
        reviewer = InMemoryAssessmentReviewer(response=_complete_response())
        assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)
        packet = reviewer.packets[0]

        body = openrouter_ai_assessment_request_body(packet, model=DEFAULT_OPENROUTER_MODEL)

        self.assertEqual(body["model"], DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertIn(AI_ASSESSMENT_TASK, json.dumps(body))
        self.assertIn("assessments", json.dumps(body))

    def test_openrouter_adapter_round_trip(self):
        captured = []
        response = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps(_complete_response()),
                },
            }],
        }).encode("utf-8")

        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            reviewer = OpenRouterAIAssessmentReviewer(api_key="ork")
            result = reviewer({"task": AI_ASSESSMENT_TASK, "paragraphs": []})

        self.assertEqual(result, _complete_response())
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.headers["Authorization"], "Bearer ork")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_configured_reviewer_builds_openrouter_gemini(self):
        with (
            patch("nda_automation.ai_assessor._configured_api_key", side_effect=lambda provider: f"{provider}-key"),
        ):
            reviewer = configured_ai_assessment_reviewer({
                "enabled": True,
                "provider": "openrouter",
                "model": DEFAULT_OPENROUTER_MODEL,
                "timeout_seconds": 20,
            })

        self.assertIsInstance(reviewer, OpenRouterAIAssessmentReviewer)
        self.assertEqual(reviewer.model, DEFAULT_OPENROUTER_MODEL)

    def test_configured_reviewer_rejects_old_provider(self):
        with self.assertRaisesRegex(AIAssessorError, "Unsupported AI provider: legacy"):
            configured_ai_assessment_reviewer({
                "enabled": True,
                "provider": "legacy",
                "model": "legacy-model",
                "timeout_seconds": 20,
            })


def _mock_urlopen(response_bytes, captured_requests):
    def urlopen(request, *args, **kwargs):
        captured_requests.append(request)
        context_manager = MagicMock()
        context_manager.__enter__.return_value.read.return_value = response_bytes
        context_manager.__exit__.return_value = False
        return context_manager

    return urlopen


if __name__ == "__main__":
    unittest.main()
