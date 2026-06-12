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


# Source text with an approved governing law (England and Wales) used by the
# truncation-guard tests.  California (SOURCE_TEXT p3) triggers the deterministic
# governing-law backstop, so a genuinely all-pass scenario requires an approved
# jurisdiction in the text.
ALL_PASS_SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, supplier, pricing, market, product, proprietary and trade secret information disclosed by either party.',
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for a fixed period of five years.",
    "Each party remains free to deal with third parties outside the Purpose.",
    "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
])


def _all_pass_response():
    # Every clause passes, each present-clause verdict grounded in a quote that
    # appears in ALL_PASS_SOURCE_TEXT so nothing is downgraded to review. Used to
    # prove a would-be clean clear is still escalated when the document is truncated.
    # NOTE: governing_law uses an approved jurisdiction (England and Wales) so the
    # deterministic backstop does not fire and the clause can legitimately pass.
    return {
        "assessments": [
            _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            _assessment("confidential_information", "pass", paragraph_id="p2", quote='"Confidential Information" means non-public business'),
            _assessment("governing_law", "pass", paragraph_id="p3", quote="laws of England and Wales"),
            _assessment("term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"),
            _assessment("non_circumvention", "pass"),
            _assessment("signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"),
        ],
    }


def _padded_source(source_text, *, filler_paragraphs):
    # Keep the real clause paragraphs at the front (so they fit the packet and
    # ground), then append enough filler paragraphs to push the document past
    # the packet budget and force truncation.
    filler = [f"Filler paragraph {index} with neutral boilerplate text." for index in range(filler_paragraphs)]
    return source_text + "\n\n" + "\n\n".join(filler)


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
        # The overall document blocks send (failed or needs review).
        self.assertTrue(result["review_state"]["blocks_send"])
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        # The deterministic governing-law backstop was removed once the primary AI
        # proved it reliably fails an unapproved jurisdiction on its own. With no AI
        # assessment for governing_law, the clause fails safe to review (the
        # missing-assessment default) and blocks the send — it is no longer
        # force-failed by a backstop.
        self.assertEqual(governing_law["decision"], "review")
        self.assertEqual(governing_law["review_state"]["state"], "review")
        self.assertTrue(governing_law["review_state"]["blocks_send"])

    def test_truncated_document_forces_manual_review_no_silent_clear(self):
        # A long document whose paragraphs exceed the packet budget is only
        # partially seen by the AI. Even when every assessed clause passes, the
        # unseen tail must force the whole document to manual review rather than
        # silently clear (the long-doc false-clear).
        # Uses ALL_PASS_SOURCE_TEXT (approved governing law) so the deterministic
        # backstop does not fire and the only escalation comes from truncation.
        long_source = _padded_source(ALL_PASS_SOURCE_TEXT, filler_paragraphs=200)
        reviewer = InMemoryAssessmentReviewer(response=_all_pass_response())

        result = assess_nda_with_ai(long_source, reviewer=reviewer)

        packet_document = reviewer.packets[0]["document"]
        self.assertTrue(packet_document["truncated"])
        self.assertGreater(packet_document["omitted_paragraph_count"], 0)

        self.assertTrue(result["truncation"]["truncated"])
        self.assertEqual(
            result["truncation"]["omitted_paragraph_count"],
            packet_document["omitted_paragraph_count"],
        )
        self.assertIn("manual review required", result["truncation"]["message"])
        self.assertIn("unreviewed", result["truncation"]["message"])

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(result["review_state"]["state"], "review")
        self.assertTrue(result["review_state"]["blocks_send"])
        self.assertTrue(result["review_state"]["truncation_forced_review"])
        self.assertEqual(result["ai_first_review"]["status"], "partial")
        self.assertTrue(result["ai_first_review"]["truncated"])

    def test_untruncated_document_is_not_escalated_by_truncation_guard(self):
        # The same all-pass response on a document that fits the budget keeps its
        # natural verdict -- the guard only fires on truncation.
        # Uses ALL_PASS_SOURCE_TEXT (approved governing law) so the deterministic
        # backstop does not fire and the untruncated result stays meets_requirements.
        reviewer = InMemoryAssessmentReviewer(response=_all_pass_response())

        result = assess_nda_with_ai(ALL_PASS_SOURCE_TEXT, reviewer=reviewer)

        self.assertFalse(reviewer.packets[0]["document"]["truncated"])
        self.assertFalse(result["truncation"]["truncated"])
        self.assertEqual(result["truncation"]["message"], "")
        self.assertNotIn("truncation_forced_review", result["review_state"])
        self.assertEqual(result["overall_status"], "meets_requirements")

    def test_truncation_does_not_soften_a_failing_document(self):
        # A truncated document that already fails must stay a fail (check), not be
        # softened to review; the guard only escalates a pass.
        long_source = _padded_source(SOURCE_TEXT, filler_paragraphs=200)
        reviewer = InMemoryAssessmentReviewer(response=_complete_response())

        result = assess_nda_with_ai(long_source, reviewer=reviewer)

        self.assertTrue(result["truncation"]["truncated"])
        self.assertEqual(result["review_state"]["state"], "check")
        self.assertEqual(result["overall_status"], "does_not_meet_requirements")
        self.assertTrue(result.get("truncation_blocks_send"))

    def test_ai_first_assessor_drops_ungroundable_quote_without_crashing(self):
        # An ungroundable quote (appears nowhere in the document) must NOT crash
        # the whole review -- the contract drops the fabricated evidence and the
        # review completes. The now-unsupported pass is downgraded to a blocking
        # human review by the evidence-grounding layer, never a silent pass.
        reviewer = InMemoryAssessmentReviewer(response={
            "assessments": [
                _assessment("mutuality", "pass", paragraph_id="p1", quote="quote that is not present"),
            ],
        })

        result = assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

        mutuality = next(c for c in result["clauses"] if c["id"] == "mutuality")
        self.assertEqual(mutuality["decision"], "review")
        self.assertNotEqual(mutuality["decision"], "pass")
        self.assertTrue(mutuality["blocks_send"])
        self.assertIn("ungrounded_finding", mutuality.get("reason_codes", []))

    def test_ai_first_assessor_rejects_nonexistent_paragraph_id(self):
        # A paragraph_id the model invented (no such reviewed paragraph) is a
        # structural error and still hard-rejects the response.
        reviewer = InMemoryAssessmentReviewer(response={
            "assessments": [
                _assessment("mutuality", "pass", paragraph_id="p999", quote="Each party may disclose Confidential Information"),
            ],
        })

        with self.assertRaisesRegex(AIAssessorError, "paragraph_id does not exist: p999"):
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

    def test_openrouter_reviewer_records_provenance_on_success(self):
        # BUGFIX (C1): last_success_provider/model must reflect the model that
        # actually produced the verdict, not just the configured settings.
        response = json.dumps({
            "choices": [{"message": {"content": json.dumps(_complete_response())}}],
        }).encode("utf-8")
        reviewer = OpenRouterAIAssessmentReviewer(api_key="ork", model="x-ai/grok-4.3")
        self.assertEqual((reviewer.last_success_provider, reviewer.last_success_model), ("", ""))
        with patch("urllib.request.urlopen", _mock_urlopen(response, [])):
            reviewer({"task": AI_ASSESSMENT_TASK, "paragraphs": []})
        self.assertEqual(reviewer.last_success_provider, "openrouter")
        self.assertEqual(reviewer.last_success_model, reviewer.model)

    def test_configured_reviewer_builds_openrouter_gemini(self):
        with (
            patch("nda_automation.ai_assessor._configured_api_key", side_effect=lambda provider: f"{provider}-key"),
            patch.dict("os.environ", {"NDA_AI_ASSESSMENT_STUB": ""}, clear=False),
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
        with patch.dict("os.environ", {"NDA_AI_ASSESSMENT_STUB": ""}, clear=False):
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
