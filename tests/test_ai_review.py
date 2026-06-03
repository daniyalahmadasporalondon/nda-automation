import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nda_automation import ai_review
from nda_automation.checker import (
    AIDraftValidationError,
    ai_second_opinion_for_clause,
    ai_validate_draft_fix,
    review_nda,
)

ROOT = Path(__file__).resolve().parent.parent


def _pass_sample_text():
    return (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")


def _first_citation(packet):
    paragraph = packet["paragraphs"][0]
    quote = str(paragraph["text"])[:80]
    return {
        "paragraph_id": paragraph["id"],
        "quote": quote,
        "relevance": "Supports the clause decision.",
    }


def _first_draft_citation(packet):
    paragraph = next(
        (item for item in packet["paragraphs"] if str(item["id"]).startswith(("draft-proposed-", "draft-action-"))),
        packet["paragraphs"][0],
    )
    quote = str(paragraph["text"])[:80]
    return {
        "paragraph_id": paragraph["id"],
        "quote": quote,
        "relevance": "Supports the draft-fix validation.",
    }


def _confirming_reviewer(packet):
    # The semantic packet is blind to Python, so the reviewer cannot read a
    # deterministic decision. These tests run on the all-pass sample, so an
    # independent reviewer that finds the clauses compliant returns "pass".
    return {
        "decision": "pass",
        "confidence": 0.93,
        "reason": "The supplied paragraphs satisfy the playbook requirement.",
        "cited_spans": [_first_citation(packet)],
        "issues": [],
        "suggested_fix": "",
    }


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {
                "NDA_AI_REVIEW_ENABLED": "",
                "NDA_AI_REVIEW_CLAUSES": "",
                "NDA_AI_REVIEW_THRESHOLD": "",
                "NDA_AI_PROVIDER": "gemini",
                "NDA_AI_MODEL": "gemini-3.5-flash",
                "ALIBABA_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
            },
            clear=False,
        )
        self.env_patch.start()
        self.ai_settings_patch = patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": None})
        self.ai_settings_patch.start()
        self.ai_key_patch = patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="")
        self.ai_key_patch.start()

    def tearDown(self):
        self.ai_key_patch.stop()
        self.ai_settings_patch.stop()
        self.env_patch.stop()

    def test_ai_review_is_disabled_by_default(self):
        result = review_nda(_pass_sample_text())

        self.assertEqual(result["ai_review"]["status"], "disabled")
        self.assertFalse(any("ai_review_analysis" in clause for clause in result["clauses"]))
        self.assertEqual(result["overall_status"], "meets_requirements")

    def test_ai_review_status_uses_persisted_toggle_over_environment(self):
        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": False}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value=""):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "GEMINI_API_KEY": "configured", "OPENROUTER_API_KEY": ""},
                    clear=False,
                ):
                    disabled_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value=""):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "GEMINI_API_KEY": "configured", "OPENROUTER_API_KEY": ""},
                    clear=False,
                ):
                    enabled_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="saved-local-key"):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "GEMINI_API_KEY": "", "OPENROUTER_API_KEY": ""},
                    clear=False,
                ):
                    local_key_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="sk-or-v1-test"):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "GEMINI_API_KEY": "", "OPENROUTER_API_KEY": ""},
                    clear=False,
                ):
                    openrouter_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="sk-ws-local-secret"):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "GEMINI_API_KEY": "", "OPENROUTER_API_KEY": "", "ALIBABA_API_KEY": ""},
                    clear=False,
                ):
                    alibaba_status = ai_review.ai_review_status()

        self.assertEqual(disabled_status["enabled"], False)
        self.assertEqual(disabled_status["stored_enabled"], False)
        self.assertEqual(disabled_status["environment_enabled"], True)
        self.assertEqual(disabled_status["api_key_configured"], True)
        self.assertEqual(disabled_status["api_key_source"], "environment")
        self.assertEqual(enabled_status["enabled"], True)
        self.assertEqual(enabled_status["stored_enabled"], True)
        self.assertEqual(enabled_status["environment_enabled"], False)
        self.assertEqual(enabled_status["api_key_source"], "environment")
        self.assertEqual(local_key_status["enabled"], True)
        self.assertEqual(local_key_status["api_key_configured"], True)
        self.assertEqual(local_key_status["api_key_source"], "local_settings")
        self.assertEqual(openrouter_status["provider"], "openrouter")
        self.assertEqual(openrouter_status["model"], "openai/gpt-4o-mini")
        self.assertEqual(openrouter_status["api_key_configured"], True)
        self.assertEqual(openrouter_status["api_key_source"], "local_settings")
        self.assertEqual(alibaba_status["provider"], "alibaba")
        self.assertEqual(alibaba_status["model"], "qwen3.5-plus")
        self.assertEqual(alibaba_status["api_key_configured"], True)
        self.assertEqual(alibaba_status["api_key_source"], "local_settings")

    def test_ai_review_can_confirm_deterministic_passes(self):
        result = review_nda(_pass_sample_text(), ai_reviewer=_confirming_reviewer)

        self.assertEqual(result["ai_review"]["status"], "completed")
        self.assertEqual(result["ai_review"]["record_count"], 5)
        self.assertEqual(result["overall_status"], "meets_requirements")
        reviewed = [clause for clause in result["clauses"] if clause.get("ai_review_analysis")]
        self.assertEqual(len(reviewed), 5)
        self.assertTrue(all(clause["ai_review_analysis"]["status"] == "confirmed" for clause in reviewed))

    def test_in_memory_reviewer_crosses_the_seam(self):
        # Injecting a real AIReviewer adapter (not mocking app_settings) exercises
        # the actual packet build (request shaping) and the verdict->arbiter path.
        def confirming(packet):
            paragraph = packet["paragraphs"][0]
            return {
                "decision": "pass",
                "confidence": 0.93,
                "reason": "Scripted in-memory confirmation.",
                "cited_spans": [{
                    "paragraph_id": paragraph["id"],
                    "quote": str(paragraph["text"])[:60],
                    "relevance": "Supports the decision.",
                }],
                "issues": [],
                "suggested_fix": "",
            }

        reviewer = ai_review.InMemoryReviewer(default=confirming)
        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)

        # The reviewer received real, blind packets through the seam.
        self.assertEqual(len(reviewer.packets), 5)
        for packet in reviewer.packets:
            self.assertEqual(packet["task"], "semantic_clause_crosscheck")
            self.assertNotIn("deterministic_result", packet)
            self.assertTrue(packet["paragraphs"])
        # The verdicts flowed through the arbiter to confirmed decisions.
        reviewed = [clause for clause in result["clauses"] if clause.get("ai_review_analysis")]
        self.assertEqual(len(reviewed), 5)
        self.assertTrue(all(clause["ai_review_analysis"]["status"] == "confirmed" for clause in reviewed))

    def test_in_memory_reviewer_scripts_per_clause_disagreement(self):
        def cite(packet):
            paragraph = packet["paragraphs"][0]
            return [{"paragraph_id": paragraph["id"], "quote": str(paragraph["text"])[:60], "relevance": "x"}]

        reviewer = ai_review.InMemoryReviewer(
            responses={
                "mutuality": lambda packet: {
                    "decision": "fail", "confidence": 0.9, "reason": "Looks one-way.",
                    "cited_spans": cite(packet), "issues": [], "suggested_fix": "",
                },
            },
            default=lambda packet: {
                "decision": "pass", "confidence": 0.92, "reason": "Fine.",
                "cited_spans": cite(packet), "issues": [], "suggested_fix": "",
            },
        )
        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)

        mutuality = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")
        self.assertEqual(mutuality["decision"], "review")
        self.assertEqual(mutuality["reason_code"], "ai_semantic_disagreement")
        self.assertEqual(governing_law["decision"], "pass")

    def test_semantic_packet_is_blind_to_python_decision(self):
        captured_packets = []

        def capturing_reviewer(packet):
            captured_packets.append(packet)
            return _confirming_reviewer(packet)

        review_nda(_pass_sample_text(), ai_reviewer=capturing_reviewer)

        self.assertEqual(len(captured_packets), 5)
        for packet in captured_packets:
            self.assertEqual(packet["task"], "semantic_clause_crosscheck")
            # Python's conclusion must never reach the semantic reviewer.
            self.assertNotIn("deterministic_result", packet)
            self.assertNotIn("analysis_objects", packet)
            self.assertFalse([key for key in packet if "deterministic" in key.lower()])
            encoded = json.dumps(packet)
            for leaked_field in ("issue_type", "what_to_fix", "matched_paragraph_ids", "needs_review"):
                self.assertNotIn(leaked_field, encoded)
            # Allowed context is still supplied so AI can decide independently.
            self.assertIn("clause", packet)
            self.assertIn("structure_context", packet)
            self.assertIn("instructions", packet)
            self.assertTrue(packet["paragraphs"])

    def test_ai_provider_error_does_not_override_deterministic_pass(self):
        def reviewer(_packet):
            raise RuntimeError("AI quota exhausted")

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")

        self.assertEqual(result["ai_review"]["status"], "completed")
        self.assertEqual(result["ai_review"]["record_count"], 5)
        self.assertTrue(all(record["status"] == "error" for record in result["ai_review"]["records"]))
        self.assertEqual(result["overall_status"], "meets_requirements")
        self.assertEqual(governing_law["decision"], "pass")
        self.assertEqual(governing_law["reason_code"], "approved_governing_law")
        self.assertEqual(governing_law["ai_review_analysis"]["status"], "error")
        self.assertIn("AI quota exhausted", governing_law["ai_review_analysis"]["reason"])

    def test_ai_provider_error_does_not_override_deterministic_fail(self):
        def reviewer(_packet):
            raise RuntimeError("AI provider unavailable")

        text = "This Agreement shall be governed by the laws of Wakanda."
        baseline = review_nda(text)
        baseline_gl = next(clause for clause in baseline["clauses"] if clause["id"] == "governing_law")
        # Guard the test: the deterministic decision here must not be a pass.
        self.assertNotEqual(baseline_gl["decision"], "pass")

        result = review_nda(text, ai_reviewer=reviewer)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")

        # An unavailable AI must fall back to the deterministic decision, never
        # downgrade a FAIL to review (or clear it).
        self.assertEqual(governing_law["decision"], baseline_gl["decision"])
        self.assertEqual(governing_law["reason_code"], baseline_gl["reason_code"])
        self.assertEqual(governing_law["ai_review_analysis"]["status"], "error")

    def test_ai_disagreement_does_not_soften_a_deterministic_fail(self):
        # Fail-floor: AI may escalate a pass to review, but it must never move a
        # deterministic FAIL off fail. The dissent is recorded, not acted on.
        def reviewer(packet):
            return {
                "decision": "pass",
                "confidence": 0.95,
                "reason": "AI thinks the failing clause is acceptable.",
                "cited_spans": [_first_citation(packet)],
                "issues": [],
                "suggested_fix": "",
            }

        result = review_nda("This Agreement shall be governed by the laws of California.", ai_reviewer=reviewer)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")

        self.assertEqual(governing_law["decision"], "fail")
        self.assertEqual(governing_law["decision_source"], "deterministic")
        self.assertEqual(governing_law["reason_code"], "unapproved_governing_law")
        self.assertEqual(governing_law["audit_trace"]["reason_code"], "unapproved_governing_law")
        # The AI disagreement is preserved for the reviewer, but did not soften the fail.
        self.assertEqual(governing_law["ai_review_analysis"]["status"], "disagreement")
        self.assertEqual(governing_law["ai_review_analysis"]["ai_decision"], "pass")
        self.assertTrue(governing_law["ai_review_analysis"]["disagreement"])

    def test_ai_disagreement_escalates_to_review_without_auto_redline(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "mutuality":
                return {
                    "decision": "fail",
                    "confidence": 0.91,
                    "reason": "The clause appears one-way.",
                    "cited_spans": [_first_citation(packet)],
                    "issues": ["possible_one_way_language"],
                    "suggested_fix": "Confirm whether obligations are reciprocal.",
                }
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        mutuality = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(mutuality["decision"], "review")
        self.assertEqual(mutuality["reason_code"], "ai_semantic_disagreement")
        self.assertTrue(mutuality["review_state"]["blocks_send"])
        self.assertEqual(mutuality["ai_review_analysis"]["ai_decision"], "fail")
        self.assertEqual(mutuality["ai_review_analysis"]["ai_reason"], "The clause appears one-way.")
        self.assertTrue(mutuality["ai_review_analysis"]["disagreement"])
        self.assertFalse([edit for edit in result["redline_edits"] if edit["clause_id"] == "mutuality"])

    def test_ai_low_confidence_escalates_to_review(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "governing_law":
                response = _confirming_reviewer(packet)
                response["confidence"] = 0.42
                return response
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(governing_law["decision"], "review")
        self.assertEqual(governing_law["reason_code"], "ai_confidence_below_threshold")
        self.assertIn("below the review threshold", governing_law["decision_reason"])

    def test_ai_invalid_citation_escalates_to_review(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "term_and_survival":
                return {
                    "decision": "pass",
                    "confidence": 0.9,
                    "reason": "The term is compliant.",
                    "cited_spans": [{
                        "paragraph_id": packet["paragraphs"][0]["id"],
                        "quote": "This quote does not exist in the paragraph.",
                        "relevance": "Invalid citation.",
                    }],
                    "issues": [],
                    "suggested_fix": "",
                }
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        term = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(term["decision"], "review")
        self.assertEqual(term["reason_code"], "ai_citation_validation_failed")
        self.assertTrue(term["ai_review_analysis"]["validation_errors"])

    def test_ai_second_opinion_targets_one_clause_and_updates_review_state(self):
        calls = []

        def reviewer(packet):
            calls.append(packet["clause"]["id"])
            return {
                "decision": "fail",
                "confidence": 0.91,
                "reason": "The mutuality language appears one-way.",
                "cited_spans": [_first_citation(packet)],
                "issues": ["possible_one_way_language"],
                "suggested_fix": "Confirm both parties are bound symmetrically.",
            }

        review_result = review_nda(_pass_sample_text())
        result = ai_second_opinion_for_clause(review_result, "mutuality", ai_reviewer=reviewer)

        self.assertEqual(calls, ["mutuality"])
        self.assertEqual(result["ai_review"]["mode"], "clause_second_opinion")
        self.assertEqual(result["ai_review"]["target_clause_id"], "mutuality")
        self.assertEqual(result["ai_review"]["record_count"], 1)
        self.assertEqual(result["clause"]["id"], "mutuality")
        self.assertEqual(result["clause"]["decision"], "review")
        self.assertEqual(result["clause"]["reason_code"], "ai_semantic_disagreement")
        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(result["review_state"]["counts"]["review"], 1)

    def test_ai_draft_fix_validation_checks_selected_redline(self):
        calls = []

        def reviewer(packet):
            calls.append({
                "task": packet["task"],
                "clause_id": packet["clause"]["id"],
                "redline_id": packet["proposed_draft"]["redline_id"],
            })
            return {
                "decision": "pass",
                "confidence": 0.94,
                "reason": "The proposed replacement uses an approved governing law.",
                "cited_spans": [_first_draft_citation(packet)],
                "issues": [],
                "suggested_fix": "",
            }

        review_result = review_nda("This Agreement shall be governed by the laws of California.")
        redline = next(edit for edit in review_result["redline_edits"] if edit["clause_id"] == "governing_law")
        result = ai_validate_draft_fix(review_result, "governing_law", redline, ai_reviewer=reviewer)

        self.assertEqual(calls, [{
            "task": "draft_fix_validation",
            "clause_id": "governing_law",
            "redline_id": redline["id"],
        }])
        self.assertEqual(result["clause_id"], "governing_law")
        self.assertEqual(result["redline_id"], redline["id"])
        self.assertEqual(result["ai_review"]["mode"], "draft_fix_validation")
        self.assertEqual(result["ai_review"]["record_count"], 1)
        self.assertEqual(result["validation"]["status"], "validated")
        self.assertEqual(result["validation"]["ai_decision"], "pass")
        self.assertEqual(result["validation"]["ai_confidence"], 0.94)

    def test_ai_draft_fix_validation_reports_disabled_ai(self):
        review_result = review_nda("This Agreement shall be governed by the laws of California.")
        redline = next(edit for edit in review_result["redline_edits"] if edit["clause_id"] == "governing_law")

        with self.assertRaises(AIDraftValidationError) as error:
            ai_validate_draft_fix(review_result, "governing_law", redline)

        self.assertEqual(error.exception.status, 409)
        self.assertIn("disabled", str(error.exception))

    def test_gemini_request_body_uses_structured_json_response_format(self):
        packet = {
            "task": "semantic_clause_crosscheck",
            "clause": {"id": "mutuality"},
            "paragraphs": [{"id": "p1", "text": "Each party is bound."}],
        }

        body = ai_review._gemini_request_body(packet)
        encoded = json.dumps(body)

        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertEqual(
            body["generationConfig"]["responseMimeType"],
            "application/json",
        )
        self.assertIn("responseSchema", body["generationConfig"])
        self.assertIn("semantic_clause_crosscheck", encoded)

    def test_openrouter_request_body_uses_chat_completion_structured_output(self):
        packet = {
            "task": "semantic_clause_crosscheck",
            "clause": {"id": "mutuality"},
            "paragraphs": [{"id": "p1", "text": "Each party is bound."}],
        }

        body = ai_review._openrouter_request_body(packet, "openai/gpt-4o-mini")

        self.assertEqual(body["model"], "openai/gpt-4o-mini")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
        self.assertIn("schema", body["response_format"]["json_schema"])
        self.assertIn("semantic_clause_crosscheck", json.dumps(body))

    def test_alibaba_request_body_uses_singapore_json_chat_completion(self):
        packet = {
            "task": "semantic_clause_crosscheck",
            "clause": {"id": "mutuality"},
            "paragraphs": [{"id": "p1", "text": "Each party is bound."}],
        }

        body = ai_review._alibaba_request_body(packet, "qwen3.5-plus")

        self.assertEqual(body["model"], "qwen3.5-plus")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["enable_thinking"], False)
        self.assertEqual(body["response_format"]["type"], "json_object")
        self.assertIn("semantic_clause_crosscheck", json.dumps(body))

    def test_sanitize_model_name_strips_unsafe_characters(self):
        self.assertEqual(ai_review._sanitize_model_name("models/gemini-3.5-flash"), "gemini-3.5-flash")
        sanitized = ai_review._sanitize_model_name("../../etc/passwd?inject=1")
        self.assertNotIn("/", sanitized)
        self.assertNotIn("?", sanitized)
        self.assertRegex(sanitized, r"^[A-Za-z0-9._-]*$")
        self.assertEqual(ai_review._sanitize_model_name("   "), ai_review.DEFAULT_GEMINI_MODEL)


def _mock_urlopen(response_bytes, captured_requests):
    def urlopen(request, *args, **kwargs):
        captured_requests.append(request)
        context_manager = MagicMock()
        context_manager.__enter__.return_value.read.return_value = response_bytes
        context_manager.__exit__.return_value = False
        return context_manager

    return urlopen


class AIProviderAdapterTests(unittest.TestCase):
    """Cross the real provider seam: each adapter builds its request and parses
    its response, with only the HTTP transport mocked. Previously nothing
    exercised the adapters' __call__ round-trip end to end."""

    PACKET = {
        "task": "semantic_clause_crosscheck",
        "clause": {"id": "mutuality"},
        "paragraphs": [{"id": "p1", "index": 1, "text": "Each party is bound."}],
    }
    VERDICT = {
        "decision": "pass",
        "confidence": 0.9,
        "reason": "Reciprocal obligations are present.",
        "cited_spans": [],
        "issues": [],
        "suggested_fix": "",
    }

    def test_gemini_adapter_round_trip(self):
        captured = []
        response = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": json.dumps(self.VERDICT)}]}}]}
        ).encode("utf-8")
        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            reviewer = ai_review.GeminiAIReviewer(api_key="k", model="gemini-3.5-flash")
            verdict = reviewer(self.PACKET)

        self.assertEqual(verdict, self.VERDICT)
        self.assertEqual(len(captured), 1)
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertIn("semantic_clause_crosscheck", json.dumps(body))

    def test_alibaba_adapter_round_trip(self):
        captured = []
        response = json.dumps({"choices": [{"message": {"content": json.dumps(self.VERDICT)}}]}).encode("utf-8")
        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            reviewer = ai_review.AlibabaAIReviewer(api_key="sk-ws-x", model="qwen3.5-plus")
            verdict = reviewer(self.PACKET)

        self.assertEqual(verdict, self.VERDICT)
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(body["model"], "qwen3.5-plus")
        self.assertEqual(body["response_format"]["type"], "json_object")

    def test_openrouter_adapter_round_trip(self):
        captured = []
        response = json.dumps({"choices": [{"message": {"content": json.dumps(self.VERDICT)}}]}).encode("utf-8")
        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            reviewer = ai_review.OpenRouterAIReviewer(api_key="sk-or-x", model="openai/gpt-4o-mini")
            verdict = reviewer(self.PACKET)

        self.assertEqual(verdict, self.VERDICT)
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(body["response_format"]["type"], "json_schema")

    def test_adapters_and_in_memory_reviewer_satisfy_the_public_interface(self):
        self.assertIsInstance(ai_review.InMemoryReviewer(), ai_review.AIReviewer)
        boom = ai_review.InMemoryReviewer(error=RuntimeError("quota exhausted"))
        with self.assertRaises(RuntimeError):
            boom({"clause": {"id": "mutuality"}, "paragraphs": []})


if __name__ == "__main__":
    unittest.main()
