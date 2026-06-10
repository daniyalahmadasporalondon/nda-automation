"""Unit tests for the dashboard_search_intent module (no HTTP).

These exercise the translator/validator directly to lock THE GOLDEN RULE contract:
* the model receives ONLY the query string + the fixed schema in the prompt — never
  any matter data,
* its output is VALIDATED against a fixed allowlist (out-of-enum dropped, ints
  clamped, bools coerced) before it leaves the module,
* the allowlist is sourced from the real workflow.py state machine, and
* every failure mode degrades to DashboardSearchIntentUnavailableError (the route
  turns that into a graceful fallback), never a crash.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from nda_automation import dashboard_search_intent as dsi
from nda_automation import workflow


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def _mock_urlopen(response_bytes, captured_requests):
    def urlopen(request, *args, **kwargs):
        captured_requests.append(request)
        return _FakeResponse(response_bytes)

    return urlopen


def _stub(spec_json):
    def transport(_request_body):
        return {"choices": [{"message": {"content": spec_json}}]}

    return transport


class ValidateFilterSpecTests(unittest.TestCase):
    def test_valid_dimensions_pass_through(self):
        spec = dsi.validate_filter_spec(
            {
                "status": "awaiting_approval",
                "phase": "review",
                "needs_attention": True,
                "human_gate": False,
                "has_issues": True,
                "text": "Acme",
                "min_age_days": 5,
                "sort": "oldest",
            }
        )
        self.assertEqual(spec["status"], "awaiting_approval")
        self.assertEqual(spec["phase"], "review")
        self.assertIs(spec["needs_attention"], True)
        self.assertIs(spec["human_gate"], False)
        self.assertIs(spec["has_issues"], True)
        self.assertEqual(spec["text"], "Acme")
        self.assertEqual(spec["min_age_days"], 5)
        self.assertEqual(spec["sort"], "oldest")

    def test_out_of_enum_status_phase_and_sort_are_dropped(self):
        spec = dsi.validate_filter_spec(
            {"status": "made_up", "phase": "shipping", "sort": "sideways"}
        )
        self.assertIsNone(spec["status"])
        self.assertIsNone(spec["phase"])
        self.assertIsNone(spec["sort"])

    def test_status_match_is_case_insensitive_and_trimmed(self):
        spec = dsi.validate_filter_spec({"status": "  Awaiting_Approval "})
        self.assertEqual(spec["status"], "awaiting_approval")

    def test_non_bool_flags_are_dropped_not_coerced(self):
        # A truthy STRING must NOT become True — only a real JSON bool counts.
        spec = dsi.validate_filter_spec(
            {"needs_attention": "yes", "human_gate": 1, "has_issues": "true"}
        )
        self.assertIsNone(spec["needs_attention"])
        self.assertIsNone(spec["human_gate"])
        self.assertIsNone(spec["has_issues"])

    def test_min_age_days_clamps_and_rejects(self):
        self.assertEqual(
            dsi.validate_filter_spec({"min_age_days": 99999})["min_age_days"],
            dsi.MAX_MIN_AGE_DAYS,
        )
        self.assertEqual(dsi.validate_filter_spec({"min_age_days": 7})["min_age_days"], 7)
        self.assertIsNone(dsi.validate_filter_spec({"min_age_days": 0})["min_age_days"])
        self.assertIsNone(dsi.validate_filter_spec({"min_age_days": -3})["min_age_days"])
        # A bool must not be read as an int (True != 1 here).
        self.assertIsNone(dsi.validate_filter_spec({"min_age_days": True})["min_age_days"])
        # A numeric string is tolerated.
        self.assertEqual(dsi.validate_filter_spec({"min_age_days": "9"})["min_age_days"], 9)
        self.assertIsNone(dsi.validate_filter_spec({"min_age_days": "lots"})["min_age_days"])

    def test_text_is_neutralized_and_capped(self):
        long_text = "x" * 1000
        spec = dsi.validate_filter_spec({"text": long_text})
        self.assertLessEqual(len(spec["text"]), dsi.MAX_TEXT_CHARS)
        self.assertIsNone(dsi.validate_filter_spec({"text": "   "})["text"])

    def test_unknown_keys_ignored_and_non_mapping_collapses_to_null(self):
        spec = dsi.validate_filter_spec({"bogus": "x", "status": "approved"})
        self.assertEqual(set(spec), set(dsi.NULL_FILTER_SPEC))
        self.assertEqual(spec["status"], "approved")
        self.assertTrue(dsi.filter_spec_is_empty(dsi.validate_filter_spec("not a dict")))
        self.assertTrue(dsi.filter_spec_is_empty(dsi.validate_filter_spec(None)))

    def test_allowlist_is_sourced_from_real_workflow_statuses(self):
        # The allowlist must match the real state machine, never drift from it.
        self.assertIn(workflow.STATUS_AWAITING_APPROVAL, dsi.ALLOWED_STATUSES)
        self.assertIn(workflow.STATUS_SENT_AWAITING_COUNTERPARTY, dsi.ALLOWED_STATUSES)
        self.assertIn(workflow.STATUS_AI_REVIEWING, dsi.ALLOWED_STATUSES)
        self.assertEqual(set(dsi.ALLOWED_PHASES), set(workflow.PHASE_ORDER))


class DescribeFilterSpecTests(unittest.TestCase):
    def test_empty_spec_is_all_documents(self):
        self.assertEqual(dsi.describe_filter_spec(dsi.NULL_FILTER_SPEC), "All documents")

    def test_describes_each_dimension(self):
        text = dsi.describe_filter_spec(
            dsi.validate_filter_spec(
                {
                    "phase": "review",
                    "needs_attention": True,
                    "min_age_days": 7,
                    "text": "Acme",
                    "sort": "oldest",
                }
            )
        )
        self.assertIn("In review", text)
        self.assertIn("Needs attention", text)
        self.assertIn("older than 7 days", text)
        self.assertIn('matching "Acme"', text)
        self.assertIn("oldest first", text)


class TranslateSearchIntentTests(unittest.TestCase):
    def test_model_prompt_carries_only_the_query_not_matter_data(self):
        captured = {}

        def transport(request_body):
            captured["body"] = request_body
            return {"choices": [{"message": {"content": '{"text":"Acme"}'}}]}

        dsi.translate_search_intent("find the Acme deal", transport=transport)
        messages = captured["body"]["messages"]
        system_message = messages[0]["content"]
        user_message = messages[1]["content"]
        # The user message carries the query as data...
        self.assertIn("find the Acme deal", user_message)
        self.assertIn("QUERY", user_message)
        # ...and the system prompt advertises the schema's real enum values.
        self.assertIn(workflow.STATUS_AWAITING_APPROVAL, system_message)
        self.assertIn("Output JSON only", system_message)
        # Temperature 0 for a deterministic translation.
        self.assertEqual(captured["body"]["temperature"], 0)

    def test_result_carries_validated_filters_and_interpreted_line(self):
        result = dsi.translate_search_intent(
            "stuck in review over a week",
            transport=_stub('{"phase":"review","min_age_days":7}'),
        )
        self.assertEqual(result["filters"]["phase"], "review")
        self.assertEqual(result["filters"]["min_age_days"], 7)
        self.assertIn("older than 7 days", result["interpreted"])

    def test_model_output_is_validated_before_returning(self):
        # The model hallucinates an invalid status; the module drops it.
        result = dsi.translate_search_intent(
            "anything", transport=_stub('{"status":"not_real","text":"Acme"}')
        )
        self.assertIsNone(result["filters"]["status"])
        self.assertEqual(result["filters"]["text"], "Acme")

    def test_json_in_code_fence_is_parsed(self):
        result = dsi.translate_search_intent(
            "x", transport=_stub('```json\n{"phase":"sent"}\n```')
        )
        self.assertEqual(result["filters"]["phase"], "sent")

    def test_unparseable_output_collapses_to_null_spec(self):
        result = dsi.translate_search_intent(
            "x", transport=_stub("I cannot help with that.")
        )
        self.assertTrue(dsi.filter_spec_is_empty(result["filters"]))

    def test_empty_query_returns_null_spec_without_calling_transport(self):
        called = {"n": 0}

        def transport(_body):
            called["n"] += 1
            return {"choices": []}

        result = dsi.translate_search_intent("   ", transport=transport)
        self.assertTrue(dsi.filter_spec_is_empty(result["filters"]))
        self.assertEqual(called["n"], 0)

    def test_transport_failure_raises_unavailable(self):
        def boom(_body):
            raise RuntimeError("network down")

        with self.assertRaises(dsi.DashboardSearchIntentUnavailableError):
            dsi.translate_search_intent("Acme", transport=boom)

    def test_ai_disabled_raises_unavailable(self):
        with self.assertRaises(dsi.DashboardSearchIntentUnavailableError):
            dsi.translate_search_intent(
                "Acme",
                settings={"enabled": False, "provider": "openrouter", "model": "x", "timeout_seconds": 20},
            )

    def test_openrouter_intent_transport_uses_shared_runtime_adapter(self):
        captured = []
        response = json.dumps({"choices": [{"message": {"content": '{"phase":"review"}'}}]}).encode("utf-8")
        body = dsi.build_intent_request_body("review matters", model="x-ai/grok-4.3")

        with patch("urllib.request.urlopen", _mock_urlopen(response, captured)):
            payload = dsi._OpenRouterIntentTransport(api_key="sk-test", timeout_seconds=20)(body)

        self.assertEqual(dsi.validate_filter_spec(dsi._spec_from_response(payload))["phase"], "review")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers["Authorization"], "Bearer sk-test")
        self.assertEqual(json.loads(captured[0].data.decode("utf-8")), body)


if __name__ == "__main__":
    unittest.main()
