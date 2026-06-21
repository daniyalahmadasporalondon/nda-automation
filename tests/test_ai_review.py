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
                "NDA_AI_PROVIDER": "openrouter",
                "NDA_AI_MODEL": "anthropic/claude-opus-4.8",
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
                    {"NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "OPENROUTER_API_KEY": "configured"},
                    clear=False,
                ):
                    disabled_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value=""):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "OPENROUTER_API_KEY": "configured"},
                    clear=False,
                ):
                    enabled_status = ai_review.ai_review_status()

        with patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": True}):
            with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="saved-local-key"):
                with patch.dict(
                    os.environ,
                    {"NDA_AI_REVIEW_ENABLED": "", "NDA_AI_PROVIDER": "", "NDA_AI_MODEL": "", "OPENROUTER_API_KEY": ""},
                    clear=False,
                ):
                    local_key_status = ai_review.ai_review_status()

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

    def test_stored_gemini_direct_key_flags_openrouter_migration(self):
        # A locally stored Google/Gemini-direct key ("AIza...") can no longer
        # authenticate now that OpenRouter is the sole provider, so the status
        # surfaces a migration hint toward an OpenRouter "sk-or-" key.
        with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="AIzaSyDexampleexampleexampleexample123"):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                migration = ai_review.stored_ai_key_migration()
                status = ai_review.ai_review_status()

        self.assertIsNotNone(migration)
        self.assertEqual(migration["code"], ai_review.STORED_KEY_MIGRATION_CODE)
        self.assertEqual(migration["expected_key_prefix"], "sk-or-")
        self.assertIn("OpenRouter", migration["message"])
        self.assertEqual(status["stored_key_migration"], migration)

    def test_openrouter_stored_key_needs_no_migration(self):
        with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="sk-or-v1-abcdef0123456789"):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                self.assertIsNone(ai_review.stored_ai_key_migration())
                self.assertIsNone(ai_review.ai_review_status()["stored_key_migration"])

    def test_env_openrouter_key_overrides_legacy_stored_key(self):
        # An env OPENROUTER_API_KEY is used over the stored key, so a legacy stored
        # key is moot and no migration is prompted.
        with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value="AIzaSyDexample123"):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-envconfigured"}, clear=False):
                self.assertIsNone(ai_review.stored_ai_key_migration())

    def test_no_stored_key_needs_no_migration(self):
        with patch.object(ai_review.app_settings, "stored_ai_api_key", return_value=""):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                self.assertIsNone(ai_review.stored_ai_key_migration())

    def test_operational_warnings_surface_stored_key_migration(self):
        # The admin status surface turns the migration hint into an operational
        # warning so the settings UI prompts the operator to swap the key.
        from nda_automation.routes import admin

        migration = {
            "code": ai_review.STORED_KEY_MIGRATION_CODE,
            "message": ai_review.STORED_KEY_MIGRATION_MESSAGE,
            "expected_key_prefix": "sk-or-",
        }
        status = {"api_key_configured": True, "stored_key_migration": migration}
        with patch.object(admin.ai_review, "ai_review_status", return_value=status):
            with patch.object(admin, "active_review_engine_status", return_value={}):
                warnings = admin._operational_warnings()

        codes = [warning["code"] for warning in warnings]
        self.assertIn(ai_review.STORED_KEY_MIGRATION_CODE, codes)
        flagged = next(w for w in warnings if w["code"] == ai_review.STORED_KEY_MIGRATION_CODE)
        self.assertIn("OpenRouter", flagged["message"])

    def test_operational_warnings_omit_migration_when_key_is_openrouter(self):
        from nda_automation.routes import admin

        status = {"api_key_configured": True, "stored_key_migration": None}
        with patch.object(admin.ai_review, "ai_review_status", return_value=status):
            with patch.object(admin, "active_review_engine_status", return_value={}):
                warnings = admin._operational_warnings()

        self.assertNotIn(
            ai_review.STORED_KEY_MIGRATION_CODE,
            [warning["code"] for warning in warnings],
        )

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

    def test_openrouter_request_body_uses_json_response_format(self):
        packet = {
            "task": "semantic_clause_crosscheck",
            "clause": {"id": "mutuality"},
            "paragraphs": [{"id": "p1", "text": "Each party is bound."}],
        }

        body = ai_review._openrouter_request_body(packet, model=ai_review.DEFAULT_OPENROUTER_MODEL)
        encoded = json.dumps(body)

        self.assertEqual(body["model"], ai_review.DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertIn("semantic_clause_crosscheck", encoded)

    def test_sanitize_model_name_strips_unsafe_characters(self):
        self.assertEqual(ai_review._sanitize_model_name("google/gemini-3.5-flash"), "google/gemini-3.5-flash")
        sanitized = ai_review._sanitize_model_name("../../etc/passwd?inject=1")
        self.assertNotIn("?", sanitized)
        self.assertRegex(sanitized, r"^[A-Za-z0-9._/-]*$")
        self.assertEqual(ai_review._sanitize_model_name("   "), ai_review.DEFAULT_OPENROUTER_MODEL)


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

    def test_openrouter_adapter_round_trip(self):
        captured = []
        response = json.dumps(
            {"choices": [{"message": {"content": json.dumps(self.VERDICT)}}]}
        ).encode("utf-8")
        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            reviewer = ai_review.OpenRouterAIReviewer(api_key="k", model=ai_review.DEFAULT_OPENROUTER_MODEL)
            verdict = reviewer(self.PACKET)

        self.assertEqual(verdict, self.VERDICT)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers["Authorization"], "Bearer k")
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(body["model"], ai_review.DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("semantic_clause_crosscheck", json.dumps(body))

    def test_adapters_and_in_memory_reviewer_satisfy_the_public_interface(self):
        self.assertIsInstance(ai_review.InMemoryReviewer(), ai_review.AIReviewer)
        boom = ai_review.InMemoryReviewer(error=RuntimeError("quota exhausted"))
        with self.assertRaises(RuntimeError):
            boom({"clause": {"id": "mutuality"}, "paragraphs": []})


class AIReviewTimeoutDefaultTests(unittest.TestCase):
    """The single review POST covers the whole packet, so a large doc makes the
    model take ~2 min. The default per-request timeout must clear that worst
    case; the previous 20s default timed out and fail-closed the review."""

    def test_default_timeout_constant_is_180_seconds(self):
        self.assertEqual(ai_review.DEFAULT_AI_TIMEOUT_SECONDS, 180)

    def test_legacy_reviewer_uses_180s_default_timeout(self):
        reviewer = ai_review.OpenRouterAIReviewer(api_key="k")
        self.assertEqual(reviewer.timeout_seconds, 180)

    def test_settings_default_timeout_flows_to_both_call_sites(self):
        # Both the AI-first assessor and the legacy reviewer resolve their
        # timeout from _ai_review_settings()["timeout_seconds"]; with no env
        # override it must be the 180s default.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("NDA_AI_TIMEOUT_SECONDS", None)
            settings = ai_review._ai_review_settings()
        self.assertEqual(settings["timeout_seconds"], 180)

    def test_env_override_still_wins_over_default(self):
        with patch.dict("os.environ", {"NDA_AI_TIMEOUT_SECONDS": "90"}, clear=False):
            settings = ai_review._ai_review_settings()
        self.assertEqual(settings["timeout_seconds"], 90)

    def test_env_override_flows_into_legacy_reviewer_build(self):
        with patch.dict("os.environ", {"NDA_AI_TIMEOUT_SECONDS": "77"}, clear=False):
            settings = ai_review._ai_review_settings()
            with patch(
                "nda_automation.ai_review._configured_api_key",
                side_effect=lambda provider: f"{provider}-key",
            ):
                reviewer = ai_review._configured_reviewer(
                    {**settings, "provider": "openrouter", "model": ai_review.DEFAULT_OPENROUTER_MODEL}
                )
        self.assertIsInstance(reviewer, ai_review.OpenRouterAIReviewer)
        self.assertEqual(reviewer.timeout_seconds, 77)


def _http_error(code):
    import io
    import urllib.error

    return urllib.error.HTTPError(
        ai_review.OPENROUTER_KEY_ENDPOINT, code, "err", {}, io.BytesIO(b"{}")
    )


class ApiKeyValidationTests(unittest.TestCase):
    """Pre-persist key validity probe. The HTTP transport is always mocked so the
    suite never touches real OpenRouter and never spends model tokens."""

    def test_valid_key_returns_valid_with_metadata_and_no_key_leak(self):
        captured = []
        body = json.dumps({"data": {"label": "demo-key", "limit_remaining": 12.5}}).encode("utf-8")
        with patch("urllib.request.urlopen", _mock_urlopen(body, captured)):
            result = ai_review.validate_api_key("sk-or-secret-value")

        self.assertEqual(result.status, "valid")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.label, "demo-key")
        self.assertEqual(result.limit_remaining, 12.5)
        # Probe is a token-free GET to /key with the bearer header — never POST,
        # never the chat-completions endpoint.
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(captured[0].full_url, ai_review.OPENROUTER_KEY_ENDPOINT)
        self.assertEqual(captured[0].headers["Authorization"], "Bearer sk-or-secret-value")
        self.assertIsNone(captured[0].data)
        # The result/message must never echo the key value.
        self.assertNotIn("secret-value", result.message)

    def test_401_is_rejected_with_clear_message(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = ai_review.validate_api_key("sk-or-bad")
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.is_valid)
        self.assertIn("rejected", result.message.lower())

    def test_403_is_rejected(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(403)):
            result = ai_review.validate_api_key("sk-or-bad")
        self.assertEqual(result.status, "rejected")

    def test_5xx_is_unreachable_not_rejected(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(503)):
            result = ai_review.validate_api_key("sk-or-maybe")
        self.assertEqual(result.status, "unreachable")
        self.assertFalse(result.is_valid)

    def test_network_error_is_unreachable(self):
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = ai_review.validate_api_key("sk-or-maybe")
        self.assertEqual(result.status, "unreachable")

    def test_timeout_is_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            result = ai_review.validate_api_key("sk-or-maybe")
        self.assertEqual(result.status, "unreachable")

    def test_empty_key_is_rejected_without_network_call(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("should not call network")):
            result = ai_review.validate_api_key("   ")
        self.assertEqual(result.status, "rejected")


if __name__ == "__main__":
    unittest.main()
